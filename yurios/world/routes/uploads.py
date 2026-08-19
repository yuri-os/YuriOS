"""POST /api/uploads — showing her something (SPEC §35, §10.5).

The inbound half of a picture. A composer that wants to send one puts the file
here first and gets an id back; the turn that follows names that id
(`POST /api/chat`, or the voice socket's `text` frame) and the picture joins the
prompt. Two calls rather than one because the alternative is a megabyte of
base64 inside a turn body — on the voice socket, inside a frame budget measured
in kilobytes — and because a picture that is refused should be refused *before*
somebody has typed a sentence to go with it.

The route exists only while she can actually see (`rt.image_input`, settled at
boot from her provider). With a text-only model it answers 409 and says why,
which is the same answer the composer already gave by not drawing a paperclip —
this is the half that holds for a client that isn't the browser.

`GET /api/uploads/{name}` serves them back: the chat transcript renders your own
picture beside the line it went with, on this page and on the next one to open.
Under `/api/` on purpose — that is what makes the host's per-character dispatcher
scope it to the right character for free (world/host.py).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from yurios.world.uploads import UploadRejected

log = logging.getLogger("world.uploads")
router = APIRouter()


@router.post("/api/uploads")
async def upload(request: Request, file: UploadFile = File(...)) -> dict:
    rt = request.app.state.rt
    if not rt.image_input:
        raise HTTPException(409, f"this model can't be sent pictures "
                                 f"({rt.image_input_status})")
    data = await file.read(rt.cfg.upload_max_bytes + 1)
    if len(data) > rt.cfg.upload_max_bytes:
        raise HTTPException(413, f"that picture is over the "
                                 f"{rt.cfg.upload_max_bytes // 1_000_000} MB limit")
    try:
        # Decoding and re-encoding a 12 MB photo is a second of CPU, and the
        # event loop is also carrying a token stream and an SSE fan-out.
        attachment = await request.app.state.rt.save_upload(data)
    except UploadRejected as e:
        raise HTTPException(415, str(e))
    except Exception as e:                    # noqa: BLE001 — a full disk, a bad path
        log.exception("upload failed")
        raise HTTPException(500, f"could not save that picture: {e}")
    return {"id": attachment.id, "url": attachment.url,
            "media_type": attachment.media_type,
            "width": attachment.width, "height": attachment.height,
            "bytes": attachment.bytes}


@router.get("/api/uploads/{name}")
async def upload_file(request: Request, name: str):
    """One saved picture. Like the selfie route this is a route rather than a
    static mount: the directory does not exist until the first upload lands."""
    attachment = request.app.state.rt.uploads.get(name)
    if attachment is None:
        raise HTTPException(404, "no such upload")
    return FileResponse(attachment.path, media_type=attachment.media_type)
