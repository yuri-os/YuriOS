"""The daily token ledger (SPEC §17.3) — rolls at local midnight on the clock."""
from __future__ import annotations

import json

from yurios.kernel.clock import VirtualClock
from yurios.mind.budget import BudgetGovernor
from yurios.mind.util import day_of

from .conftest import SIM_START


def test_the_budget_file_rolls_at_midnight_without_a_debit(tmp_path):
    """A REST-only day never calls debit(). The debug page reads the file, so
    rolling only inside debit() left yesterday's spend labeled as today's."""
    clock = VirtualClock(start=SIM_START.timestamp())
    gov = BudgetGovernor(tmp_path, clock, daily_tokens=10_000)
    gov.debit("a" * 400)
    spent = json.loads((tmp_path / "budget.json").read_text())
    assert spent["spent_tokens"] > 0
    yesterday = spent["date"]
    clock.advance(86400)
    snap = gov.snapshot()
    on_disk = json.loads((tmp_path / "budget.json").read_text())
    assert snap["spent_tokens"] == 0
    assert on_disk["spent_tokens"] == 0
    assert on_disk["date"] == snap["date"] == day_of(clock.now())
    assert on_disk["date"] != yesterday


def test_a_reset_zeroes_the_day_and_says_it_happened(tmp_path):
    """Once a runaway night is dealt with, the rest of the day should not stay
    shed for it — but the ledger keeps when and from what, until midnight."""
    clock = VirtualClock(start=SIM_START.timestamp())
    gov = BudgetGovernor(tmp_path, clock, daily_tokens=100)
    gov.debit("a" * 4000)
    assert gov.pressure() >= 1.0
    spent = gov.snapshot()["spent_tokens"]
    snap = gov.reset()
    assert snap["spent_tokens"] == 0 and snap["calls"] == 0
    assert snap["pressure"] == 0.0
    assert snap["reset_from"] == spent and snap["reset_at"]
    gov.debit("a" * 40)
    assert gov.snapshot()["spent_tokens"] == 10, "spending carries on from zero"
    clock.advance(86400)
    assert "reset_at" not in gov.snapshot(), "midnight rolls the note away"
