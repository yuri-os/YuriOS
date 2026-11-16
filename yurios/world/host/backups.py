"""Snapshots, backups and restores of a whole character (SPEC §29.8).

The format and the unpacking live in `characters/backup.py`. This module is the
lifecycle around them, which is most of what can go wrong:

- **A copy is taken of a quiet tree.** A running character is stopped for the
  copy and started again after, as a clone does (§36.3), because a mind waking
  mid-copy writes into the tree being copied. The copy runs on a worker, and
  every other room keeps answering.
- **A restore in her place archives her first.** The state it replaces is one
  unarchive away (§29.6), never gone. A backup that fails to unpack is found out
  *before* she is archived, so a bad file costs nothing.
- **An uploaded backup is a file from outside.** It restores under review, as an
  imported card does (§30.3). A snapshot this node wrote keeps the lifecycle it
  recorded.

Downloads and uploads go through `DATA_DIR/.backups/`: a backup is written there
and fetched by a one-time URL, so a 400 MB file is streamed by the browser's own
download manager and never held as a blob in a page. An upload is streamed there,
read, and shown to you before anything is restored from it. Both expire.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import logging
import os
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from yurios.characters import CharacterRecord
from yurios.characters import archive as archive_model
from yurios.characters import backup as backup_model
from yurios.characters.models import new_character_id

from .hosting import CharacterHost
from .switchboard import archive_character

log = logging.getLogger("world.host")

T = TypeVar("T")

UPLOAD_PATH = "/api/backups/uploads"
#: How long a prepared download or a read upload waits for you.
PENDING_TTL_S = 60 * 60
#: How many may wait at once, per kind. The oldest goes first.
MAX_PENDING = 4
_WRITE_BATCH = 4 * 1024 * 1024


@dataclass(slots=True)
class Pending:
    path: Path
    kind: str                       # "download" | "upload"
    expires: float
    filename: str = ""
    manifest: backup_model.Manifest | None = None


def register(app: FastAPI, host: CharacterHost, require) -> None:
    """Declare the backup routes; like every host module, before the dispatcher mounts."""
    registry = host.registry
    data_root = registry.data_root
    work_root = data_root / ".backups"
    # Anything here is from a previous process: its tokens died with it.
    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)
    pending: dict[str, Pending] = {}
    app.state.backup_pending = pending

    def _library():
        return getattr(app.state, "bodies", None)

    # ---- pending files -------------------------------------------------------

    def _drop(token: str) -> None:
        item = pending.pop(token, None)
        if item is not None:
            item.path.unlink(missing_ok=True)

    def _prune(kind: str) -> None:
        now = time.monotonic()
        for token, item in list(pending.items()):
            if item.expires <= now:
                _drop(token)
        mine = [t for t, item in pending.items() if item.kind == kind]
        while len(mine) >= MAX_PENDING:
            _drop(mine.pop(0))

    def _new_pending(kind: str) -> tuple[str, Path]:
        _prune(kind)
        token = secrets.token_urlsafe(18)
        folder = work_root / f"{kind}s"
        folder.mkdir(parents=True, exist_ok=True)
        return token, folder / f"{token}.zip"

    def _take_pending(token: str, kind: str) -> Pending:
        item = pending.get(token)
        if item is None or item.kind != kind or item.expires <= time.monotonic():
            raise HTTPException(404, f"that {kind} has expired — start again")
        return item

    # ---- a quiet copy --------------------------------------------------------

    async def _quiet(record: CharacterRecord,
                     work: Callable[[], T]) -> tuple[T, str | None]:
        """Run ``work`` on a worker with her runtime stopped, then start her again.

        Holds the lifecycle lock, as a clone does. A source that fails to come
        back is reported beside the answer, never in place of it."""
        character_id = record.id
        async with app.state.lifecycle_lock:
            host._busy[character_id] = "backed up"
            was_running = host.runtime(character_id) is not None
            source_error: str | None = None
            try:
                if was_running:
                    await host.stop(character_id)
                task = asyncio.create_task(asyncio.to_thread(work))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A cancelled request cannot abandon a worker mid-write.
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            finally:
                host._busy.pop(character_id, None)
                if was_running:
                    try:
                        await host.start(character_id)
                    except Exception as exc:             # already logged by start()
                        source_error = str(exc)
            return result, source_error

    async def _guarded(call: Awaitable[T]) -> T:
        try:
            return await call
        except backup_model.BackupError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---- snapshots -----------------------------------------------------------

    @app.get("/api/snapshots")
    async def all_snapshots():
        """Every snapshot on the node, including those of characters no longer on
        the board — the shelf a purge does not reach."""
        rows = await asyncio.to_thread(backup_model.list_snapshots, data_root)
        for row in rows:
            row["on_board"] = registry.get(row["id"]) is not None
        return {"snapshots": rows}

    @app.get("/api/characters/{character_id}/snapshots")
    async def snapshots(character_id: str):
        require(character_id)
        rows = await asyncio.to_thread(backup_model.list_snapshots, data_root, character_id)
        return {"snapshots": rows}

    @app.post("/api/characters/{character_id}/snapshots")
    async def take_snapshot(character_id: str):
        record = copy.deepcopy(require(character_id))
        dest = backup_model.new_snapshot_path(data_root, character_id)
        manifest, source_error = await _guarded(_quiet(record, lambda: backup_model.write_backup(
            record, data_root, dest, library=_library(), kind="snapshot")))
        row = {**manifest.summary(), "snapshot": dest.name, "size": dest.stat().st_size}
        return JSONResponse({"snapshot": row, "source_error": source_error},
                            status_code=201)

    def _snapshot(character_id: str, name: str) -> Path:
        path = backup_model.snapshot_path(data_root, character_id, name)
        if path is None:
            raise HTTPException(404, "no such snapshot")
        return path

    @app.get("/api/characters/{character_id}/snapshots/{name}")
    async def download_snapshot(character_id: str, name: str):
        path = _snapshot(character_id, name)
        return FileResponse(path, media_type="application/zip", filename=f"yurios-{name}",
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/characters/{character_id}/snapshots/{name}")
    async def delete_snapshot(character_id: str, name: str):
        path = _snapshot(character_id, name)
        await asyncio.to_thread(path.unlink)
        try:
            path.parent.rmdir()                     # her shelf, if that was the last
        except OSError:
            pass
        return {"deleted": True, "snapshot": name}

    @app.post("/api/characters/{character_id}/snapshots/{name}/restore")
    async def restore_snapshot(character_id: str, name: str, request: Request):
        path = _snapshot(character_id, name)
        body = await _json_object(request)
        manifest = await _guarded(asyncio.to_thread(backup_model.read_manifest, path))
        return await _restore(path, manifest, body, trusted=True, target_id=character_id)

    # ---- a backup to take away -----------------------------------------------

    @app.post("/api/characters/{character_id}/backup")
    async def prepare_backup(character_id: str):
        """Write her whole character to a file and answer the URL that fetches it once."""
        record = copy.deepcopy(require(character_id))
        token, dest = _new_pending("download")
        try:
            manifest, source_error = await _guarded(_quiet(
                record, lambda: backup_model.write_backup(
                    record, data_root, dest, library=_library(), kind="backup")))
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        # Local time, as a snapshot's name and an archive folder's are.
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"yurios-{character_id}-{stamp}.zip"
        pending[token] = Pending(dest, "download", time.monotonic() + PENDING_TTL_S,
                                 filename=filename, manifest=manifest)
        return {"url": f"/api/backups/downloads/{token}", "filename": filename,
                "size": dest.stat().st_size, "backup": manifest.summary(),
                "source_error": source_error}

    @app.get("/api/backups/downloads/{token}")
    async def fetch_backup(token: str):
        item = _take_pending(token, "download")
        # Kept until it has been sent once, or until it expires: a download the
        # browser abandoned can be asked for again within the hour.
        return FileResponse(item.path, media_type="application/zip", filename=item.filename,
                            headers={"Cache-Control": "no-store"},
                            background=BackgroundTask(_drop, token))

    # ---- a backup brought back -----------------------------------------------

    @app.post(UPLOAD_PATH)
    async def upload_backup(request: Request):
        """Stream a backup file to disk and say what is in it. Restores nothing."""
        token, dest = _new_pending("upload")
        limit = backup_model.DEFAULT_LIMITS.archive_bytes
        size = 0
        buffer = bytearray()
        try:
            with dest.open("wb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "that backup is over the "
                                                 f"{limit // (1024 ** 3)} GB this node restores")
                    buffer += chunk
                    if len(buffer) >= _WRITE_BATCH:
                        await asyncio.to_thread(handle.write, bytes(buffer))
                        buffer.clear()
                if buffer:
                    await asyncio.to_thread(handle.write, bytes(buffer))
            if size == 0:
                raise HTTPException(400, "no file arrived — choose a backup to restore")
            manifest = await _guarded(asyncio.to_thread(backup_model.read_manifest, dest))
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        pending[token] = Pending(dest, "upload", time.monotonic() + PENDING_TTL_S,
                                 manifest=manifest)
        existing = registry.get(manifest.character_id)
        # `size` is the file, as the drawer lists a snapshot: what a person
        # compares. The manifest's `bytes` is her tree alone, without her bodies.
        return {"upload": token, "backup": {**manifest.summary(), "size": size},
                "exists": existing is not None,
                "existing_name": existing.display.name if existing else None}

    @app.delete("/api/backups/uploads/{token}")
    async def cancel_upload(token: str):
        _drop(token)
        return {"cancelled": True}

    @app.post("/api/backups/uploads/{token}/restore")
    async def restore_upload(token: str, request: Request):
        item = _take_pending(token, "upload")
        body = await _json_object(request)
        assert item.manifest is not None
        result = await _restore(item.path, item.manifest, body, trusted=False,
                                target_id=item.manifest.character_id)
        _drop(token)
        return result

    # ---- the restore itself --------------------------------------------------

    async def _json_object(request: Request) -> Mapping[str, Any]:
        body = await request.json() if await request.body() else {}
        if not isinstance(body, Mapping):
            raise HTTPException(400, "restore request must be a JSON object")
        return body

    async def _restore(source: Path, manifest: backup_model.Manifest,
                       body: Mapping[str, Any], *, trusted: bool,
                       target_id: str) -> JSONResponse:
        mode = str(body.get("mode") or "new")
        if mode not in ("new", "replace"):
            raise HTTPException(400, "mode must be 'new' or 'replace'")
        name = str(body.get("name") or "").strip() or None
        library = _library()
        async with app.state.lifecycle_lock:
            current = registry.get(target_id)
            if mode == "replace" and current is not None:
                character_id = target_id
            else:
                characters_dir = data_root / "characters"
                taken = {record.id for record in registry}
                if characters_dir.is_dir():
                    taken.update(path.name for path in characters_dir.iterdir())
                if target_id not in taken:
                    character_id = target_id
                else:
                    name = name or f"{manifest.name} (restored)"
                    character_id = new_character_id(name, taken)
            # Unpack first, with her still in place: a file that will not restore
            # is found out while there is still nothing to undo.
            staged = await _guarded(asyncio.to_thread(
                backup_model.stage_restore, source, registry,
                character_id=character_id, name=name, trusted=trusted,
                library=library, allow_existing=current is not None and mode == "replace"))
            archived: str | None = None
            try:
                if mode == "replace" and current is not None:
                    archived = await archive_character(app, host, current)
                await asyncio.to_thread(staged.install, registry)
            except BaseException:
                await asyncio.to_thread(staged.discard, library)
                if archived is not None:
                    await _unarchive(archived, current)
                raise
            record = staged.record
            started, detail = False, None
            if record.lifecycle.enabled and not record.lifecycle.review_required:
                try:
                    await host.start(record.id)
                    started = True
                except Exception as exc:                   # already logged by start()
                    detail = str(exc)
            return JSONResponse(
                {"character": host.summary(record), "started": started, "error": detail,
                 "archived": archived, "warnings": staged.warnings},
                status_code=201)

    async def _unarchive(name: str, record: CharacterRecord | None) -> None:
        """Put back the character a failed replace archived. Best effort, and loud."""
        if record is None:
            return
        source = data_root / "archives" / name
        try:
            await asyncio.to_thread(os.replace, source, record.paths.root)
            registry.add(record)
            (record.paths.root / archive_model.SNAPSHOT_NAME).unlink(missing_ok=True)
        except Exception:
            log.exception("restore failed and %s could not be put back from %s; "
                          "she is still in the archive", record.id, name)
