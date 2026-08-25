"""The DREAM pipeline (SPEC §21.2) — several jobs, one night, one budget.

`test_dream.py` still pins consolidation itself; this file is about the
pipeline around it: priority order, the shared budget, per-job resumable
progress, and the two rules that are easy to get wrong and expensive to get
wrong — that a job which decided to do nothing still finishes with its day, and
that a job which raises doesn't take the night with it.
"""
from __future__ import annotations

import pytest
import yaml

from yurios.app.memory.store import FileMemoryStore
from yurios.mind.dream import DreamConsolidator
from yurios.mind.dreamjobs import (JOB_NAME_RE, PROMPT_OVERHEAD_CHARS,
                                   REPORT_REASONING_ALLOWANCE, REPORT_TIMEOUT_S,
                                   ROUND_MAX_TOKENS, DreamJob, DreamRunner,
                                   JobReport, PromptJob, ResearchJob,
                                   validate_job_file)
# The internals are addressed at the module that owns them rather than through
# the package's public face, so a test that pokes at one says which it means.
from yurios.mind.dreamjobs.filedsl import _shorter_effort
from yurios.mind.dreamjobs.research import _already_asked, _lede, _query_key
from yurios.mind.vaultio import MindVault
from yurios.mind.workspace import SkillStore, Workspace
from yurios.kernel import correlate
from yurios.kernel.clock import VirtualClock
from yurios.world.tools.fetch import FakeFetcher
from yurios.world.tools.search import FakeSearch

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


# ------------------------------------------------------------------ the audit

async def test_a_nights_desk_writes_leave_an_audit_line(rig):
    """The Tools page is "every call her hands made". A diary entry is her
    hands writing a file, so a night has to show up there beside the daytime
    `write_note` calls — otherwise the one surface that answers "what touched
    this vault" is blind to the hours she spends unattended."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["you: hey  ⇄  her: [happy] hi"])
    lines = []
    runner.audit = lambda tool, args, verdict, ms, result: lines.append(
        (tool, args, verdict, result))
    await runner.run(only="diary", token_budget=40000)
    assert [t for t, *_ in lines] == ["write_note"]
    tool, args, verdict, result = lines[0]
    assert args["path"] == "diary/2026-07-04.md" and args["bytes"] > 0
    assert verdict == "ok" and "diary/2026-07-04.md" in result


async def test_a_dreamt_selfie_carries_the_corr_id_that_joins_it_to_its_photo(rig, cfg):
    """The Tools page joins a render to the call that asked for it on `corr_id`
    (`debug._generations_by_corr`), and a dreamt render is the case where that
    join is load-bearing: nothing else connects a photo landing at 02:41 to the
    job that described it. Shipped without this, the page showed a `take_selfie`
    row with no picture and a picture nothing pointed at."""
    runner, _clock, vault = rig
    runner.cfg = cfg.model_copy(update={"selfie_backend": "diffusers"})
    _day_file(vault, "2026-07-04", ["you: hey  ⇄  her: [happy] hi"])
    sent, audited = [], []
    runner.selfie = sent.append
    runner.audit = lambda tool, args, verdict, ms, result: audited.append(
        correlate.stamp().get("corr_id"))
    with correlate.scope(kind="dream", tick_id="t-abc"):
        await runner.run(only="selfie", token_budget=40000)
    assert sent, "the camera was never reached"
    assert sent[0]["id"] == "dream-2026-07-04"
    # the photo's key and the call's key have to be the same key
    assert sent[0]["_corr_id"] and sent[0]["_corr_id"] == audited[-1]


async def test_a_dry_run_claims_no_call_it_did_not_make(rig):
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["you: hey  ⇄  her: [happy] hi"])
    lines = []
    runner.audit = lambda *a: lines.append(a)
    await runner.run(only="diary", token_budget=40000, dry_run=True)
    assert lines == []


async def test_a_broken_audit_seam_does_not_cost_her_the_night(rig):
    """An observation must never be the reason the thing it observes fails."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["you: hey  ⇄  her: [happy] hi"])

    def exploding(*a):
        raise RuntimeError("the log is on fire")

    runner.audit = exploding
    report = await runner.run(only="diary", token_budget=40000)
    assert report.jobs[0].changed and not report.jobs[0].failed
    assert (vault / "workspace" / "diary" / "2026-07-04.md").is_file()


# ------------------------------------------------------------------ the voice

def test_the_journal_is_relabelled_before_a_model_sees_it():
    """The live bug that two rounds of prompt wording could not reach.

    A journal line labels the *other* person `you:`, because it is written for
    a human reading her diary. Under a system prompt opening "You are Rikku",
    that word points at two different people at once, and the model resolves it
    against her — it wrote her diary as the client who came to her yoga class.
    Positional relabelling removes the ambiguity rather than arguing with it.
    """
    from yurios.mind.dreamjobs import relabel
    out = relabel(
        "# Journal — 2026-08-07\n"
        "### 00:20  you: hey  ⇄  rikku: [playful] Hey! *Rikku tilted her head*\n"
        "### 01:12  [she] thought about the studio; chose not to interrupt\n")
    assert "### 00:20  THEM: hey  ⇄  ME: [playful] Hey!" in out
    assert "you:" not in out and "rikku:" not in out
    # her own acts are not an exchange and keep their marker
    assert "### 01:12  [she] thought about the studio" in out


def test_relabelling_survives_a_name_that_is_not_the_configured_one():
    """The halves are positional, so whatever the two sides were called — a
    configured user name, a bare `you`, a nickname she picked up — both get
    replaced without the code having to know either."""
    from yurios.mind.dreamjobs import relabel
    out = relabel("### 09:30  Sam: morning  ⇄  Yuri-chan: [happy] morning!\n")
    assert out.startswith("### 09:30  THEM: morning  ⇄  ME: [happy] morning!")


async def test_the_prompts_claim_her_own_stage_directions_for_her(rig):
    """A live regression, twice. A journal is a two-person transcript, and her
    own half is roleplay prose whose stage directions describe her from the
    outside — often by name. A prompt that only says "the half after ⇄ is
    yours" loses that side to the narration: the first live diary was written
    from the other person's chair, and the first live selfie described the view
    from where they were standing rather than a picture of her."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04",
              ["you: hey  ⇄  her: [playful] Hey! *She tilted her head*"])
    seen: list[str] = []
    inner = runner.utility

    async def recording(messages, **kw):
        seen.append(messages[0]["content"])
        return await inner(messages, **kw)

    runner.utility = recording
    await runner.run(only="diary", token_budget=40000)
    system = seen[-1]
    assert "stage directions" in system
    assert "not someone else watching you do it" in system
    assert "never write as the person" in system


# ------------------------------------------------------------------- isolation

async def test_two_characters_never_dream_at_the_same_time(tmp_path, cfg):
    """The window opens for every character at the same hour, and a night's
    calls are not shareable — one utility model, one camera. Two runs started
    together must queue behind `_NIGHT_LOCK`, not interleave: the second
    character's diary prompt must not reach the model while the first
    character's night is still inside one."""
    import asyncio
    active = peak = 0
    fake = FakeUtility()

    async def guarded(messages, **kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)          # a real yield: unlocked runs interleave here
        try:
            return await fake.complete(messages, **kw)
        finally:
            active -= 1

    runners = []
    for who in ("one", "two"):
        clock = VirtualClock(start=SIM_START.timestamp())
        vault = MindVault(tmp_path / who / "vault")
        store = FileMemoryStore(tmp_path / who / "vault", FakeEmbedder(),
                                embed_dim=FakeEmbedder.dim)
        runner = DreamRunner(
            vault, store, clock, cfg.model_copy(update={"selfie_backend": "off"}),
            consolidator=DreamConsolidator(vault, store, clock, utility=guarded),
            workspace=Workspace(tmp_path / who / "vault" / "workspace"),
            utility=guarded)
        _day_file(tmp_path / who / "vault", "2026-07-04",
                  ["user: hello  ⇄  yuri: hi"])
        runners.append(runner)

    await asyncio.gather(*(r.run(only="diary", token_budget=40000)
                           for r in runners))
    assert peak == 1, "two nights reached the model at the same time"


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


async def test_a_dry_run_says_so_in_the_one_line_the_page_shows(rig):
    """The summary is the debug page's toast as well as the commit message, and
    a rehearsal's reads "diary: wrote 1 day" — a sentence about a file that was
    never written. It has to say which kind of run it was."""
    runner, _clock, vault = rig
    _day_file(vault, "2026-07-04", ["user: remember the boat  \u21c4  yuri: noted"])
    dry = await runner.run(only="diary", day="2026-07-04", dry_run=True)
    assert dry.summary.startswith("DREAM (dry run)")
    wet = await runner.run(only="diary", day="2026-07-04")
    assert wet.summary.startswith("DREAM \u2014")


async def test_a_night_with_nothing_to_do_says_which_kind_it_was_too(rig):
    runner, _clock, _vault = rig
    assert (await runner.run(only="diary", dry_run=True)).summary \
        == "DREAM (dry run): nothing to do"
    assert (await runner.run(only="diary")).summary == "DREAM: nothing to do"


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


# ----------------------------------------------- jobs a character owns (§21.2)
#
# The night used to be a Python tuple and an `enabled()` that said True to
# everything, so every character's night was the same night: the same four jobs,
# the same order, the same questions. That is a strange shape for the one part of
# the system whose whole subject is what *this* character made of *her* day.

def _job_file(vault, name, front="", body="a prompt"):
    d = vault / "dreams"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\n{front}---\n\n{body}\n",
                                  encoding="utf-8")


def _runner(tmp_path, cfg, vault_dir):
    clock = VirtualClock(start=SIM_START.timestamp())
    vault = MindVault(vault_dir)
    store = FileMemoryStore(vault_dir, FakeEmbedder(), embed_dim=FakeEmbedder.dim)
    return DreamRunner(
        vault, store, clock, cfg.model_copy(update={"selfie_backend": "off"}),
        consolidator=DreamConsolidator(vault, store, clock,
                                       utility=FakeUtility().complete),
        workspace=Workspace(vault_dir / "workspace"),
        skills=SkillStore(vault_dir / "skills"),
        utility=FakeUtility().complete)


def test_a_job_file_can_switch_a_builtin_off(tmp_path, cfg):
    """The one thing the roster could never do. `enabled()` returned True for
    every builtin, so "she should not keep a diary" required editing Python."""
    v = tmp_path / "vault"
    _job_file(v, "diary", front="enabled: false\n")
    runner = _runner(tmp_path, cfg, v)
    assert "diary" not in [j.name for j in runner.enabled_jobs()]
    # …and it is still *listed*, disabled, so the page can say why (the rule the
    # camera-off selfie already follows)
    assert {j["name"]: j for j in runner.status()}["diary"]["enabled"] is False


def test_a_job_file_retunes_a_builtin_without_replacing_it(tmp_path, cfg):
    """A file may change the question and never the bookkeeping. `diary` stays a
    `DiaryJob`, so it still reads the journal through `relabel()` and still marks
    its day — the two things a hand-written prompt cannot be trusted to do."""
    v = tmp_path / "vault"
    _job_file(v, "diary", front="priority: 0.95\nsoul: off\n",
              body="Write one line about the day and nothing else.")
    runner = _runner(tmp_path, cfg, v)
    diary = runner.get("diary")
    assert type(diary).__name__ == "DiaryJob"
    assert diary.priority == 0.95
    assert diary.soul == "off"
    assert diary.system("BUILT IN") == "Write one line about the day and nothing else."
    assert diary.as_dict()["from_file"] is True


async def test_a_new_name_becomes_a_job(tmp_path, cfg):
    """Adding a fifth kind of night used to be a class and a line in
    `BUILTIN_JOBS`. §21.2 promised the ladder, trace, budget, debug page and
    manual trigger would all derive from the roster; this is that promise cashed
    by somebody who does not write Python."""
    v = tmp_path / "vault"
    _job_file(v, "gratitude",
              front="title: Gratitude\ndescription: One thing worth keeping.\n"
                    "priority: 0.2\noutput: gratitude/{day}.md\n",
              body="Name the one thing from this day you're glad happened.")
    _day_file(v, "2026-07-04", ["user: we walked to the water  ⇄  yuri: it was cold"])
    runner = _runner(tmp_path, cfg, v)
    assert "gratitude" in [j.name for j in runner.enabled_jobs()]
    await runner.run(only="gratitude", token_budget=40000)
    written = (v / "workspace" / "gratitude" / "2026-07-04.md")
    assert written.is_file() and written.read_text().strip()


def test_a_mangled_job_file_costs_one_job_and_not_the_night(tmp_path, cfg):
    """§34.3's rule for a broken `SKILL.md`, and it matters more here: these are
    edited by hand at midnight by someone who wanted a different diary, and one
    stray colon must not be why nothing consolidated."""
    v = tmp_path / "vault"
    (v / "dreams").mkdir(parents=True)
    (v / "dreams" / "broken.md").write_text("---\nname: broken\n  x: [unclosed\n---\nb\n",
                                            encoding="utf-8")
    _job_file(v, "gratitude", front="priority: 0.2\n")
    runner = _runner(tmp_path, cfg, v)
    names = [j.name for j in runner.jobs]
    assert "broken" not in names
    assert "gratitude" in names and "consolidate" in names and "diary" in names


def test_the_folder_readme_is_not_a_job(tmp_path, cfg):
    """The seeders put a README in every folder they make (§34.1). One that ran
    as a nightly prompt would ask the model to be a help page, every night."""
    v = tmp_path / "vault"
    (v / "dreams").mkdir(parents=True)
    (v / "dreams" / "README.md").write_text("# Dreams\n\nWhat she does at night.\n",
                                            encoding="utf-8")
    (v / "dreams" / "scratch.md").write_text("notes I left in here\n", encoding="utf-8")
    runner = _runner(tmp_path, cfg, v)
    names = [j.name for j in runner.jobs]
    assert "README" not in names and "readme" not in names
    assert "scratch" not in names, "a file with no frontmatter is not a job"


def test_a_file_cannot_force_on_a_job_the_house_has_no_backend_for(tmp_path, cfg):
    """The two-switch rule (§18.4.6, §26.1) applied to the night: hers may say
    no, and may not say yes over the house's head."""
    v = tmp_path / "vault"
    _job_file(v, "selfie", front="enabled: true\n")
    runner = _runner(tmp_path, cfg, v)          # cfg has selfie_backend off
    assert "selfie" not in [j.name for j in runner.enabled_jobs()]


def test_the_seeded_roster_reproduces_the_builtin_night(tmp_path, cfg):
    """A fresh vault must dream exactly as it did before this folder existed, or
    the first `git log` entry for a job file is unreadable — you cannot see what
    somebody changed if the baseline was never written down."""
    from yurios.mind.dreamjobs import seed_job_files
    v = tmp_path / "vault"
    (v / "dreams").mkdir(parents=True)
    for fname, body in seed_job_files().items():
        (v / "dreams" / fname).write_text(body, encoding="utf-8")
    from yurios.mind.dreamjobs import (DIARY_SYSTEM, SELFIE_SYSTEM,
                                       STRATEGY_SYSTEM)
    seeded = _runner(tmp_path, cfg, v)
    bare = _runner(tmp_path, cfg, tmp_path / "empty")

    # every builtin still there, with the same flags
    for job in bare.jobs:
        mine = seeded.get(job.name)
        assert mine is not None, f"{job.name} vanished"
        assert mine.priority == job.priority, job.name
        assert mine.per_day == job.per_day, job.name
        assert mine.soul == job.soul, job.name
    assert [j.name for j in seeded.jobs] == [j.name for j in bare.jobs]

    # …and the prompts round-trip through the file byte for byte. This is the
    # assertion that makes the seeding honest: it is what says the file you are
    # about to edit currently says exactly what the code says.
    for name, builtin in (("diary", DIARY_SYSTEM), ("strategy", STRATEGY_SYSTEM),
                          ("selfie", SELFIE_SYSTEM)):
        assert seeded.get(name).system("") == builtin.strip(), name


def test_the_prose_jobs_ask_for_first_person_outright(tmp_path, cfg):
    """Most character cards are written as a third-person dossier, and since
    the soul reaches these prompts the model has that prose in front of it —
    which it will copy in voice as well as in content. Saying "answer as
    yourself" is not enough: the diary says "first person" outright and gets
    it, and the night the strategy note said nothing it came back as "Adia
    weighed the two pending threads…". Both prose jobs must ask.

    Not the selfie: a picture described from outside is still a picture of
    her, and the renderer prefers it that way.
    """
    from yurios.mind.dreamjobs import DIARY_SYSTEM, STRATEGY_SYSTEM
    for name, prompt in (("diary", DIARY_SYSTEM), ("strategy", STRATEGY_SYSTEM)):
        assert "first person" in prompt.lower(), name

    # and the seeded file a character actually edits carries it too — the
    # roster reads the file, so a clause only in Python reaches nobody
    v = tmp_path / "vault"
    (v / "memory").mkdir(parents=True)
    runner = _runner(tmp_path, cfg, v)
    assert "first person" in runner.get("strategy").system("").lower()


def test_an_existing_vault_grows_the_folder_without_changing_its_night(
        tmp_path, cfg):
    """The seeders run once, at creation. A folder invented today exists in no
    vault created yesterday — so the runner writes it on first sight, the way
    the knowledge index's gitignore and the inbox already do. The night it
    describes must be the night that vault already had."""
    v = tmp_path / "vault"
    (v / "memory").mkdir(parents=True)                 # a vault, with no dreams/
    assert not (v / "dreams").exists()

    before = _runner(tmp_path, cfg, tmp_path / "never-seeded")
    before_night = [(j.name, j.priority, j.per_day, j.soul) for j in before.jobs]

    runner = _runner(tmp_path, cfg, v)
    assert (v / "dreams" / "README.md").is_file()
    assert (v / "dreams" / "diary.md").is_file()
    assert [(j.name, j.priority, j.per_day, j.soul)
            for j in runner.jobs] == before_night


def test_a_deleted_job_file_stays_deleted(tmp_path, cfg):
    """The lazy seed fires on an absent *folder*, never an absent file. A
    character who deleted a job meant it, and a seeder that put it back every
    boot would be a bug that reads as a haunting."""
    v = tmp_path / "vault"
    _runner(tmp_path, cfg, v)                          # seeds
    (v / "dreams" / "diary.md").unlink()
    _runner(tmp_path, cfg, v)                          # boots again
    assert not (v / "dreams" / "diary.md").exists()


# =============================================================== research jobs
#
# `kind: research` (SPEC §21.2) — the night that looks outward. Everything below
# runs against `FakeSearch` and `FakeFetcher`, so the loop is pinned and the web
# is never touched (AGENTS.md: no test may need a live service).

KIND_RESEARCH = """---
name: market-brief
title: Overnight market brief
description: Read the tape overnight and write one page on it.
kind: research
priority: 0.2
enabled: true
soul: full
topics: ["semis", "macro"]
max_searches: 4
max_pages: 4
max_steps: 8
deliver: chat
output: reports/market-brief/{day}.md
---

You are {char}. Write {user} their morning brief.

## The tape
Where things stand.
"""

#: What the fake model answers when asked for the report itself.
REPORT = "## The tape\nSemis led, everything else drifted."


class Boom(Exception):
    """A scripted reply that raises instead of answering."""


def _with_front(text, front: dict) -> str:
    """The template with these frontmatter keys set, added or replaced.

    Real YAML rather than a string substitution, which is what this was first
    and which silently did nothing for any key the template did not already
    carry — so a test asking for `report_thinking: false` asserted against the
    default and passed for the wrong reason.
    """
    if not front:
        return text
    head, _, body = text.partition("---\n")[2].partition("---\n")
    loaded = yaml.safe_load(head) or {}
    loaded.update(front)
    return "---\n" + yaml.safe_dump(loaded, sort_keys=False) + "---\n" + body


def _write_job(vault, name, text):
    root = vault / "dreams"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(text, encoding="utf-8")
    return root / f"{name}.md"


class ScriptedModel:
    """One reply per call, in order, and a record of what was asked.

    A research loop is N calls whose *inputs* depend on the answers to the ones
    before, so a fake that keys on the prompt cannot script it: the point of
    these tests is what she does after being told something specific.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, messages, **params):
        system = messages[0].get("content", "") if messages else ""
        user = messages[1].get("content", "") if len(messages) > 1 else ""
        self.calls.append((system, user))
        reply = self.replies.pop(0) if self.replies else "think nothing further"
        if isinstance(reply, Boom) or (isinstance(reply, type)
                                       and issubclass(reply, Boom)):
            raise Boom("the model fell over")
        return reply


class ScriptedFetcher(FakeFetcher):
    """`FakeFetcher` with URLs that refuse to open, and ones that open empty.

    The two are different failures and the loop treats them differently: a
    raise is the web being broken, an empty body is a paywall or a page that
    wanted a browser — the commonest thing a real night meets.
    """

    def __init__(self):
        super().__init__()
        self.fail: set[str] = set()
        self.empty: set[str] = set()

    async def fetch(self, url):
        if url in self.fail:
            self.fetched.append(url)
            raise ValueError(f"{url} is gone")
        page = await super().fetch(url)
        if url in self.empty:
            page = dict(page, text="")
        return page


class _Shelf:
    """Stands in for `Researcher`: the two seams and the ingestion path."""

    def __init__(self, fetcher):
        self.search = FakeSearch()
        self.fetcher = fetcher
        self.shelved: list[dict] = []

    def shelve(self, page):
        self.shelved.append(page)


@pytest.fixture
def research_rig_with(tmp_path, cfg):
    """A runner with one research job, driven by a scripted model."""
    def build(replies, front=None):
        clock = VirtualClock(start=SIM_START.timestamp())
        vault = MindVault(tmp_path / "vault")
        store = FileMemoryStore(tmp_path / "vault", FakeEmbedder(),
                                embed_dim=FakeEmbedder.dim)
        text = _with_front(KIND_RESEARCH, front or {})
        _write_job(tmp_path / "vault", "market-brief", text)
        model = ScriptedModel(replies)
        fetcher = ScriptedFetcher()
        runner = DreamRunner(
            vault, store, clock,
            cfg.model_copy(update={"selfie_backend": "off",
                                   "search_backend": "fake"}),
            consolidator=DreamConsolidator(vault, store, clock, utility=None),
            workspace=Workspace(tmp_path / "vault" / "workspace"),
            skills=SkillStore(tmp_path / "vault" / "skills"),
            research=_Shelf(fetcher),
            utility=model.complete)
        return runner, tmp_path / "vault", model, fetcher
    return build


@pytest.fixture
def research_rig(research_rig_with):
    return research_rig_with([
        'use web_search {"query": "semis"}',
        'use read_page {"url": "https://example.invalid/overview"}',
        "think nothing further",
        REPORT])


# ------------------------------------------------------- a roster she can edit

def test_a_file_with_a_new_name_and_a_kind_becomes_that_kind(rig):
    """`kind:` is the extension seam. Without it every new job is a PromptJob,
    which is the shape that reads her journal — and a job about the world has
    no business reading her journal."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    job = runner.get("market-brief")
    assert isinstance(job, ResearchJob)
    assert job.kind == "research"
    assert job.topics == ["semis", "macro"]


def test_an_unknown_kind_costs_that_job_its_kind_and_never_the_night(rig):
    """§21.2's rule for a mangled file, applied to a `kind:` this build has
    never heard of — a file written against a newer YuriOS, or a typo. Running
    it as a prompt job is a worse night than intended; dropping it silently is
    a job that vanished."""
    runner, _clock, vault = rig
    _write_job(vault, "odd", KIND_RESEARCH
               .replace("kind: research", "kind: telepathy")
               .replace("name: market-brief", "name: odd"))
    runner.reload()
    assert isinstance(runner.get("odd"), PromptJob)
    assert "consolidate" in [j.name for j in runner.jobs]


def test_reload_forgets_a_key_the_file_no_longer_sets(rig):
    """The subtlety `reload()` exists for. `_apply_job_files` mutates builtin
    *instances*, so re-overlaying onto the same objects would leave a deleted
    key still applied — and the first edit anybody makes is to switch a job
    back on."""
    runner, _clock, vault = rig
    _write_job(vault, "diary", "---\nname: diary\nenabled: false\n---\n\nWrite.\n")
    runner.reload()
    assert runner.get("diary").enabled(runner.cfg) is False
    (vault / "dreams" / "diary.md").write_text(
        "---\nname: diary\n---\n\nWrite.\n", encoding="utf-8")
    runner.reload()
    assert runner.get("diary").enabled(runner.cfg) is True


def test_a_job_name_that_is_a_path_is_not_a_name():
    """The one place a name becomes a path. `../../soul/PERSONA` is not a job
    that doesn't exist — it is a name that is not a name."""
    assert JOB_NAME_RE.match("market-brief")
    for bad in ("../../soul/PERSONA", "Market Brief", ".hidden", "a" * 65, ""):
        assert not JOB_NAME_RE.match(bad), bad


def test_validate_says_what_a_working_file_looks_like():
    """§34.2: a refusal that teaches. 'invalid' sends you nowhere; the shape of
    the thing you failed to write is the whole answer."""
    assert "frontmatter" in validate_job_file("x", "just some prose")
    assert "have to agree" in validate_job_file(
        "x", "---\nname: y\n---\n\nbody\n")
    assert "can't be empty" in validate_job_file("x", "---\nname: x\n---\n\n  \n")
    assert validate_job_file("x", "---\nname: x\n---\n\nYou are {char}.\n") == ""


# ------------------------------------------------------------ standing nights

def test_a_standing_job_owes_yesterday_with_no_journal_at_all(rig):
    """A day nobody spoke to her is not a day the episodic folder has, so a job
    whose subject is the world would never get a turn. The market does not wait
    for a conversation."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    job = runner.get("market-brief")
    ctx = runner._context()
    assert ctx.finished_days() == []          # nothing was ever said
    assert job.backlog(ctx, runner.ledger) == ["2026-07-05"]   # yesterday


def test_a_standing_job_never_walks_backwards_through_the_archive(rig):
    """Nine nights of market briefs written nine nights late are nine wrong
    answers, not a backlog worth eating."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    job = runner.get("market-brief")
    runner.ledger.mark("market-brief", "2026-07-05")
    assert job.backlog(runner._context(), runner.ledger) == []


# ------------------------------------------------------------ the search loop

async def test_she_searches_reads_and_writes_a_report(research_rig):
    """The whole shape in one pass: she plans a search, opens what she found,
    and the report is written from the pages rather than from the journal."""
    runner, vault, model, fetcher = research_rig
    report = await runner.run(only="market-brief")
    job = report.jobs[0]
    assert job.changed and not job.failed
    assert fetcher.fetched, "she never opened anything"
    written = (vault / "workspace" / "reports" / "market-brief"
               / "2026-07-05.md")
    assert written.is_file()
    assert "THE TAPE" in written.read_text().upper()
    # the corpus reached the writing call, not just the loop
    assert "example.invalid" in model.calls[-1][1]


async def test_the_loop_stops_after_two_quiet_rounds(research_rig_with):
    """A local 27B that has nothing left to fetch says so in prose, or says
    nothing parseable at all. Either way the loop must stop rather than spend
    its remaining rounds asking again."""
    runner, vault, model, fetcher = research_rig_with(
        ["use web_search {\"query\": \"semis\"}",
         "use read_page {\"url\": \"https://example.invalid/overview\"}",
         "I think that's the picture, really.",
         "Yes, that about covers it.",
         "use web_search {\"query\": \"this should never run\"}",
         REPORT])
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed
    assert "two quiet rounds" in report.jobs[0].result
    # the round after the second quiet one was never asked for
    assert not any("this should never run" in call[1] for call in model.calls)


async def test_thinking_before_the_first_search_is_not_a_quiet_round(research_rig_with):
    """The bug this defends against, found by running one against a real local
    model: a reasoning model told not to think out loud puts its first move in
    the answer instead — a bare `think` about where to start. Counting that as
    quiet ended the night on round two with an empty corpus, every night."""
    runner, _vault, _model, fetcher = research_rig_with(
        ["Starting fresh on US equities; I need the snapshot first.",
         "Actually the macro calendar matters more this week.",
         'use web_search {"query": "semis"}',
         'use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further",
         REPORT])
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed
    assert fetcher.fetched


async def test_a_paywall_is_not_her_having_had_enough(research_rig_with):
    """The exact trace a real night produced, and the bug it found.

    She searched, picked a Morningstar page, and it returned zero characters —
    a paywall or a page that needs a browser. She retried the same URL. Both
    rounds counted as quiet, so the night ended after two steps of a twelve-step
    budget with an empty corpus. `quiet` now counts rounds where *she* stopped
    reaching; the web failing to cooperate is bounded by `max_steps` instead.
    """
    runner, vault, _model, fetcher = research_rig_with(
        ['use web_search {"query": "sector rotation"}',
         'use read_page {"url": "https://example.invalid/paywalled"}',
         'use read_page {"url": "https://example.invalid/paywalled"}',   # retry
         'use read_page {"url": "https://example.invalid/overview"}',    # good
         "think nothing further",
         REPORT])
    fetcher.pages["https://example.invalid/paywalled"] = ""      # zero chars back
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed, report.jobs[0].result
    assert (vault / "workspace" / "reports" / "market-brief"
            / "2026-07-05.md").is_file()


async def test_two_quiet_rounds_still_stop_it_once_she_has_gathered(research_rig_with):
    """…and the forgiveness is only for the empty session. Once she has reached
    for something, two rounds that gather nothing mean she is done."""
    runner, _vault, _model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "Hmm.", "Yes, quite.",
         'use web_search {"query": "never runs"}',
         REPORT])
    report = await runner.run(only="market-brief")
    assert "two quiet rounds" in report.jobs[0].result


async def test_a_research_round_asks_for_no_reasoning_pass(research_rig_with):
    """Measured, not guessed: one round of this loop on a local 27B cost 1200
    reasoning tokens and 200 seconds, and twelve of those is a night that never
    finishes. The line naming her next search is not a question thinking
    improves — the report at the end is, and keeps its full pass.

    Both calls are bounded, and separately. The write inherited
    `UTILITY_MAX_TOKENS` at first, which is sized for extraction: against the
    same model that call ran past nineteen minutes and never returned, because
    a reasoning model handed 15,000 tokens will use them. Bounding it was not
    enough either — see `test_the_report_never_comes_back_empty_after_thinking`.
    """
    runner, _vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT])
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    rounds, write = model.params[:-1], model.params[-1]
    assert rounds and all(p.get("thinking") is False for p in rounds)
    assert all(p.get("max_tokens") == ROUND_MAX_TOKENS for p in rounds)
    # …and the report gets room the rounds do not: what the report is worth,
    # plus room to think, because a ceiling bounds the call and not the pass.
    assert write["max_tokens"] == 2500 + REPORT_REASONING_ALLOWANCE
    assert write["max_tokens"] > ROUND_MAX_TOKENS


def test_the_house_caps_can_actually_be_spent(rig):
    """A night has to be able to finish on purpose. Moves have to cover the
    searches *and* the pages *and* the thinking in between, or the reach caps
    are decoration and every night ends "out of rounds" mid-gather — which is
    what both live nights did at 10 searches, 10 pages and 12 moves."""
    runner, _clock, vault = rig
    # a file that asks for nothing in particular, so the defaults are what
    # answer — the fixture's own job deliberately asks for a short night
    _write_job(vault, "market-brief", "---\nname: market-brief\n"
               "kind: research\n---\n\nWrite them the brief.\n")
    runner.reload()
    job = runner.get("market-brief")
    searches, pages, steps = job.caps(runner.cfg)
    assert steps > searches + pages
    # …and pages stays inside what the corpus can hold, since a page gathered
    # and then trimmed away cost a move for nothing
    assert pages <= job.context_chars // job.step_chars


async def test_a_page_that_gave_nothing_is_still_her_reaching(research_rig_with):
    """Two quiet rounds mean she has stopped reaching, and a dead link is not
    that. A live night ended on "two quiet rounds" with one thought either side
    of a page that came back empty — the same mistake as counting the paywall,
    one step further down. What bounds a night of bad links is `max_steps`."""
    runner, _vault, model, fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think that gave me the shape of it",
         'use read_page {"url": "https://example.invalid/empty"}',
         "think still nothing on the second half",
         'use read_page {"url": "https://example.invalid/deep"}',
         "think nothing further", REPORT])
    fetcher.empty.add("https://example.invalid/empty")
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed
    assert "she had enough" in report.jobs[0].result


async def test_the_same_question_in_other_words_is_the_same_question(research_rig_with):
    """The duplicate that costs a night is never a duplicate — it is one word
    moved. A live night ran "stock market sector rotation leaders laggards
    August 19 2026" and then, two rounds later, the same line with
    "performance" for "rotation": same results, one move gone. A model that has
    just been let down by a dead link reaches for the rephrase every time."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use web_search {"query": "stock market sector rotation leaders August 19"}',
         'use web_search {"query": "stock market sector performance leaders August 19"}',
         'use web_search {"query": "gold silver price outlook"}',
         'use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT])
    runner.utility = _recording(model)
    report = await runner.run(only="market-brief")
    searches = [s for s in report.steps if s.tool == "web_search"]
    assert len(searches) == 2                  # the rephrase never went out
    assert "gold silver price outlook" in str(searches[-1].args)
    # …and she is told which earlier question it was, not just refused — and
    # pointed somewhere, because a live night answered "ask something else" by
    # asking the identical thing again the very next round
    refused = model.seen[2][-1]["content"]
    assert "sector rotation leaders" in refused
    assert "its results are above" in refused
    assert "semis" in refused                  # …the plan, as somewhere to go


def test_a_genuine_follow_up_is_not_mistaken_for_a_rephrase():
    """The guard has to let the second question through, or a night that starts
    with gold can never ask about silver."""
    close = _query_key("stock market sector rotation leaders laggards Aug 19")
    same = _query_key("stock market sector performance leaders laggards Aug 19")
    apart = _query_key("gold silver price today")
    assert _already_asked(same, {"a": close}) == "a"
    assert _already_asked(apart, {"a": _query_key("gold silver price outlook")}) == ""
    # …and the noise words are not what makes two questions alike
    assert _query_key("what is the price of gold") == _query_key("price gold")
    # The one a live night got wrong at a lower threshold. Eight words in
    # common, but sentiment and sector rotation are two different things to go
    # and find out, and refusing the second cost her two moves and a repeat.
    sentiment = _query_key("US stock market today August 20 2026 sentiment "
                           "momentum leaders")
    rotation = _query_key("US stock market sector rotation momentum leaders "
                          "August 20 2026")
    assert _already_asked(rotation, {"a": sentiment}) == ""


async def test_the_write_call_is_told_the_corpus_is_all_she_has(research_rig_with):
    """The one instruction the job file cannot give, because the file is the
    brief and this is the material. A market brief with a price in it she never
    read is worse than no market brief, so the framing says where the figures
    have to come from and what to do where they run out."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT])
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    write = model.seen[-1][-1]["content"]
    assert "That is all you have" in write
    assert "has seen none of it" in write
    assert "example.invalid/overview" in write          # …around the corpus


def test_the_chat_lede_is_her_first_sentence_not_her_first_heading():
    """A report opening `## The tape` would otherwise arrive in chat as the
    words "The tape" — which repeats the card's title and says nothing."""
    assert _lede("## The tape\nSemis led, everything else drifted.") == (
        "Semis led, everything else drifted.")
    assert _lede("# Brief\n\n- Energy is the only bid.") == (
        "Energy is the only bid.")
    # a bullet loses its marker and keeps its emphasis
    assert _lede("1. *Energy* is the only bid.") == "*Energy* is the only bid."
    # nothing but headings still says something, and an empty report says so
    assert _lede("## The tape\n### Later") == "The tape"
    assert _lede("   \n---\n***\n") == "I wrote you something."


async def test_every_round_is_told_what_is_left_of_the_night(research_rig_with):
    """A model that cannot see its budget spends it. The first full live night
    went the whole twelve rounds without once saying it had enough — five of
    them bare thoughts — and stopped because the moves ran out, mid-gather.
    The numbers make stopping arithmetic instead of a guess, and they have to
    be the real ones: a budget line that lies is worse than none."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use web_search {"query": "the tape"}',
         'use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT],
        front={"max_steps": 12, "max_searches": 8, "max_pages": 6})
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    first, second, third = (m[-1]["content"] for m in model.seen[:3])
    assert "12 move(s) left, 8 search(es) and 6 page(s)" in first
    assert "11 move(s) left, 7 search(es) and 6 page(s)" in second
    assert "10 move(s) left, 7 search(es) and 5 page(s)" in third


async def test_the_writing_call_is_given_a_nights_worth_of_wall_clock(research_rig_with):
    """The limit that actually bit, and the one no token ceiling could fix.
    A report call ran 1,802 seconds and died of the client's 600-second default
    rather than of anything wrong with the answer — and at a local model's few
    tokens a second, 600 seconds is under 4,000 tokens however much room the
    call was given. Nobody is waiting at 4am."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT])
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    rounds, write = model.params[:-1], model.params[-1]
    assert write["timeout"] == REPORT_TIMEOUT_S >= 1800
    # …and the rounds keep the ordinary one: a round that hangs for half an
    # hour is a night that never finishes, and its answer is one line.
    assert all("timeout" not in p for p in rounds)


async def test_the_report_never_comes_back_empty_after_thinking(research_rig_with):
    """The rule this whole job is built on: the failure mode is a shorter
    report, never no report.

    Capping a reasoning model's tokens bounds the *call*, not the thinking, so
    the entire ceiling can go into a <think> block that is then cut off — which
    is exactly what a live night did: 431 seconds, 2,500 tokens, and an empty
    string back. The answer is more room, not less thinking: this is the one
    call in the night that earns a reasoning pass, and trading it away to avoid
    an empty answer would be fixing the wrong half.
    """
    runner, vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further",
         "",            # thought past the ceiling and never spoke
         REPORT])       # …and says it plainly with room to finish
    runner.utility = _recording(model)
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed
    assert (vault / "workspace" / "reports" / "market-brief"
            / "2026-07-05.md").read_text().startswith("## The tape")
    first, retry = model.params[-2], model.params[-1]
    assert first["thinking"] is True and retry["thinking"] is True
    assert retry["max_tokens"] > first["max_tokens"]
    # …and the pass is shortened, not dropped: the retry answers both halves
    # of what went wrong — too little room, and too much of it spent thinking.
    assert first["reasoning_effort"] == ""      # …whatever the server does
    assert retry["reasoning_effort"] == "low"   # …and the shortest it will ask


def test_the_retry_asks_for_what_the_window_actually_has(rig):
    """Not a bigger guess — the window minus the prompt. A second ceiling that
    does not fit alongside the corpus truncates in exactly the same place the
    first one did, which would make the retry ceremony."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    job = runner.get("market-brief")
    cfg = runner.cfg.model_copy(update={"context_length": 24576})
    assert job.retry_max_tokens(cfg, 20000) == 24576 - 5000 - 512
    # a prompt that fills the window leaves the first ceiling, never less
    assert job.retry_max_tokens(cfg, 400000) == job.report_max_tokens
    # …and with no window configured it still asks for meaningfully more than
    # the first call already had, which is the only thing that makes it a retry
    unset = runner.cfg.model_copy(update={"context_length": 0})
    assert job.retry_max_tokens(unset, 20000) > job.report_ceiling(unset, 20000)


async def test_the_report_is_the_call_that_gets_to_think(research_rig_with):
    """The rounds go without a reasoning pass so that this one can have it.
    Everything before the report is plumbing — which page to open next is not
    a question thinking improves — and this is where she decides what she
    actually thinks about what she read."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT])
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    assert model.params[-1]["thinking"] is True
    assert all(p["thinking"] is False for p in model.params[:-1])


async def test_the_report_call_is_tunable_from_the_file(research_rig_with):
    """How much a report is worth is the character's decision, not this file's
    — and `report_max_tokens` is what the *report* is worth. The thinking gets
    its own allowance on top, so that raising one never quietly starves the
    other; a job that asks for no pass gets exactly the number it asked for."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT],
        front={"report_thinking": True, "report_max_tokens": 9000})
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    write = model.params[-1]
    assert write["thinking"] is True
    assert write["max_tokens"] == 9000 + REPORT_REASONING_ALLOWANCE


def test_a_report_that_does_not_think_asks_for_exactly_what_it_wants(rig):
    """The allowance is room for a pass, so a job that has turned the pass off
    is handed the number it asked for and not a token more."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", _with_front(
        KIND_RESEARCH, {"report_thinking": False, "report_max_tokens": 900}))
    runner.reload()
    job = runner.get("market-brief")
    cfg = runner.cfg.model_copy(update={"context_length": 24576})
    assert job.report_ceiling(cfg, 20000) == 900


def test_the_first_write_call_never_overruns_the_window(rig):
    """Room to think is asked for, not assumed: a local server handed more than
    its window has does not always clamp, and a first call that overruns fails
    exactly the way the retry exists to rescue."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    job = runner.get("market-brief")
    cfg = runner.cfg.model_copy(update={"context_length": 24576})
    # a small prompt leaves room for the whole allowance…
    assert job.report_ceiling(cfg, 4000) == 2500 + REPORT_REASONING_ALLOWANCE
    # …and a prompt that fills the window takes what is left, never more
    assert job.report_ceiling(cfg, 70000) == job.retry_max_tokens(cfg, 70000)


async def test_the_length_of_the_reasoning_pass_is_the_files_call(research_rig_with):
    """`report_effort` is the knob between the two measured failures: a pass
    long enough to fill the ceiling and answer with nothing, and no pass at
    all. The file gets to choose where on that ladder its model sits."""
    runner, _vault, model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT],
        front={"report_effort": "high"})
    runner.utility = _recording(model)
    await runner.run(only="market-brief")
    assert model.params[-1]["reasoning_effort"] == "high"


def test_an_effort_the_server_would_refuse_is_not_sent(rig):
    """A rejected `reasoning_effort` is a failed call, and a job file is
    written by hand. Unset and misspelt both fall back to whatever the server
    does on its own, which is never a failure — the editor is where a typo gets
    told about, not the night."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    assert runner.get("market-brief").report_effort == ""
    _write_job(vault, "market-brief", _with_front(KIND_RESEARCH,
                                                  {"report_effort": "ludicrous"}))
    runner.reload()
    assert runner.get("market-brief").report_effort == ""


def test_the_editor_catches_an_effort_the_runner_would_shrug_at():
    """Both halves of the same rule: the runner never fails a night over a
    typo, and the door somebody is standing at says what the typo was."""
    good = _with_front(KIND_RESEARCH, {"report_effort": "high"})
    assert validate_job_file("market-brief", good) == ""
    bad = _with_front(KIND_RESEARCH, {"report_effort": "ludicrous"})
    assert "low, medium, high" in validate_job_file("market-brief", bad)


def test_the_shorter_pass_bottoms_out_rather_than_turning_off():
    """One notch down each time, and `low` is the floor — the retry may ask her
    to think less and must never arrive at not thinking."""
    assert _shorter_effort("high") == "medium"
    assert _shorter_effort("medium") == "low"
    assert _shorter_effort("low") == "low"
    # …and unset steps down rather than staying unset: the retry exists because
    # a pass ran away, and the shortest one is the only "think less" to send.
    assert _shorter_effort("") == "low"


def _recording(model):
    """Wrap the scripted model so the per-call params and prompts are visible."""
    model.params = []
    model.seen = []

    async def call(messages, **params):
        model.params.append(params)
        model.seen.append(messages)
        return await model.complete(messages, **params)
    return call


async def test_nothing_further_ends_it_at_once(research_rig_with):
    runner, _vault, _model, _fetcher = research_rig_with(
        ["use read_page {\"url\": \"https://example.invalid/overview\"}",
         "think nothing further",
         REPORT])
    report = await runner.run(only="market-brief")
    assert "she had enough" in report.jobs[0].result


async def test_a_page_that_will_not_open_is_skipped_not_fatal(research_rig_with):
    """§7.7's rule for `research`, which matters more unattended: one dead link
    at 4am must not be why there is no brief in the morning."""
    runner, vault, _model, fetcher = research_rig_with(
        ["use read_page {\"url\": \"https://example.invalid/gone\"}",
         "use read_page {\"url\": \"https://example.invalid/overview\"}",
         "think nothing further",
         REPORT])
    fetcher.fail = {"https://example.invalid/gone"}
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed
    assert [s for s in report.steps if s.failed]        # the audit says so
    assert (vault / "workspace" / "reports" / "market-brief"
            / "2026-07-05.md").is_file()


async def test_a_night_that_opened_nothing_is_handled_not_produced(research_rig_with):
    """§21.2: handled is not produced. She looked, there was nothing, and the
    day is marked — or she re-decides this every night forever."""
    runner, vault, _model, _fetcher = research_rig_with(
        ["use web_search {\"query\": \"semis\"}", "think nothing further"])
    report = await runner.run(only="market-brief")
    job = report.jobs[0]
    assert job.days == ["2026-07-05"] and not job.changed
    assert "nothing worth a report" in job.result
    assert not (vault / "workspace" / "reports").exists()
    assert runner.get("market-brief").backlog(
        runner._context(), runner.ledger) == []


async def test_the_caps_are_the_houses_and_the_file_may_only_lower_them(research_rig_with):
    """§26.1's two-switch rule, one layer down: a job file is the character's
    and the ceiling is the machine's."""
    runner, _vault, _model, fetcher = research_rig_with(
        ["use read_page {\"url\": \"https://example.invalid/%d\"}" % i
         for i in range(8)] + [REPORT],
        front={"max_pages": 99})            # asks for far more than the house
    runner.cfg = runner.cfg.model_copy(update={"mind_dream_research_pages": 2})
    report = await runner.run(only="market-brief")
    assert len(fetcher.fetched) == 2
    assert "out of pages" in report.jobs[0].result


async def test_a_loop_that_dies_with_pages_in_hand_still_writes(research_rig_with):
    """The partial-report rule. A brief from four pages beats no brief, and a
    job that raises does not mark its day — so the alternative is retrying
    tomorrow with nothing to show for tonight."""
    runner, vault, model, _fetcher = research_rig_with(
        ["use read_page {\"url\": \"https://example.invalid/overview\"}",
         Boom(), REPORT])
    report = await runner.run(only="market-brief")
    assert report.jobs[0].changed
    assert "failed part-way" in report.jobs[0].result
    assert (vault / "workspace" / "reports" / "market-brief"
            / "2026-07-05.md").is_file()


async def test_a_loop_that_dies_with_nothing_retries_tomorrow(research_rig_with):
    runner, _vault, _model, _fetcher = research_rig_with([Boom()])
    report = await runner.run(only="market-brief")
    assert report.jobs[0].failed
    assert report.jobs[0].days == []          # unmarked: it comes back


# ---------------------------------------------------------------- the budget

def test_research_is_priced_for_the_whole_loop_not_one_call(rig):
    """§21.2's MUST. The budget check happens once, before the job starts, so
    anything `cost()` does not price is spent unbilled — and a job priced at
    one call would run a twelve-call loop on a night that looked affordable."""
    runner, _clock, vault = rig
    _write_job(vault, "market-brief", KIND_RESEARCH)
    runner.reload()
    job, ctx = runner.get("market-brief"), runner._context()
    one_call = (PROMPT_OVERHEAD_CHARS + job.context_chars) // 4
    assert job.cost(ctx, "2026-07-05") > one_call * 4


async def test_the_two_lanes_are_spent_separately(research_rig_with):
    """The change to §21.2, and the whole reason for it.

    A research job prices at ~60k against a 40k tick budget, so on one shared
    ceiling it either never runs or it runs and starves everything behind it.
    Here the shared lane is exhausted — the diary and the stock-take are vetoed
    — and the reading still happens, because it is not billed out of that
    allowance at all.
    """
    runner, vault, _model, _fetcher = research_rig_with(
        ['use read_page {"url": "https://example.invalid/overview"}',
         "think nothing further", REPORT])
    _day_file(vault, "2026-07-04", ["user: hello  ⇄  yuri: hi"])
    _day_file(vault, "2026-07-05", ["user: morning  ⇄  yuri: mm"])
    report = await runner.run(token_budget=1)        # the shared lane, spent
    ran = {j.name for j in report.jobs}
    assert report.exhausted_budget                   # …and it says so
    assert "diary" not in ran and "strategy" not in ran
    assert "market-brief" in ran
    assert next(j for j in report.jobs if j.name == "market-brief").changed


# -------------------------------------------------------------- the delivery

async def test_deliver_chat_hands_the_report_to_the_inbox(research_rig_with):
    """§18.2a's third lane. Not Gate 2 deciding to interrupt — a standing
    instruction its owner wrote into a job file."""
    runner, _vault, _model, _fetcher = research_rig_with(
        ["use read_page {\"url\": \"https://example.invalid/overview\"}",
         "think nothing further", REPORT])
    delivered: list[dict] = []
    runner.deliver_report = lambda **kw: delivered.append(kw)
    report = await runner.run(only="market-brief")
    assert len(delivered) == 1
    assert delivered[0]["path"] == "reports/market-brief/2026-07-05.md"
    assert delivered[0]["job"] == "market-brief"
    assert delivered[0]["title"] == "Overnight market brief"
    assert report.delivered == [delivered[0]["path"]]


async def test_a_dry_run_reads_but_writes_nothing_and_delivers_nothing(research_rig_with):
    """§21.3: the same model calls, no writes, no ledger, no commit — and now
    no shelving and no delivery either. The button has to be safe to press on
    a live vault."""
    runner, vault, model, fetcher = research_rig_with(
        ["use read_page {\"url\": \"https://example.invalid/overview\"}",
         "think nothing further", REPORT])
    delivered: list[dict] = []
    runner.deliver_report = lambda **kw: delivered.append(kw)
    report = await runner.run(only="market-brief", dry_run=True)
    assert model.calls and fetcher.fetched      # she really did the work
    assert not delivered                        # …and told nobody
    assert report.writes == ["reports/market-brief/2026-07-05.md"]
    assert not (vault / "workspace" / "reports").exists()
    assert runner.ledger.done("market-brief") == set()


async def test_the_page_shows_the_searches_beside_the_prompts(research_rig_with):
    """§21.3 promises the prompts. A research night is only readable if the
    page also says which searches produced the corpus behind them."""
    runner, _vault, _model, _fetcher = research_rig_with(
        ["use web_search {\"query\": \"semis\"}",
         "use read_page {\"url\": \"https://example.invalid/overview\"}",
         "think nothing further", REPORT])
    data = (await runner.run(only="market-brief")).as_dict()
    tools = [s["tool"] for s in data["steps"]]
    assert "web_search" in tools and "read_page" in tools
    assert data["exchanges"], "the model calls are still there too"
