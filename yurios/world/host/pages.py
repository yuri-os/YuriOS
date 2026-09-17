"""The HTML entry points (SPEC §29.3).

Seven routes that return a page rather than JSON: the switchboard at `/` and at
its own `/dashboard/`, the studio, and the four ways into one character — her
sanctuary, her Live2D body, the text client, and the mind debug page. Each
hands off to a Vite bundle mounted further down `create_host_app`.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse)

from ..main import DIST_DIR
from .hosting import (CharacterHost)

log = logging.getLogger("world.host")


def _html(path):
    """Vite entry HTML keeps a stable URL while hashed `/assets/*` names move.

    `Cache-Control: no-cache` (SPEC §32.1, revalidate every load) so a rebuild is not stuck
    behind yesterday's index until a hard refresh — the same rule as unhashed
    `/js/` and `/shared/` on the character app (`world/main.py`).
    """
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


def _entry(*parts: str):
    """One Vite entry's `index.html` — no-cache (SPEC §32.1), or "not built".

    The switchboard and the studio live *under* their own `StaticFiles` mounts,
    which serve their unhashed siblings and send no `Cache-Control` at all —
    which is how `/studio/` kept handing back yesterday's index after a rebuild
    while `/` had already been fixed. Declaring the directory URL here, ahead of
    those mounts, makes the entry HTML a route and leaves everything beside it
    on the mount.
    """
    path = DIST_DIR.joinpath(*parts, "index.html")
    if not path.is_file():
        return JSONResponse({"detail": "frontend not built; run npm run build in web"}, 503)
    return _html(path)


def register(app: FastAPI, host: CharacterHost, require) -> None:
    """Declare this module's routes on the host app.

    A plain closure rather than an `APIRouter` because these routes read the
    host and the registry out of the enclosing scope the way they always did,
    and rebinding them here keeps the bodies byte-identical to the single
    function they were extracted from. Declaration order is the order these
    `register` calls run in, which matters: every explicit route has to be on
    the app before `create_host_app` mounts the runtime dispatcher over
    `/api/characters`.
    """
    @app.get("/")
    async def dashboard(request: Request):
        if "desktop" in request.query_params and host.primary_id:
            query = request.url.query
            return RedirectResponse(
                f"/characters/{host.primary_id}/sanctuary/" + (f"?{query}" if query else ""))
        return _entry("dashboard")

    # The same board by its own URL (the tray's "Switchboard" opens `/dashboard/`)
    # and the studio the switchboard links to as `/studio/?character=…`. The
    # studio selects its character by query parameter, so this is the whole path
    # and it shadows none of the mount's assets.
    @app.get("/dashboard/")
    async def dashboard_page():
        return _entry("dashboard")

    @app.get("/studio/")
    async def studio_page():
        return _entry("studio")

    @app.get("/characters/{character_id}/sanctuary")
    @app.get("/characters/{character_id}/sanctuary/")
    async def sanctuary(character_id: str):
        require(character_id)
        return _html(DIST_DIR / "index.html")

    @app.get("/characters/{character_id}/live2d")
    async def character_live2d(character_id: str):
        require(character_id)
        return RedirectResponse(f"/live2d/?character={character_id}")

    # The bodyless client (SPEC §6.7): the transcript, the composer and the voice
    # loop with no renderer behind them. Same bundle root, so /assets/* below
    # serves it; shared/runtime.js reads the character out of this path.
    @app.get("/characters/{character_id}/text")
    @app.get("/characters/{character_id}/text/")
    async def character_text(character_id: str):
        require(character_id)
        return _html(DIST_DIR / "text" / "index.html")

    # The mind debug page (SPEC §24.3): not a room — it never speaks to her, it
    # reads her files. Character-scoped by path like the rooms above, so
    # shared/runtime.js can aim its /api/characters/{id}/debug/* calls.
    @app.get("/characters/{character_id}/mind")
    @app.get("/characters/{character_id}/mind/")
    async def character_mind(character_id: str):
        require(character_id)
        return _html(DIST_DIR / "mind" / "index.html")
