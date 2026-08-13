"""/api/inbox — what she is still owed a look at (SPEC §18.4, §32.5).

Three routes over `world/inbox.py`:

  - `GET  /api/inbox`         — the pending run, for the chat view's
    "while you were away"; `?all=1` includes what has already been seen.
  - `POST /api/inbox/read`    — everything pending has now been seen.
  - `GET  /api/notifications` — the desktop shell's stream (channels/notify.py).

The read route is a state change made from her room, so it is owner-gated like
the other switches; the reads are not, because they say no more than
`/api/history` already does to the same origin.

`/api/notifications` is **not** an `/api/events` alias and must never become one.
Attaching to `/api/events` posts a `user_present` signal, and a desktop shell is
attached for as long as it is running: she would read a tray icon as company,
Gate 2 would treat every reach-out as an interruption of a conversation already
under way, and the stream built to deliver her initiative would suppress it
instead. This one signals nothing.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from yurios.security import owner_or_loopback

router = APIRouter()


def _inbox(request: Request):
    rt = request.app.state.rt
    return rt.inbox


@router.get("/api/inbox")
async def inbox(request: Request, all: bool = False) -> dict:
    """Her pending reach-outs, oldest first — the order she said them in."""
    box = _inbox(request)
    entries = box.entries() if all else box.pending()
    return {"entries": entries, "unread": box.unread()}


@router.post("/api/inbox/read")
async def mark_read(request: Request) -> dict:
    """Being in her room is the acknowledgement (§32.5). No per-entry dismiss:
    two contradictory answers to "did you see this?" is worse than one."""
    if not owner_or_loopback(request):
        raise HTTPException(403, "owner authentication required")
    box = _inbox(request)
    return {"marked": box.mark_read(), "unread": box.unread()}


def _notify_channel(request: Request):
    rt = getattr(request.app.state, "rt", None)
    mgr = getattr(rt, "channels", None)
    for ch in getattr(mgr, "channels", []):
        if ch.name == "notify":
            return ch
    return None


@router.get("/api/notifications")
async def notifications(request: Request):
    """The desktop shell's feed of things worth drawing on the desktop.

    404 when the channel is off, so the shell can stop asking rather than
    reconnect into a stream that will never carry anything.
    """
    channel = _notify_channel(request)
    if channel is None:
        raise HTTPException(404, "notifications are off (NOTIFY_ENABLED)")
    rt = request.app.state.rt
    q = channel.subscribe()
    server = getattr(request.app.state, "server", None)

    async def stream():
        try:
            yield "data: " + json.dumps({"type": "hello",
                                         "character": rt.cfg.character_id,
                                         "name": rt.cfg.companion_name}) + "\n\n"
            idle = 0
            # the /api/events discipline, verbatim: wake every second so an open
            # stream never holds graceful shutdown open, ping every ~10 s so a
            # proxy doesn't reap the pipe.
            while not rt.stopping.is_set() and not (server and server.should_exit):
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    idle += 1
                    if idle >= 10:
                        idle = 0
                        yield ": ping\n\n"
                    continue
                if item is None:
                    break                       # the channel is shutting down
                idle = 0
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        finally:
            channel.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")
