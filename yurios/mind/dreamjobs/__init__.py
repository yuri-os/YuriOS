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

Split into five modules, top of the stack last — each imports only from the ones
above it, and `tests/test_layering.py` keeps it that way:

    context.py   what a job is handed and hands back, and the sizing budget
    builtins.py  consolidate / diary / strategy / selfie, and the `DreamJob` base
    filedsl.py   the job file: read it, write it, and the two classes it becomes
    research.py  the gathering loop — the one job that is not a single call
    runner.py    the roster, and the night that works through it

This module is the package's public face; everything the rest of YuriOS uses is
re-exported below, so `from yurios.mind.dreamjobs import DreamRunner` means what
it always did. Reach past it to a submodule only for something not on that list —
an internal a test is pinning down, say — and never from outside `yurios/mind`.
"""
from __future__ import annotations

from .builtins import (DIARY_SYSTEM, JOURNAL_FORMAT, SELFIE_SYSTEM,
                       STRATEGY_SYSTEM, ConsolidateJob, DiaryJob, DreamJob,
                       SelfieJob, StrategyCandidate, StrategyDecision,
                       StrategyJob, parse_strategy_decision)
from .context import (JOURNAL_CHARS, PROMPT_OVERHEAD_CHARS, REPORT_EFFORTS,
                      REPORT_REASONING_ALLOWANCE, REPORT_TIMEOUT_S,
                      REPORT_WINDOW_MARGIN, ROUND_MAX_TOKENS, SELF_GOAL,
                      SELF_GOAL_TTL_DAYS, SEARCH_SNIPPET_CHARS, DreamContext,
                      Exchange, JobLedger, JobReport, Step, UtilityCall,
                      relabel)
from .filedsl import (DREAMS_README, JOB_FILE_KEYS, FileJob, JobFile,
                      PromptJob, load_job_files, seed_job_files)
from .research import RESEARCH_BUDGET, ResearchJob
from .runner import (BUILTIN_JOBS, BUILTIN_NAMES, JOB_KINDS, JOB_NAME_RE,
                     DreamRunner, NightReport, validate_job_file)

__all__ = [
    "BUILTIN_JOBS", "BUILTIN_NAMES", "DIARY_SYSTEM", "DREAMS_README",
    "JOB_FILE_KEYS", "JOB_KINDS", "JOB_NAME_RE", "JOURNAL_CHARS",
    "JOURNAL_FORMAT", "PROMPT_OVERHEAD_CHARS", "REPORT_EFFORTS",
    "REPORT_REASONING_ALLOWANCE", "REPORT_TIMEOUT_S", "REPORT_WINDOW_MARGIN",
    "RESEARCH_BUDGET", "ROUND_MAX_TOKENS", "SEARCH_SNIPPET_CHARS",
    "SELFIE_SYSTEM", "SELF_GOAL", "SELF_GOAL_TTL_DAYS", "STRATEGY_SYSTEM",
    "ConsolidateJob", "DiaryJob", "DreamContext", "DreamJob", "DreamRunner",
    "Exchange", "FileJob", "JobFile", "JobLedger", "JobReport", "NightReport",
    "PromptJob", "ResearchJob", "SelfieJob", "Step", "StrategyCandidate",
    "StrategyDecision", "StrategyJob", "UtilityCall", "load_job_files",
    "parse_strategy_decision", "relabel", "seed_job_files",
    "validate_job_file",
]
