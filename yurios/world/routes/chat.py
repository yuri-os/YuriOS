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

`POST /api/greeting` is the other half of the same seam: the voice route greets
on connect, and a text channel has no connect — so it asks. Same runner, same
once-per-session rule, same first-arrival cold open (§5.4).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from yurios.desktop.voice.transcript import is_meaningful_transcript

log = logging.getLogger("world.chat")
router = APIRouter()


class ChatRequest(BaseModel):
    text: str
    session_id: str | None = None
    client_id: str | None = Field(default=None, max_length=64,
                                  pattern=r"^[A-Za-z0-9_-]+$")
    # who's asking (shows up on the transcript + signal source): cli, api, …
    channel: str = Field(default="api", max_length=24, pattern=r"^[a-z0-9_-]+$")


class GreetRequest(BaseModel):
    session_id: str | None = None
    channel: str = Field(default="api", max_length=24, pattern=r"^[a-z0-9_-]+$")
    client_id: str | None = Field(default=None, max_length=64,
                                  pattern=r"^[A-Za-z0-9_-]+$")


class CancelRequest(BaseModel):
    client_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    selfie_ids: list[str] = Field(default_factory=list, max_length=16)


def _tasks(request: Request) -> dict[str, asyncio.Task]:
    tasks = getattr(request.app.state, "chat_tasks", None)
    if tasks is None:
        tasks = request.app.state.chat_tasks = {}
    return tasks


def _cancelled(request: Request) -> set[str]:
    cancelled = getattr(request.app.state, "chat_cancelled", None)
    if cancelled is None:
        cancelled = request.app.state.chat_cancelled = set()
    return cancelled


def _stopped(request: Request) -> set[str]:
    """Turns `/api/chat/cancel` cancelled on purpose.

    The discriminator `_tracked` needs. A `CancelledError` at `await task` has
    two sources — the Stop button, and Starlette cancelling the handler because
    the client disconnected or the server is shutting down — and only the first
    is a 409. Answering both with one meant an ordinary disconnect was reported
    as a user-initiated stop and, worse, the cancellation stopped propagating,
    so shutdown had to wait out a turn that nobody was listening to.
    """
    stopped = getattr(request.app.state, "chat_stopped", None)
    if stopped is None:
        stopped = request.app.state.chat_stopped = set()
    return stopped


async def _tracked(request: Request, client_id: str | None, coroutine):
    if not client_id:
        return await coroutine
    cancelled = _cancelled(request)
    if client_id in cancelled:
        cancelled.discard(client_id)
        coroutine.close()
        raise HTTPException(409, "turn cancelled")
    tasks = _tasks(request)
    if client_id in tasks:
        coroutine.close()
        raise HTTPException(409, "request already processing")
    task = asyncio.create_task(coroutine, name=f"chat-{client_id}")
    tasks[client_id] = task
    stopped = _stopped(request)
    try:
        return await task
    except asyncio.CancelledError:
        if client_id in stopped:
            raise HTTPException(409, "turn cancelled") from None
        raise                                  # the client left, or we're closing
    finally:
        stopped.discard(client_id)
        if tasks.get(client_id) is task:
            tasks.pop(client_id, None)


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    rt = request.app.state.rt
    if req.channel == "telegram":
        raise HTTPException(422, "telegram origin is reserved for the bot adapter")
    if not is_meaningful_transcript(req.text):
        raise HTTPException(422, "not a meaningful turn")
    try:
        return await _tracked(request, req.client_id, rt.turns.run(
            req.text, channel=req.channel, session_id=req.session_id,
            client_id=req.client_id))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — the turn left no trace (turns.py)
        raise HTTPException(502, f"turn failed: {e}")


@router.post("/api/greeting")
async def greeting(req: GreetRequest, request: Request):
    """She speaks first (SPEC §7): the text channels' half of the voice route's
    greeting fork. A terminal or a thin client calls this once on arrival and
    renders the `message` it gets back (or watches it land on `/api/events`,
    like every other line she says).

    Idempotent per session per run — a second call returns `message: null`
    rather than greeting twice — so a client that reconnects can simply call it
    again. On the very first arrival this is the path that plays `BOOTSTRAP.md`'s
    cold open and then retires it (§5.4).
    """
    rt = request.app.state.rt
    if req.channel == "telegram":
        raise HTTPException(422, "telegram origin is reserved for the bot adapter")
    try:
        return await _tracked(request, req.client_id, rt.turns.greet(
            channel=req.channel, session_id=req.session_id))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — nothing was committed (turns.py)
        raise HTTPException(502, f"greeting failed: {e}")


@router.post("/api/chat/cancel")
async def cancel_chat(req: CancelRequest, request: Request):
    """Stop one browser request and any camera work it started."""
    task = _tasks(request).get(req.client_id)
    if task and not task.done():
        # Marked before the cancel, so `_tracked` knows this one was the Stop
        # button and not a disconnect (see `_stopped`).
        _stopped(request).add(req.client_id)
        task.cancel()
    elif len(_cancelled(request)) < 256:
        # The stop POST can beat the original request after AbortController
        # closes its connection. Keep a short tombstone so that late request
        # cannot start generation after the UI has already reported it stopped.
        cancelled = _cancelled(request)
        cancelled.add(req.client_id)
        asyncio.get_running_loop().call_later(30, cancelled.discard, req.client_id)
    rt = request.app.state.rt
    cancelled_selfies = await rt.selfies.cancel(
        req.selfie_ids, client_id=req.client_id) if rt.selfies else []
    return {"cancelled": bool(task) or bool(cancelled_selfies),
            "selfie_ids": cancelled_selfies}
