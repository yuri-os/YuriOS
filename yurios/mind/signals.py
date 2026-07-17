"""The signal bus (SPEC §16) — the inbound inbox Build #4 deliberately left out.

Build #4 shipped only the *outbound* half of the split (the EventHub); the
inbound mirror had no consumer until the tick loop existed. It exists now:
everything that happens *to* her — a user turn, a timer landing, a finished
selfie, a page attaching, a scheduled wake — is one typed, timestamped `Signal`
appended here, and SENSE drains the queue by offset. Producers never call into
the mind; they post a fact and the loop decides what it means.

Every arrival is also appended to `signals.jsonl` (the arrival record): "what
woke her at 3am" is a file you read, the same honesty rule as the tool audit.
The in-memory queue is the working copy; the log is not replayed on restart —
a restart starts from silence plus the suspend-gap catch-up (SPEC §15.4).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from yurios.world.clock import Clock

from .util import iso_of, jsonl_append, new_id

# the open enum (SPEC §16.2). Unknown types are legal — they appraise low.
SIGNAL_TYPES = (
    "user_message",     # a committed user turn (text; the voice route tees it)
    "turn_committed",   # a full exchange committed (payload: text, reply)
    "user_present",     # a frontend attached / the enter gesture landed
    "user_absent",      # the last frontend detached
    "timer",            # a TimerBoard countdown landed (payload: label)
    "task_completion",  # dispatched work finished (a selfie render, …)
    "selfedit_decision",  # the user ruled on a queued self-edit (id, approve)
    "wakeup",           # a wake the loop scheduled for itself (a due goal)
    "fs_event",         # something changed on a watched surface (knowledge drop)
    "suspend_gap",      # synthesized by SENSE: the machine slept (hours)
)


@dataclass
class Signal:
    id: str
    type: str
    ts: str                                  # ISO, local wall time
    payload: dict = field(default_factory=dict)
    source: str = "host"


class SignalBus:
    """Append-only inbox, drained by offset. Thread-safe on the publish side
    the same way the EventHub is: `post()` may be called from the event loop
    or a worker thread; the wake event hop is loop-safe."""

    def __init__(self, clock: Clock, log_dir: Path | None = None):
        self.clock = clock
        self.log_path = (log_dir / "signals.jsonl") if log_dir else None
        self._signals: list[Signal] = []
        self.wake = asyncio.Event()          # the loop sleeps on this (SPEC §15.1)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def post(self, type_: str, payload: dict | None = None,
             source: str = "host") -> Signal:
        sig = Signal(id=new_id("sig"), type=type_,
                     ts=iso_of(self.clock.now()),
                     payload=payload or {}, source=source)
        self._signals.append(sig)
        if self.log_path is not None:
            jsonl_append(self.log_path, {"id": sig.id, "type": sig.type,
                                         "ts": sig.ts, "payload": sig.payload,
                                         "source": sig.source})
        self._set_wake()
        return sig

    def _set_wake(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self.wake.set()                  # pre-loop (tests, early boot)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.wake.set()
        else:                                # a worker thread (TTS, a script)
            loop.call_soon_threadsafe(self.wake.set)

    def bind_loop(self) -> None:
        """Called by the tick loop once it runs, so off-loop posts can wake it."""
        self._loop = asyncio.get_running_loop()

    def next(self, offset: int, limit: int = 64) -> tuple[list[Signal], int]:
        batch = self._signals[offset:offset + limit]
        return batch, offset + len(batch)

    def __len__(self) -> int:
        return len(self._signals)
