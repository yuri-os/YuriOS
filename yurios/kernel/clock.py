"""Injected time (SPEC §15.1) — one rule, and the whole test story hangs on it.

Everything timed in this build — the tick loop, the activity states, the timer
board, the guard's rate buckets, DREAM's window — takes a `Clock` and never
reads the wall clock or bare-sleeps. A `VirtualClock` then runs *days* of an
always-on mind in milliseconds (SPEC §27), deterministically, on any machine —
which is the only way the interrupt threshold can ship tuned instead of vibed.
"""
from __future__ import annotations

import asyncio
import time


class Clock:
    """Real time. Every read and every cadence wait goes through this object."""

    def now(self) -> float:
        """Seconds, monotonic-enough for scheduling (wall epoch)."""
        return time.time()

    async def sleep(self, seconds: float, *, wake: asyncio.Event | None = None) -> None:
        """Sleep up to `seconds`, waking early if `wake` is set (a turn started,
        a timer landed — the idle machine reacts now, not at the next tick)."""
        if wake is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class VirtualClock(Clock):
    """Deterministic clock for sim-time tests: hours run in milliseconds."""

    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    async def sleep(self, seconds: float, *, wake: asyncio.Event | None = None) -> None:
        # Simulated waits advance virtual time and yield once so other tasks run.
        if wake is not None and wake.is_set():
            return
        self.advance(seconds)
        await asyncio.sleep(0)
