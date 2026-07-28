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
import logging
from dataclasses import dataclass, field

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
        return LMStudioEmbedder(cfg.embed_model, cfg.embed_dim, cfg.lmstudio_base_url)
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
    if chat_model is None or (cfg.utility_enabled and utility_model is None):
        from yurios.app.providers.openrouter import LiteLLMChatModel, LiteLLMUtilityModel
        # Local ids carry no key; they need their server's base url instead — the
        # LM Studio /v1 endpoint, or the Ollama root (so a non-default OLLAMA_BASE_URL
        # follows through to chat routing, not just the settings-panel model list).
        def _base(model: str) -> str:
            if model.startswith("lm_studio/"):
                return cfg.lmstudio_base_url
            if model.startswith("ollama/"):
                return cfg.ollama_base_url
            return ""
        chat_model = chat_model or LiteLLMChatModel(
            cfg.chat_model, cfg.openrouter_api_key, cfg.temperature,
            api_base=_base(cfg.chat_model), thinking=cfg.chat_thinking)
        if cfg.utility_enabled and utility_model is None:
            utility_model = LiteLLMUtilityModel(
                cfg.utility_model, cfg.openrouter_api_key,
                max_tokens=cfg.utility_max_tokens, thinking=cfg.utility_thinking,
                api_base=_base(cfg.utility_model))

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
