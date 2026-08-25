"""POST /api/session + GET /api/session/{id}/history (SPEC §10)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.post("/api/session")
async def create_session(request: Request):
    state = request.app.state.mvw
    return {"session_id": state.sessions.create()}


@router.get("/api/session/{session_id}/history")
async def history(session_id: str, request: Request):
    state = request.app.state.mvw
    session = state.sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    # The messages live in the conversation log now (app/conversation.py), not
    # in the session record. Same answer, same shape — a large ceiling rather
    # than a slice, because this route has always meant "the whole session".
    return {"messages": state.sessions.window(session_id, 100_000)}
