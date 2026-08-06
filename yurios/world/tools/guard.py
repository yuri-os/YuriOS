"""Guardrails (SPEC §7.3) — the game-NPC lesson, applied (→ ch. 17; ch. 02 §1).

She can be *asked* anything; this object decides what her hands actually do.
Policy, not intelligence: an allowlist (exactly the discovered tools), per-tool
token-bucket rate limits on the injected clock, result truncation, and one JSONL
audit line for every call — allowed or denied — so "what did she do while I was
away" is a file you can read, not a vibe.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from yurios.mind.util import jsonl_append, new_id

from .. import correlate
from ..clock import Clock

log = logging.getLogger("world.guard")

RESULT_MAX_CHARS = 600      # a tool result is a fact for her to speak to, not a payload


def _fingerprint(tool: str, args: dict | None) -> str:
    """What counts as "the same call". Exact, deliberately: `cozy` and `bare`
    are two different photos she may well have meant, so only a byte-identical
    repeat is a repeat — a near-miss is her changing her mind, and policy does
    not get to guess about that."""
    return tool + "\0" + json.dumps(args or {}, sort_keys=True, default=str)


class Turn:
    """One reply's worth of dedupe memory, handed out by `Guard.turn()`.

    The scope lives with the caller rather than on the Guard because turns
    overlap: sessions stream concurrently against the one shared Guard, and a
    duplicate is only a duplicate *within the reply that made it*. A held Turn
    is therefore the only state that distinguishes "she asked twice" from "she
    asked again later", and it dies with the pass loop that owns it.
    """

    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen: set[str] = set()


class Guard:
    def __init__(self, *, rates_per_min: dict[str, int], log_dir: Path,
                 clock: Clock, max_bytes: int | None = None):
        """`rates_per_min` doubles as the allowlist: a tool absent from it does
        not exist, whatever the model claims (SPEC §7.3)."""
        self.clock = clock
        self.log_path = Path(log_dir) / "calls.jsonl"
        self.max_bytes = max_bytes
        self._rates = dict(rates_per_min)
        now = clock.now()
        self._buckets = {t: {"tokens": float(r), "at": now}
                         for t, r in self._rates.items()}

    # ---- policy ----

    def turn(self) -> Turn:
        """A fresh dedupe scope — one per reply (world/brain.py's pass loop)."""
        return Turn()

    def check(self, tool: str, args: dict | None = None, *,
              turn: Turn | None = None) -> tuple[bool, str]:
        """Allowlist + one-per-turn dedupe + rate limit. Returns
        (allowed, reason-if-denied). Without a `turn` the dedupe is simply not
        in play — the other two rules stand on their own."""
        if tool not in self._rates:
            return False, "not a tool she has"
        # Same hand, same arguments, same reply: the model re-emitting a marker
        # it already spent, not a second thing she meant. Start-don't-await
        # results (§7.6) invite exactly this — `status: started` carries no
        # photo, so the continuation reads as though nothing happened and she
        # reaches for the camera again. The rate limit can't catch it (a burst
        # of two is what the bucket is *for*), and the per-turn cap only bounds
        # how many duplicates land. Checked before the bucket, so a repeat costs
        # her nothing but the answer.
        fp = _fingerprint(tool, args) if turn is not None else ""
        if turn is not None and fp in turn.seen:
            return False, "already done this turn"
        b = self._buckets[tool]
        rate = self._rates[tool]
        now = self.clock.now()
        b["tokens"] = min(float(rate), b["tokens"] + (now - b["at"]) / 60.0 * rate)
        b["at"] = now
        if b["tokens"] < 1.0:
            return False, "rate limit"
        b["tokens"] -= 1.0
        if turn is not None:          # only a call she actually got to make
            turn.seen.add(fp)
        return True, ""

    @staticmethod
    def truncate(text: str) -> str:
        if len(text) <= RESULT_MAX_CHARS:
            return text
        return text[: RESULT_MAX_CHARS - 1] + "…"

    # ---- the audit line (SPEC §7.3) ----

    def audit(self, tool: str, args: dict, verdict: str, duration_ms: float,
              result: str, *, origin: "correlate.Origin | None" = None) -> None:
        """One line per call, allowed or denied — plus who asked.

        The correlation fields come from whatever unit of work is in scope
        (world/correlate.py) and are all nullable: a call the mind made for
        itself, with no turn in view, still writes a complete line marked
        `origin: "host"`. That is the ordinary case, not an error case."""
        line = {"ts": self.clock.now(), "call_id": new_id("call"),
                "tool": tool, "args": args,
                "verdict": verdict, "duration_ms": round(duration_ms, 1),
                "result": result[:200],
                **(origin.stamp() if origin is not None else correlate.stamp())}
        try:
            jsonl_append(self.log_path, line, max_bytes=self.max_bytes)
        except Exception:
            # An audit line is an observation. It must never be the reason the
            # turn it is observing fails.
            log.exception("audit write failed")
