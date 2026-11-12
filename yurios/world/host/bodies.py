"""The body library and what each character wears (SPEC §6.8).

The house keeps one library of bodies (`characters/bodylib.py`) — the VRMs and
Live2D rigs you uploaded, plus the ones that ship — and each character names one
of each from it. The studio's Body page is where both happen: add and remove
bodies, choose hers, and choose which of the two her desktop window floats.

Every room asks the host, not its own runtime, what to load: `GET
/api/characters/<id>/body` for a VRM room and `GET /api/characters/<id>/config`
for the Live2D one. Both read her record at request time, which is what lets a
body change without rebuilding her (§31.4).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from yurios.characters import CharacterRecord, bodylib, vrmbody
from yurios.characters.bodylib import BodyLibrary

from .hosting import CharacterHost

log = logging.getLogger("world.host")


def library_for(host: CharacterHost) -> BodyLibrary:
    from yurios.world.main import WEB_DIR
    return BodyLibrary(host.base.data_dir / "bodies",
                       live2d_vendor_dir=WEB_DIR / "live2d",
                       vrm_bundled=WEB_DIR / "models" / "avatar.vrm")


def adopt_legacy_bodies(host: CharacterHost, library: BodyLibrary) -> None:
    """File any `body/body.vrm` a character still has into the library, once.

    Bodies used to live in each character's folder. A model found there moves
    into the library, she is set to wear it, and the empty folder goes — so the
    next start finds nothing to do.
    """
    for record in host.registry.list():
        legacy = record.paths.body_vrm
        if not legacy.is_file():
            continue
        try:
            entry = library.adopt_vrm(legacy, f"{record.display.name} body")
        except (OSError, ValueError):
            log.warning("could not file %s's old body into the library", record.id,
                        exc_info=True)
            continue
        record.body.vrm = entry.id
        host.registry.upsert(record)
        try:
            legacy.parent.rmdir()
        except OSError:
            pass
        log.info("filed %s's own body into the library as %s", record.id, entry.id)


def register(app: FastAPI, host: CharacterHost, require) -> None:
    """Declare the body routes; like every host module, before the dispatcher mounts."""
    registry = host.registry
    library = library_for(host)
    adopt_legacy_bodies(host, library)
    app.state.bodies = library

    def _kind(kind: str) -> str:
        if kind not in bodylib.KINDS:
            raise HTTPException(404, "no such kind of body")
        return kind

    def _listing() -> dict[str, Any]:
        """Every body, each with who wears it — resolved, so the default lists the
        characters who never chose, and a body nobody can wear lists nobody."""
        records = registry.list()
        listing: dict[str, Any] = {}
        for kind in bodylib.KINDS:
            worn: dict[str, list[dict[str, str]]] = {}
            for record in records:
                chosen = record.body.vrm if kind == "vrm" else record.body.model
                worn.setdefault(library.resolve(kind, chosen).id, []).append(
                    {"id": record.id, "name": record.display.name})
            listing[kind] = [{**entry.to_dict(), "worn_by": worn.get(entry.id, [])}
                             for entry in library.entries(kind)]
        listing["limits"] = {"vrm": vrmbody.MAX_VRM_BYTES,
                             "live2d": bodylib.MAX_LIVE2D_ZIP_BYTES}
        return listing

    def _state(record: CharacterRecord) -> dict[str, Any]:
        """What she wears. `vrm_url` / `live2d_url` are what a room loads, resolved here
        so no page decides for itself what a missing or unknown body falls back to."""
        vrm = library.resolve("vrm", record.body.vrm)
        live2d = library.resolve("live2d", record.body.model)
        return {
            "backend": record.body.backend,
            "desktop_body": host.desktop_body(record.id),
            "vrm": vrm.to_dict(), "vrm_url": vrm.url,
            "live2d": live2d.to_dict(), "live2d_url": live2d.url,
        }

    def _wear(record: CharacterRecord, state: dict[str, Any]) -> None:
        """Put her VRM on in any VRM room of hers that is open — the desktop window too."""
        rt = host.runtime(record.id)
        if rt is not None:
            rt.controller.load_model(state["vrm_url"])

    # ---- the library ----

    @app.get("/api/bodies")
    async def list_bodies():
        return await asyncio.to_thread(_listing)

    @app.post("/api/bodies/{kind}")
    async def add_body(kind: str, file: UploadFile):
        """Add a body: a `.vrm`, or a Live2D rig's folder zipped. Checked before it is kept."""
        add = library.add_vrm if _kind(kind) == "vrm" else library.add_live2d
        try:
            entry = await asyncio.to_thread(add, file.file, file.filename or kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"body": entry.to_dict(), "library": await asyncio.to_thread(_listing)}

    @app.delete("/api/bodies/{kind}/{body_id}")
    async def remove_body(kind: str, body_id: str):
        """Take a body out of the library. Whoever wore it goes back to the default."""
        _kind(kind)

        def remove() -> list[CharacterRecord]:
            entry = library.get(kind, body_id)
            if entry is None:
                raise KeyError(body_id)
            wearers = [r for r in registry.list()
                       if (r.body.vrm if kind == "vrm" else r.body.model) == entry.id]
            library.remove(kind, entry.id)
            for record in wearers:
                if kind == "vrm":
                    record.body.vrm = ""
                else:
                    record.body.model = ""
                registry.upsert(record)
            return wearers

        try:
            wearers = await asyncio.to_thread(remove)
        except KeyError:
            raise HTTPException(404, "no such body") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if kind == "vrm":
            for record in wearers:
                _wear(record, await asyncio.to_thread(_state, record))
        return await asyncio.to_thread(_listing)

    @app.get("/api/bodies/vrm/{body_id}.vrm")
    async def vrm_file(body_id: str):
        path = library.vrm_file(body_id)
        if path is None:
            raise HTTPException(404, "no such body")
        return FileResponse(path, media_type="model/gltf-binary",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/bodies/live2d/{body_id}/files/{relative:path}")
    async def live2d_file(body_id: str, relative: str):
        path = library.live2d_file(body_id, relative)
        if path is None:
            raise HTTPException(404, "no such file in that rig")
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache"})

    # ---- what she wears ----

    @app.get("/api/characters/{character_id}/body")
    async def body_state(character_id: str):
        return await asyncio.to_thread(_state, require(character_id))

    @app.put("/api/characters/{character_id}/body")
    async def choose_body(character_id: str, request: Request):
        """Choose her bodies and which one floats: `{backend?, vrm?, live2d?}`.

        Each field is optional and applied as given — an id the library does not
        know is refused rather than saved, so a typo cannot quietly put her in
        the default. A new VRM is put on in her open rooms at once.
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected an object")
        record = require(character_id)
        before = record.body.vrm
        if "backend" in body:
            backend = str(body["backend"] or "")
            if backend not in ("", "vrm", "live2d"):
                raise HTTPException(400, "backend is vrm, live2d or empty")
            record.body.backend = backend
        for field, kind in (("vrm", "vrm"), ("live2d", "live2d")):
            if field not in body:
                continue
            chosen = str(body[field] or "").strip().lower()
            if chosen and await asyncio.to_thread(library.get, kind, chosen) is None:
                raise HTTPException(400, f"no {kind} body called {chosen!r} in the library")
            if kind == "vrm":
                record.body.vrm = "" if chosen == bodylib.BUNDLED_VRM else chosen
            else:
                record.body.model = chosen
        await asyncio.to_thread(registry.upsert, record)
        state = await asyncio.to_thread(_state, record)
        if record.body.vrm != before:
            _wear(record, state)
        return state

    @app.get("/api/characters/{character_id}/config")
    async def live2d_config(character_id: str):
        """The Live2D room's question (SPEC §6.6), answered from her record and the library.

        Shadows the runtime's own `/api/config`, which can only see the vendor
        rigs and her config as it was when she was built.
        """
        record = require(character_id)
        rigs = await asyncio.to_thread(library.entries, "live2d")
        rig = await asyncio.to_thread(library.resolve, "live2d", record.body.model)
        return {"avatar_model": rig.id, "avatar_model_url": rig.url,
                "avatar_available": [r.id for r in rigs]}
