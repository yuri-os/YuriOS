"""TickTrace (SPEC §24.2) — the developer-facing why-record.

One structured JSONL record per tick: what SENSE collected, the salience
APPRAISE assigned, what DECIDE committed to (and the runners-up), what ACT did,
and the interrupt decision with its factors. Because the loop is
one-intention-per-tick, the trace reads linearly — a diary with its reasoning
shown — and it is what the scenario tests assert over: "silent for the right
forty ticks, one message, at the right one" is a query over this file.
"""
from __future__ import annotations

from pathlib import Path

from yurios.world.clock import Clock

from .util import iso_of, jsonl_append, jsonl_tail, new_id


class TickTrace:
    def __init__(self, trace_dir: Path, clock: Clock):
        self.path = trace_dir / "ticks.jsonl"
        self.clock = clock

    def record(self, *, tick_id: str | None = None, activity_state: str,
               sensed: list[dict], appraised: list[dict], decided: dict,
               acted: dict, interrupt: dict | None = None) -> str:
        tick_id = tick_id or new_id("t")
        jsonl_append(self.path, {
            "tick_id": tick_id, "ts": iso_of(self.clock.now()),
            "activity_state": activity_state, "sensed": sensed,
            "appraised": appraised, "decided": decided, "acted": acted,
            "interrupt": interrupt or {}})
        return tick_id

    def tail(self, n: int = 50) -> list[dict]:
        return jsonl_tail(self.path, n)
