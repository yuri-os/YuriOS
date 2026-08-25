"""The roster, and the night that runs it (SPEC §21.2).

What jobs exist (`BUILTIN_JOBS`, `JOB_KINDS`), whether a file describing one is
usable (`validate_job_file`), and the thing that runs them: `DreamRunner`, which
builds the night's roster from the builtins overlaid with the character's own
files, and works through it a chunk at a time under one process-wide lock.

This is the top of the package — it imports every other module here and nothing
imports it back.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from yurios.kernel.clock import Clock
from yurios.app.providers.admission import InferenceBusy

from .builtins import (ConsolidateJob, DiaryJob, DreamJob, SelfieJob,
                       StrategyJob)
from .context import (REPORT_EFFORTS, DreamContext, Exchange, JobLedger,
                      JobReport, Step, UtilityCall)
from .filedsl import (DREAMS_README, FileJob, JobFile,
                      PromptJob, _as_effort, load_job_files, seed_job_files)
from .research import ResearchJob
from ..dream import DreamConsolidator
from ..util import iso_of
from ..vaultio import MindVault
from ..workspace import FRONTMATTER_RE, SkillStore, Workspace

log = logging.getLogger("mind.dreamjobs")


#: Every `kind:` a job file may declare. A new kind is a class above and a name
#: here — the same one-line extension point `BUILTIN_JOBS` is for the roster.
JOB_KINDS: dict[str, type[FileJob]] = {
    "prompt": PromptJob,
    "research": ResearchJob,
}


#: What a job may be called, and therefore what its file may be called. The
#: pattern `DreamRunRequest.job` already uses, kept here because this is where a
#: name turns into a path and the two must not drift.
JOB_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def validate_job_file(name: str, text: str) -> str:
    """Why this file would not work as a job, or "" when it would.

    Written as a sentence rather than a code, and phrased as what a working file
    looks like: §34.2's rule that a refusal has to teach, applied to the one
    surface where somebody is typing YAML into a textarea at midnight.

    Deliberately not a schema. The loader already tolerates a file that is
    mostly right — an unknown `kind:` runs as a prompt job, an unreadable number
    falls back to its default — and a stricter door than the runner would refuse
    files that in fact work.
    """
    if not JOB_NAME_RE.match(name or ""):
        return ("a job name is lowercase letters, digits, - and _ "
                "(it becomes vault/dreams/<name>.md)")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return ("a job starts with YAML frontmatter between two --- lines, "
                "then the prompt body:\n\n---\nname: " + name +
                "\ntitle: …\nenabled: true\n---\n\nYou are {char}, …")
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        return f"the frontmatter isn't valid YAML: {e}"
    if not isinstance(front, dict):
        return "the frontmatter has to be a mapping of key: value lines"
    declared = str(front.get("name") or name)
    if declared != name:
        return (f"this file is {name}.md but its frontmatter says "
                f"name: {declared} — they have to agree")
    if not text[match.end():].strip():
        return ("the body below the frontmatter is the system prompt she is "
                "given, and it can't be empty")
    kind = str(front.get("kind") or "prompt").strip().lower()
    if kind not in JOB_KINDS and name not in BUILTIN_NAMES:
        return (f"kind: {kind} isn't one this build knows — "
                f"it's one of {', '.join(sorted(JOB_KINDS))}")
    # The one place a typo here can be caught while somebody is looking at it.
    # The runner stays lenient — an effort it does not know is dropped rather
    # than allowed to fail a night at 4am — but a door that says nothing lets
    # the typo through to a report written with a setting nobody chose.
    if front.get("report_effort") is not None and not _as_effort(
            front.get("report_effort"), ""):
        return (f"report_effort: {front['report_effort']} isn't one of "
                f"{', '.join(REPORT_EFFORTS)} — it is how long the reasoning "
                "pass before the report may run")
    return ""


def _build_job(spec: JobFile) -> FileJob:
    """One job file → the job it declares.

    An unknown `kind:` falls back to `prompt` rather than refusing the file.
    The alternative is that a typo — or a file written against a newer build —
    silently removes a job from the night, which is the failure §21.2 spends a
    paragraph avoiding for mangled frontmatter and is no better here.
    """
    wanted = str(spec.front.get("kind") or "prompt").strip().lower()
    cls = JOB_KINDS.get(wanted)
    if cls is None:
        log.warning("dream job %s asks for kind %r, which this build has no "
                    "idea about; running it as a plain prompt job",
                    spec.name, wanted)
        cls = PromptJob
    return cls(spec)


#: The night's roster. `consolidate` is constructed by the runner because it
#: needs the consolidator; the rest take no arguments. Adding a job is adding a
#: class above and a name here.
BUILTIN_JOBS: tuple[type[DreamJob], ...] = (DiaryJob, StrategyJob, SelfieJob)

#: Every name a file may *retune* rather than define. `consolidate` is in it and
#: is not in `BUILTIN_JOBS`, because the runner constructs that one itself.
BUILTIN_NAMES: frozenset[str] = frozenset(
    {ConsolidateJob.name} | {cls.name for cls in BUILTIN_JOBS})


# --------------------------------------------------------------------- runner


#: The one-night-at-a-time lock, shared by every runner in the process. It is
#: module-global rather than handed in by the host for the same reason the
#: roster is: characters each build their own `DreamRunner` and nothing else in
#: the system should have to know they must not dream at once. Held for one
#: `run` call — one tick's chunk, or one manual run — never for a whole night.
_NIGHT_LOCK = asyncio.Lock()


@dataclass
class NightReport:
    jobs: list[JobReport] = field(default_factory=list)
    exhausted_budget: bool = False
    nothing_to_do: bool = False
    exchanges: list[Exchange] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def notes(self) -> list[str]:
        return [j.note for j in self.jobs if j.note]

    @property
    def summary(self) -> str:
        """One line for the trace and the commit message.

        Aggregated per job rather than per day: a night that consolidated four
        days would otherwise read "consolidate: 1 day; consolidate: 1 day;
        consolidate: 1 day; consolidate: 1 day", which is four times as long
        and says less.
        """
        # A rehearsal says so, first thing. This line is the debug page's toast
        # as well as the commit message, and "market-brief: read 4 pages over 3
        # searches and wrote 4898 chars" is a sentence about a file on her desk
        # — which is exactly what a dry run has not got. The wet wording is
        # unchanged; `dream_now` only ever commits with the wet one.
        head = "DREAM (dry run)" if self.dry_run else "DREAM"
        by_job: dict[str, list[JobReport]] = {}
        for j in self.jobs:
            if j.changed or j.failed:
                by_job.setdefault(j.name, []).append(j)
        if not by_job:
            return f"{head}: nothing to do"
        parts = []
        for name, runs in by_job.items():
            failed = [r for r in runs if r.failed]
            if failed:
                parts.append(f"{name}: {failed[0].failed}")
            elif len(runs) == 1:
                parts.append(f"{name}: {runs[0].result}")
            else:
                parts.append(f"{name}: {len(runs)} days")
        return (f"{head} — " + "; ".join(parts)
                + (", budget spent — backlog remains" if self.exhausted_budget
                   else ""))

    def as_dict(self) -> dict:
        return {"jobs": [j.as_dict() for j in self.jobs],
                "exhausted_budget": self.exhausted_budget,
                "nothing_to_do": self.nothing_to_do,
                "dry_run": self.dry_run,
                "writes": self.writes,
                "summary": self.summary,
                "exchanges": [e.as_dict() for e in self.exchanges],
                # The night's hands, beside the night's model calls. §21.3
                # promises the prompts; a research job is only readable if the
                # page also says which searches produced the corpus behind them.
                "steps": [t.as_dict() for t in self.steps],
                "delivered": list(self.delivered)}


class DreamRunner:
    """The pipeline. Owns the roster, the ledger and the night's budget."""

    def __init__(self, vault: MindVault, store, clock: Clock, cfg, *,
                 consolidator: DreamConsolidator,
                 goals=None, workspace: Workspace | None = None,
                 skills: SkillStore | None = None,
                 utility: UtilityCall | None = None,
                 selfie: Callable[[dict], None] | None = None,
                 research: object | None = None,
                 deliver_report: Callable[..., None] | None = None,
                 audit: Callable[..., None] | None = None,
                 soul_text: Callable[[], str] | None = None,
                 drives: Callable[[], list[str]] | None = None):
        self.vault = vault
        self.store = store
        self.clock = clock
        self.cfg = cfg
        self.goals = goals
        self.workspace = workspace
        self.skills = skills
        self.utility = utility
        self.selfie = selfie
        self.research = research
        self.deliver_report = deliver_report
        self.audit = audit
        self.soul_text = soul_text
        self.drives = drives
        self.ledger = JobLedger(vault.vault / "state" / "dream_jobs.json")
        #: Kept because `reload()` has to build a fresh `ConsolidateJob`, and a
        #: consolidator is the one thing here the runner cannot construct.
        self._consolidator = consolidator
        self.jobs: list[DreamJob] = []
        self.reload()

    #: Where a character keeps the jobs that are hers (§21.2, §34.1). Versioned
    #: like `skills/` and unlike `workspace/`: a night's job is a durable
    #: statement about how she spends the hours nobody sees, and changing one is
    #: exactly the kind of change worth reading back.
    JOBS_DIR = "dreams"

    @staticmethod
    def _seed_jobs_dir(root: Path) -> None:
        """Write the folder on first sight, for a vault that predates it.

        The seeders (§34.1) only run once, at creation, so a folder invented
        today exists in no vault created yesterday — the reason
        `KnowledgeStore.INDEX_GITIGNORE` and the inbox both write themselves
        lazily, and the same reason applies here with more force: a roster
        nobody can see is a roster nobody will edit.

        Seeded with the prompts already compiled into this file, so an existing
        vault's night does not change on the boot that grows the folder. Only
        ever written when the directory is absent — a character who deleted a
        job file meant it, and a seeder that restored it every boot would be a
        bug that looks like a haunting.
        """
        if root.exists():
            return
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text(DREAMS_README, encoding="utf-8")
        for fname, body in seed_job_files().items():
            (root / fname).write_text(body, encoding="utf-8")
        log.info("seeded the dream roster into %s", root)

    def reload(self) -> None:
        """Rebuild the roster from the builtins and `vault/dreams/`.

        Called at construction and again whenever a job file is written, so an
        edit takes effect without a restart.

        It rebuilds rather than re-overlays, and that is the whole subtlety:
        `_apply_job_files` mutates builtin **instances** in place, so re-running
        it over the same objects would leave a key that has since been deleted
        from a file still applied — the edit that removes `enabled: false` would
        not switch the job back on, which is precisely the edit somebody makes
        first. Fresh instances have the compiled defaults and nothing else.
        """
        self.jobs = [ConsolidateJob(self._consolidator)]
        self.jobs += [cls() for cls in BUILTIN_JOBS]
        self._apply_job_files()
        self.jobs.sort(key=lambda j: j.priority, reverse=True)

    def _apply_job_files(self) -> None:
        """Overlay `vault/dreams/` onto the built-in roster.

        Builtins first, files second, and the order is the design. A file whose
        name matches a builtin retunes it — prompt, priority, cadence, whether
        it runs at all — and does **not** replace its `work`, so `diary` keeps
        `relabel()` and its day bookkeeping however its prompt is rewritten. A
        file with a new name becomes a `PromptJob`. `consolidate` accepts a file
        like any other, which is worth saying plainly: it can be retuned, and it
        cannot be removed, because it is the job every other job reads from.
        """
        root = self.vault.vault / self.JOBS_DIR
        try:
            self._seed_jobs_dir(root)
            specs = load_job_files(root)
        except Exception:  # noqa: BLE001 — a bad folder is not a lost night
            log.exception("dream job files could not be read")
            return
        by_name = {j.name: j for j in self.jobs}
        for spec in specs:
            existing = by_name.get(spec.name)
            try:
                if existing is not None:
                    spec.applies_to(existing)
                else:
                    job = _build_job(spec)
                    self.jobs.append(job)
                    by_name[job.name] = job
            except Exception:  # noqa: BLE001 — one bad file, one lost job
                log.exception("dream job %s could not be loaded", spec.name)

    # ------------------------------------------------------------------ roster

    def enabled_jobs(self) -> list[DreamJob]:
        return [j for j in self.jobs if j.enabled(self.cfg)]

    def get(self, name: str) -> DreamJob | None:
        return next((j for j in self.jobs if j.name == name), None)

    def _context(self, **kw) -> DreamContext:
        return DreamContext(
            vault=self.vault, store=self.store, clock=self.clock,
            goals=self.goals, workspace=self.workspace, skills=self.skills,
            utility=self.utility, selfie=self.selfie, audit=self.audit,
            research=self.research, deliver_report=self.deliver_report,
            soul_text=self.soul_text, drives=self.drives, cfg=self.cfg,
            char_name=str(getattr(self.cfg, "companion_name", "") or "she"),
            user_name=str(getattr(self.cfg, "user_name", "") or "the user"),
            **kw)

    def backlog(self) -> list[str]:
        """The days any enabled job still owes work on — what the ladder reads.

        Deliberately a flat list of day strings rather than a per-job map: the
        activity controller's question is "is there night work pending", and
        `dream.py`'s answer has always had that shape. The per-job detail is in
        `status()`, for the page that wants it.
        """
        ctx = self._context()
        days: set[str] = set()
        for job in self.enabled_jobs():
            try:
                days.update(job.backlog(ctx, self.ledger))
            except Exception:  # noqa: BLE001 — a broken job is not a broken ladder
                log.exception("DREAM job %s: backlog failed", job.name)
        return sorted(days)

    def status(self) -> list[dict]:
        """Per-job state for the debug page: enabled, backlog, last run."""
        ctx = self._context()
        out = []
        for job in self.jobs:
            enabled = job.enabled(self.cfg)
            try:
                pending = job.backlog(ctx, self.ledger) if enabled else []
            except Exception:  # noqa: BLE001
                log.exception("DREAM job %s: backlog failed", job.name)
                pending = []
            out.append({**job.as_dict(), "enabled": enabled,
                        "backlog": pending, **self.ledger.summary(job.name)})
        return out

    # -------------------------------------------------------------------- run

    async def run(self, *, token_budget: int = 40000,
                  research_budget: int | None = None,
                  only: str | None = None, day: str | None = None,
                  dry_run: bool = False) -> NightReport:
        """One DREAM tick's worth of work.

        `only` restricts the night to one job and `day` pins the day it works
        on — together they are the debug page's "test this job" button, and
        separately they are how you catch up one job that fell behind. With
        neither, this is the night: every enabled job, priority order, shared
        budget.

        Queues on `_NIGHT_LOCK` first: a night that starts while another
        character's is running waits for that chunk to finish rather than
        interleaving its model calls and camera work with hers.
        """
        if _NIGHT_LOCK.locked():
            log.info("DREAM for %s queued: another night is running",
                     getattr(self.cfg, "companion_name", "?") or "?")
        if research_budget is None:
            research_budget = int(getattr(self.cfg,
                                          "mind_dream_research_tokens", 0)
                                  or token_budget)
        async with _NIGHT_LOCK:
            return await self._run(token_budget=token_budget,
                                   research_budget=research_budget,
                                   only=only, day=day, dry_run=dry_run)

    async def _run(self, *, token_budget: int, research_budget: int,
                   only: str | None, day: str | None,
                   dry_run: bool) -> NightReport:
        """The night itself, run under the lock — see `run` for the contract.

        Two budgets, not one (§21.2). A job that declares `own_budget` is billed
        against its own ceiling: a night of reading the web is an order of
        magnitude past a diary entry, and sharing one allowance means either the
        reading never runs or it eats consolidation on the night it does. The
        rules inside each lane are unchanged — priority order, and the first
        item runs however big it is.
        """
        report = NightReport(dry_run=dry_run)
        jobs = [j for j in self.enabled_jobs() if only is None or j.name == only]
        if only is not None and not jobs:
            raise KeyError(f"no dream job called {only!r}")
        spent = {False: 0, True: 0}
        ceiling = {False: token_budget, True: research_budget}
        touched = False

        for job in jobs:
            ctx = self._context(day="", dry_run=dry_run, job=job.name,
                                soul=job.soul)
            try:
                pending = [day] if day else job.backlog(ctx, self.ledger)
            except Exception:  # noqa: BLE001
                log.exception("DREAM job %s: backlog failed", job.name)
                continue
            lane = job.own_budget
            for target in pending:
                cost = job.cost(ctx, target)
                # `spent` is 0 only for the very first item of this lane, which
                # always runs however big it is — `dream.py` explains why at
                # length, and the rule matters more here: with several jobs
                # queued, a veto on the first one would starve every job behind
                # it too.
                if spent[lane] and spent[lane] + cost > ceiling[lane]:
                    report.exhausted_budget = True
                    break
                ctx.day = target
                out = await self._run_one(job, ctx, target)
                report.jobs.append(out)
                spent[lane] += cost
                # `days` is handled, not produced — see JobReport. A job that
                # decided there was nothing to write still finished with that
                # day, and must not be asked about it again tomorrow.
                if out.days and not out.failed and not dry_run \
                        and not job.owns_ledger:
                    touched = True
                    for finished in out.days:
                        self.ledger.mark(job.name, finished)
                    self.ledger.note_run(job.name, at=iso_of(self.clock.now()),
                                         result=out.result)
            report.exchanges.extend(ctx.exchanges)
            report.steps.extend(ctx.steps)
            report.writes.extend(ctx.writes)
            report.delivered.extend(ctx.delivered)
            # No `break` on an exhausted budget: one expensive job hitting the
            # ceiling on its next day says nothing about whether the cheap jobs
            # behind it fit. They are each gated by the same check above, so
            # nothing overruns — a costly diary simply stops deferring the
            # strategy review and the selfie, which cost a few hundred tokens
            # between them, to a night that may not come.

        if touched:
            self.ledger.save()
            self.vault.mark_dirty()
        report.nothing_to_do = not any(j.changed or j.failed for j in report.jobs)
        return report

    async def _run_one(self, job: DreamJob, ctx: DreamContext,
                       day: str) -> JobReport:
        """One job on one day, with its failure contained.

        A job that raises does not mark its day done, so it retries tomorrow —
        and the night carries on. The alternative is one bad prompt in a job you
        added last week costing you consolidation for as long as it takes you to
        notice, which is exactly the failure an unattended nightly pass must not
        have.
        """
        try:
            return await job.work(ctx, day)
        except InferenceBusy:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("DREAM job %s failed on %s", job.name, day)
            return JobReport(name=job.name, result=f"failed: {e}",
                             failed=str(e)[:200])
