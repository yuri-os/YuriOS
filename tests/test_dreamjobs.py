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
from yurios.world import correlate
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
    from yurios.characters.importer import _seed_job_files
    v = tmp_path / "vault"
    (v / "dreams").mkdir(parents=True)
    for fname, body in _seed_job_files().items():
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
