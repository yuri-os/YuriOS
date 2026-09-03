"""/api/events — the one outbound stream (SPEC §4, §10), plus its two sidecars.

The YuriOS shape (host/http_api.py), ported: frontends subscribe once and every
host event — chat `message`s, streaming `draft`s, `avatar` puppet commands, the
`hello` — arrives as one typed JSON object over SSE. Sticky scene state (rain,
music, tints) replays to every new subscriber, so a reload doesn't reset the
room. This route replaces the old `/ws/avatar` puppet socket.

The SSE discipline is YuriOS's, verbatim in shape: an open stream must never
hold graceful shutdown hostage — wake every second to re-check the host's stop
flag and end cleanly (well inside uvicorn's force-close cap), pinging every
~10 s so proxies keep the pipe open.

Sidecars:
  - GET /api/history        — the transcript, for chat backfill on load and
                              for the walk back through it (`before=`, §2.6)
  - GET /selfies/{name}     — the saved selfie PNGs (SPEC §7.6)
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..main import HISTORY_MAX, HISTORY_PAGE

router = APIRouter()


@router.get("/api/events")
async def events(request: Request):
    rt = request.app.state.rt
    q = rt.hub.subscribe(viewer=True)
    # presence is a signal, not a guess (SPEC §16.2): a page attaching is the
    # closest thing the host has to "someone walked in", and the mind's world
    # model tracks it. Count viewers, not hub.subscribers — Telegram and notify
    # drain the same bus and are not company. The mirror lands in the finally
    # below.
    rt.signals.post("user_present", {"viewers": rt.hub.viewers},
                    source="frontend")
    # rt.stopping isn't set until lifespan shutdown, which uvicorn runs *after*
    # it drains connections — so during the drain this stream would never learn
    # to stop and would hold shutdown open. server.should_exit flips the instant
    # Ctrl+C lands (before the drain wait), letting the 1 s tick end the stream
    # well inside build_server's graceful cap. Absent under TestClient → None.
    server = getattr(request.app.state, "server", None)

    async def stream():
        try:
            yield "data: " + json.dumps({"type": "hello",
                                         "character": rt.cfg.companion_name}) + "\n\n"
            # sticky replay first (SPEC §4): everything subscribe() pre-loaded
            # flushes before the live loop, so a fresh page always has the
            # scene before its first live event
            while not q.empty():
                yield "data: " + json.dumps(q.get_nowait(),
                                            ensure_ascii=False) + "\n\n"
            idle = 0
            while not rt.stopping.is_set() and not (server and server.should_exit):
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    idle += 1
                    if idle >= 10:
                        idle = 0
                        yield ": ping\n\n"
                    continue
                idle = 0
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        finally:
            rt.hub.unsubscribe(q)
            # …but not on the way down. `stopping` is one of the two things that
            # ends the loop above, so this `finally` runs on every shutdown —
            # and posting there writes `traces/signals.jsonl` *after* the
            # runtime stopped, for a mind that is already cancelled and will
            # never read it. Archive moves the character's directory the moment
            # `host.stop` returns, so the write landed in the old path and left
            # a phantom folder for a character no longer in the registry.
            if rt.hub.viewers == 0 and not rt.stopping.is_set():
                rt.signals.post("user_absent", {}, source="frontend")

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/api/history")
async def history(request: Request,
                  limit: int = Query(default=HISTORY_PAGE, ge=1, le=HISTORY_MAX),
                  before: str | None = Query(default=None, max_length=64,
                                             pattern=r"^[A-Za-z0-9_-]+$")):
    """The chat backfill, and the walk back through it (SPEC §2.6).

    No arguments is the end of the conversation — what a page draws on load,
    and since the ring is seeded from disk that is still the last thing either
    of you said after a restart. `before=<message id>` is the entries just
    *older* than that one, `limit` of them: the "load the previous six" button
    at the top of the column, which walks back through the archive one press at
    a time rather than dumping a thousand lines into a page that wanted six.
    """
    return request.app.state.rt.history(limit=limit, before=before)


@router.get("/selfies/{name}")
async def selfie_file(request: Request, name: str):
    """One saved selfie. The dir may not exist until the first shot lands, so
    this is a route, not a static mount; the name is pinned to the flat dir."""
    base = request.app.state.rt.cfg.selfie_dir.resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        return JSONResponse({"error": "no such selfie"}, status_code=404)
    return FileResponse(path, media_type="image/png")
