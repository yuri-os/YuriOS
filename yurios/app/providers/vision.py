"""Can the model she is speaking through actually *see*? (SPEC §35.1)

The chat seam takes text in and text out, and for a text-only model that is the
whole story. A multimodal one takes a second kind of part — an image — and the
only honest way to offer someone a paperclip is to know, before they click it,
that the model on the other end will not simply refuse the request.

So this asks the server. Every provider already publishes the answer, each in
its own alphabet, and each on the endpoint the model picker already talks to
(`providers/catalog.py` — the same "what can this server actually do right now"
question, one field over):

    lm_studio  →  GET  {root}/api/v1/models  →  capabilities.vision
                  (older builds: /api/v0/models → type == "vlm")
    ollama     →  POST {base}/api/show       →  "vision" in capabilities
    openrouter →  GET  /api/v1/models        →  architecture.input_modalities

A live answer rather than LiteLLM's bundled cost map, because that map is a
snapshot of the hosted catalogue on the day it shipped: it has never heard of
the gguf you pulled last night, and it says `False` for models OpenRouter itself
lists as `text+image->text`. The map is still the fallback for a route with no
listing of its own (a raw `openai/…`, a custom gateway) — being wrong about a
hosted model is better than assuming every unlisted one is blind.

Never raises. An unreachable server, a listing in a shape this doesn't know, no
network at all: all of them mean "no picture button", which is exactly what a
text-only model looks like from the outside. `CHAT_IMAGE_INPUT=on|off` is the
override for the case this gets wrong — a probe that guesses is not allowed to
be the last word on a capability the user can see with their own eyes.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("mvw.vision")

#: Model-id prefixes that name a route rather than a vendor — LiteLLM's, and
#: the same tuple `providers/openrouter.py` routes on.
_ROUTES = ("lm_studio", "ollama", "openrouter", "gguf", "openai", "anthropic")

#: Short: this runs at boot, in front of everything else waiting to start, and a
#: model picker that hangs for ten seconds is worse than one that says "no".
_TIMEOUT_S = 6.0


async def _get(url: str, *, headers: dict | None = None, transport=None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
        response = await client.get(url, headers=headers or {})
        response.raise_for_status()
        return response.json()


async def _post(url: str, payload: dict, *, headers: dict | None = None,
                transport=None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
        response = await client.post(url, json=payload, headers=headers or {})
        response.raise_for_status()
        return response.json()


def _bearer(key: str) -> dict | None:
    return {"Authorization": f"Bearer {key}"} if key else None


async def _lmstudio(cfg, name: str, *, transport=None) -> bool | None:
    """LM Studio's developer API, which is the one that knows about vision.

    The OpenAI-compatible `/v1/models` this server also serves says nothing but
    ids, so both attempts here are on the developer API beside it: `/api/v1`
    carries `capabilities.vision`, and the older `/api/v0` says the same thing
    by typing the model `vlm`. None = neither answered; the caller falls back.
    """
    from yurios.app.providers.lmstudio import _api_root, _resolve_key

    root = _api_root(cfg.lmstudio_base_url)
    headers = _bearer(cfg.connection_api_key)
    try:
        catalog = (await _get(f"{root}/api/v1/models", headers=headers,
                              transport=transport))["models"]
    except Exception as e:                        # noqa: BLE001 — older, or down
        log.debug("LM Studio /api/v1/models unavailable (%s); trying /api/v0", e)
    else:
        key = _resolve_key(catalog, name)
        if key is None:
            return None                            # not downloaded here; unknown
        entry = next(m for m in catalog if m.get("key") == key)
        return bool((entry.get("capabilities") or {}).get("vision"))
    try:
        rows = (await _get(f"{root}/api/v0/models", headers=headers,
                           transport=transport))["data"]
    except Exception as e:                        # noqa: BLE001
        log.debug("LM Studio has no developer API at %s (%s)", root, e)
        return None
    want = name.strip().lower()
    for row in rows:
        row_id = str(row.get("id", ""))
        if row_id.lower() in (want, want.rsplit("/", 1)[-1]):
            return row.get("type") == "vlm"
    return None


async def _ollama(cfg, name: str, *, transport=None) -> bool | None:
    base = cfg.ollama_base_url.rstrip("/")
    try:
        shown = await _post(f"{base}/api/show", {"model": name},
                            headers=_bearer(cfg.connection_api_key),
                            transport=transport)
    except Exception as e:                        # noqa: BLE001
        log.debug("Ollama would not describe %r (%s)", name, e)
        return None
    caps = shown.get("capabilities")
    if not isinstance(caps, list):                # pre-0.4: no capabilities field
        return None
    return "vision" in caps


async def _openrouter(cfg, name: str, *, transport=None) -> bool | None:
    """OpenRouter's public catalogue: `architecture.input_modalities`.

    The key rides along when there is one, so a private or BYOK model is
    described too — the same reason `catalog.py` attaches it.
    """
    try:
        rows = (await _get("https://openrouter.ai/api/v1/models",
                           headers=_bearer(cfg.openrouter_api_key),
                           transport=transport))["data"]
    except Exception as e:                        # noqa: BLE001 — offline is fine
        log.debug("OpenRouter catalogue unavailable (%s)", e)
        return None
    want = name.strip().lower()
    for row in rows:
        if str(row.get("id", "")).lower() == want:
            modalities = (row.get("architecture") or {}).get("input_modalities")
            if not isinstance(modalities, list):
                return None
            return "image" in modalities
    return None


def _from_litellm(route: str) -> bool:
    """LiteLLM's bundled model map — the fallback, and the whole answer for a
    route with no catalogue of its own. Its `False` means "not in the map",
    which for an unlisted model is a guess; we take it, because the alternative
    is offering a paperclip that errors."""
    try:
        import litellm
        return bool(litellm.supports_vision(model=route))
    except Exception:                             # noqa: BLE001 — an unknown id
        return False


async def probe(cfg, model: str = "", *, transport=None) -> tuple[bool, str]:
    """`(she can be sent pictures, why we say so)`.

    The second half is for `/api/health` and the boot panel: "no picture button"
    with no reason attached is the kind of thing someone re-reads the docs over.
    """
    model = (model or cfg.chat_model or "").strip()
    forced = (getattr(cfg, "chat_image_input", "auto") or "auto").strip().lower()
    if forced in ("on", "1", "true", "yes"):
        return True, f"forced on (CHAT_IMAGE_INPUT={forced})"
    if forced in ("off", "0", "false", "no"):
        return False, f"forced off (CHAT_IMAGE_INPUT={forced})"
    if not model or model.upper() == "NONE":
        return False, "no chat model configured"

    # The same route rule the provider seam applies (providers/openrouter.py's
    # `_route`): a prefix this list knows picks the server, and ANYTHING else —
    # including a bare `vendor/model`, which has a slash but not a route — is an
    # OpenRouter id. Splitting on the slash alone read `qwen/qwen3-vl` as a
    # provider called qwen and skipped the one catalogue that describes it.
    route, _, name = model.partition("/")
    if route not in _ROUTES or not name:
        route, name = "openrouter", model

    live: bool | None = None
    if route == "lm_studio":
        live = await _lmstudio(cfg, name, transport=transport)
        where = "LM Studio"
    elif route == "ollama":
        live = await _ollama(cfg, name, transport=transport)
        where = "Ollama"
    elif route == "openrouter":
        live = await _openrouter(cfg, name, transport=transport)
        where = "OpenRouter"
    elif route == "gguf":
        # llama.cpp in-process (providers/gguf.py) loads weights alone — no
        # projector, so no eyes, whatever the model could do elsewhere.
        return False, "gguf route: text only"
    else:
        where = route

    if live is not None:
        return live, f"{where} says {'vision' if live else 'text only'}"
    fallback = _from_litellm(model if "/" in model else f"openrouter/{model}")
    return fallback, (f"{where} didn't say; LiteLLM's model map says "
                      f"{'vision' if fallback else 'text only'}")
