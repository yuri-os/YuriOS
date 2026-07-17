"""GET /api/health (SPEC §10) — `vault_head` is the current Vault git SHA:
the mind's version, one request away."""
from __future__ import annotations

from fastapi import APIRouter, Request

from yurios.app import vaultgit

router = APIRouter()


@router.get("/api/health")
async def health(request: Request):
    state = request.app.state.mvw
    return {
        "ok": True,
        "model": state.cfg.chat_model,
        "embedder": f"{state.cfg.embed_backend}:{state.cfg.embed_model}",
        "vault_head": vaultgit.head(state.cfg.vault_dir),
    }
