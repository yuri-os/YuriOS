"""/api/health — the truth about what's actually wired (B2 §3's honesty rule).

Backends degrade gracefully (voice falls back to fakes, tools to off), so the
health endpoint is where "why is she silent / why won't she set a timer?" gets
answered without reading logs.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/health")
async def health(request: Request) -> dict:
    rt = request.app.state.rt
    return {
        "ok": True,
        "character": rt.cfg.companion_name,
        "channels": rt.channels_status,    # "off" | "telegram · @bot" | "… failed: …" (§10.5)
        "voice": {
            "ready": rt.voice_ready.is_set(),
            "stt": rt.stt_name,
            "tts": rt.tts_name,
            "vad": rt.vad_name,
        },
        "tools": rt.tools_status,          # "mcp" | "fake" | "off" | "failed: …"
        "mind": rt.mind_status,            # "running" | "disabled" | "failed: …" (§15)
        "activity": rt.mind.activity.state if rt.mind else None,
        "selfies": rt.selfies_status,      # "openrouter" | "mock" | "mock (no key…)" | "off" (§7.6)
        "viewers": rt.hub.subscribers,     # attached /api/events subscribers
    }


@router.get("/api/boot")
async def boot(request: Request) -> dict:
    """The startup board (SPEC §6.4) the enter gate polls while she wakes — each
    service's pending → loading → ready|failed|skipped state, with timings. Not
    on the /api/events bus on purpose: that stream only opens after the enter
    gesture, and this is what fills the wait *before* it (world/boot.py)."""
    return request.app.state.rt.boot.snapshot()
