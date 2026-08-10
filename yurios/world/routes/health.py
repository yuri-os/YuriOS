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
        # Which brain she is speaking through *right now*: a character may run on
        # her own model rather than the one in .env, and that can be changed
        # mid-conversation (§31.4), so this reads the live Config, not the file.
        "model": rt.cfg.chat_model,
        "model_configured": rt.model_configured,
        "utility_model": rt.cfg.utility_model if rt.cfg.utility_enabled else None,
        "channels": rt.channels_status,    # "off" | "telegram · @bot" | "… failed: …" (§10.5)
        # loaded/listeners are the on-demand stack (§9.9): "unloaded" backends
        # with no listeners is her at rest, not her broken — she loads them when
        # somebody opens /ws/voice.
        "voice": rt.voice.status(),
        "tools": rt.tools_status,          # "mcp" | "fake" | "off" | "failed: …"
        "tool_count": rt.tool_count,       # discovered calls admitted to the brain
        "mind": rt.mind_status,            # "running" | "disabled" | "failed: …" (§15)
        "activity": rt.mind.activity.state if rt.mind else None,
        "selfies": rt.selfies_status,      # "openrouter" | "mock" | "mock (no key…)" | "off" (§7.6)
        "web": rt.research_status,         # "searxng" | "fake" | "off" (§7.7)
        "viewers": rt.hub.subscribers,     # attached /api/events subscribers
        "context": rt.context.snapshot(),  # prompt tokens vs the window (§11)
    }


@router.get("/api/context")
async def context(request: Request) -> dict:
    """How full her context window is (SPEC §11) — what the masthead readout
    shows, for a caller that isn't holding the event stream open.

    `limit` is CONTEXT_LENGTH when .env set one, else whatever LM Studio admitted
    to at boot, else null: a hosted route never says how big its window is, and a
    made-up ceiling would be worse than none. `used` is the last prompt measured —
    exact when the server volunteered usage, a ~4-chars/token estimate otherwise
    (`exact` says which). The turn has to fit `used + reserve`, not `used`."""
    return request.app.state.rt.context.snapshot()


@router.get("/api/boot")
async def boot(request: Request) -> dict:
    """The startup board (SPEC §6.4) the enter gate polls while she wakes — each
    service's pending → loading → ready|failed|skipped state, with timings. Not
    on the /api/events bus on purpose: that stream only opens after the enter
    gesture, and this is what fills the wait *before* it (world/boot.py).

    Her name rides along for the same reason: with a registry of characters
    (world/host.py) the gate has to say whose room this is, and it paints long
    before the `hello` event that would otherwise tell it."""
    rt = request.app.state.rt
    return {**rt.boot.snapshot(), "character": rt.cfg.companion_name}
