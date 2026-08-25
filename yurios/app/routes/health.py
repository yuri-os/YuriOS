"""GET /api/health (SPEC §10) — `vault_head` is the current Vault git SHA:
the mind's version, one request away."""
from __future__ import annotations

import asyncio

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
        # `git rev-parse` is a subprocess and /api/health is polled: off the
        # loop, or every poll is a stall the whole node pays for
        "vault_head": await asyncio.to_thread(vaultgit.head, state.cfg.vault_dir),
    }
