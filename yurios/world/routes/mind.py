"""/api/mind — the inner-life surface (SPEC §24.3).

The journal and the dashboard are the product half of autonomy: "what did she
do while I was gone" must be a page you open, not a vibe. Everything here
reads *through* the mind's own stores — the same files she reads — so the
dashboard can never disagree with reality. The one write path is the self-edit
decision, and even that is only a signal: the loop consumes it on its next
tick, exactly like everything else that happens to her.
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _mind(request: Request):
    mind = request.app.state.rt.mind
    if mind is None:
        raise HTTPException(503, "the mind isn't running (MIND_ENABLED, or a test brain)")
    return mind


@router.get("/api/mind")
async def mind_state(request: Request) -> dict:
    """Activity state, cadence, budget, goals, the shelf, pending self-edits."""
    return _mind(request).snapshot()


@router.get("/api/mind/journal")
async def journal(request: Request, days: int = 3) -> dict:
    """The last `days` of the shared journal — her acts flagged `hers`."""
    mind = _mind(request)
    now = mind.clock.now()
    out = []
    for i in range(max(1, min(days, 30))):
        day = datetime.datetime.fromtimestamp(
            now - i * 86400).strftime("%Y-%m-%d")
        entries = mind.journal.day_entries(day)
        if entries:
            out.append({"day": day, "entries": entries})
    return {"days": out}


@router.get("/api/mind/trace")
async def trace(request: Request, n: int = 40) -> dict:
    """The tick trace tail — the why-record behind the journal."""
    return {"ticks": _mind(request).trace.tail(max(1, min(n, 200)))}


@router.post("/api/mind/edits/{edit_id}")
async def decide_edit(edit_id: str, request: Request) -> dict:
    """Rule on a queued self-edit. Body: {"approve": true|false}. The decision
    is a signal; the loop applies (or rejects) it on its next tick, commits it,
    and journals what you decided — so even your rulings leave a trail."""
    body = await request.json()
    mind = _mind(request)
    if not any(p["id"] == edit_id for p in mind.selfedit.pending()):
        raise HTTPException(404, f"no pending edit {edit_id}")
    request.app.state.rt.signals.post(
        "selfedit_decision",
        {"id": edit_id, "approve": bool(body.get("approve"))}, source="user")
    return {"queued": True, "id": edit_id}
