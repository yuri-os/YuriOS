"""What a job is handed, and what it hands back (SPEC §21.2).

`DreamContext` is the night's whole interface to the world: the journal, the
Vault, the utility model, the day's budget. A job gets one and returns a
`JobReport`, and that narrowness is deliberate — it is what lets a job be tested
against a scripted model and a temp folder rather than a running mind.

The sizing constants live here rather than beside the jobs that spend them
because they are one budget: how much journal reaches a prompt, what a prompt
costs before the journal is added, how much room a reasoning model is left. Read
them together or not at all.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from yurios.kernel import correlate
from yurios.kernel.clock import Clock

from ..journal import canonical_day, is_canonical_day
from ..util import day_of, iso_of, read_json, write_json
from ..vaultio import MindVault
from ..goals import Goal, GoalStore, echoes, night_owned
from ..workspace import SkillStore, Workspace

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
    goals: GoalStore | None = None
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
                if str(getattr(goal, "meta", {}).get("auto") or "") == "dream":
                    line += " | the night owns this — do not file a desk task for it"
                lines.append(line)
            parts.append("OPEN GOALS\n" + "\n".join(lines))
        else:
            parts.append("OPEN GOALS\n- (nothing open right now)")

        parts.append(
            "THE NIGHT ALREADY DOES THIS\n"
            "Consolidation of finished days is DREAM's job. It writes durable "
            "facts to memory/semantic/facts.md — that is kept memory. There is "
            "no kept-memory folder on your desk. diary/ entries are a separate "
            "night job, already written when that night ran. A standing research "
            "job (a market brief, an overnight report) writes to reports/ on "
            "the desk and is delivered in the morning — do not file a daytime "
            "task to pull prices or write a morning brief. A maintenance goal "
            "about catching up on nights is a reminder for DREAM, not a task "
            "for your hands. Do not file a desk task to list, match, or create "
            "kept-memory entries.")

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
                  meta: dict | None = None) -> Goal | None:
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

        Five limits, in the order they are checked: the switch
        (`MIND_GOAL_FILING_ENABLED`), the cap on how many of hers may be open at
        once (`MIND_SELF_GOALS_MAX`), whether she is already carrying this goal
        under another wording (`echoes`), whether the text is DREAM's job
        (`night_owned` — consolidation, kept-memory, catching up on nights),
        and `GoalStore.add`'s own exact-text merge. Returns None when any of
        them refuses and leaves the reason in `goal_refusal`, so a job can say
        which silence this was rather than claiming a goal it did not get.
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
        if night_owned(text):
            self.goal_refusal = "night"
            self.note_call("file_goal", {"text": text[:200]},
                           result="the night already does that")
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
