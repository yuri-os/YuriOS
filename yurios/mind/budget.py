"""The budget governor (SPEC §17.3) — what makes always-on affordable.

An always-on loop with an unmetered model is a space heater. The governor
holds one number — estimated tokens spent today against a daily cap — and
REGULATE reads its pressure: at 1.0 the IDLE state sheds to DORMANT (goal work
stops; conversation is never blocked — a governor that silences her when you
speak has failed at its one job). The ledger is a file the dashboard renders;
the day rolls at local midnight on the injected clock.

Estimates, not billing: chars/4 in both directions is accurate enough to stop
a runaway loop, which is the actual threat model.
"""
from __future__ import annotations

from pathlib import Path

from yurios.kernel.clock import Clock

from .util import day_of, estimate_tokens, iso_of, read_json, write_json


class BudgetGovernor:
    def __init__(self, state_dir: Path, clock: Clock, *, daily_tokens: int):
        self.path = state_dir / "budget.json"
        self.clock = clock
        self.daily_tokens = max(1, daily_tokens)

    def _state(self) -> dict:
        today = day_of(self.clock.now())
        st = read_json(self.path, None) or {}
        if st.get("date") != today:
            # SPEC §17.3: the ledger rolls at local midnight. Rolling only
            # inside debit() left yesterday's spend on disk through a REST-only
            # day, and the debug page reads the file.
            st = {"date": today, "spent_tokens": 0, "calls": 0}
            write_json(self.path, st)
        return st

    def debit(self, prompt_text: str, reply_text: str = "") -> None:
        st = self._state()
        st["spent_tokens"] += estimate_tokens(prompt_text) + estimate_tokens(reply_text)
        st["calls"] += 1
        write_json(self.path, st)

    def reset(self) -> dict:
        """Zero today's spend by hand (SPEC §17.3).

        The cap is a guard against a runaway loop, and once the runaway is dealt
        with the rest of the day should not stay shed for it. The ledger keeps
        when it was reset and what it read, so a day that went through a reset
        says so; midnight rolls both away with the rest.
        """
        st = self._state()
        st.update(reset_at=iso_of(self.clock.now()), reset_from=st["spent_tokens"],
                  spent_tokens=0, calls=0)
        write_json(self.path, st)
        return self.snapshot()

    def pressure(self) -> float:
        """0.0 = fresh day, ≥1.0 = the cap is spent (REGULATE sheds IDLE)."""
        return self._state()["spent_tokens"] / self.daily_tokens

    def snapshot(self) -> dict:
        st = self._state()
        return {**st, "daily_tokens": self.daily_tokens,
                "pressure": round(self.pressure(), 3)}
