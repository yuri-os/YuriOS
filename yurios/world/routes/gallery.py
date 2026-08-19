"""/api/gallery — her pictures as a page you can walk back through (SPEC §7.6).

The chat column's third panel. The bytes themselves still come from
`/selfies/{name}` (routes/events.py) — this is only the index over them, and
the one write path is a score out of ten.

Reads through `world/gallery.py`, which reads the forge's own ledger: like
`/api/mind`, the panel can never disagree with the files, and it answers on a
character whose loop is stopped — a shelf of photographs is history, and
history does not need her running to be looked at.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt, field_validator

from yurios.world import gallery

router = APIRouter()


class Score(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    #: Strict, so a JSON `true` or `"9"` is a 422 rather than a nine. `None`
    #: clears the score — the way back out of a rating you regret.
    score: StrictInt | None = None

    @field_validator("score")
    @classmethod
    def in_range(cls, value: int | None) -> int | None:
        if value is not None and not gallery.SCORE_MIN <= value <= gallery.SCORE_MAX:
            raise ValueError(f"a score is {gallery.SCORE_MIN}–{gallery.SCORE_MAX}, "
                             f"or null to clear it")
        return value


@router.get("/api/gallery")
async def shelf(request: Request, page: int = 0,
                limit: int = gallery.DEFAULT_LIMIT) -> dict:
    """One newest-first page of everything her camera has made."""
    return gallery.page(request.app.state.rt.cfg.selfie_dir,
                        page=page, limit=limit)


@router.post("/api/gallery/rate")
async def rate(body: Score, request: Request) -> dict:
    """Score one shot out of ten (or `null` to take the score back).

    Published on the bus for the same reason a workspace write is: two open
    rooms are one shelf, and the second one should not have to be reloaded to
    stop showing a stale seven.
    """
    rt = request.app.state.rt
    try:
        row = gallery.rate(rt.cfg.selfie_dir, body.name, body.score)
    except gallery.UnknownShot:
        raise HTTPException(404, "no such picture on the shelf") from None
    rt.hub.publish("gallery", {"action": "rate", **row})
    return {"rating": row}
