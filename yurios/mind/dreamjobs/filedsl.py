"""The job file: frontmatter over a prompt (SPEC §21.2, §34.3).

A `SKILL.md` pointed at the night. This module is the whole round trip of that
format — the coercions that read a hand-written value without trusting it,
`load_job_files` which reads the folder, `seed_job_files` which writes the
builtins out as files so a fresh vault has a roster to edit, and the two classes
a file can become: `FileJob` (a builtin, retuned) and `PromptJob` (a new job,
defined entirely by its file).

The seeder and the loader are deliberately the same file. They are two halves of
one format, and a format whose writer and reader live apart is a format that
drifts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .builtins import (DIARY_SYSTEM, SELFIE_SYSTEM, STRATEGY_SYSTEM, DiaryJob,
                       DreamJob, SelfieJob, StrategyJob)
from .context import REPORT_EFFORTS, DreamContext, JobReport
from ..workspace import FRONTMATTER_RE

log = logging.getLogger("mind.dreamjobs")


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



#: The night's roster, as files (§21.2) — what `load_job_files` above reads,
#: written out. A fresh vault therefore behaves exactly as it did before the
#: folder existed, and the first edit shows up as a real diff: a character whose
#: diary should ask a different question edits one file, and one who should not
#: keep a diary at all sets one flag.
#:
#: It renders the compiled jobs rather than a second copy of their prose, so the
#: two can never drift — improve the built-in diary prompt and every vault
#: seeded afterwards gets the improvement. That is also why this lives here
#: rather than beside the rest of the vault seeding in `characters/importer.py`,
#: where it began: reading these constants from over there meant the importer
#: imported the mind and the mind imported the importer back, a cycle that only
#: held together because both sides did it from inside a function.
def seed_job_files() -> dict[str, str]:
    def front(name, title, desc, priority, per_day, soul, body):
        # `yaml.safe_dump` for the free-text fields, not an f-string: a
        # description is prose and prose contains colons. One of these written by
        # hand with a colon in it is a job that silently stops loading, which is
        # exactly the failure the loader's warning exists to make visible — no
        # reason to seed one.
        meta = yaml.safe_dump(
            {"name": name, "title": title, "description": desc,
             "priority": priority, "per_day": per_day,
             "enabled": True, "soul": soul},
            sort_keys=False, allow_unicode=True, default_flow_style=False)
        return f"---\n{meta}---\n\n{body.strip()}\n"

    return {
        "diary.md": front(
            "diary", "Diary", DiaryJob.description, DiaryJob.priority,
            DiaryJob.per_day, DiaryJob.soul, DIARY_SYSTEM),
        "strategy.md": front(
            "strategy", "Strategy", StrategyJob.description,
            StrategyJob.priority, StrategyJob.per_day,
            StrategyJob.soul, STRATEGY_SYSTEM),
        "selfie.md": front(
            "selfie", "Selfie", SelfieJob.description, SelfieJob.priority,
            SelfieJob.per_day, SelfieJob.soul, SELFIE_SYSTEM),
    }


DREAMS_README = """# Dreams

What she does at night, one file per job. Each is YAML frontmatter over a body
that **is** the system prompt she is given:

    ---
    name: diary
    title: Diary
    description: A private entry per day, in her own voice.
    priority: 0.6
    per_day: true
    enabled: true
    soul: full
    ---

    Write YOUR private diary entry about that day...

A file named after a built-in job (`consolidate`, `diary`, `strategy`, `selfie`)
**retunes** it — the prompt, the priority, whether it runs at all — and leaves
its behaviour alone, so the diary still knows which half of the journal is hers
however you rewrite the question. A file with any other name is a **new job**,
and `kind:` says what sort:

`kind: prompt` (the default) reads the day's journal, asks what you asked, and
writes the answer to her desk at `output:` (default `<name>/{day}.md`).

`kind: research` sends her to the web instead. She plans her own searches,
reads what looks worth reading, and writes the report your body asks for — so
for this kind the body is the brief for the *report*, not for the search. It
needs `SEARCH_BACKEND` to be on, and it is bounded: `max_searches`, `max_pages`
and `max_steps` (each capped by the house `MIND_DREAM_RESEARCH_*` settings),
`topics:` for where to start, and `shelve: false` if you would rather what she
read did not go into her knowledge store.

    ---
    name: market-brief
    title: Overnight market brief
    kind: research
    standing: true
    deliver: chat
    topics: ["US equities momentum", "macro calendar"]
    max_searches: 10
    output: reports/market-brief/{day}.md
    ---

    You are {char}. Write {user} their morning brief...

`deliver: chat` puts the finished report where you will find it the next time
you open her chat, the way a dream selfie arrives. Only the newest one waits —
come back after a week and you get this morning's, not seven of them; the rest
are still on her desk.

The numbers to keep an eye on are all about the call that writes the report.
`context_chars` (default 24000) is how much of what she gathered is handed to
it. The night will not overrun her model's context window — what is left of it
after the corpus is exactly what the writing call asks for — but a corpus that
fills the window leaves nothing to think in, so if reports come back empty or
cut off, halve this first. `report_max_tokens` (default 2500) is what
the *report* is worth — one page is about 800 — and room to think is added on
top of it rather than taken out of it. A ceiling bounds the call and not the
pass inside it, so a number sized for the answer is a number the thinking eats
before she writes a word: 2,500 tokens once went 2,500 to reasoning and none to
the report, where the same model given room spent 10,049 on the thinking and
698 on the page. Asking high is free — nothing bills for a ceiling, only for
what gets written.

The report is the only call in the night that thinks. The rounds in between
never do, and that is what pays for it: asking a reasoning model which page to
open next cost 1200 reasoning tokens and 200 seconds a round on a local 27B,
and twelve of those is a night that never ends. Deciding what she makes of what
she read is worth the pass; deciding which link to click is not.

`report_timeout_s` (default 3600) is how long that call may take, and on a
local model it is the number that bites first: a reasoning pass over a night of
reading takes minutes, and the ordinary ten-minute client deadline killed a
report that was still being written. The one that finally finished took
thirty-six minutes: ten thousand tokens of thinking, then a page. Nobody is
waiting at 4am, and a faster model never notices the number. What keeps the
night finite is `max_steps` and the caps, not the clock on one call.

`report_effort` — `low`, `medium` or `high` — asks for a shorter pass on a
model that takes the hint. It reaches LM Studio as of 0.4.8, and reaching it is
not the same as being obeyed: a 27B Qwen that advertises only `on` and `off`
spent 236 reasoning tokens at `low` against 220 for saying nothing. Ask for it
by all means, but if a report has to be quick the knob that always works is
`report_thinking: false`, and the model's own card decides the rest.

If a report comes back empty it is because the thinking ran out of room, not
because it thought too much — so it is asked again with whatever the context
window has left, and the shortest pass it can ask for, rather than with the
pass taken away. `report_thinking: false` turns it off entirely if you would
rather have speed.

`standing: true` runs a job every night whether or not you spoke to her.
Anything that looks at the world rather than at the conversation needs it,
because a day nobody talked is not a day the journal has.

`soul:` decides whether her character card leads the prompt. `full` for anything
written in her voice — that is what stops every character's diary reading the
same. `off` for mechanical work; `consolidate` ships that way, because the facts
it distils are read by everyone afterwards.

`enabled: false` switches a job off. It cannot switch one *on* that the house has
no backend for — `selfie` still needs a camera, `research` still needs search.

The mind debug page's **Dreams** section edits all of this, and gives every job
two buttons: **Dry run** makes all the model calls against any day you like and
shows you the prompt it sent and what came back, writing nothing; **Run for
real** does the same work and keeps it — the file, the delivery, the day marked
done.

This folder is versioned, like `skills/` and unlike `workspace/`: how she spends
the hours nobody sees is worth being able to read back.
"""


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
