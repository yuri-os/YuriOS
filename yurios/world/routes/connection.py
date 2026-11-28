"""/api/connection — whether her model's server answers, and the button that
starts it (SPEC §31.5).

The state itself rides the sticky `connection` event to every room
(world/connection.py); these are for a caller not holding the stream, and for
the two things the room's banner can ask for. Starting runs a command on this
machine, so it is the owner's alone, like every other route that changes the
house (desktop/routes/settings._require_local). Checking only asks.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from yurios.desktop.routes.settings import _require_local

router = APIRouter()


@router.get("/api/connection")
async def connection(request: Request) -> dict:
    return request.app.state.rt.connection.snapshot()


@router.post("/api/connection/check")
async def check(request: Request) -> dict:
    return await request.app.state.rt.connection.recheck()


@router.post("/api/connection/start")
async def start(request: Request) -> dict:
    _require_local(request)
    return await request.app.state.rt.connection.start()
