"""The DREAM pipeline (SPEC §21.2) — more than one thing happens at night.

Build #5 shipped DREAM as exactly one job: compact yesterday's journal into
durable facts (`dream.py`, still the implementation and still the first job to
run). That was the right first job and the wrong shape to stop at. Sleep is
where the expensive, unhurried, nobody-is-waiting work goes — and consolidation
is only the most obvious member of that set. Keeping a diary. Standing back from
her goals. Whatever you decide belongs there next.

So DREAM is a **pipeline of jobs over a shared budget**, and this module is the
pipeline. A job is a small object with a name, a backlog, and a `run` — see
`DreamJob` below. Four ship built in; adding a fifth is one class and one line
in `BUILTIN_JOBS`, and everything else in the system (the ladder, the trace, the
budget, the debug page, the manual trigger) picks it up without being told.

The disciplines are `dream.py`'s, generalised from one job to N:

  * **Oldest-first, resumable, per job.** Each job keeps its own list of days
    done, in `state/dream_jobs.json`. A job added to a vault with six months of
    history has a six-month backlog and eats it a night at a time; a job removed
    leaves its ledger behind, harmless, in case it comes back.
  * **One budget for the night, spent in priority order.** Jobs run
    highest-priority first, and the budget is shared: consolidation is worth
    more than a diary entry, so consolidation gets the tokens first and the
    diary waits for tomorrow night rather than the two of them each getting
    half a job done.
  * **Always forward.** The first item of the night runs however big it is,
    for the reason `dream.py` gives at length: a budget that can veto the
    oldest item is a backlog that wedges on it forever.
  * **A failed job is a failed job.** It is caught, reported, and does not mark
    its day done — so it retries tomorrow — and the rest of the night runs.
  * **One night at a time, process-wide.** Every character keeps her own
    runner, ledger and vault, but the window opens for all of them at the same
    hour, and the work a night makes is not shareable: every prompt reaches one
    utility model, and a dreamt selfie reaches one camera. Two nights running
    at once interleave those calls and the renders answer the wrong dream. So
    `run` holds a process-wide lock (`_NIGHT_LOCK` below); a second night —
    another character's tick, or this one's debug button — queues until the
    running one finishes its chunk. The wait is bounded by design: the night is
    chunked (`mind_dream_cadence_s`), so the lock changes hands every few
    seconds of model work rather than every morning.

Everything a job needs arrives in a `DreamContext`, including `ask()`, the one
way to reach the utility model. `ask` records every exchange on the context, and
that recording is what the debug page's "test this job" button shows you: the
exact system prompt, the exact input, the raw completion, before anything was
parsed out of it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from yurios.world import correlate
from yurios.world.clock import Clock
from yurios.app.providers.admission import InferenceBusy

from .dream import DreamConsolidator
from .journal import canonical_day, is_canonical_day
from .util import day_of, iso_of, read_json, write_json
from .vaultio import MindVault
from .workspace import FRONTMATTER_RE, SkillStore, Workspace

log = logging.getLogger("mind.dreamjobs")

UtilityCall = Callable[[list[dict]], Awaitable[str]]

#: How much of one day's journal reaches a prompt. `dream.py`'s number, kept:
#: an oversized day must cost a bounded call, not a proportional one.
JOURNAL_CHARS = 6000

#: Everything in a job's call that isn't the journal — the system prompt, the
#: goals or facts a job pulls in, and the completion coming back. A flat
#: allowance beats a per-job estimate here: the budget only has to be right
#: enough to stop a runaway night, and being wrong high costs a job its turn.
PROMPT_OVERHEAD_CHARS = 4000


# --------------------------------------------------------------------- ledger


class JobLedger:
    """Which days each job has already done — `state/dream_jobs.json`.

    Separate from `state/dream_progress.json`, which belongs to the
    consolidator and predates this file. Two ledgers rather than a migration:
    the consolidation ledger is the one thing in here that already exists in
    every shipped vault, and moving it would mean a night that re-consolidates
    six months because the new reader looked in the new place and found nothing.
    """

    def __init__(self, path):
        self.path = path
        self._data = read_json(path, {}) or {}
        self._data.setdefault("jobs", {})

    def _job(self, name: str) -> dict:
        return self._data["jobs"].setdefault(name, {"days": [], "runs": 0})

    def done(self, name: str) -> set[str]:
        return set(self._job(name).get("days", []))

    def mark(self, name: str, day: str) -> None:
        job = self._job(name)
        if day not in job["days"]:
            job["days"].append(day)

    def note_run(self, name: str, *, at: str, result: str) -> None:
        job = self._job(name)
        job["runs"] = int(job.get("runs", 0)) + 1
        job["last_run"] = at
        job["last_result"] = result[:200]

    def summary(self, name: str) -> dict:
        job = dict(self._job(name))
        job["days"] = len(job.get("days", []))
        return job

    def save(self) -> None:
        write_json(self.path, self._data)


# -------------------------------------------------------------------- context


@dataclass
class Exchange:
    """One utility call a job made, kept verbatim for the debug page."""
    job: str
    system: str
    user: str
    completion: str

    def as_dict(self) -> dict:
        return {"job": self.job, "system": self.system, "user": self.user,
                "completion": self.completion}


@dataclass
class DreamContext:
    """Everything a job is handed, and the only way it reaches anything.

    A job never touches the vault path, the clock, or the model directly. That
    is not ceremony: it is what makes `dry_run` a property of the pipeline
    rather than something every job has to remember to honour, and what lets
    the debug page replay a job against a day of its choosing.
    """
    vault: MindVault
    store: object                      # the Build #1 FileMemoryStore
    clock: Clock
    goals: object | None = None        # GoalStore
    workspace: Workspace | None = None
    skills: SkillStore | None = None
    utility: UtilityCall | None = None
    selfie: Callable[[dict], None] | None = None   # SelfieLab.start, or None
    audit: Callable[..., None] | None = None       # Guard.audit, or None
    # Who the prompts are about. Not decoration: the episodic journal is a
    # transcript of two people, so a prompt that does not say which one is
    # writing gets an entry in the wrong voice — see DIARY_SYSTEM.
    char_name: str = "she"
    user_name: str = "the user"
    #: Renders the persona blocks a job's prompt opens with (§22.4). A callable
    #: rather than a string because the runner is built once at boot and a soul
    #: can change under it — through the self-edit gate, or because the user
    #: edited PERSONA.md at lunchtime. `MindLoop._soul_text` does the caching.
    soul_text: Callable[[], str] | None = None
    #: This job's appetite for it, set by the runner before each job from the
    #: job's own `soul` attribute. Not one switch for the night: the diary and
    #: the stock-take are hers and must sound like it, while consolidation is
    #: extraction and `facts.md` should read the same whoever distilled it.
    soul: str = "off"                  # full | off
    day: str = ""                      # the day this run is working on
    dry_run: bool = False
    job: str = ""                      # set by the runner before each job
    exchanges: list[Exchange] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ model

    async def ask(self, system: str, user: str) -> str:
        """One utility-model call, recorded. Returns "" when there is no model.

        Every job goes through here rather than holding `utility` itself, so
        the transcript is complete by construction — a job cannot make a call
        the debug page doesn't see, and there is one place that decides what
        happens when the small model isn't there.
        """
        if self.utility is None:
            self.exchanges.append(Exchange(self.job, system, user,
                                           "(no utility model configured)"))
            return ""
        # Who is writing this (§22.4). Fused onto the system message here, not
        # inside each job's prompt constant, so that a job added by dropping a
        # file in `vault/dreams/` gets it for free and cannot forget to. The
        # `Exchange` below records the fused text, which is the point: the debug
        # page has to show the prompt that was actually sent, or the transcript
        # is a description of a different call.
        if self.soul != "off" and self.soul_text is not None:
            try:
                preamble = self.soul_text()
            except Exception:  # noqa: BLE001 — an absent self is not a dead job
                log.warning("DREAM job %s: no soul preamble", self.job,
                            exc_info=True)
                preamble = ""
            if preamble:
                system = f"{preamble}\n\n{system}".strip()
        try:
            out = await self.utility([{"role": "system", "content": system},
                                      {"role": "user", "content": user}])
        except Exception:  # noqa: BLE001 — a failed job retries tomorrow
            log.exception("DREAM job %s: the utility call failed", self.job)
            self.exchanges.append(Exchange(self.job, system, user, "(call failed)"))
            raise
        self.exchanges.append(Exchange(self.job, system, user, out))
        return (out or "").strip()

    # ------------------------------------------------------------------- read

    def _journal_path(self, day: str):
        day = canonical_day(day)
        return self.vault.vault / "memory" / "episodic" / f"{day}.md"

    def journal(self, day: str, *, limit: int = JOURNAL_CHARS) -> str:
        path = self._journal_path(day)
        if not path.is_file():
            return ""
        return relabel(path.read_text(encoding="utf-8")[:limit])

    def journal_bytes(self, day: str) -> int:
        try:
            return self._journal_path(day).stat().st_size
        except OSError:
            return 0

    def finished_days(self) -> list[str]:
        """Every day with a journal except today's, which is still being
        written and is therefore not a day yet (`dream.py`'s rule, shared)."""
        episodic = self.vault.vault / "memory" / "episodic"
        if not episodic.is_dir():
            return []
        today = day_of(self.clock.now())
        return sorted(p.stem for p in episodic.glob("*.md")
                      if is_canonical_day(p.stem) and p.stem < today)

    def facts(self, *, limit: int = 2000) -> str:
        return self.vault.read("memory/semantic/facts.md")[-limit:]

    # ------------------------------------------------------------------ write

    def put(self, rel: str, text: str) -> None:
        """Write to her desk — the only output path a job has.

        Jobs write to `workspace/`, never to `memory/` or `soul/`. A nightly
        job that could append to semantic memory would be a second, unaudited
        consolidator; one that could touch `soul/` would be the self-edit gate
        with the gate taken off. Consolidation writes to `memory/` because it
        *is* the consolidator, through its own long-standing path.

        A dry run records the write and performs none of it, which is what
        makes the debug page's "test" button safe to press on a live vault.

        No `mark_dirty()`: the desk is not versioned (§34.1), so a diary entry
        is not a commit. What a night *did* still reaches `git log`, through the
        journal line the job returns and the ledger the runner saves.
        """
        self.writes.append(rel)
        if self.dry_run or self.workspace is None:
            return
        started = self.clock.now()
        self.workspace.write(rel, text)
        # Her hands wrote a file, so the audit says so — the same line
        # `write_note` leaves when she does it mid-conversation (§7.3), and the
        # reason the Tools page can answer "what did last night actually do to
        # this vault" without knowing DREAM exists. Dry runs write nothing and
        # so claim nothing; that transcript is on the debug page already.
        self.note_call("write_note", {"path": rel, "bytes": len(text)},
                       result=f"wrote {rel}",
                       duration_ms=(self.clock.now() - started) * 1000.0)

    def soul_chars(self) -> int:
        """How many characters of persona this job's prompt will carry (§22.4).

        Rendering it to measure it is not waste: `MindLoop._soul_text` caches,
        so the estimate and the call that follows it share one render.
        """
        if self.soul == "off" or self.soul_text is None:
            return 0
        try:
            return len(self.soul_text())
        except Exception:  # noqa: BLE001
            return 0

    def corr_id(self) -> str | None:
        """The unit of work this job is running inside (`world/correlate.py`).

        Only the camera needs it, and needs it badly: a render finishes minutes
        after the job that asked, so the `corr_id` carried on the contract is
        the only thing that later joins the photo to the call that started it.
        Without it the Tools page shows a `take_selfie` line with no picture
        under it and a picture nothing points at.
        """
        return correlate.stamp().get("corr_id")

    def note_call(self, tool: str, args: dict, *, result: str = "",
                  duration_ms: float = 0.0) -> None:
        """One audit line for something a job did with her hands.

        Never raises: an observation must not be the reason a night fails, and
        the guard's own `audit` already holds to that — this only has to not
        undo it when the audit seam is absent, as it is in most tests.
        """
        if self.audit is None:
            return
        try:
            self.audit(tool, args, "ok", duration_ms, result)
        except Exception:  # noqa: BLE001
            log.exception("DREAM job %s: the audit line failed", self.job)


# ----------------------------------------------------------------------- jobs


@dataclass
class JobReport:
    """What one job did with one day.

    `days` and `changed` are different questions and the difference is
    load-bearing. `days` is *handled* — the job looked at that day and has no
    further business with it, so the ledger records it and the backlog shrinks.
    `changed` is *produced something* — a file, a fact, a picture.

    Conflating them costs you the ladder. A strategy review on a night with no
    open goals rightly writes nothing; if writing nothing left the day unmarked,
    that night would stay in the backlog forever, the DREAM → DORMANT
    transition would never fire, and she would spend every night from then on
    re-deciding not to write the same note.
    """
    name: str
    days: list[str] = field(default_factory=list)
    changed: bool = False
    note: str = ""                  # the journal line, if the job earned one
    result: str = "nothing to do"
    failed: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "days": self.days, "changed": self.changed,
                "note": self.note, "result": self.result, "failed": self.failed}


class DreamJob:
    """One thing that happens at night.

    Subclass, set the four class attributes, implement `work`. The backlog
    machinery, the ledger and the budget are the runner's problem.
    """

    #: Stable id: the ledger key, the API's `job=` parameter, the trace label.
    name = ""
    #: What the debug page calls it.
    title = ""
    #: One line, for the debug page and for you six months from now.
    description = ""
    #: Higher runs first, and so gets the night's budget first.
    priority = 0.5
    #: True  → the backlog is every finished day this job hasn't seen.
    #: False → it runs once a night, on the most recent finished day.
    per_day = True
    #: True  → the job keeps its own progress and the runner's ledger must not
    #: also record it. Only `consolidate` does, because `dream_progress.json`
    #: predates this file and is the ledger every shipped vault already has.
    owns_ledger = False
    #: Whether this job's prompt opens with who she is (§22.4). `full` for the
    #: jobs that write in her voice about her own day; `off` for the ones doing
    #: mechanical extraction, where a persona would be a thumb on the scale.
    #: Overridable per character from `vault/dreams/<name>.md`.
    soul = "full"
    #: Set from a job file's `enabled:`. A separate attribute rather than a
    #: mutated `enabled()` because two jobs already override that method for
    #: reasons of their own — the selfie needs a camera — and a file must be
    #: able to say "not tonight" without arguing with `SELFIE_BACKEND=off`.
    #: Both have to agree: a file can switch a job off, never force one on that
    #: the house has no backend for.
    _enabled = True
    #: A job file's body, when one replaced this job's prompt. Empty = the
    #: prompt compiled into this file, which is also what a fresh vault gets.
    prompt_override = ""

    def enabled(self, cfg) -> bool:
        """Config's say, and the character's. On whenever DREAM itself is,
        unless her own `vault/dreams/<name>.md` says otherwise."""
        return self._enabled

    def system(self, default: str) -> str:
        """This job's system prompt: hers if she wrote one, else the built-in.

        Every job asks for its prompt through here, so overriding one is a file
        drop rather than a patch — and so the override cannot silently miss a
        job that built its prompt some other way.
        """
        return self.prompt_override or default

    def backlog(self, ctx: DreamContext, ledger: JobLedger) -> list[str]:
        days = ctx.finished_days()
        done = ledger.done(self.name)
        if self.per_day:
            return [d for d in days if d not in done]
        # A once-a-night job asks one question: has the most recent finished day
        # been seen? Not "which days are unseen" — filtering the whole history
        # and taking the last of it looks equivalent and is not: it hands back
        # yesterday tonight, the day before tomorrow night, and walks backwards
        # through the archive one night at a time, never emptying. Whatever it
        # skipped is skipped on purpose; ninety nights of catch-up strategy
        # reviews would be ninety calls to say the same thing.
        return [d for d in days[-1:] if d not in done]

    def cost(self, ctx: DreamContext, day: str) -> int:
        """Rough tokens this day will cost the model — `dream.py`'s chars/4,
        over what the prompt will actually carry.

        That is the journal *capped at JOURNAL_CHARS*, not the file. Billing a
        day for bytes the model never sees is what the budget governor is not
        for: one talkative 180KB day is a ~1.7k-token prompt, and charging it
        45k emptied the night's whole allowance on a single diary entry and
        stalled every job queued behind it.
        """
        read = min(ctx.journal_bytes(day), JOURNAL_CHARS)
        # …plus the persona blocks, when this job carries them (§22.4). Not
        # optional accounting: the preamble is over a thousand tokens, and §21.2
        # runs the night's first item however big it is. Underpricing the first
        # job means the ceiling is hit by the *second*, and every cheaper job
        # queued behind it starves on a night that looked affordable.
        return max(64, (read + PROMPT_OVERHEAD_CHARS + ctx.soul_chars()) // 4)

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        raise NotImplementedError

    def as_dict(self) -> dict:
        return {"name": self.name, "title": self.title,
                "description": self.description, "priority": self.priority,
                "per_day": self.per_day, "soul": self.soul,
                # Where this job's prompt came from. Named `from_file` and not
                # `custom`, which is what it said first and was wrong the moment
                # a seeded vault reported every job as customised: the seeders
                # write the built-in prompts *as* files, so "there is a file" and
                # "somebody changed it" are different claims. This is the first,
                # and it is the one the debug page needs — it says where to go
                # and look.
                "from_file": bool(self.prompt_override)}


class ConsolidateJob(DreamJob):
    """The original DREAM: a day's journal → the few durable facts (§21).

    The one job that runs without her persona in front of it (`soul = "off"`).
    Distilling "they have a sister called Mei" is extraction, and `facts.md` is
    the store every other job and every turn reads *from* — a fact coloured by
    the mood of the character who wrote it down is a fact the next character to
    read this vault inherits wrong. The flag is overridable per character like
    any other, for whoever disagrees.

    Wraps `DreamConsolidator` rather than reimplementing it, and delegates its
    backlog and its ledger there too — that class has owned
    `state/dream_progress.json` since Build #5 and there is no version of this
    refactor worth a vault that forgets what it already consolidated.

    Runs first, at the highest priority, because the jobs below read
    `facts.md`: on any given night the diary should be able to see what
    consolidation just learned.
    """

    soul = "off"

    name = "consolidate"
    title = "Consolidate"
    description = ("Compact each finished day's journal into the few durable "
                   "facts worth keeping, deduped and indexed at high salience.")
    priority = 1.0
    owns_ledger = True

    def __init__(self, consolidator: DreamConsolidator):
        self.consolidator = consolidator

    def backlog(self, ctx, ledger) -> list[str]:
        return self.consolidator.backlog()

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        # The consolidator is budgeted and resumable in its own right, so one
        # "day" of this job is one day of its backlog: hand it a budget that
        # takes exactly the oldest day and let the runner decide about the next.
        report = await self.consolidator.consolidate(token_budget=1)
        out = JobReport(name=self.name, days=list(report.days_processed),
                        changed=bool(report.facts_added))
        out.result = (f"{len(report.days_processed)} day(s), "
                      f"{report.facts_added} fact(s)")
        if report.days_processed:
            out.note = (f"slept on it: folded {', '.join(report.days_processed)} "
                        "into what I keep")
        return out


#: Formatted with `char` and `user` before it is sent, and the naming is
#: load-bearing rather than polite.
#:
#: The episodic journal is a transcript of *two* people — `[she]` lines for her
#: own acts, `user: … ⇄ {char}: …` for the exchanges. Handed that with an
#: unqualified "write your diary", a small model reads the transcript from the
#: outside and picks the wrong side of it: the first live run of this job
#: produced an entry in the user's voice, about waiting for *her* to reply.
#: Saying whose diary it is, and what the two kinds of line mean, fixes it.
#: A journal line reads `### HH:MM  you: …  ⇄  rikku: …`, and that `you:` is
#: the *other* person — the label is written from her point of view, for a human
#: reading her diary. Handed to a model under a system prompt that opens "You
#: are Rikku", the same word now points at two different people in one context,
#: and the model resolves it the way the transcript is denser about: it writes
#: her diary as the client who came to her yoga class.
#:
#: No amount of prose fixes that. Two rounds of increasingly explicit wording —
#: naming her, labelling both halves, spelling out which was hers — all lost to
#: one pronoun. So the transcript stops being ambiguous instead: her own half is
#: relabelled ME and the other person's THEM before either reaches a prompt.
#: Neither word can be read as pointing anywhere else.
_EXCHANGE = re.compile(r"^(\s*###\s*\d{1,2}:\d{2}\s+)([^:⇄]{1,40}):")
_REPLY = re.compile(r"^(\s*)([^:⇄]{1,40}):")


def relabel(text: str) -> str:
    """Rewrite a journal's speaker labels to ME and THEM.

    Whatever the two sides were called — the user's configured name, the bare
    `you`, her own name — the halves are positional, so the labels can be
    replaced without knowing either. Lines with no ⇄ are her own acts and are
    left alone, `[she]` marker included.
    """
    out = []
    for line in text.splitlines():
        if "⇄" in line:
            theirs, _, hers = line.partition("⇄")
            theirs = _EXCHANGE.sub(r"\1THEM:", theirs, count=1)
            hers = _REPLY.sub(r"\1ME:", hers, count=1)
            line = f"{theirs}⇄{hers}"
        out.append(line)
    return "\n".join(out)


JOURNAL_FORMAT = (
    "Lines marked [she] are things you did alone. Every other line is one "
    "exchange with two halves split by ⇄: the half before it, labelled "
    "'THEM:', is the other person speaking to you, and the half after it, "
    "labelled 'ME:', is your own reply. Your ME half is written the way it was "
    "performed — dialogue, moods in square brackets, and stage directions "
    "between asterisks that describe you from the outside, sometimes as 'she' "
    "and sometimes by your own name. All of it is yours: '*{char} tilted her "
    "head*' inside your half is you tilting your head, not someone else "
    "watching you do it.")

DIARY_SYSTEM = (
    "You are {char}. Below is your own journal for the day just past. " +
    JOURNAL_FORMAT +
    " Write YOUR private diary entry about that day — yours, for nobody else to "
    "read, from your side only. You are {char}; never write as the person "
    "labelled 'THEM'. Two short paragraphs at most: what the day was actually "
    "like and what you made of it, not a summary of events and not a report to "
    "anyone. First person, your own voice, no preamble, no heading. If the day "
    "held nothing worth a diary entry, output NOTHING.")


class DiaryJob(DreamJob):
    """A private entry per day, on her desk (`workspace/diary/`).

    Distinct from the journal, which is a log of acts, and from facts, which is
    what she keeps. This is what she *made* of a day — the thing you cannot
    reconstruct from either of the other two, and the reason the diary lives on
    the desk rather than in memory: it is writing, not a record.
    """

    name = "diary"
    title = "Diary"
    description = ("Write a short private diary entry for each finished day — "
                   "what the day was like, not what happened in it.")
    priority = 0.6

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        text = ctx.journal(day)
        out = JobReport(name=self.name, days=[day])
        if not text.strip():
            out.result = "nothing happened that day"
            return out
        entry = await ctx.ask(
            self.system(DIARY_SYSTEM).format(char=ctx.char_name,
                                             user=ctx.user_name),
            f"The day: {day}\n\n{text}")
        if not entry or entry.strip().upper().startswith("NOTHING"):
            out.result = "nothing worth writing down"
            return out
        ctx.put(f"diary/{day}.md", f"# {day}\n\n{entry}\n")
        out.changed = True
        out.result = f"wrote {len(entry)} chars"
        out.note = f"wrote a diary entry for {day}"
        return out


#: The only one of these that never named her at all. The other three at least
#: said "You are {char}"; this one opened "You are taking stock of your own
#: goals", which is a sentence any character could have thought and therefore a
#: sentence none of them thought in particular. With the persona blocks above it
#: (§22.4) the naming is redundant, and it is kept anyway — the same belt-and-
#: braces §21.2 records for the journal's pronouns, where two rounds of clever
#: wording lost to one plain label.
STRATEGY_SYSTEM = (
    "You are {char}, taking stock of your own goals, alone, with no one "
    "waiting. Look at what you're carrying and say plainly: what actually "
    "matters here, what has gone stale, and the one thing worth doing next — "
    "and let it be *your* judgement of what matters, not a neutral audit. "
    "Under 150 words. No preamble, no headings, no numbered list — just the "
    "thinking.")


class StrategyJob(DreamJob):
    """Standing back from her own goals, once a night.

    Not per-day, because the answer barely changes overnight and ninety nights
    of backlog would be ninety near-identical reviews. The output is a note on
    the desk, never a change to the goal store: DREAM that could silently
    reprioritise her goals would make the goals page a thing you read *after*
    the fact rather than a thing you can trust.
    """

    name = "strategy"
    title = "Strategy"
    description = ("Once a night, stand back from the open goals and write down "
                   "what matters, what's gone stale, and what to do next.")
    priority = 0.4
    per_day = False

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        out = JobReport(name=self.name, days=[day])
        open_goals = []
        if ctx.goals is not None:
            open_goals = [g for g in ctx.goals.open_goals()]
        if not open_goals:
            out.result = "no open goals to think about"
            return out
        listing = "\n".join(
            f"- {g.text} (kind {g.kind}, priority {g.priority}, state {g.state}"
            + (f", due {g.due}" if g.due else "") + ")"
            for g in open_goals[:20])
        thinking = await ctx.ask(
            self.system(STRATEGY_SYSTEM).format(char=ctx.char_name),
            f"Today is {day}.\n\nYour open goals:\n{listing}\n\n"
            f"What you know:\n{ctx.facts()}")
        if not thinking:
            out.result = "nothing came of it"
            return out
        ctx.put(f"strategy/{day}.md", f"# Taking stock — {day}\n\n{thinking}\n")
        out.changed = True
        out.result = f"reviewed {len(open_goals)} goal(s)"
        out.note = "stood back and looked at what I'm carrying"
        return out


SELFIE_SYSTEM = (
    "You are {char}. Below is your own journal for this day. " +
    JOURNAL_FORMAT +
    " Pick the one moment you'd want a picture of *yourself* in, and describe "
    "that picture the way you'd describe a photograph: where you are, the "
    "light, what you're doing, how you're sitting. The picture is of you, seen "
    "as you are in it — not of the other person, and not the view from where "
    "they were standing. One paragraph, no preamble, no explanation of why you "
    "chose it. If nothing in the day suggests a picture, output NOTHING.")


class SelfieJob(DreamJob):
    """A picture from the day just past, dreamt rather than asked for.

    With `SELFIE_BACKEND=off` it is not in the night's list at all — the rule
    the tool server already follows. With the camera on, the render is started
    and never awaited (§7.6), so the picture arrives in the chat whenever it is
    done, exactly as a daytime selfie does.

    The description always lands on her desk first, even when the render is
    skipped (a dry run, or a forge that failed to build): the dreaming is the
    job, the render is what a backend adds to it.
    """

    name = "selfie"
    title = "Selfie dream"
    description = ("Pick the moment from the day that wants a picture, describe "
                   "it, and send it to the camera.")
    priority = 0.3
    per_day = False

    def enabled(self, cfg) -> bool:
        return (self._enabled
                and getattr(cfg, "selfie_backend", "off") != "off")

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        out = JobReport(name=self.name, days=[day])
        text = ctx.journal(day)
        if not text.strip():
            out.result = "nothing happened that day"
            return out
        look = await ctx.ask(
            self.system(SELFIE_SYSTEM).format(char=ctx.char_name,
                                              user=ctx.user_name),
            f"The day: {day}\n\n{text}")
        if not look or look.strip().upper().startswith("NOTHING"):
            out.result = "no picture in that day"
            return out
        ctx.put(f"dreams/{day}-selfie.md", f"# A picture from {day}\n\n{look}\n")
        out.changed = True
        if ctx.dry_run:
            # Say which of the two reasons stopped it. These used to share a
            # branch, so a dry run on a machine with a perfectly good camera
            # reported "no camera wired" — an answer that sends you off
            # debugging the forge instead of unticking the box.
            out.result = ("described it (dry run — not sent to the camera)"
                          if ctx.selfie is not None else
                          "described it (dry run; no camera wired either)")
            out.note = f"dreamt a picture of {day}"
        elif ctx.selfie is not None:
            # start-don't-await (§7.6): the render happens off-tick and arrives
            # in the chat when it's done, exactly as a daytime selfie does
            ctx.selfie({"id": f"dream-{day}", "kind": "selfie", "look": look,
                        "status": "started", "_dream": True,
                        "_corr_id": ctx.corr_id()})
            # The audit line goes in at dispatch, not at delivery — this is
            # start-don't-await, and the render lands minutes later. Same shape
            # as the `take_selfie` line a daytime shot leaves; the photo it
            # produced joins it on the Tools page through the shared selfie id.
            ctx.note_call("take_selfie", {"look": look, "id": f"dream-{day}"},
                          result="started")
            out.result = "described it and sent it to the camera"
            out.note = f"dreamt a picture of {day} and had it made"
        else:
            out.result = "described it (no camera wired)"
            out.note = f"dreamt a picture of {day}"
        return out


#: The night's roster. `consolidate` is constructed by the runner because it
#: needs the consolidator; the rest take no arguments. Adding a job is adding a
# --- jobs a character owns: `vault/dreams/<name>.md` (SPEC §21.2) --------------
#
# The roster used to be this file and only this file: a Python tuple, and an
# `enabled()` that returned True for everything. So every character's night was
# the same night — the same four jobs, in the same order, asking the same
# questions — which is a strange shape for the one part of the system whose
# entire subject is what *this* character made of *her* day.
#
# A job file is a `SKILL.md` pointed at the night (§34.3): frontmatter that says
# what the job is and when it runs, over a body that IS the system prompt. The
# format is borrowed rather than invented because it is already the format she
# writes in, already versioned, and already survives being edited by hand.

#: Frontmatter keys a file may set on a builtin. Deliberately not `name` (it is
#: the match key) and not anything that would change a builtin's `work` — a file
#: may retune a job, never re-implement one. `DiaryJob` keeps `relabel()` and its
#: day bookkeeping no matter what its file says, because those are correctness
#: and the prompt is taste.
JOB_FILE_KEYS = ("title", "description", "priority", "per_day", "enabled", "soul")


@dataclass
class JobFile:
    """One `vault/dreams/<name>.md`, parsed."""
    name: str
    front: dict
    prompt: str

    def applies_to(self, job: "DreamJob") -> None:
        """Overlay this file's frontmatter onto a builtin, in place."""
        for key in JOB_FILE_KEYS:
            if key not in self.front:
                continue
            value = self.front[key]
            if key == "priority":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            elif key in ("per_day", "enabled"):
                value = value is not False
            elif key == "soul":
                value = "off" if str(value).lower() in ("off", "none", "false") else "full"
            else:
                value = str(value)
            setattr(job, key if key != "enabled" else "_enabled", value)
        if self.prompt.strip():
            job.prompt_override = self.prompt.strip()


def load_job_files(root: Path) -> list[JobFile]:
    """Every readable `<name>.md` under `vault/dreams/`.

    A mangled file costs that one job and never the night — §34.3's rule for a
    broken `SKILL.md`, and it matters more here: these files are edited by hand
    at midnight by someone who wanted a different diary, and one stray colon
    must not be why nothing consolidated.
    """
    if not root.is_dir():
        return []
    out: list[JobFile] = []
    for path in sorted(root.glob("*.md")):
        # The folder's own README is documentation, not a job. Skipped by name
        # *and* by the frontmatter rule below, because the seeders put a README
        # in every folder they make (§34.1) and one that ran as a nightly prompt
        # would ask the model to be a help page, every night, forever.
        if path.name.startswith(".") or path.stem.lower() == "readme":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            log.warning("dream job %s could not be read; skipping", path.name)
            continue
        m = FRONTMATTER_RE.match(text)
        if not m:
            # A job declares itself or it is not one. Without this, any stray
            # `.md` somebody drops in here — notes, a draft, a paste — becomes a
            # prompt the model is handed at 4am.
            log.warning("dream job %s has no frontmatter; skipping", path.name)
            continue
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            log.warning("dream job %s has unreadable frontmatter; skipping",
                        path.name)
            continue
        front: dict = loaded if isinstance(loaded, dict) else {}
        body = text[m.end():]
        name = str(front.get("name") or path.stem).strip()
        if not name:
            continue
        out.append(JobFile(name=name, front=front, prompt=body))
    return out


class PromptJob(DreamJob):
    """A job that is nothing but a prompt — the shape a new one takes.

    Read the day's journal, ask the question the file asks, write the answer to
    the desk. That covers most of what the built-in jobs do; what it cannot do
    is the reason the other three stay in Python (consolidation writes to
    `memory/`, the selfie dispatches a camera, the stock-take reads the goal
    store). Adding a fifth kind of night no longer means adding a fifth class.
    """

    owns_ledger = False

    def __init__(self, spec: JobFile):
        self.name = spec.name
        self.title = str(spec.front.get("title") or spec.name.title())
        self.description = str(spec.front.get("description") or "")
        try:
            self.priority = float(spec.front.get("priority", 0.3))
        except (TypeError, ValueError):
            self.priority = 0.3
        self.per_day = spec.front.get("per_day", True) is not False
        self._enabled = spec.front.get("enabled", True) is not False
        self.soul = ("off" if str(spec.front.get("soul", "full")).lower()
                     in ("off", "none", "false") else "full")
        self.output = str(spec.front.get("output") or f"{self.name}/{{day}}.md")
        self.prompt_override = spec.prompt.strip()

    def enabled(self, cfg) -> bool:
        return self._enabled

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        out = JobReport(name=self.name, days=[day])
        journal = ctx.journal(day)
        if not journal.strip():
            out.result = "nothing in the journal for that day"
            return out
        system = self.system("").format(char=ctx.char_name,
                                        user=ctx.user_name)
        answer = await ctx.ask(system, f"Today is {day}.\n\n{journal}")
        if not answer:
            out.result = "nothing came of it"
            return out
        # `{day}` is the only substitution, and `Workspace.resolve` refuses the
        # rest: a path out of the desk, a dotfile, a `..`. A job file is written
        # by the person who owns the vault, but it is still a path from a file.
        ctx.put(self.output.format(day=day), f"{answer}\n")
        out.changed = True
        out.result = f"wrote {len(answer)} chars"
        out.note = f"{self.title.lower()}: wrote something for {day}"
        return out


#: class above and a name here.
BUILTIN_JOBS: tuple[type[DreamJob], ...] = (DiaryJob, StrategyJob, SelfieJob)


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
    writes: list[str] = field(default_factory=list)
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
        by_job: dict[str, list[JobReport]] = {}
        for j in self.jobs:
            if j.changed or j.failed:
                by_job.setdefault(j.name, []).append(j)
        if not by_job:
            return "DREAM: nothing to do"
        parts = []
        for name, runs in by_job.items():
            failed = [r for r in runs if r.failed]
            if failed:
                parts.append(f"{name}: {failed[0].failed}")
            elif len(runs) == 1:
                parts.append(f"{name}: {runs[0].result}")
            else:
                parts.append(f"{name}: {len(runs)} days")
        return ("DREAM — " + "; ".join(parts)
                + (", budget spent — backlog remains" if self.exhausted_budget
                   else ""))

    def as_dict(self) -> dict:
        return {"jobs": [j.as_dict() for j in self.jobs],
                "exhausted_budget": self.exhausted_budget,
                "nothing_to_do": self.nothing_to_do,
                "dry_run": self.dry_run,
                "writes": self.writes,
                "summary": self.summary,
                "exchanges": [e.as_dict() for e in self.exchanges]}


class DreamRunner:
    """The pipeline. Owns the roster, the ledger and the night's budget."""

    def __init__(self, vault: MindVault, store, clock: Clock, cfg, *,
                 consolidator: DreamConsolidator,
                 goals=None, workspace: Workspace | None = None,
                 skills: SkillStore | None = None,
                 utility: UtilityCall | None = None,
                 selfie: Callable[[dict], None] | None = None,
                 audit: Callable[..., None] | None = None,
                 soul_text: Callable[[], str] | None = None):
        self.vault = vault
        self.store = store
        self.clock = clock
        self.cfg = cfg
        self.goals = goals
        self.workspace = workspace
        self.skills = skills
        self.utility = utility
        self.selfie = selfie
        self.audit = audit
        self.soul_text = soul_text
        self.ledger = JobLedger(vault.vault / "state" / "dream_jobs.json")
        self.jobs: list[DreamJob] = [ConsolidateJob(consolidator)]
        self.jobs += [cls() for cls in BUILTIN_JOBS]
        self._apply_job_files()
        self.jobs.sort(key=lambda j: j.priority, reverse=True)

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
        from yurios.characters.importer import DREAMS_README, _seed_job_files
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text(DREAMS_README, encoding="utf-8")
        for fname, body in _seed_job_files().items():
            (root / fname).write_text(body, encoding="utf-8")
        log.info("seeded the dream roster into %s", root)

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
                    job = PromptJob(spec)
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
            soul_text=self.soul_text,
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
        async with _NIGHT_LOCK:
            return await self._run(token_budget=token_budget, only=only,
                                   day=day, dry_run=dry_run)

    async def _run(self, *, token_budget: int, only: str | None,
                   day: str | None, dry_run: bool) -> NightReport:
        """The night itself, run under the lock — see `run` for the contract."""
        report = NightReport(dry_run=dry_run)
        jobs = [j for j in self.enabled_jobs() if only is None or j.name == only]
        if only is not None and not jobs:
            raise KeyError(f"no dream job called {only!r}")
        spent = 0
        touched = False

        for job in jobs:
            ctx = self._context(day="", dry_run=dry_run, job=job.name,
                                soul=job.soul)
            try:
                pending = [day] if day else job.backlog(ctx, self.ledger)
            except Exception:  # noqa: BLE001
                log.exception("DREAM job %s: backlog failed", job.name)
                continue
            for target in pending:
                cost = job.cost(ctx, target)
                # `spent` is 0 only for the very first item of the night, which
                # always runs however big it is — `dream.py` explains why at
                # length, and the rule matters more here: with several jobs
                # queued, a veto on the first one would starve every job behind
                # it too.
                if spent and spent + cost > token_budget:
                    report.exhausted_budget = True
                    break
                ctx.day = target
                out = await self._run_one(job, ctx, target)
                report.jobs.append(out)
                spent += cost
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
            report.writes.extend(ctx.writes)
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
