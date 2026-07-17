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

from ..clock import Clock

log = logging.getLogger("world.guard")

RESULT_MAX_CHARS = 600      # a tool result is a fact for her to speak to, not a payload


class Guard:
    def __init__(self, *, rates_per_min: dict[str, int], log_dir: Path,
                 clock: Clock):
        """`rates_per_min` doubles as the allowlist: a tool absent from it does
        not exist, whatever the model claims (SPEC §7.3)."""
        self.clock = clock
        self.log_path = Path(log_dir) / "calls.jsonl"
        self._rates = dict(rates_per_min)
        now = clock.now()
        self._buckets = {t: {"tokens": float(r), "at": now}
                         for t, r in self._rates.items()}

    # ---- policy ----

    def check(self, tool: str) -> tuple[bool, str]:
        """Allowlist + rate limit. Returns (allowed, reason-if-denied)."""
        if tool not in self._rates:
            return False, "not a tool she has"
        b = self._buckets[tool]
        rate = self._rates[tool]
        now = self.clock.now()
        b["tokens"] = min(float(rate), b["tokens"] + (now - b["at"]) / 60.0 * rate)
        b["at"] = now
        if b["tokens"] < 1.0:
            return False, "rate limit"
        b["tokens"] -= 1.0
        return True, ""

    @staticmethod
    def truncate(text: str) -> str:
        if len(text) <= RESULT_MAX_CHARS:
            return text
        return text[: RESULT_MAX_CHARS - 1] + "…"

    # ---- the audit line (SPEC §7.3) ----

    def audit(self, tool: str, args: dict, verdict: str, duration_ms: float,
              result: str) -> None:
        line = {"ts": self.clock.now(), "tool": tool, "args": args,
                "verdict": verdict, "duration_ms": round(duration_ms, 1),
                "result": result[:200]}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.exception("audit write failed")
