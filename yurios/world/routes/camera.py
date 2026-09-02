"""Owner-requested camera shots (SPEC §7.6, §36).

The MCP hands `take_selfie` / `show_picture` are how *she* asks. These routes
are how *you* ask: the same lab, the same contract, no Guard and no mind tick.
The product lands in the gallery (`_deliver: "vault"`) rather than as a chat
bubble, so a CLI render does not inject a blank selfie into an open room.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class SelfieRequest(BaseModel):
    look: str = ""
    scene: str = ""
    framing: str = ""
    lighting: str = ""
    mood: str = ""
    wardrobe: str = ""
    avoid: str = ""


class PictureRequest(BaseModel):
    subject: str = Field(min_length=1)
    avoid: str = ""


def _lab(request: Request):
    rt = request.app.state.rt
    if rt.selfies is None:
        raise HTTPException(409, f"camera is {rt.selfies_status or 'off'}")
    return rt.selfies


def _slot(value: str) -> str | None:
    text = (value or "").strip()
    return text or None


@router.post("/api/selfie")
async def owner_selfie(request: Request, body: SelfieRequest | None = None) -> dict:
    """Start a selfie. Returns immediately; the PNG lands on the gallery."""
    body = body or SelfieRequest()
    lab = _lab(request)
    contract = {
        "id": uuid.uuid4().hex[:8],
        "look": body.look.strip() or None,
        "scene": _slot(body.scene),
        "framing": _slot(body.framing),
        "lighting": _slot(body.lighting),
        "mood": _slot(body.mood),
        "wardrobe": _slot(body.wardrobe),
        "avoid": _slot(body.avoid),
        "kind": "selfie",
        "status": "started",
        "_deliver": "vault",
    }
    lab.start(contract)
    return {"id": contract["id"], "kind": "selfie", "status": "started"}


@router.post("/api/picture")
async def owner_picture(request: Request, body: PictureRequest) -> dict:
    """Start a picture of something that is not her. Gallery only."""
    subject = body.subject.strip()
    if not subject:
        raise HTTPException(422, "subject must say what the picture is of")
    lab = _lab(request)
    contract = {
        "id": uuid.uuid4().hex[:8],
        "kind": "picture",
        "subject": subject,
        "avoid": _slot(body.avoid),
        "status": "started",
        "_deliver": "vault",
    }
    lab.start(contract)
    return {"id": contract["id"], "kind": "picture", "status": "started"}
