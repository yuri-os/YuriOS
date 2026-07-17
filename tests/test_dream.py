"""DREAM consolidation (SPEC §21) — oldest-first, resumable, budget-capped,
never today's live journal. Build #1's consolidate() stub, finally implemented.
"""
from __future__ import annotations

import pytest

from yurios.app.memory.store import FileMemoryStore
from yurios.mind.dream import DreamConsolidator
from yurios.mind.vaultio import MindVault
from yurios.world.clock import VirtualClock

from .conftest import SIM_START, FakeEmbedder, FakeUtility


def _day_file(vault, day, lines):
    p = vault / "memory" / "episodic" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# Journal — {day}\n\n" + "".join(
        f"### 10:0{i}  {line}\n" for i, line in enumerate(lines)))
    return p


@pytest.fixture
def rig(tmp_path):
    clock = VirtualClock(start=SIM_START.timestamp())     # Monday 2026-07-06
    vault = MindVault(tmp_path / "vault")
    store = FileMemoryStore(tmp_path / "vault", FakeEmbedder(), embed_dim=FakeEmbedder.dim)
    dream = DreamConsolidator(vault, store, clock, utility=FakeUtility().complete)
    return dream, clock, tmp_path / "vault", store


async def test_backlog_is_finished_days_only(rig):
    dream, clock, vault, _ = rig
    _day_file(vault, "2026-07-04", ["user: hello  ⇄  yuri: hi"])
    _day_file(vault, "2026-07-05", ["user: remember I sail sundays  ⇄  yuri: noted"])
    _day_file(vault, "2026-07-06", ["user: today's live log  ⇄  yuri: mm"])
    assert dream.backlog() == ["2026-07-04", "2026-07-05"]   # never today


async def test_consolidation_extracts_dedupes_and_indexes(rig):
    dream, clock, vault, store = rig
    _day_file(vault, "2026-07-05",
              ["user: remember I sail sundays  ⇄  yuri: noted"])
    report = await dream.consolidate()
    assert report.days_processed == ["2026-07-05"]
    assert report.facts_added == 1
    facts = (vault / "memory" / "semantic" / "facts.md").read_text()
    assert "sail sundays" in facts and "(2026-07-05)" in facts
    # the distilled fact outranks the raw exchange at recall (salience 2.0)
    row = next(c for c in store.index.all() if "sail sundays" in c.text)
    assert row.salience == 2.0 and row.kind == "fact"
    # a second night adds nothing new — dedupe against what's already kept
    _day_file(vault, "2026-07-05b"[:10], [])                 # no new day
    (vault / "state" / "dream_progress.json").unlink()       # force re-run
    report2 = await dream.consolidate()
    assert report2.facts_added == 0


async def test_budget_leaves_a_backlog_not_an_overrun(rig):
    dream, clock, vault, _ = rig
    for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
        _day_file(vault, day, [f"user: remember day {day}  ⇄  yuri: ok"] * 40)
    report = await dream.consolidate(token_budget=800)
    assert report.exhausted_budget
    assert report.days_processed == ["2026-07-01"]           # oldest first
    assert dream.backlog() == ["2026-07-02", "2026-07-03"]   # resumable
    report2 = await dream.consolidate(token_budget=10_000)
    assert report2.days_processed == ["2026-07-02", "2026-07-03"]
    assert dream.backlog() == []


async def test_offline_heuristic_still_runs(tmp_path):
    """No utility model at all: the pass degrades, it never dies."""
    clock = VirtualClock(start=SIM_START.timestamp())
    vault = MindVault(tmp_path / "vault")
    store = FileMemoryStore(tmp_path / "vault", FakeEmbedder(), embed_dim=FakeEmbedder.dim)
    dream = DreamConsolidator(vault, store, clock, utility=None)
    _day_file(tmp_path / "vault", "2026-07-05",
              ["user: remember the 14th is our anniversary  ⇄  yuri: always"])
    report = await dream.consolidate()
    assert report.facts_added == 1
    assert "anniversary" in (tmp_path / "vault" / "memory" / "semantic"
                             / "facts.md").read_text()
