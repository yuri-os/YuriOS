"""The research job: a gathering loop, then a report (SPEC §21.2).

The one builtin `kind` that is not a single call. It searches, fetches, reads,
and decides what to look at next — for as long as its budget allows — and only
then writes. That shape is why it lives in its own module: everything here is
the machinery *between* the brief and the report, and none of it is shared with
a job that asks the model one question.

The expensive one. `RESEARCH_BUDGET`, the caps and the round limits are the
whole of what stands between a brief and a night that never ends.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .context import (PROMPT_OVERHEAD_CHARS, REPORT_REASONING_ALLOWANCE,
                      REPORT_TIMEOUT_S, REPORT_WINDOW_MARGIN,
                      ROUND_MAX_TOKENS, SEARCH_SNIPPET_CHARS, DreamContext,
                      JobReport)
from .filedsl import FileJob, JobFile, _as_effort, _as_int, _shorter_effort
from ..hands import parse_intent

log = logging.getLogger("mind.dreamjobs")


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
