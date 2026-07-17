"""POST /api/rate — the 👍/👎 sidecar (SPEC §8.1, §10).

Ratings arrive after a reply, so they are never patched into `turns.jsonl`;
they land in `ratings.jsonl` keyed by turn id and merge at export. This is
the KTO/DPO asset (→ ch. 20).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class Rating(BaseModel):
    turn_id: str
    thumbs: Literal[1, -1]


@router.post("/api/rate")
async def rate(rating: Rating, request: Request):
    request.app.state.mvw.corpus.log_rating(rating.turn_id, rating.thumbs)
    return {"ok": True}
