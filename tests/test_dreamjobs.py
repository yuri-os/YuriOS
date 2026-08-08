"""The DREAM pipeline (SPEC §21.2) — several jobs, one night, one budget.

`test_dream.py` still pins consolidation itself; this file is about the
pipeline around it: priority order, the shared budget, per-job resumable
progress, and the two rules that are easy to get wrong and expensive to get
wrong — that a job which decided to do nothing still finishes with its day, and
that a job which raises doesn't take the night with it.
"""
from __future__ import annotations

import pytest

from yurios.app.memory.store import FileMemoryStore
from yurios.mind.dream import DreamConsolidator
from yurios.mind.dreamjobs import DreamJob, DreamRunner, JobReport
from yurios.mind.vaultio import MindVault
from yurios.mind.workspace import SkillStore, Workspace
from yurios.world.clock import VirtualClock

from .conftest import SIM_START, FakeEmbedder, FakeUtility


def _day_file(vault, day, lines):
    p = vault / "memory" / "episodic" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# Journal — {day}\n\n" + "".join(
        f"### 10:0{i}  {line}\n" for i, line in enumerate(lines)))
    return p


@pytest.fixture
def rig(tmp_path, cfg):
    """A runner over a real vault, with the camera off (so `selfie` is not in
    the night) and a fake utility model."""
    clock = VirtualClock(start=SIM_START.timestamp())   # Monday 2026-07-06
    vault = MindVault(tmp_path / "vault")
    store = FileMemoryStore(tmp_path / "vault", FakeEmbedder(),
                            embed_dim=FakeEmbedder.dim)
    cfg = cfg.model_copy(update={"selfie_backend": "off"})
    runner = DreamRunner(
        vault, store, clock, cfg,
        consolidator=DreamConsolidator(vault, store, clock,
                                       utility=FakeUtility().complete),
        workspace=Workspace(tmp_path / "vault" / "workspace"),
        skills=SkillStore(tmp_path / "vault" / "skills"),
        utility=FakeUtility().complete)
    return runner, clock, tmp_path / "vault"


# ------------------------------------------------------------------ the roster

def test_jobs_run_highest_priority_first(rig):
    """Consolidation before everything: the jobs below it read `facts.md`, so
    on any given night the diary should see what consolidation just learned."""
    runner, _clock, _vault = rig
    assert [j.name for j in runner.jobs][0] == "consolidate"
    priorities = [j.priority for j in runner.jobs]
    assert priorities == sorted(priorities, reverse=True)


def test_the_camera_being_off_takes_the_selfie_job_out_of_the_night(rig):
    runner, _clock, _vault = rig
    assert "selfie" not in [j.name for j in runner.enabled_jobs()]
    # …but it is still *listed*, disabled, so the page can say why it isn't running
    listed = {j["name"]: j for j in runner.status()}
    assert listed["selfie"]["enabled"] is False


# ----------------------------------------------------------------- the backlog

async def test_a_night_drains_the_backlog_and_leaves_the_ladder_free(rig):
    """The bug this test exists for: a job that legitimately writes nothing
    must still mark its day done. If it doesn't, the backlog never empties,
    DREAM → DORMANT never fires, and she spends every night from then on
    re-deciding not to write the same note."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: remember I sail sundays  ⇄  yuri: noted"])
    _day_file(vault, "2026-07-05", ["user: morning  ⇄  yuri: mm"])
    assert runner.backlog() == ["2026-07-04", "2026-07-05"]
    await runner.run(token_budget=40000)
    assert runner.backlog() == []


async def test_today_is_never_dreamt_about(rig):
    """`dream.py`'s rule, shared by every job through `finished_days()`: the
    file still being written is not a day yet."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-06", ["user: this is today  ⇄  yuri: mm"])
    assert runner.backlog() == []


async def test_progress_is_per_job_and_resumable(rig):
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: remember the boat  ⇄  yuri: noted"])
    await runner.run(only="diary", token_budget=40000)
    ledger = {j["name"]: j for j in runner.status()}
    assert ledger["diary"]["backlog"] == []
    # consolidation never ran, so it still owes that day — one job's night off
    # is not another's
    assert ledger["consolidate"]["backlog"] == ["2026-07-04"]


async def test_a_once_a_night_job_does_not_walk_backwards_through_history(rig):
    """A `per_day=False` job asks "has the most recent finished day been seen",
    not "which days are unseen" — the second walks the archive one night at a
    time and never empties."""
    runner, _clock, vault = rig
    for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"):
        _day_file(vault, day, ["user: hello  ⇄  yuri: hi"])
    strategy = runner.get("strategy")
    ctx = runner._context()
    assert strategy.backlog(ctx, runner.ledger) == ["2026-07-04"]   # the newest only
    runner.ledger.mark("strategy", "2026-07-04")
    assert strategy.backlog(ctx, runner.ledger) == []


# ------------------------------------------------------------------ the budget

async def test_the_budget_is_shared_and_leaves_a_backlog_not_an_overrun(rig):
    runner, _clock, vault = rig
    for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
        _day_file(vault, day, ["user: remember day " + day + "  ⇄  yuri: ok"] * 40)
    report = await runner.run(token_budget=800)
    assert report.exhausted_budget
    assert runner.backlog()                       # resumable, not lost
    await runner.run(token_budget=100_000)
    assert runner.backlog() == []


async def test_a_talkative_day_is_charged_for_the_prompt_not_for_the_file(rig):
    """A day's journal reaches the model capped at JOURNAL_CHARS, so a 180KB
    day is a ~1.7k-token call. Charging it 45k — which the first version did,
    off the file's size — spent the whole night's allowance on one diary entry
    and left every other job to a night that might not come."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: and another thing  ⇄  yuri: mm"] * 5000)
    assert (vault / "memory" / "episodic" / "2026-07-04.md").stat().st_size > 180_000
    ctx = runner._context()
    assert runner.get("diary").cost(ctx, "2026-07-04") < 3000

    # …and so the whole night fits in one tick, which is the point of the number
    report = await runner.run(token_budget=40000)
    assert not report.exhausted_budget
    assert {j.name for j in report.jobs} == {"consolidate", "diary", "strategy"}


async def test_an_expensive_job_hitting_the_ceiling_leaves_the_cheap_ones_alone(rig):
    """`exhausted_budget` used to break the night, not the job. One costly
    diary day would then defer a strategy review costing a few hundred tokens,
    even though it fit."""
    runner, _clock, vault = rig
    for day in ("2026-07-03", "2026-07-04"):
        _day_file(vault, day, ["user: remember the boat  ⇄  yuri: noted"])

    class Ruinous(DreamJob):
        """Cheap on its first day and priced past the ceiling on its second,
        so it runs, then stops mid-backlog — the shape a real diary hits."""
        name, title, priority = "ruinous", "Ruinous", 0.9

        def cost(self, ctx, day) -> int:
            return 100 if day == "2026-07-03" else 10_000_000

        async def work(self, ctx, day):
            return JobReport(name=self.name, days=[day], changed=True)

    class Pennies(DreamJob):
        """A few hundred tokens, and last in the queue."""
        name, title, priority = "pennies", "Pennies", 0.01

        def cost(self, ctx, day) -> int:
            return 100

        async def work(self, ctx, day):
            return JobReport(name=self.name, days=[day], changed=True)

    runner.jobs.insert(0, Ruinous())
    runner.jobs.append(Pennies())
    report = await runner.run(token_budget=40000)
    assert report.exhausted_budget                    # ruinous could not finish
    assert "pennies" in {j.name for j in report.jobs}


async def test_the_first_item_of_the_night_always_runs_however_big(rig):
    """Otherwise the backlog wedges on one outsized journal forever, and every
    DREAM tick from then on re-breaks against the same file."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: remember this  ⇄  yuri: ok"] * 500)
    report = await runner.run(token_budget=1)
    assert report.jobs and report.jobs[0].days == ["2026-07-04"]


# ------------------------------------------------------------------- isolation

async def test_a_failing_job_does_not_take_the_night_with_it(rig):
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: remember the boat  ⇄  yuri: noted"])

    class Exploding(DreamJob):
        name, title, priority = "boom", "Boom", 0.9

        async def work(self, ctx, day):
            raise RuntimeError("the prompt was nonsense")

    runner.jobs.insert(0, Exploding())
    report = await runner.run(token_budget=40000)
    failed = [j for j in report.jobs if j.failed]
    assert failed and "nonsense" in failed[0].failed
    assert any(j.name == "diary" and j.changed for j in report.jobs)


async def test_a_failed_job_keeps_its_day_and_retries(rig):
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: hello  ⇄  yuri: hi"])
    calls = []

    class Flaky(DreamJob):
        name, title, priority = "flaky", "Flaky", 0.9

        async def work(self, ctx, day):
            calls.append(day)
            if len(calls) == 1:
                raise RuntimeError("not tonight")
            return JobReport(name=self.name, days=[day], changed=True,
                             result="worked on the retry")

    runner.jobs.insert(0, Flaky())
    await runner.run(token_budget=40000)
    await runner.run(token_budget=40000)
    assert calls == ["2026-07-04", "2026-07-04"]   # retried, not marked done


# ---------------------------------------------------------------- the dry run

async def test_a_dry_run_thinks_but_writes_nothing(rig):
    """What makes the debug page's button safe to press on a live vault."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: remember the boat  ⇄  yuri: noted"])
    report = await runner.run(only="diary", token_budget=40000, dry_run=True)
    assert report.dry_run
    assert report.writes == ["diary/2026-07-04.md"]        # what it *would* write
    assert not (vault / "workspace" / "diary").exists()    # and did not
    assert runner.backlog() == ["2026-07-04"]              # ledger untouched
    assert report.exchanges                                # but the model did run


async def test_the_report_carries_the_prompt_verbatim(rig):
    """The reason the button exists: a dream job is a prompt whose output you
    otherwise cannot see until tomorrow morning."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: the rain kept up  ⇄  yuri: mm"])
    report = await runner.run(only="diary", day="2026-07-04", dry_run=True)
    exchange = report.exchanges[0]
    assert exchange.job == "diary"
    assert "diary entry" in exchange.system
    assert "the rain kept up" in exchange.user
    assert "rain kept up all afternoon" in exchange.completion


async def test_pinning_a_day_overrides_the_backlog(rig):
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: one  ⇄  yuri: mm"])
    _day_file(vault, "2026-07-05", ["user: two  ⇄  yuri: mm"])
    report = await runner.run(only="diary", day="2026-07-05", token_budget=40000)
    assert [j.days for j in report.jobs] == [["2026-07-05"]]


async def test_an_unknown_job_is_an_error_not_a_silent_no_op(rig):
    runner, _clock, _vault = rig
    with pytest.raises(KeyError):
        await runner.run(only="nonesuch")


# ----------------------------------------------------------------- the outputs

async def test_the_diary_lands_on_her_desk_not_in_memory(rig):
    """Jobs write to `workspace/`. A nightly job that could append to semantic
    memory would be a second, unaudited consolidator."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: the rain kept up  ⇄  yuri: mm"])
    await runner.run(only="diary", token_budget=40000)
    entry = (vault / "workspace" / "diary" / "2026-07-04.md").read_text()
    assert "rain kept up all afternoon" in entry
    facts = vault / "memory" / "semantic" / "facts.md"
    assert not facts.exists() or "rain kept up all afternoon" not in facts.read_text()


async def test_with_no_utility_model_the_night_runs_and_writes_nothing(tmp_path, cfg):
    """The offline rule `dream.py` set: the pass degrades, it never dies."""
    clock = VirtualClock(start=SIM_START.timestamp())
    vault = MindVault(tmp_path / "vault")
    store = FileMemoryStore(tmp_path / "vault", FakeEmbedder(),
                            embed_dim=FakeEmbedder.dim)
    runner = DreamRunner(
        vault, store, clock, cfg.model_copy(update={"selfie_backend": "off"}),
        consolidator=DreamConsolidator(vault, store, clock, utility=None),
        workspace=Workspace(tmp_path / "vault" / "workspace"), utility=None)
    _day_file(tmp_path / "vault", "2026-07-04", ["user: hello  ⇄  yuri: hi"])
    report = await runner.run(token_budget=40000)
    assert not any(j.failed for j in report.jobs)
    assert runner.backlog() == []
