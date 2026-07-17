"""Latency budget instrumentation (SPEC §4.2, → ch. 21, ch. 24).

The bar is ≤ ~1.2 s from end-of-speech to first audio out. The pipeline has
five stages and any one will happily eat the whole second, so the loop is
measured, not assumed — "per-stage numbers lie when queues hide between the
stages" (ch. 32), so this records both per-stage marks *and* the one number
that matters: end-of-speech → first sample out of the speaker.

Usage from the TurnController:

    budget = TurnTrace()
    budget.mark("endpoint")            # VAD said the user stopped
    ... run STT ...
    budget.mark("stt_final")
    ... assemble + first token ...
    budget.mark("first_token")
    ... first TTS chunk ready ...
    budget.mark("first_audio")         # THE number: endpoint → here
    budget.finish(barged_in=False)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# §4.2 per-stage targets, milliseconds — the guardrails, not billing.
TARGETS_MS = {
    "endpoint->stt_final": 300,     # STT final segment
    "stt_final->first_token": 450,  # assembly + LLM time-to-first-token (warm KV)
    "first_token->first_audio": 300,  # TTS time-to-first-audio
    "endpoint->first_audio": 1200,  # THE end-to-end bar
}


@dataclass
class TurnTrace:
    """Timestamps for one turn. `mark` stamps a named instant; spans are derived."""
    t0: float = field(default_factory=time.perf_counter)
    marks: dict[str, float] = field(default_factory=dict)
    barged_in: bool = False
    masked: bool = False            # did a filler cover the gap? (§5)

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.perf_counter())

    def span_ms(self, a: str, b: str) -> float | None:
        if a in self.marks and b in self.marks:
            return (self.marks[b] - self.marks[a]) * 1000.0
        return None

    def first_audio_ms(self) -> float | None:
        """The one number that matters (SPEC §4.2)."""
        return self.span_ms("endpoint", "first_audio")

    def report(self) -> dict:
        spans = {k: self.span_ms(*k.split("->")) for k in TARGETS_MS}
        over = {
            k: round(spans[k], 1)
            for k, tgt in TARGETS_MS.items()
            if spans.get(k) is not None and spans[k] > tgt
        }
        return {
            "first_audio_ms": (round(self.first_audio_ms(), 1)
                               if self.first_audio_ms() is not None else None),
            "spans_ms": {k: (round(v, 1) if v is not None else None)
                         for k, v in spans.items()},
            "over_budget": over,          # empty dict == within budget
            "barged_in": self.barged_in,
            "masked": self.masked,
        }

    def finish(self, *, barged_in: bool = False, trace_dir: Path | None = None) -> dict:
        self.barged_in = barged_in
        rep = self.report()
        if trace_dir is not None:
            trace_dir.mkdir(parents=True, exist_ok=True)
            with (trace_dir / "latency.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), **rep}) + "\n")
        return rep
