"""The jobs every character starts with (SPEC §21.2).

Consolidate, diary, strategy, selfie: the night as it shipped, before a
character could own a roster. They stay Python rather than becoming files
because their `work` is behaviour — `diary` relabels the transcript before it
prompts, `selfie` files a gift rather than sending one — and a file may retune a
builtin's prompt, never re-implement it.

`DreamJob` is the base all of them share, and the one `mind/jobs`' file-defined
jobs subclass in `filedsl.py`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .context import (JobLedger, JOURNAL_CHARS, PROMPT_OVERHEAD_CHARS, DreamContext, JobReport)
from ..dream import DreamConsolidator
from ..util import day_of

log = logging.getLogger("mind.dreamjobs")


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
            # What the note may claim is what start-don't-await can know at
            # dispatch: the render lands minutes later and may not land at all
            # (SPEC §16.3). "Had it made" is a line she reads back tomorrow as
            # a photo that exists, on the mornings after her camera OOM'd.
            out.note = f"dreamt a picture of {day} and sent it to the camera"
        else:
            out.result = "described it (no camera wired)"
            out.note = f"dreamt a picture of {day}"
        return out
