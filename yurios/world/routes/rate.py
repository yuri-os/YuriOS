"""POST /api/rate — 👍/👎 on one of her replies (SPEC §37).

The page names the line it drew, by transcript id; the runtime resolves that to
the corpus record behind it and appends the rating to `corpus/ratings.jsonl`.
Published on the bus as a `rating` event for the reason a gallery score is: two
open rooms are one conversation, and the second should not keep offering a
thumb the first one already pressed.

Nothing here reaches her. A rating is about a reply, for the corpus export
(`scripts/export_corpus.py`) — not feedback she hears, remembers or commits.
"""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt, field_validator

from yurios.app.corpus import THUMBS, UnratableLine

router = APIRouter()


class Rating(BaseModel):
    # the transcript id the page holds — the shape `/api/history` pages by
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    #: up, down, or `0` to take it back. Strict, so a JSON `true` is a 422
    #: rather than a 👍.
    thumbs: StrictInt

    @field_validator("thumbs")
    @classmethod
    def is_a_thumb(cls, value: int) -> int:
        if value not in THUMBS:
            raise ValueError("thumbs is 1, -1, or 0 to take it back")
        return value


@router.post("/api/rate")
async def rate(body: Rating, request: Request) -> dict:
    rt = request.app.state.rt
    try:
        # The log is read whole to find the line: off the loop, which every
        # other character on this node is also talking through.
        rating = await asyncio.to_thread(rt.rate, body.id, body.thumbs)
    except KeyError:
        raise HTTPException(404, "no such line in this conversation") from None
    except UnratableLine:
        raise HTTPException(
            409, "only a reply she made in a turn can be rated") from None
    return {"rating": rating}
