"""Local Embedder via LM Studio (SPEC §3 — the `EMBED_BACKEND=lm_studio` option).

LM Studio serves embeddings on the same OpenAI-compatible server as its chat
models (POST {base}/embeddings), so a single local process can back BOTH the mind
(CHAT_MODEL=lm_studio/…) and its memory — no Ollama needed. Load an embedding
model in LM Studio (e.g. `text-embedding-nomic-embed-text-v1.5`, 768-d) and set
EMBED_MODEL + EMBED_DIM to match. A local chat model routes through LiteLLM
(`CHAT_MODEL=lm_studio/<name>`), so only the Embedder seam lives here (§3.1).

`ensure_resident()` is the other half: it pins both models in memory at boot so
they stop evicting each other every turn (see its docstring).
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("mvw.lmstudio")


class LMStudioEmbedder:
    def __init__(self, model_name: str = "text-embedding-nomic-embed-text-v1.5",
                 dim: int = 768, base_url: str = "http://localhost:1234/v1"):
        self.model_name = model_name
        self.dim = dim
        self.base_url = base_url.rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(timeout=60) as client:
            r = client.post(f"{self.base_url}/embeddings",
                            json={"model": self.model_name, "input": texts})
            r.raise_for_status()
            # OpenAI shape: {"data": [{"index": i, "embedding": [...]}, ...]}.
            # Sort by index — the server does not guarantee input order.
            rows = sorted(r.json()["data"], key=lambda d: d["index"])
        out = [row["embedding"] for row in rows]
        for vec in out:
            if len(vec) != self.dim:
                raise ValueError(
                    f"EMBED_DIM={self.dim} but {self.model_name} returned "
                    f"{len(vec)}-d — fix .env (§3)")
        return out


# --- keeping both models resident (§3.1) -------------------------------------
#
# LM Studio JIT-loads a model when a request names one that isn't loaded, and by
# default ("Only Keep Last JIT Loaded Model", on in Developer settings) it unloads
# the previously JIT-loaded model to make room. YuriOS talks to TWO models on that
# one server every single turn — the chat model streams the reply, the embedder
# recalls and then remembers — so under that default each turn evicts the other's
# model and pays a full reload. Measured on a 6.3 GB chat model + the 84 MB nomic
# embedder: 5.7 s to reload the chat model and 1.9 s to reload the embedder, on
# every turn, forever.
#
# The fix is to load them EXPLICITLY through LM Studio's developer REST API
# (POST /api/v1/models/load, LM Studio 0.4+) instead of letting requests JIT them
# in. An explicit load is not a JIT load, so the JIT eviction rule never touches
# it, and omitting `ttl_seconds` means no idle timeout — both stay resident for as
# long as LM Studio runs. This writes NOTHING to the user's LM Studio settings: it
# is the same operation as pressing Load in the UI, so it works as-is on someone
# else's machine whatever their JIT/TTL preferences are.

_LOAD_TIMEOUT_S = 600.0   # a cold 20 GB model off a slow disk is legitimately slow
_QUERY_TIMEOUT_S = 10.0


def _api_root(base_url: str) -> str:
    """The server root, given the OpenAI-compatible endpoint from config.

    LMSTUDIO_BASE_URL points at `…/v1` (that is what LiteLLM and /embeddings want);
    the developer API lives beside it at `…/api/v1`, off the same root."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _resolve_key(catalog: list[dict], model_id: str) -> str | None:
    """Configured model id → the catalog `key` that /models/load demands.

    The two are not the same alphabet. /v1/chat/completions happily takes the
    id as it appears in CHAT_MODEL (`HauhauCS/Gemma-4-E4B-Uncensored-…`), but
    /api/v1/models/load only knows canonical keys (`gemma-4-e4b-uncensored-…`)
    and 404s on anything else — so match on the publisher-stripped tail too."""
    want = model_id.strip().lower()
    tail = want.rsplit("/", 1)[-1]
    keys = {m.get("key", "").lower(): m.get("key", "") for m in catalog}
    for candidate in (want, tail):
        if candidate in keys:
            return keys[candidate]
    for m in catalog:                       # `google/gemma-4-12b-qat` style keys
        key = m.get("key", "")
        publisher = (m.get("publisher") or "").lower()
        if want in (f"{publisher}/{key.lower()}", key.lower().rsplit("/", 1)[-1]):
            return key
    return None


def ensure_resident(base_url: str, model_ids: list[str], *,
                    timeout: float = _LOAD_TIMEOUT_S,
                    transport: httpx.BaseTransport | None = None) -> list[str]:
    """Pin `model_ids` in LM Studio's memory, with no idle TTL. Never raises.

    Returns the keys that ended up resident — for the caller's boot panel, and
    short of the whole list when something could not be loaded.

    Best-effort by design: an unreachable server, an LM Studio too old to have the
    developer API, a model that isn't downloaded, or one that will not fit in RAM
    are all reasons to log and carry on. YuriOS still runs — the models just get
    JIT-loaded the old way, which is slow, not broken."""
    root = _api_root(base_url)
    wanted = list(dict.fromkeys(model_ids))   # chat and utility are often the same
    resident: list[str] = []
    try:
        with httpx.Client(timeout=_QUERY_TIMEOUT_S, transport=transport) as client:
            r = client.get(f"{root}/api/v1/models")
            r.raise_for_status()
            catalog = r.json()["models"]
    except Exception as e:
        log.warning("LM Studio model list unavailable at %s (%s) — leaving model "
                    "residency to LM Studio's JIT loader", root, e)
        return resident

    for model_id in wanted:
        key = _resolve_key(catalog, model_id)
        if key is None:
            log.warning("LM Studio has no model matching %r — download it in LM "
                        "Studio, or fix the id in .env", model_id)
            continue
        entry = next(m for m in catalog if m.get("key") == key)
        instances = entry.get("loaded_instances") or []
        # An instance with no TTL is already pinned — nothing to do. One WITH a
        # TTL was JIT-loaded (by us on an earlier run, or by anything else on this
        # machine): it expires on idle and comes back through the evicting path,
        # so trade it for a pinned one. Loading on top of it would just make a
        # second instance and pay for the weights twice.
        if any(i.get("remaining_ttl_seconds") is None for i in instances):
            log.info("LM Studio: %s already resident", key)
            resident.append(key)
            continue
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                for inst in instances:
                    log.info("LM Studio: dropping the TTL'd instance of %s "
                             "(expires in %ss)", key, inst.get("remaining_ttl_seconds"))
                    client.post(f"{root}/api/v1/models/unload",
                                json={"instance_id": inst["id"]}).raise_for_status()
                # no ttl_seconds → no idle unload; no context_length → LM Studio's
                # own per-model default, the same config its UI would load with
                r = client.post(f"{root}/api/v1/models/load", json={"model": key})
                r.raise_for_status()
            log.info("LM Studio: pinned %s in memory (%.1fs)",
                     key, r.json().get("load_time_seconds", 0.0))
            resident.append(key)
        except Exception as e:
            detail = getattr(getattr(e, "response", None), "text", "") or str(e)
            log.warning("LM Studio would not load %s (%s) — it will be JIT-loaded "
                        "per request instead", key, detail.strip()[:200])
    return resident
