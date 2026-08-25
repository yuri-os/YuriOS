"""`create_host_app` — the switchboard app, composed (SPEC §29).

What is left here is the shape of the host app and nothing about any route it
serves: the lifespan that starts and stops every character, the owner boundary,
the five route modules in the order they must be declared, and the mounts —
which come last because the dispatcher over `/api/characters` would otherwise
swallow the explicit routes above it.

This file was 1,138 lines and 69 routes. The routes did not get smaller; they
got an address, so changing the studio no longer means reading the host's
lifecycle to find it.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from starlette.staticfiles import StaticFiles

from yurios.characters import CharacterRecord, CharacterRegistry

from ..config import Config
from ..main import DIST_DIR, WEB_DIR
from . import brains, pages, studio, switchboard
from . import debug as debug_routes
from .hosting import CharacterHost, _RuntimeDispatcher, _turn_away

log = logging.getLogger("world.host")


def create_host_app(base: Config, registry: CharacterRegistry | None = None) -> FastAPI:
    if registry is None:
        registry = CharacterRegistry(base.data_dir)
    host = CharacterHost(base, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await host.start_all()
        # After the characters, because the tray's first read should show the
        # house as it actually is rather than an empty one it corrects a beat
        # later. Never fatal: `start()` swallows a missing session bus, a
        # missing dbus-fast and a missing tray host, because none of those is a
        # reason for the daemon not to run.
        tray = None
        if base.tray_enabled:
            from ..tray import Tray
            tray = Tray(host, base_url=f"http://{base.host}:{base.port}")
            try:
                log.info("tray: %s", await tray.start())
            except Exception:            # noqa: BLE001
                log.warning("tray failed to start", exc_info=True)
                tray = None
        app.state.tray = tray
        yield
        if tray is not None:
            await tray.stop()
        await host.stop_all()

    app = FastAPI(title="YuriOS Host", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.host = host
    app.state.lifecycle_lock = asyncio.Lock()
    app.state.purge_challenges = {}
    from yurios.security import install_http_boundaries, install_owner_security
    install_http_boundaries(app)
    host.owner_boundary = install_owner_security(app, base)

    # The house `.env` panel (SPEC §11), on the board as well as in every room.
    # Declared here rather than left to the primary-character fallback at the
    # bottom of this file, because the switchboard is the surface you reach for
    # when *nothing* is running — a fresh install with no character yet, or a
    # node where every one of them is parked — and a settings screen that is
    # only reachable once she is up cannot be where you go to fix her config.
    from yurios.desktop.routes import settings as env_panel
    app.include_router(env_panel.router)

    def require(character_id: str) -> CharacterRecord:
        record = registry.get(character_id)
        if record is None:
            raise HTTPException(404, "no such character")
        return record

    # The routes, in five modules and in this order (SPEC §29). Order is not
    # cosmetic: every explicit route has to be declared before the runtime
    # dispatcher is mounted over `/api/characters` below, or the dispatcher
    # swallows them — and within `debug`, `/debug/prompts/days` has to precede
    # `/debug/prompts/{prompt_id}` or the literal never matches.
    switchboard.register(app, host, require)
    brains.register(app, host, require)
    studio.register(app, host, require)
    debug_routes.register(app, host, require)
    pages.register(app, host, require)


    # Both Vite entry points emit their hashed bundles into this shared root.
    # Keep it ahead of the primary-runtime fallback so /assets/* is not
    # dispatched to a character app that was created without its frontend.
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets", check_dir=False),
              name="frontend-assets")
    # …and the panel's own source, for the same reason its API is here: the board
    # loads /shared/settings.{js,css} directly, and without this mount the path
    # falls through to the primary character — which, on the node this panel
    # exists to rescue, is not there.
    app.mount("/shared", StaticFiles(directory=WEB_DIR / "shared"), name="host-shared")
    app.mount("/dashboard", StaticFiles(directory=DIST_DIR / "dashboard", html=True,
                                         check_dir=False), name="dashboard")
    # The studio selects its character by query parameter (`/studio/?character=…`)
    # rather than by path, following `/live2d/?character=…`: a `/studio/{id}` route
    # would have to be declared before this mount and would shadow its assets.
    app.mount("/studio", StaticFiles(directory=DIST_DIR / "studio", html=True,
                                     check_dir=False), name="studio")
    app.mount("/api/characters", _RuntimeDispatcher(host, "api"), name="character-api")
    app.mount("/ws/characters", _RuntimeDispatcher(host, "ws"), name="character-ws")

    # Root compatibility APIs and shared assets continue to address the primary.
    class Primary:
        async def __call__(self, scope, receive, send):
            child = host.apps.get(host.primary_id or "")
            if child is None:
                # /ws/voice lands here on a single-character node, so this refusal
                # has to speak websocket too (see _turn_away).
                await _turn_away(scope, receive, send, "no active character",
                                 status=503)
                return
            child.state.server = getattr(app.state, "server", None)
            await child(scope, receive, send)

    app.mount("/", Primary(), name="primary-character")
    return app
