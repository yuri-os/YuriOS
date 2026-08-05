"""Build #1 — the FastAPI app (SPEC §2, §14).

Wires the pieces together and serves the sanctuary. Run:

    python scripts/seed_vault.py               # once — Vault from ../yuri-soul
    python -m app                              # reads HOST/PORT from .env (§11)

`uvicorn app.main:app --factory` also works and takes its own --host/--port.

`create_app()` takes injected providers so the test suite can run the real
handlers against fakes — no API key, no model download (§13.3).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from dataclasses import dataclass, field

import httpx

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from yurios.app.config import Config
from yurios.app.core.soul import SoulLoader
from yurios.app.corpus import CorpusLogger, UtilityLogger
from yurios.app.memory.store import FileMemoryStore
from yurios.app.routes import chat, greeting, health, rate, session
from yurios.app.sessions import SessionStore

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"

log = logging.getLogger("mvw.main")


def _ensure_index_matches_embedder(store, cfg: Config) -> None:
    """Keep the recall cache honest across an embedder change (§4.3).

    The index stores which embedder built its vectors. A same-dim model swap
    (e.g. ollama→lm_studio, both 768-d nomic) would not crash but would silently
    poison recall — the stored vectors and new query vectors live in different
    spaces. So on any mismatch we rebuild from the authoritative .md files instead
    of trusting stale vectors. A fresh/empty index is just stamped."""
    from yurios.app.memory.reindex import reindex, _embedder_id

    current = _embedder_id(cfg)
    if store.index.stored_embedder_id == current:
        return
    if store.index.count() == 0:
        store.index.set_embedder_id(current)  # nothing to rebuild, just record it
        return
    log.warning(
        "re-indexing memory: embedding model changed (%s → %s) — rebuilding the "
        "recall cache from the Vault's .md files",
        store.index.stored_embedder_id or "unknown", current)
    n = reindex(store.vault, embedder=store.embedder, embed_dim=cfg.embed_dim,
                embedder_id=current, index=store.index)
    log.warning("re-indexing done: %d chunks rebuilt with %s", n, current)


@dataclass
class AppState:
    """Everything a handler needs, one attribute away (request.app.state.mvw)."""
    cfg: Config
    soul_loader: SoulLoader
    soul_name: str
    store: FileMemoryStore
    sessions: SessionStore
    corpus: CorpusLogger
    utility_log: UtilityLogger
    chat: object       # ChatModel (§3.1)
    utility: object    # UtilityModel
    embedder: object   # Embedder
    vault_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_tasks: set = field(default_factory=set)  # keep post-turn tasks alive


def _default_embedder(cfg: Config):
    if cfg.embed_backend == "ollama":
        from yurios.app.providers.ollama import OllamaEmbedder
        return OllamaEmbedder(cfg.embed_model, cfg.embed_dim)
    if cfg.embed_backend == "lm_studio":
        # same local LM Studio server as the chat model — one process, no Ollama
        from yurios.app.providers.lmstudio import LMStudioEmbedder
        embedder = LMStudioEmbedder(cfg.embed_model, cfg.embed_dim, cfg.lmstudio_base_url)
        try:
            # Construction only records the endpoint; verify it before opening the
            # index so a disconnected default server can use the local fallback.
            embedder.embed(["YuriOS embedding backend availability check."])
        except Exception as e:
            from yurios.app.providers.sentence_tf import (
                DEFAULT_DIM, DEFAULT_MODEL, SentenceTFEmbedder,
            )

            log.warning(
                "LM Studio embeddings unavailable at %s (%s); falling back to "
                "sentence-transformers",
                cfg.lmstudio_base_url, e)
            # The fallback's vectors occupy a different space and width. Store its
            # effective configuration so index creation and provenance stay correct.
            cfg.embed_backend = "sentence_tf"
            cfg.embed_model = DEFAULT_MODEL
            cfg.embed_dim = DEFAULT_DIM
            return SentenceTFEmbedder(cfg.embed_model, cfg.embed_dim)
        return embedder
    from yurios.app.providers.sentence_tf import SentenceTFEmbedder
    return SentenceTFEmbedder(cfg.embed_model, cfg.embed_dim)


def _lmstudio_ids(cfg: Config, *, chat: bool, embed: bool) -> list[str]:
    """Which of her models live on the LM Studio server, as LM Studio names them.

    Two seams can land on that one server: the reply voice (lm_studio/… ids — the
    prefix is LiteLLM routing, not part of the id) and the embedder
    (EMBED_BACKEND=lm_studio). Only counts the seams we are actually building —
    an injected fake never touches a server, so the test suite never reaches for
    localhost:1234."""
    ids: list[str] = []
    if embed and cfg.embed_backend == "lm_studio":
        ids.append(cfg.embed_model)
    if chat:
        models = (cfg.chat_model, cfg.utility_model) if cfg.utility_enabled else (cfg.chat_model,)
        ids += [m.split("/", 1)[1] for m in models
                if m.startswith("lm_studio/")]
    return ids


def _preload_lmstudio(cfg: Config, *, chat: bool, embed: bool) -> list[str]:
    """Pin those models in LM Studio before the first request; return what stuck.

    Left to JIT loading they evict each other on every turn — the story is in
    providers/lmstudio.ensure_resident. CONTEXT_LENGTH, if set, is the window
    the chat model is pinned with (§11) — the cure for a conversation that ends
    in "Context size has been exceeded"."""
    ids = _lmstudio_ids(cfg, chat=chat, embed=embed)
    if not ids:
        return []
    from yurios.app.providers.lmstudio import ensure_resident
    return ensure_resident(cfg.lmstudio_base_url, ids,
                           context_length=cfg.context_length,
                           timeout=cfg.lmstudio_load_timeout_s)


def model_api_base(cfg: Config, model: str) -> str:
    """The server a model id is reached on, if it is a local one.

    Local ids carry no key; they need their server's base url instead — the
    LM Studio /v1 endpoint, or the Ollama root (so a non-default OLLAMA_BASE_URL
    follows through to chat routing, not just the settings-panel model list).
    A hosted route answers with "" and rides the api key."""
    if model.startswith("lm_studio/"):
        return cfg.lmstudio_base_url
    if model.startswith("ollama/"):
        return cfg.ollama_base_url
    return ""


def _lmstudio_available(cfg: Config) -> bool:
    """Can the configured OpenAI-compatible LM Studio endpoint answer requests?

    This deliberately checks only the server, not whether a particular model is
    already loaded: LM Studio's normal JIT/preload flow owns model residency. A
    dead server is the case where the direct GGUF fallback is useful.
    """
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(f"{cfg.lmstudio_base_url.rstrip('/')}/models")
            response.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("LM Studio unavailable at %s (%s); using direct GGUF fallback",
                    cfg.lmstudio_base_url, e)
        return False


def _use_gguf_fallback(cfg: Config, model: str) -> bool:
    # Keep a minimal `pip install -e .` usable with its existing LiteLLM route.
    # install.sh includes this optional runtime, while source/test installs need
    # not compile or download llama.cpp merely by constructing a provider.
    return (cfg.gguf_fallback and importlib.util.find_spec("llama_cpp") is not None
            and model.startswith("lm_studio/")
            and not _lmstudio_available(cfg))


def _uses_direct_gguf(model: str) -> bool:
    return model.startswith("gguf/")


class UnconfiguredChatModel:
    """A deliberate offline seam until first-run model selection is complete."""

    def __init__(self, cfg, *, meter=None):
        # Match the normal provider's small inspection surface so live settings can
        # still be changed before a model is selected.
        self.model = "NONE"
        self.thinking = cfg.chat_thinking
        self.meter = meter

    async def stream(self, messages, **params):
        raise RuntimeError("No chat model is configured. Open YuriOS and choose a model first.")
        yield ""  # pragma: no cover - keeps this an async generator for the protocol


class UnconfiguredUtilityModel:
    def __init__(self, cfg):
        self.model = "NONE"
        self.thinking = cfg.utility_thinking
        self.max_tokens = cfg.utility_max_tokens

    async def complete(self, messages, **params):
        raise RuntimeError("No utility model is configured. Choose a model first.")


def build_chat_model(cfg: Config, *, meter=None):
    """Her reply voice, from config alone. One construction path, so a model
    swapped at runtime (world/rewire.py) is built exactly like the boot one."""
    if not cfg.chat_model or cfg.chat_model.upper() == "NONE":
        return UnconfiguredChatModel(cfg, meter=meter)
    if _uses_direct_gguf(cfg.chat_model) or _use_gguf_fallback(cfg, cfg.chat_model):
        from yurios.app.providers.gguf import GGUFChatModel
        return GGUFChatModel(cfg.chat_model, cfg, temperature=cfg.temperature, meter=meter)
    from yurios.app.providers.openrouter import LiteLLMChatModel
    return LiteLLMChatModel(cfg.chat_model, cfg.openrouter_api_key, cfg.temperature,
                            api_base=model_api_base(cfg, cfg.chat_model),
                            thinking=cfg.chat_thinking, meter=meter)


def build_utility_model(cfg: Config):
    """The extraction/summary model, or None when utility work is off."""
    if not cfg.utility_enabled:
        return None
    if not cfg.utility_model or cfg.utility_model.upper() == "NONE":
        return UnconfiguredUtilityModel(cfg)
    if _uses_direct_gguf(cfg.utility_model) or _use_gguf_fallback(cfg, cfg.utility_model):
        from yurios.app.providers.gguf import GGUFUtilityModel
        return GGUFUtilityModel(cfg.utility_model, cfg, max_tokens=cfg.utility_max_tokens,
                                thinking=cfg.utility_thinking)
    from yurios.app.providers.openrouter import LiteLLMUtilityModel
    return LiteLLMUtilityModel(cfg.utility_model, cfg.openrouter_api_key,
                               max_tokens=cfg.utility_max_tokens,
                               thinking=cfg.utility_thinking,
                               api_base=model_api_base(cfg, cfg.utility_model))


def create_app(cfg: Config | None = None, *, chat_model=None, utility_model=None,
               embedder=None) -> FastAPI:
    cfg = cfg or Config()

    if cfg.lmstudio_preload:
        _preload_lmstudio(cfg, chat=chat_model is None or utility_model is None,
                          embed=embedder is None)

    soul_dir = cfg.vault_dir / "soul"
    if not (soul_dir / "soul.yaml").exists():
        raise RuntimeError(
            f"No Vault at {cfg.vault_dir} — run `python scripts/seed_vault.py` "
            "first (§5.1: seed once from ../yuri-soul, then the mind lives in "
            "the Vault).")

    embedder = embedder or _default_embedder(cfg)
    chat_model = chat_model or build_chat_model(cfg)
    if cfg.utility_enabled and utility_model is None:
        utility_model = build_utility_model(cfg)

    loader = SoulLoader(soul_dir, user_name=cfg.user_name)
    soul_name = loader.load().name
    utility_log = UtilityLogger(cfg.corpus_dir)
    store = FileMemoryStore(
        cfg.vault_dir, embedder, utility_model,
        char_name=soul_name.lower(), user_name=cfg.user_name,
        embed_dim=cfg.embed_dim,
        retrieval_min_sim=cfg.retrieval_min_sim,
        half_life_days=cfg.half_life_days,
        utility_log=utility_log)
    # rebuild the recall cache if the embedder changed since it was last built (§4.3)
    _ensure_index_matches_embedder(store, cfg)

    app = FastAPI(title="minimum-viable-waifu", docs_url=None, redoc_url=None)
    app.state.mvw = AppState(
        cfg=cfg, soul_loader=loader, soul_name=soul_name, store=store,
        sessions=SessionStore(cfg.vault_dir), corpus=CorpusLogger(cfg.corpus_dir),
        utility_log=utility_log,
        chat=chat_model, utility=utility_model, embedder=embedder)

    for r in (chat, greeting, session, rate, health):
        app.include_router(r.router)
    # the sanctuary: one static page, no build step (§9)
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def app() -> FastAPI:
    """uvicorn factory: `uvicorn app.main:app --factory`. Kept as a factory so
    importing this module (tests, scripts) never boots providers or the Vault."""
    return create_app()
