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
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from yurios.kernel import correlate
from yurios.kernel.clock import Clock
from yurios.app.providers.admission import InferenceBusy

from .dream import DreamConsolidator
from .goals import echoes
from .hands import parse_intent
from .journal import canonical_day, is_canonical_day
from .util import day_of, iso_of, read_json, write_json
from .vaultio import MindVault
from .workspace import FRONTMATTER_RE, SkillStore, Workspace

log = logging.getLogger("mind.dreamjobs")

#: `...` rather than `[list[dict]]`: a job may pass per-call model parameters
#: (see `DreamContext.ask`), which every provider's `complete` already accepts
#: as `**params` and the narrower signature quietly forbade.
UtilityCall = Callable[..., Awaitable[str]]

#: How much of one day's journal reaches a prompt. `dream.py`'s number, kept:
#: an oversized day must cost a bounded call, not a proportional one.
JOURNAL_CHARS = 6000

#: Everything in a job's call that isn't the journal — the system prompt, the
#: goals or facts a job pulls in, and the completion coming back. A flat
#: allowance beats a per-job estimate here: the budget only has to be right
#: enough to stop a runaway night, and being wrong high costs a job its turn.
PROMPT_OVERHEAD_CHARS = 4000

#: Slack left between the report prompt and the model's context window when the
#: retry works out how much room the answer has. chars/4 is an estimate and the
#: tokeniser is entitled to disagree with it; a retry that overshoots the window
#: fails the same way the first attempt did.
REPORT_WINDOW_MARGIN = 512

#: How long the reasoning pass before the report may run, where the server
#: honours it — `characters/optimize.py` climbs the same ladder on the same
#: failure, and it is the knob that leaves room for the answer without turning
#: the model into a different one.
#:
#: Unset by default, and that is still a measurement rather than caution. LM
#: Studio 0.4.8 added `reasoning_effort` to the OpenAI-compatible endpoint, so
#: it is no longer thrown away in transit — but *reaching* the server and
#: *changing the answer* are two different claims, and only the first is
#: reliably true. The blunt version, from LM Studio's own native endpoint,
#: which unlike the compatible ones refuses what it cannot do:
#:
#:     Reasoning setting 'low' is not supported by model '…'.
#:     Supported settings: 'off', 'on'.
#:
#: Two positions, not a ladder. `/api/v1/models` says the same thing ahead of
#: time in `capabilities.reasoning.allowed_options`, and it is worth reading
#: before believing any of this: a model that advertises `low`/`medium` will do
#: more with the hint than the one measured here.
#:
#: What the OpenAI-compatible endpoints do with a rung the model hasn't got is
#: take it and quietly round it off. Four runs of one question at each rung:
#: `low` spent 61–127 reasoning tokens (mean 94), `medium` 80–125 (mean 102),
#: `high` 59–105 (mean **82**). The spread inside one rung is twice the gap
#: between rungs and the means come out backwards — sampling noise with a
#: parameter name on it. One run per rung says the opposite and says it
#: convincingly, which is the trap: this needs repeats to measure at all.
#:
#: Nor is asking in words a way round it, which is the obvious next thought.
#: Effort moved out of the parameter and into the system prompt: `Reasoning:
#: low` and `Reasoning: high` came out four tokens apart (82 and 86, against a
#: spread of 43–110 inside one of them), and plain English ran backwards —
#: "think briefly" produced the *longest* pass measured anywhere, mean 168
#: against 109 for no system message at all. A reasoning model reads an
#: instruction about how hard to think as more to think about.
#:
#: So: a hint, and the only categorical setting is off. Off is 0 reasoning
#: tokens and ~2 seconds against ~90 tokens and ~15 for every thinking rung
#: alike, and it is what `thinking=False` already sends. The knobs that bite
#: everywhere are that one and the room the retry hands over.
#:
#: The values are the server's and not ours to invent. LM Studio rejects
#: anything outside `none, minimal, low, medium, high, xhigh` with a 400, and a
#: rejected parameter fails the whole call rather than degrading — which is why
#: `_as_effort` drops what it doesn't recognise instead of passing it through.
#: Note the two vocabularies do not match: the model card's `off` is spelled
#: `none` here, and `on` has no spelling at all.
REPORT_EFFORTS = ("low", "medium", "high")

#: Room to think, on top of what the report itself is worth. A ceiling bounds
#: the *call*, not the pass inside it, so a budget sized for the answer is a
#: budget the thinking eats before she writes a word — 2,500 tokens went 2,500
#: to reasoning and 0 to the report, measured. `report_max_tokens` therefore
#: means what the report is worth, and this is added to it rather than the two
#: fighting over one number.
#:
#: The size is measured too, and it is why this is twice
#: `characters/optimize.py`'s `REASONING_ALLOWANCE` rather than equal to it: a
#: local 27B handed a night's reading (4,339 prompt tokens, three pages) spent
#: **10,049 tokens** thinking and then wrote the whole report in 698. A pass
#: over a night of gathered material is simply a longer pass than a pass over
#: one character card. Asking high is free — nothing bills for a ceiling, only
#: for what is generated — and asking low costs the whole call.
REPORT_REASONING_ALLOWANCE = 12288

#: How long the writing call may take, in seconds. Not a token count — a wall
#: clock, and the one the report kept dying against: LiteLLM's own default is
#: 600s, which is generous for a turn somebody is waiting on and short for a
#: reasoning model writing a page. Measured, a report call ran **1,802 seconds**
#: and died of the timeout rather than of anything wrong with the answer, and at
#: this model's ~6.5 tokens a second 600s is under 4,000 tokens however large a
#: ceiling the call was given.
#:
#: An hour, because the first guess of half an hour was also measured and also
#: too short: the call that finally finished took **2,151 seconds** — 10,049
#: tokens of thinking and 698 of report. Nobody is waiting at 4am, and what
#: keeps the night finite is the caps above, not this. A faster model finishes
#: in a fraction of it and never notices the number.
REPORT_TIMEOUT_S = 3600

#: A research round's answer is one line of intent, so it is capped hard —
#: belt and braces beside `thinking=False`, for a model that ignores the
#: soft-switch and would otherwise think until `UTILITY_MAX_TOKENS` runs out.
ROUND_MAX_TOKENS = 600

#: A search result line as it reaches her, and how many of them she sees at
#: once. Deliberately smaller than `SEARCH_RESULTS` feeds a daytime turn: a
#: research loop makes ten of these calls and every row it keeps is a row the
#: next round re-sends.
SEARCH_SNIPPET_CHARS = 200


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
class Step:
    """One reach for a hand a job made, kept for the debug page.

    Deliberately not an `Exchange`. §21.3's contract is about *model calls* —
    the exact system message, the exact input, the raw completion — and folding
    a search into that list would make the transcript a description of calls
    that were never made. A research night is two kinds of event interleaved
    and the page has to be able to tell them apart.
    """
    job: str
    tool: str
    args: dict
    result: str
    failed: str = ""

    def as_dict(self) -> dict:
        return {"job": self.job, "tool": self.tool, "args": self.args,
                "result": self.result, "failed": self.failed}


#: Provenance prefix for a goal she filed herself, out of a night's stock-take
#: (§22.1). Load-bearing rather than cosmetic: it is what the inner-life panel
#: marks as hers, what the cap counts, and what the switch retires. `strategy:`
#: because the job is the author — the same shape as `promise:her-own-words`
#: and `maintenance:shelf`, which name the mechanism that filed them.
SELF_GOAL = "strategy:"

#: Days before a self-filed goal she never advanced lets go of itself. Not a
#: deadline she is working to — it is the `due` that makes `reconsider()` able
#: to see it as stale at all (goals.py), since an open-minded goal with no due
#: date is immortal.
SELF_GOAL_TTL_DAYS = 3.0

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
    #: `world/research.py`'s `Researcher`, or None. Held whole rather than as
    #: two loose callables because that object already owns the three things a
    #: night needs and keeps them consistent: the `SearchProvider`, the
    #: `PageFetcher`, and `shelve()` — the ingestion path that makes §7.7's
    #: "what she reads she keeps" true of the unattended hours too.
    research: object | None = None
    #: `MindLoop._deliver_report`, or None. The one way a job reaches the user
    #: (§18.2a): it files a named artifact in the inbox, waiting for the next
    #: time they look. Nullable like every other seam here — a runtime with no
    #: inbox still dreams, and still writes the report to the desk.
    deliver_report: Callable[..., None] | None = None
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
    drives: Callable[[], list[str]] | None = None
    #: This job's appetite for it, set by the runner before each job from the
    #: job's own `soul` attribute. Not one switch for the night: the diary and
    #: the stock-take are hers and must sound like it, while consolidation is
    #: extraction and `facts.md` should read the same whoever distilled it.
    soul: str = "off"                  # full | off
    #: The house config, for the jobs whose limits are clamped by it. On the
    #: context rather than passed to `work()` because every other thing a job
    #: reaches arrives this way, and a second channel would be a second place to
    #: look when a job behaves differently than its file says.
    cfg: object | None = None
    day: str = ""                      # the day this run is working on
    dry_run: bool = False
    job: str = ""                      # set by the runner before each job
    exchanges: list[Exchange] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    #: Goal ids this context filed (§22.1). Recorded like `writes` below, so a
    #: dry run can claim specifically that it filed nothing.
    filed: list[str] = field(default_factory=list)
    #: Why the last `file_goal` filed nothing: "" | off | capped | echo | dry.
    #: A night that had nothing to say and a night that was refused look the
    #: same in the log otherwise, and only some of them are worth acting on.
    goal_refusal: str = ""
    #: What this context handed to the inbox. Recorded rather than counted so a
    #: dry run can claim, specifically, that it delivered nothing.
    delivered: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ model

    async def ask(self, system: str, user: str, **params) -> str:
        """One utility-model call, recorded. Returns "" when there is no model.

        Every job goes through here rather than holding `utility` itself, so
        the transcript is complete by construction — a job cannot make a call
        the debug page doesn't see, and there is one place that decides what
        happens when the small model isn't there.

        `params` reach the provider untouched (`thinking`, `max_tokens`,
        `temperature`, `reasoning_effort`). Worth having for one reason:
        research asks a reasoning model for a single line naming its next
        search, and measured against a local 27B that question cost 1200
        reasoning tokens and 200 seconds — twelve of which is a night that never
        finishes. Thinking is right for the report and wrong for the plumbing,
        and only the job knows which call is which.
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
                                      {"role": "user", "content": user}],
                                     **params)
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

    def strategy_context(self, day: str, open_goals: list) -> str:
        """Bounded evidence for deciding what deserves to become a new goal."""
        parts: list[str] = [f"DAY UNDER REVIEW\n{day}"]
        drives: list[str] = []
        if self.drives is not None:
            try:
                drives = self.drives()[:8]
            except Exception:  # noqa: BLE001 — missing drives mean no drive block
                log.warning("strategy: character drives unavailable", exc_info=True)
        if drives:
            parts.append("DURABLE DRIVES (values, not tasks)\n" + "\n".join(
                f"- {str(drive)[:300]}" for drive in drives))
        enabled = bool(getattr(self.cfg, "mind_tools_enabled", False))
        allowlist = str(getattr(self.cfg, "mind_tool_allowlist", "") or "")
        capability = allowlist if enabled and allowlist.strip() else "thought-only"
        parts.append("AVAILABLE AUTONOMOUS CAPABILITIES\n" + capability)

        if open_goals:
            lines = []
            for goal in open_goals[:20]:
                about = str(getattr(goal, "meta", {}).get("about") or "")[:180]
                line = (f"- {str(goal.text)[:300]} | {goal.kind} | {goal.state} | "
                        f"priority {goal.priority} | from {goal.provenance}")
                if goal.due:
                    line += f" | due {goal.due}"
                if about:
                    line += f" | source: {about}"
                lines.append(line)
            parts.append("OPEN GOALS\n" + "\n".join(lines))
        else:
            parts.append("OPEN GOALS\n- (nothing open right now)")

        journal = self.journal(day, limit=3500).strip()
        if journal:
            parts.append("WHAT ACTUALLY HAPPENED THAT DAY\n" + journal)
        summary = self.vault.read("memory/summary.md")[-1600:].strip()
        if summary:
            parts.append("RECENT RELATIONSHIP CONTEXT\n" + summary)
        facts = self.facts(limit=1600).strip()
        if facts:
            parts.append("DURABLE FACTS\n" + facts)

        if self.workspace is not None:
            previous = [entry for entry in self.workspace.list("strategy")
                        if not entry.is_dir and entry.path.endswith(".md")
                        and entry.path != f"strategy/{day}.md"]
            if previous:
                latest = previous[-1].path
                note = self.workspace.read(latest, default="")[-1200:].strip()
                if note:
                    parts.append(f"PREVIOUS STRATEGY ({latest})\n{note}")
            digest = self.workspace.digest(limit=10)
            if digest:
                parts.append("DESK (paths only)\n" + digest[:1200])
        if self.skills is not None:
            catalog = self.skills.catalog(limit=8)
            if catalog:
                parts.append("KNOWN SKILLS\n" + catalog[:1000])

        rendered = "\n\n".join(parts)
        return rendered[:12000]

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

    def file_goal(self, text: str, *, day: str,
                  meta: dict | None = None) -> object | None:
        """File one goal of her own. The second output path, and the only one
        that leaves `workspace/`.

        `put` above is jailed to the desk for good reasons that all still hold.
        This is not a hole in that: it is a different act with its own limits.
        The standing objection was that a DREAM which could file goals makes the
        goals page "a thing you read *after* the fact rather than a thing you
        can trust" — but that is an argument about *legibility*, not about
        authority, and it is answered by provenance rather than by an empty
        write path. Every goal filed here says `from: strategy:<day>` on its own
        line in `goals.md`, which is what the inner-life panel prints and what
        the switch below retires. `SelfEdit.classify` already treats `goals.md`
        as a working product rather than a soul surface, applied and never
        queued; this agrees with it.

        Four limits, in the order they are checked: the switch
        (`MIND_GOAL_FILING_ENABLED`), the cap on how many of hers may be open at
        once (`MIND_SELF_GOALS_MAX`), whether she is already carrying this goal
        under another wording (`echoes`), and `GoalStore.add`'s own exact-text
        merge. Returns None when any of them refuses and leaves the reason in
        `goal_refusal`, so a job can say which silence this was rather than
        claiming a goal it did not get.
        """
        self.goal_refusal = ""
        text = (text or "").strip()
        if not text or self.goals is None:
            return None
        if not getattr(self.cfg, "mind_goal_filing_enabled", True):
            self.goal_refusal = "off"
            return None
        open_goals = list(self.goals.open_goals())
        cap = int(getattr(self.cfg, "mind_self_goals_max", 3) or 0)
        mine = [g for g in open_goals
                if str(g.provenance or "").startswith(SELF_GOAL)]
        if len(mine) >= cap:
            self.goal_refusal = "capped"
            return None
        # Against *every* open goal, not only hers. The defect is carrying the
        # same intention twice, and where the first copy came from does not
        # change that — a promise she made you out loud is as much a thing she
        # is already doing as an idea she had at 4am.
        echo = echoes(text, open_goals)
        if echo is not None:
            self.goal_refusal = "echo"
            self.note_call("file_goal", {"text": text[:200]},
                           result=f"already carrying it as {echo.id}")
            return None
        if self.dry_run:
            self.goal_refusal = "dry"
            return None
        started = self.clock.now()
        goal = self.goals.add(
            text,
            kind="task",
            # Workable without a due date, and still ranked under anything she
            # promised you out loud. `appraise_goal` scores priority * 0.6
            # against MIND_ACT_THRESHOLD, so a self-filed goal at an intuitive
            # 0.45 scores 0.27 and never gets worked at all — it would sit until
            # its own expiry. 0.68 clears the gate at 0.408 and still sits below
            # a promise-derived task's 0.42.
            priority=0.68,
            due=iso_of(self.clock.now() + SELF_GOAL_TTL_DAYS * 86400),
            # Open-minded is what makes it disposable: `reconsider()` abandons
            # exactly the stale open-minded goals, so one she never advances
            # lets go of itself and the list cannot silt up.
            commitment="open-minded",
            provenance=f"{SELF_GOAL}{day}",
            meta=meta,
        )
        self.filed.append(goal.id)
        self.note_call("file_goal", {"text": text[:200], "id": goal.id},
                       result=f"filed {goal.id}",
                       duration_ms=(self.clock.now() - started) * 1000.0)
        return goal

    def deliver(self, *, title: str, path: str, summary: str) -> None:
        """Hand one named artifact to the inbox, to be waiting when they look.

        Not a message she decided to send (§18.2a): a job file said
        `deliver: chat`, which is a standing instruction its owner wrote, and
        the inbox is where a thing owed to somebody waits. The report is on the
        desk either way — this only decides whether they are told about it.

        A dry run records the delivery and performs none of it, for the same
        reason `put` does: the debug page's test button has to be safe to press
        on a live vault.
        """
        self.delivered.append(path)
        if self.dry_run or self.deliver_report is None:
            return
        self.deliver_report(title=title, path=path, summary=summary,
                            job=self.job)

    # ------------------------------------------------------------------ hands

    def _hand(self, tool: str, args: dict, result: str, failed: str = "") -> None:
        """Record one reach, for the debug page and for `tool-logs/calls.jsonl`.

        Both audiences, one call. A night's hands are audited like her daytime
        hands (§21.2) so the Tools surface can answer "what touched this vault"
        for the unattended hours; the `Step` is the other half, and is what
        makes a research night readable rather than a report from nowhere.
        """
        self.steps.append(Step(self.job, tool, args, result, failed))
        if not failed:
            self.note_call(tool, args, result=result)

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """One web search, or [] when the house has no search backend.

        Returns the provider's rows untouched (`{"title", "url", "snippet"}`);
        trimming them for a prompt is the job's business, not this seam's.
        """
        provider = getattr(self.research, "search", None)
        if provider is None:
            self._hand("web_search", {"query": query}, "", failed="no search backend")
            return []
        try:
            rows = await provider.search(query, k)
        except Exception as e:  # noqa: BLE001 — one dead search is not a dead night
            log.warning("DREAM job %s: search %r failed", self.job, query,
                        exc_info=True)
            self._hand("web_search", {"query": query}, "", failed=str(e)[:200])
            raise
        self._hand("web_search", {"query": query, "k": k},
                   f"{len(rows)} results")
        return list(rows)

    async def read_page(self, url: str, *, shelve: bool = True) -> dict:
        """Open one page. `{"url", "title", "text"}`, or {} with no backend.

        `shelve` is §7.7's rule applied to the night: a page she read is
        knowledge, not a tool result, so unless the job says otherwise it goes
        to the shelf with its source URL and is hers to cite tomorrow. The
        ingestion is fire-and-forget by construction (`Researcher.shelve`), so
        this does not make the night wait on an embedder.
        """
        fetcher = getattr(self.research, "fetcher", None)
        if fetcher is None:
            self._hand("read_page", {"url": url}, "", failed="no fetch backend")
            return {}
        try:
            page = await fetcher.fetch(url)
        except Exception as e:  # noqa: BLE001 — §7.7: a page that won't open is skipped
            log.info("DREAM job %s: %s wouldn't open", self.job, url)
            self._hand("read_page", {"url": url}, "", failed=str(e)[:200])
            return {}
        text = str(page.get("text") or "")
        self._hand("read_page", {"url": url}, f"read {len(text)} chars")
        # A dry run reads (the model calls behind it have to be real, §21.3)
        # but files nothing: shelving writes documents and rewrites an index,
        # which is exactly the class of thing a rehearsal must not do.
        if shelve and not self.dry_run:
            try:
                self.research.shelve(page)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — the shelf is not the report
                log.warning("DREAM job %s: couldn't shelve %s", self.job, url,
                            exc_info=True)
        return dict(page)

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
    #: True → the day comes from the calendar, not from the journal.
    #:
    #: `finished_days()` reads `memory/episodic/*.md`, so a day nobody spoke to
    #: her is not a day at all — which is right for a diary and wrong for every
    #: job whose subject is the world rather than the conversation. A standing
    #: job owes yesterday whether or not anything happened, and consequently
    #: keeps the ladder entering DREAM on quiet nights. That is the point of it.
    standing = False
    #: True → billed against `MIND_DREAM_RESEARCH_TOKENS` rather than the
    #: night's shared tick budget (§21.2). For work an order of magnitude past a
    #: diary entry: shared, it would either never run or eat consolidation.
    own_budget = False
    #: What kind of thing this is, for the debug page and the job-file loader.
    kind = "builtin"
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
        done = ledger.done(self.name)
        if self.standing:
            # Yesterday, off the clock. Never a catch-up run: a job that reads
            # the world is answering "what is true now", and nine nights of
            # market briefs written nine nights late are nine wrong answers, not
            # a backlog worth eating. Same judgement `per_day = False` makes
            # below, for a different reason.
            day = day_of(ctx.clock.now() - 86400.0)
            return [day] if day not in done else []
        days = ctx.finished_days()
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
                "standing": self.standing, "kind": self.kind,
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
    # A card written as a third-person dossier — most of them are — will be
    # copied in voice as well as in content unless the prompt says otherwise.
    # The diary asks for first person outright and gets it; this one used to
    # ask for nothing and got '{char} weighed the two pending threads'.
    "Write it as yourself, in first person — 'I', not your own name, and never "
    "as someone describing you from outside. "
    "Under 150 words. No preamble, no headings, no numbered list — just the "
    "thinking. "
    # The one machine-read line in an otherwise human note. Asked for last so
    # the thinking above it is not written backwards from a conclusion, and
    # made optional out loud, because a night that files nothing is the common
    # case and a prompt that always demands a line always gets one.
    "Then, only if something is genuinely worth starting, end with one final "
    "line of the form 'next: <the one thing worth doing>' — concrete enough to "
    "act on, and something you can do yourself. It must be something new, not "
    "one of the goals above said in different words: if the thing worth doing "
    "is already on that list, you are already doing it. If nothing is worth "
    "starting, leave that line out entirely.")


#: Pulls the machine-read tail off the end of a strategy note. Anchored to a
#: line start and tolerant of the bullet or bold a model reaches for unasked.
NEXT_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?next(?:\*\*)?\s*:\s*(.+?)\s*$",
                     re.IGNORECASE | re.MULTILINE)

STRATEGY_OUTPUT = """\
Return exactly one JSON object and no prose:
{"reflection":"your first-person stock-take","next":null}
or
{"reflection":"your first-person stock-take","next":{"objective":"one concrete \
standalone action","why":"why this follows from your drives and current evidence",\
"evidence":"the specific recent fact or event supporting it","success":"an \
observable completion condition","first_action":"the first bounded step",\
"capability":"the available capability it uses, or thought-only"}}
Do not file a value, feeling, relationship posture, metaphorical physical act, or
anything containing an unresolved reference such as 'the thing'. If no new action
is both character-specific and executable with the capabilities shown, use null.
"""


@dataclass(frozen=True)
class StrategyCandidate:
    objective: str
    why: str
    evidence: str
    success: str
    first_action: str
    capability: str


@dataclass(frozen=True)
class StrategyDecision:
    reflection: str
    next: StrategyCandidate | None


def parse_strategy_decision(raw: str) -> StrategyDecision:
    """Structured strategy output, with the old `next:` shape as compatibility."""
    text = (raw or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    candidate_json = fence.group(1) if fence else text
    try:
        value = json.loads(candidate_json)
    except (TypeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict) and set(value) == {"reflection", "next"}:
        reflection = str(value["reflection"] or "").strip()[:2000]
        item = value["next"]
        if item is None:
            return StrategyDecision(reflection=reflection, next=None)
        fields = {"objective", "why", "evidence", "success", "first_action",
                  "capability"}
        if isinstance(item, dict) and set(item) == fields:
            cleaned = {key: " ".join(str(item[key] or "").split())
                       for key in fields}
            objective = cleaned["objective"][:240]
            if (objective and "|" not in objective
                    and all(cleaned[key] for key in fields - {"objective"})):
                return StrategyDecision(
                    reflection=reflection,
                    next=StrategyCandidate(
                        objective=objective,
                        why=cleaned["why"][:400],
                        evidence=cleaned["evidence"][:400],
                        success=cleaned["success"][:400],
                        first_action=cleaned["first_action"][:400],
                        capability=cleaned["capability"][:120]))
        return StrategyDecision(reflection=reflection, next=None)

    hit = NEXT_RE.search(text)
    reflection = NEXT_RE.sub("", text).strip()[:2000]
    legacy = " ".join(hit.group(1).split())[:240] if hit else ""
    next_goal = (StrategyCandidate(
        objective=legacy, why="legacy strategy candidate",
        evidence="legacy strategy note", success="the objective is completed",
        first_action=legacy, capability="unspecified")
                 if legacy and "|" not in legacy else None)
    return StrategyDecision(reflection=reflection, next=next_goal)


class StrategyJob(DreamJob):
    """Standing back from her own goals, once a night.

    Not per-day, because the answer barely changes overnight and ninety nights
    of backlog would be ninety near-identical reviews.

    Two outputs, and the asymmetry is the point. The note goes to the desk and
    says whatever it likes. The goal store gets at most one new goal, filed
    under its own provenance so the page still answers "who wanted this" — she
    may *add* to what she is carrying, and she still cannot silently
    reprioritise or drop what you asked her for. Filing is capped, expires on
    its own, and is switchable off mid-flight; see `DreamContext.file_goal`.
    """

    name = "strategy"
    title = "Strategy"
    description = ("Once a night, stand back from the open goals and write down "
                   "what matters, what's gone stale, and what to do next.")
    priority = 0.4
    per_day = False

    def cost(self, ctx: DreamContext, day: str) -> int:
        open_goals = list(ctx.goals.open_goals()) if ctx.goals is not None else []
        context = ctx.strategy_context(day, open_goals)
        system = (self.system(STRATEGY_SYSTEM).format(
            char=ctx.char_name, user=ctx.user_name) + "\n\n" + STRATEGY_OUTPUT)
        # 800 output tokens is the `max_tokens` below; convert it to the same
        # conservative character accounting the rest of the night uses.
        return max(64, (len(context) + len(system) + ctx.soul_chars() + 3200) // 4)

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        out = JobReport(name=self.name, days=[day])
        open_goals = []
        if ctx.goals is not None:
            open_goals = [g for g in ctx.goals.open_goals()]
        system = (self.system(STRATEGY_SYSTEM).format(
            char=ctx.char_name, user=ctx.user_name)
                  + "\n\n" + STRATEGY_OUTPUT)
        thinking = await ctx.ask(
            system, ctx.strategy_context(day, open_goals),
            thinking=False, max_tokens=800)
        if not thinking:
            out.result = "nothing came of it"
            return out
        decision = parse_strategy_decision(thinking)
        note = decision.reflection
        ctx.put(f"strategy/{day}.md", f"# Taking stock — {day}\n\n{note}\n")
        out.changed = True
        reviewed = f"reviewed {len(open_goals)} goal(s)"
        candidate = decision.next
        filed = ctx.file_goal(
            candidate.objective, day=day,
            meta={"strategy_note": f"strategy/{day}.md",
                  "rationale": candidate.why,
                  "evidence": candidate.evidence,
                  "success": candidate.success,
                  "first_action": candidate.first_action,
                  "capability": candidate.capability}
        ) if candidate else None
        if filed is not None:
            out.result = f"{reviewed}, filed one of my own"
            out.note = f"decided this matters: {filed.text}"
        elif candidate is None:
            out.result = reviewed
            out.note = "stood back and looked at what I'm carrying"
        else:
            # Say which silence this was. A night with nothing to add, a night
            # that was capped or switched off, and a night that named something
            # she is already doing look identical in the log otherwise — and
            # they call for three different things from you.
            out.result = (f"{reviewed}, already carrying that one"
                          if ctx.goal_refusal == "echo"
                          else f"{reviewed}, kept one to myself")
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
#: `kind` is absent on purpose alongside `name`: it selects the `work` a new
#: job gets, and a file may retune a builtin, never re-implement one.
JOB_FILE_KEYS = ("title", "description", "priority", "per_day", "enabled",
                 "soul", "standing")


def _as_float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_effort(value, fallback: str) -> str:
    """One of `REPORT_EFFORTS`, or "" for whatever the server does by default.

    An unset key takes whatever the caller's default is; anything the server
    would not recognise becomes "" rather than a guess, because a rejected
    `reasoning_effort` is a failed call and a job file is written by hand.
    """
    if value is None:
        return fallback
    text = str(value).strip().lower()
    return text if text in REPORT_EFFORTS else ""


def _shorter_effort(effort: str) -> str:
    """One notch down the ladder, and `low` is the floor.

    Unset steps down to `low` rather than staying unset: the retry is there
    because a pass ran away with the ceiling, and asking for the shortest one
    is the only version of "think less" available to send. On a server that
    ignores it that costs nothing, which is the case it was measured against.
    """
    if not effort:
        return "low"
    if effort in ("high", "medium"):
        return REPORT_EFFORTS[REPORT_EFFORTS.index(effort) - 1]
    return effort


def _as_int(value, fallback: int, *, ceiling: int) -> int:
    """A frontmatter number, floored at 1 and clamped to the house ceiling.

    A job file may ask for less than the house allows and never for more: the
    file is the character's, the ceiling is the machine's, and §26.1's
    two-switch rule is the same rule one layer down.
    """
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = fallback
    return max(1, min(wanted, ceiling))


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
            elif key in ("per_day", "enabled", "standing"):
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


class FileJob(DreamJob):
    """The half of a job-file job that is just reading the frontmatter.

    Shared by every `kind:`, so a key means the same thing whichever kind of
    night declares it and a new kind gets the common ones for free.
    """

    owns_ledger = False
    #: `standing` unless the kind says otherwise — see `DreamJob.standing`.
    default_standing = False
    default_priority = 0.3

    def __init__(self, spec: JobFile):
        self.name = spec.name
        self.title = str(spec.front.get("title") or spec.name.title())
        self.description = str(spec.front.get("description") or "")
        self.priority = _as_float(spec.front.get("priority"),
                                  self.default_priority)
        self.per_day = spec.front.get("per_day", True) is not False
        self.standing = spec.front.get("standing",
                                       self.default_standing) is not False
        self._enabled = spec.front.get("enabled", True) is not False
        self.soul = ("off" if str(spec.front.get("soul", "full")).lower()
                     in ("off", "none", "false") else "full")
        self.output = str(spec.front.get("output") or f"{self.name}/{{day}}.md")
        self.prompt_override = spec.prompt.strip()

    def enabled(self, cfg) -> bool:
        return self._enabled

    def _write(self, ctx: DreamContext, day: str, text: str) -> str:
        """Put one answer on the desk at this job's `output:`, and say where.

        `{day}` is the only substitution, and `Workspace.resolve` refuses the
        rest: a path out of the desk, a dotfile, a `..`. A job file is written
        by the person who owns the vault, but it is still a path from a file.
        """
        rel = self.output.format(day=day)
        ctx.put(rel, text)
        return rel


class PromptJob(FileJob):
    """A job that is nothing but a prompt — the shape a new one takes.

    Read the day's journal, ask the question the file asks, write the answer to
    the desk. That covers most of what the built-in jobs do; what it cannot do
    is the reason the other three stay in Python (consolidation writes to
    `memory/`, the selfie dispatches a camera, the stock-take reads the goal
    store). Adding a fifth kind of night no longer means adding a fifth class.
    """

    kind = "prompt"

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        out = JobReport(name=self.name, days=[day])
        journal = ctx.journal(day)
        if not journal.strip() and not self.standing:
            out.result = "nothing in the journal for that day"
            return out
        system = self.system("").format(char=ctx.char_name,
                                        user=ctx.user_name)
        answer = await ctx.ask(system, f"Today is {day}.\n\n{journal}")
        if not answer:
            out.result = "nothing came of it"
            return out
        self._write(ctx, day, f"{answer}\n")
        out.changed = True
        out.result = f"wrote {len(answer)} chars"
        out.note = f"{self.title.lower()}: wrote something for {day}"
        return out


#: The framing a research loop runs under. Not the job file's body: that is the
#: brief for the *report*, and is handed to the writing call at the end. This is
#: the machinery in between — the part that is the same whether she is reading
#: the tape or reading the literature, and therefore the part that belongs in
#: Python rather than in every research job's file.
RESEARCH_LOOP_SYSTEM = """You are {char}, awake at night with nobody waiting, finding out what is true today. This is what you are working on:

{brief}

You are GATHERING, not writing. The writing comes afterwards and you will be asked for it separately — do not write the report now. Right now you are deciding what to look at next.

Below is everything you have gathered so far this session."""

#: How she answers each round. Same one-line-of-intent shape `Hands.catalog`
#: uses, and parsed by the same `parse_intent`, for the same reason: a reply is
#: a stream she is talking through and a research round is not, and one
#: structured line is what a small local model gets right at four in the morning.
RESEARCH_CATALOG = """You may take ONE action now, and only one. Answer with one line, in one of these three forms:

  think <what you make of what you have so far>
  use web_search {"query": "..."}
  use read_page {"url": "https://..."}

Put a `think` line above a hand saying why you reached for it — the result alone won't tell you next time. Search to find what is out there; open the pages actually worth reading. Never search for something you have already searched, and never open a page you have already opened. When you have enough to write from, answer with exactly:

  think nothing further"""

#: Added to the catalog only while she has reached for nothing. Appending it to
#: every round told her she had nothing on the round after she had just read
#: something, which is both false and the kind of contradiction a small model
#: resolves by searching again for what it already has.
RESEARCH_FIRST_MOVE = ("\n\nYou have gathered nothing yet, so your first action "
                       "is a search. Thinking alone gathers nothing, and there "
                       "is no report to write from an empty session.")

#: Appended to every round, with the numbers as they stand. A model that
#: cannot see its budget spends it: the first full live night went twelve rounds
#: without once saying it had enough, five of them bare thoughts that gathered
#: nothing, and stopped mid-gather because the moves ran out rather than because
#: she was done. Telling her what is left turns "when do I stop" from a guess
#: into arithmetic, and the report is written from a corpus she chose to finish
#: with instead of one a cap cut off.
RESEARCH_BUDGET = ("\n\nYou have {steps} move(s) left, {searches} search(es) and "
                   "{pages} page(s). When the moves run out you write the report "
                   "from whatever you have by then — so spend them on gathering. "
                   "A line that is only a thought still costs a move and brings "
                   "nothing back.")

#: What the writing call is handed, around everything she gathered. Two things
#: it has to say and the job file cannot, because the file is the brief and this
#: is the material: that the corpus is the whole of what she has — a market
#: brief with a price in it she never read is worse than no brief — and that the
#: person reading has seen none of it.
REPORT_CORPUS = (
    "Today is {day}. This is everything you gathered tonight: your searches, "
    "the pages you opened, and your own notes as you went.\n\n{gathered}\n\n"
    "That is all you have. Every figure and every claim in what you write now "
    "comes from what is above — where it does not go far enough, say so plainly "
    "instead of filling the gap. Whoever reads this has seen none of it.")

#: Words that carry no search intent, dropped before two queries are compared.
_QUERY_NOISE = frozenset(
    "a an and are as at by for from how in is it of on or the to what when "
    "where which who why with".split())

#: How alike two queries have to be before the second is treated as the first
#: one again. Measured off a live night: "stock market sector rotation leaders
#: laggards August 19 2026" followed two rounds later by "stock market sector
#: performance leaders laggards August 19 2026" — one word apart, the same
#: results, a move gone. Exact-match dedupe cannot see that, and a model that
#: has just been let down by a dead link reaches for the rephrase every time.
#:
#: The number is arithmetic rather than taste, and it was set twice. Swapping
#: one word of an n-word query scores (n-1)/(n+1) — 0.78 at eight words, 0.8 at
#: nine — while changing *two* scores (n-2)/(n+2), which is 0.67 at ten. Two
#: thirds caught both, and a live night showed what that costs: "US stock
#: market today August 20 2026 sentiment momentum leaders" and "US stock market
#: sector rotation momentum leaders August 20 2026" score exactly 0.67 and are
#: not the same question — sentiment and rotation are two different things to
#: go and find out. Three quarters catches the single swapped word from eight
#: words up and lets a two-word change through at any length, which is the line
#: between a rephrase and a follow-up.
QUERY_SAME_ENOUGH = 0.75

#: What she says to stop early. Matched as a substring rather than parsed, and
#: the loop stops on two quiet rounds anyway — this only saves a round.
RESEARCH_DONE = "nothing further"


@dataclass
class _Gathering:
    """The corpus a research loop builds, and what it forgets first.

    Trimming drops the oldest **page** first and never a search-result row or
    one of her own notes. What she looked at and what she made of it is the
    thread of the session — lose that and the next round re-searches ground it
    already covered — while a page body has already done most of its work by
    being read once.
    """
    rows: list[dict] = field(default_factory=list)

    def add(self, kind: str, text: str) -> None:
        self.rows.append({"kind": kind, "text": text})

    def pages(self) -> int:
        return sum(1 for r in self.rows if r["kind"] == "page")

    def render(self, limit: int) -> str:
        total = sum(len(r["text"]) for r in self.rows)
        dropped: set[int] = set()
        for index, row in enumerate(self.rows):
            if total <= limit:
                break
            if row["kind"] != "page":
                continue
            total -= len(row["text"]) - len(_DROPPED)
            dropped.add(index)
        return "\n\n".join(_DROPPED if i in dropped else r["text"]
                            for i, r in enumerate(self.rows)) or "(nothing yet)"


_DROPPED = "[a page read earlier this session, set aside to make room]"


class ResearchJob(FileJob):
    """A night spent reading the web, ending in one report.

    The other kinds of night look inward — at the journal, at the goals, at the
    day. This one looks out, and is the reason `DreamContext` grew hands: a
    market read, a literature scan, a what-changed-in-my-field digest are the
    same job with a different brief, and none of them can be written from a
    vault alone.

    Agentic on purpose. A fixed query list cannot follow the one thing that
    turned out to matter, and following it is most of what makes research worth
    reading. The cost of that is every way an unattended loop can go wrong at
    4am, so the bounds are hard and the failure is always a shorter report
    rather than no report: `max_steps` rounds, `max_searches`, `max_pages`, a
    stop on two quiet rounds, a context ceiling that forgets pages first, and a
    write step in `finally` that runs on whatever was gathered.
    """

    kind = "research"
    default_standing = True          # the market does not wait for a journal
    default_priority = 0.2           # after consolidation, the diary, the goals
    own_budget = True                # §21.2: its own lane, not the tick budget

    def __init__(self, spec: JobFile):
        super().__init__(spec)
        self.per_day = spec.front.get("per_day", False) is not False
        front = spec.front
        self.topics = [str(t).strip() for t in _as_list(front.get("topics"))
                       if str(t).strip()]
        self._max_searches = front.get("max_searches")
        self._max_pages = front.get("max_pages")
        self._max_steps = front.get("max_steps")
        self.step_chars = max(500, _as_int(front.get("step_chars"), 4000,
                                           ceiling=20000))
        self.context_chars = max(2000, _as_int(front.get("context_chars"),
                                               24000, ceiling=200000))
        self.results = _as_int(front.get("results"), 5, ceiling=20)
        # What the *writing* call may spend. Not `UTILITY_MAX_TOKENS`, which is
        # sized for extraction and summarisation: measured against a local 27B,
        # a report call given the house default ran past nineteen minutes and
        # never returned, because a reasoning model handed 15,000 tokens will
        # use them. One page is about 800; this leaves room for that and a
        # think, and is the number to raise if reports come back cut off.
        self.report_max_tokens = _as_int(front.get("report_max_tokens"), 2500,
                                         ceiling=32000)
        # …and whether it gets a reasoning pass. ON: the report is the one call
        # in the night that earns one — everything before it is plumbing, and
        # this is where she actually decides what she thinks. The rounds run
        # without it precisely so that this one can afford it.
        self.report_thinking = front.get("report_thinking", True) is not False
        # …and how long that pass may be, on a server that implements the ask
        # (`REPORT_EFFORTS`). Unset takes whatever the model does on its own.
        self.report_effort = _as_effort(front.get("report_effort"), "")
        # …and how long the call may take. See `REPORT_TIMEOUT_S`: on a local
        # reasoning model this is the limit that actually bites, long before
        # any of the token numbers above do.
        self.report_timeout_s = _as_int(front.get("report_timeout_s"),
                                        REPORT_TIMEOUT_S, ceiling=7200)
        self.shelve = front.get("shelve", True) is not False
        self.deliver = ("chat" if str(front.get("deliver", "desk")).lower()
                        == "chat" else "desk")
        self.output = str(front.get("output")
                          or f"reports/{self.name}/{{day}}.md")

    def report_ceiling(self, cfg, prompt_chars: int) -> int:
        """What the first writing call may spend: the report, and room to think.

        Without a pass this is simply what the report is worth. With one it is
        that plus `REPORT_REASONING_ALLOWANCE`, and the window is still the hard
        stop — asking a server for more than fits is not free everywhere, and a
        first call that overruns the window fails the way the retry is there to
        rescue.
        """
        if not self.report_thinking:
            return self.report_max_tokens
        return min(self.report_max_tokens + REPORT_REASONING_ALLOWANCE,
                   self.retry_max_tokens(cfg, prompt_chars))

    def retry_max_tokens(self, cfg, prompt_chars: int) -> int:
        """Everything the window has left, once the prompt is in it.

        The retry exists because the first ceiling was too small for the
        thinking this model wanted; a second guess of the same shape would
        truncate in the same place. So it is not a guess — it is the window
        minus what the prompt occupies, minus a margin for the tokeniser
        disagreeing with chars/4.
        """
        window = int(getattr(cfg, "context_length", 0) or 0)
        if window <= 0:
            # No window configured is not a reason to give up on the report;
            # it is a reason not to pretend to know. Twice the allowance is
            # what `characters/optimize.py` reaches for on its own retry, and
            # it is guaranteed to be more than the first call already asked
            # for — a retry that asks for what it just had is ceremony.
            return self.report_max_tokens + REPORT_REASONING_ALLOWANCE * 2
        return max(self.report_max_tokens,
                   window - (prompt_chars // 4) - REPORT_WINDOW_MARGIN)

    # ---------------------------------------------------------------- limits

    def caps(self, cfg) -> tuple[int, int, int]:
        """`(searches, pages, steps)`, the file clamped to the house ceilings."""
        return (_as_int(self._max_searches, 8,
                        ceiling=int(getattr(cfg, "mind_dream_research_searches", 10))),
                _as_int(self._max_pages, 6,
                        ceiling=int(getattr(cfg, "mind_dream_research_pages", 10))),
                _as_int(self._max_steps, 16,
                        ceiling=int(getattr(cfg, "mind_dream_research_steps", 12))))

    def enabled(self, cfg) -> bool:
        """The §26.1 two-switch rule, applied to the night (`SelfieJob`'s rule).

        A job file may say "not tonight"; it may not switch on a night of
        reading in a house whose `SEARCH_BACKEND` is off.
        """
        return (self._enabled
                and getattr(cfg, "search_backend", "off") != "off")

    def cost(self, ctx: DreamContext, day: str) -> int:
        """The whole loop, priced before any of it runs (§21.2's MUST).

        A per-round estimate would be right and useless: the budget check
        happens once, before the job starts, so anything this does not price
        here is spent unbilled. The corpus grows across the session, so the
        average round carries about half the ceiling and the last one carries
        all of it.
        """
        steps = self._steps_hint()
        overhead = (PROMPT_OVERHEAD_CHARS + ctx.soul_chars()
                    + len(self.prompt_override))
        chars = (steps / 2 + 1) * self.context_chars + (steps + 1) * overhead
        # …plus what the report is allowed to say. Prompt tokens are most of a
        # night but not all of it, and the writing call is the one place the
        # completion is a real number rather than a line of intent — on a
        # reasoning model it is the largest single thing the night spends.
        return max(64, int(chars // 4) + self.report_max_tokens
                   + (REPORT_REASONING_ALLOWANCE if self.report_thinking else 0))

    def _steps_hint(self) -> int:
        """`max_steps` as the file asks for it, with no config to clamp against.

        `cost()` is called by the runner, which holds the config — but pricing
        must never be *cheaper* than the run, so the unclamped number is the
        safe one here: the ceiling can only lower it.
        """
        return _as_int(self._max_steps, 16, ceiling=64)

    # ------------------------------------------------------------------ work

    async def work(self, ctx: DreamContext, day: str) -> JobReport:
        out = JobReport(name=self.name, days=[day])
        searches, pages, steps = self.caps(ctx.cfg or _CAPS_DEFAULTS)
        brief = self.system("").format(char=ctx.char_name, user=ctx.user_name)
        gathered = _Gathering()
        if self.topics:
            gathered.add("plan", "What you set out to look at: "
                         + "; ".join(self.topics))
        seen_queries: dict[str, frozenset[str]] = {}
        seen_urls: set[str] = set()
        quiet = 0
        searched = 0
        opened = 0
        broke = ""
        try:
            for move in range(steps):
                system = RESEARCH_LOOP_SYSTEM.format(char=ctx.char_name,
                                                     brief=brief)
                reply = await ctx.ask(
                    system,
                    f"Today is {day}.\n\n{gathered.render(self.context_chars)}"
                    f"\n\n{RESEARCH_CATALOG}"
                    + ("" if searched or opened else RESEARCH_FIRST_MOVE)
                    + RESEARCH_BUDGET.format(steps=steps - move,
                                             searches=searches - searched,
                                             pages=pages - opened),
                    # No reasoning pass, and a short leash. The answer is one
                    # line naming a search or a page; there is no version of
                    # thinking harder that improves it, and on a local
                    # reasoning model the pass costs minutes per round. The
                    # report below gets the full one.
                    thinking=False, max_tokens=ROUND_MAX_TOKENS)
                intent = parse_intent(reply, allowed=("web_search", "read_page"))
                if intent.kind == "think":
                    if intent.text:
                        gathered.add("note", f"You thought: {intent.text}")
                    if RESEARCH_DONE in intent.text.lower():
                        broke = "she had enough"
                        break
                    # `quiet` counts rounds where SHE stopped reaching — not
                    # rounds where the web failed to cooperate. Both of those
                    # used to increment it, and against the real web that ended
                    # a night after one dead link and one retry: a search, a
                    # Morningstar page that returned zero characters, a retry of
                    # the same URL, done. A paywall is not her having had enough.
                    # Everything else is bounded by `max_steps` and the caps.
                    #
                    # A bare thought before she has reached for anything is also
                    # not quiet — it is her working out where to start, and a
                    # reasoning model asked not to think out loud puts that
                    # first move in the answer instead of in a <think> block.
                    if searched or opened:
                        quiet += 1
                    if quiet >= 2:
                        broke = "two quiet rounds"
                        break
                    continue
                if intent.text:
                    gathered.add("note", f"You thought: {intent.text}")
                # She reached for something, so she has not gone quiet — and
                # that is true however the reach turns out. Resetting this only
                # on a *successful* one meant a dead link left the counter
                # standing and the next thought ended the night: the same
                # mistake as counting the paywall, one step further down. What
                # bounds a night of bad links is `max_steps`, not this.
                quiet = 0
                if intent.tool == "web_search":
                    query = str(intent.args.get("query") or "").strip()
                    already = (_already_asked(_query_key(query), seen_queries)
                               if query else "")
                    if not query or already:
                        # Naming the earlier query is not enough on its own:
                        # asked to try something else, she asked the identical
                        # thing again the very next round. The refusal has to
                        # point somewhere — the results are already above, and
                        # the plan says what has not been looked at yet.
                        plan = ("; ".join(self.topics) if self.topics
                                else "something you have not covered yet")
                        gathered.add("note", f'(you already searched "{already}"'
                                             " — its results are above. Do not "
                                             "ask it again in other words. Open "
                                             "one of those results, or search "
                                             f"for one of these instead: {plan})")
                        continue
                    if searched >= searches:
                        broke = "out of searches"
                        break
                    seen_queries[query] = _query_key(query)
                    searched += 1
                    rows = await ctx.search(query, self.results)
                    gathered.add("results", _search_rows(query, rows))
                    continue
                url = str(intent.args.get("url") or "").strip()
                if not url or url in seen_urls:
                    gathered.add("note", "(you have already opened that page and "
                                         "it gave nothing — open a DIFFERENT "
                                         "one, or search for another source)")
                    continue
                if opened >= pages:
                    broke = "out of pages"
                    break
                seen_urls.add(url)
                opened += 1
                page = await ctx.read_page(url, shelve=self.shelve)
                text = str(page.get("text") or "").strip()
                if not text:
                    gathered.add("note", f"({url} gave nothing back — a paywall, "
                                         "or a page that needs a browser. Try a "
                                         "different source.)")
                    continue
                gathered.add("page", f"{page.get('title') or url}\n{url}\n"
                                     f"{text[:self.step_chars]}")
            else:
                broke = "out of rounds"
        except Exception:
            # A loop that died with something in hand still owes a report; one
            # that died with nothing has nothing to say and should retry
            # tomorrow, which is what re-raising buys (`_run_one`).
            if not gathered.pages():
                raise
            log.exception("DREAM job %s: the loop failed with %d pages in hand",
                          self.name, gathered.pages())
            broke = "the loop failed part-way"

        out.result = f"{searched} searches, {opened} pages ({broke})"
        if not gathered.pages():
            # Handled, not produced (§21.2): she looked and there was nothing.
            # Marking the day is what stops her re-deciding this every night.
            out.result = f"nothing worth a report ({broke})"
            return out
        corpus = REPORT_CORPUS.format(
            day=day, gathered=gathered.render(self.context_chars))
        prompt_chars = len(brief) + len(corpus)
        ceiling = self.report_ceiling(ctx.cfg, prompt_chars)
        report = await ctx.ask(brief, corpus, thinking=self.report_thinking,
                               reasoning_effort=self.report_effort,
                               timeout=self.report_timeout_s,
                               max_tokens=ceiling)
        if not report and self.report_thinking:
            # It thought until the ceiling and never spoke. A ceiling bounds
            # the *call*, not the thinking, so the whole budget can land inside
            # a <think> block that is then cut off — 431 seconds and an empty
            # string, measured. Both halves of the retry answer that: more
            # room, and a shorter pass to fit in it. What it never does is
            # drop the pass — that would trade the one call in the night that
            # earns thinking for the one failure it must never have.
            #
            # Bounded by the window, not by hope: a second ceiling that does
            # not fit alongside the prompt would truncate in exactly the same
            # place, so the retry asks for what is actually left.
            room = self.retry_max_tokens(ctx.cfg, prompt_chars)
            effort = _shorter_effort(self.report_effort)
            if room > ceiling or effort != self.report_effort:
                log.info("DREAM job %s: the report thought past %d tokens; "
                         "giving it %d at %s effort and asking again",
                         self.name, ceiling, room,
                         effort or "the server default")
                report = await ctx.ask(brief, corpus, thinking=True,
                                       reasoning_effort=effort,
                                       timeout=self.report_timeout_s,
                                       max_tokens=room)
        if not report:
            out.result = f"gathered {opened} pages and wrote nothing of them"
            return out
        rel = self._write(ctx, day, f"{report}\n")
        out.changed = True
        # Why she stopped belongs in the result even when the night worked.
        # "out of pages" and "she had enough" are the same length of report and
        # completely different facts about it, and the second is the only place
        # they are ever told apart.
        out.result = (f"read {opened} pages over {searched} searches and wrote "
                      f"{len(report)} chars ({broke})")
        out.note = (f"{self.title.lower()}: read {opened} pages and wrote it up "
                    f"for {day}")
        if self.deliver == "chat":
            ctx.deliver(title=self.title, path=rel,
                        summary=_lede(report))
            out.note += " — left it where they'll see it"
        return out


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _search_rows(query: str, rows: list[dict]) -> str:
    """Search results as she sees them: enough to choose from, no more.

    The snippet is trimmed hard because these rows are the part of the corpus
    that never gets forgotten (`_Gathering.render`), so every character here is
    re-sent on every remaining round of the session.
    """
    if not rows:
        return f'You searched "{query}" and found nothing.'
    lines = [f'You searched "{query}":']
    for row in rows:
        snippet = " ".join(str(row.get("snippet") or "").split())
        lines.append(f"  · {row.get('title') or '(untitled)'} — "
                     f"{row.get('url') or ''}\n    "
                     f"{snippet[:SEARCH_SNIPPET_CHARS]}")
    return "\n".join(lines)


#: A list marker at the head of a line, and nothing else — `*Energy*` keeps
#: its emphasis where a blanket strip would eat it.
_BULLET = re.compile(r"^[-*•+]\s+|^\d+[.)]\s+")


def _query_key(query: str) -> frozenset[str]:
    """A query as the words that carry its intent, order and noise gone."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    return frozenset(w for w in words if w not in _QUERY_NOISE)


def _already_asked(key: frozenset[str], seen) -> str:
    """The earlier query this one is a rephrase of, or "".

    Jaccard rather than equality, for the reason in `QUERY_SAME_ENOUGH`: the
    duplicate that costs a night is never a duplicate, it is one word moved.
    """
    for query, other in seen.items():
        both = key | other
        if both and len(key & other) / len(both) >= QUERY_SAME_ENOUGH:
            return query
    return ""


def _lede(report: str) -> str:
    """The one line that goes in the chat, off the top of the report.

    Her own first sentence rather than a summary of it: asking a model to
    summarise something it just wrote is a call spent to say the same thing
    worse, and the report is one click away in any case.

    Her first *sentence*, though, not her first line — a report that opens
    "## The tape" would otherwise arrive in chat as the words "The tape", which
    tells you nothing you did not already know from the card's title. Headings
    are skipped in favour of the prose under them, and only a report that is
    nothing but headings falls back to one.
    """
    heading = ""
    for raw in report.splitlines():
        line = raw.strip()
        if not line or line.startswith(("---", "===", "***", "___", "|")):
            continue
        if line.startswith("#"):
            heading = heading or line.lstrip("#").strip()
            continue
        # A bullet is prose with a dash in front of it; the dash is not hers.
        # Only the marker, though — `- *Energy* is the only bid` keeps its
        # emphasis, and a line that is nothing but rule characters is not prose.
        line = _BULLET.sub("", line, count=1).strip()
        if line:
            return line[:280]
    return heading[:280] or "I wrote you something."


class _CapsDefaults:
    """Stand-in config for a `work()` called without one (tests, replay)."""
    mind_dream_research_searches = 10
    mind_dream_research_pages = 10
    mind_dream_research_steps = 12


_CAPS_DEFAULTS = _CapsDefaults()


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
        from yurios.characters.importer import DREAMS_README, _seed_job_files
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text(DREAMS_README, encoding="utf-8")
        for fname, body in _seed_job_files().items():
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
