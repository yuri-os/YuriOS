"""GET /api/health — liveness + which backends are wired (SPEC §10)."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/health")
async def health(request: Request):
    rt = request.app.state.rt
    # report what's ACTUALLY wired (after any fallback to a fake), not what was
    # requested — so "why is she silent?" is answerable with one curl.
    return {
        "ok": True,
        "chat_model": rt.cfg.chat_model,
        "stt": rt.stt_name,
        "tts": rt.tts_name,
        "vad": rt.vad_name,
        "stt_requested": rt.cfg.stt_backend,
        "tts_requested": rt.cfg.tts_backend,
        "vad_requested": rt.cfg.vad_backend,
        "masking": rt.filler_bank is not None,
    }
