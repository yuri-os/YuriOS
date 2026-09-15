"""The origin scope — nesting, and surviving a generator nobody finished.

`correlate.scope` is a ContextVar, and a ContextVar token may only be reset in
the Context that set it. Most scopes open and close in one frame, so that rule
is invisible. The one that does not is a scope held across a `yield`: an async
generator has no Context of its own, so it enters the scope in whichever task
first pumps it and is finalized wherever the loop reaches it — a different one,
whenever the consumer walks away. That is a hang-up mid-greeting, not a bug in
her, and it must not surface as an unretrieved task exception (SPEC §24.2).
"""
from __future__ import annotations

import asyncio

from yurios.kernel import correlate


def test_a_nested_scope_refines_without_restarting():
    with correlate.scope(kind=correlate.TICK, tick_id="t-1") as tick:
        with correlate.scope(kind=correlate.DREAM) as dream:
            assert dream.corr_id == tick.corr_id
            assert correlate.stamp()["origin"] == correlate.DREAM
        assert correlate.stamp()["origin"] == correlate.TICK
    assert correlate.stamp()["corr_id"] is None


async def test_an_abandoned_stream_closes_without_raising():
    """`desktop/brain.py`'s `stream_greeting` shape: the scope spans the yields."""
    said: list[str] = []

    async def greeting():
        with correlate.scope(kind=correlate.GREETING, session_id="s-1"):
            for token in ("hel", "lo"):
                said.append(token)
                yield token

    async def start():
        agen = greeting()
        await agen.__anext__()          # the scope is entered in *this* Context
        return agen

    # The task's Context dies with it, so the close below runs in another one.
    agen = await asyncio.create_task(start())
    await agen.aclose()

    assert said == ["hel"]               # abandoned after the first token
    assert correlate.stamp()["corr_id"] is None   # and the scope still ended
