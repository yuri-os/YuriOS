"""POST /api/chat — one text turn over plain HTTP (SPEC §10.5).

The remote face of the `TextTurns` runner: the CLI chat (`python -m
yurios.chat`), a script, or any future thin frontend POSTs a line and gets the
committed reply back. Live token progress and her proactive lines ride the one
outbound bus (`/api/events`: `draft` and `message` events) — this route only
starts the turn and returns its commit, so a caller that never opens the
stream still gets a working conversation.

Unlike `/ws/voice` this path never waits on the voice stack: the brain is up
as soon as the server is, so a text channel talks while TTS models are still
warming.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from yurios.desktop.voice.transcript import is_meaningful_transcript

log = logging.getLogger("world.chat")
router = APIRouter()


class ChatRequest(BaseModel):
    text: str
    session_id: str | None = None
    # who's asking (shows up on the transcript + signal source): cli, api, …
    channel: str = Field(default="api", max_length=24, pattern=r"^[a-z0-9_-]+$")


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    rt = request.app.state.rt
    if not is_meaningful_transcript(req.text):
        raise HTTPException(422, "not a meaningful turn")
    try:
        return await rt.turns.run(req.text, channel=req.channel,
                                  session_id=req.session_id)
    except Exception as e:  # noqa: BLE001 — the turn left no trace (turns.py)
        raise HTTPException(502, f"turn failed: {e}")
