"""Goals & intentions (SPEC §22) — `goals.md` is the store, human-readable.

This is what gives the background loop direction; without it an always-on
agent is a screensaver. Every goal carries **provenance** (who created it,
from what), a **commitment strategy** (how hard it defends itself against a
changing world), and a lifecycle: pending → active → waiting → done|abandoned.

Goal genesis is designed, not assumed — a store only the user writes to
starves the loop within weeks. The sources, stamped on every goal:
  * `user:*`     — explicit asks ("remind me to…").
  * `promise:*`  — REFLECT scans her own replies for commitments she made
    ("I'll look into that") and files each one; a companion who forgets her
    own promises is worse than one who forgets yours.
  * `maintenance:*` — DREAM backlog, knowledge drops.
  * `followup:<goal>` — work she finished, waiting to be mentioned.
The file itself is a markdown checklist: `cat vault/goals.md` reads as her
to-do list, because it is one.

A promise splits two ways and the difference decides everything downstream.
"I'll tell you when it lands" is a `reach_out`: the whole content is that you
hear it, so its act is a message and Gate 2 rules on it. "I'll look into that"
is a `task`: the content is work, its act is a working step, and it may reach
for a hand (§26.2). Filing both as `reach_out` — which is what this did until
`promise_kind` — meant she talked about everything and did none of it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from typing import Iterable

from yurios.kernel.clock import Clock

from .util import iso_of, new_id, ts_of_iso
from .vaultio import MindVault

COMMITMENTS = ("blind", "single-minded", "open-minded")

# Content-word overlap catches one intention rephrased without collapsing two
# generic relationship goals whose embeddings happen to sit close together.
GOAL_ECHO_THRESHOLD = 0.28
_ECHO_STOP = frozenset("""
a an the and or but if then so to of in on at for with without from by as is
are was were be been being it its this that these those there here you your
yours i me my mine we our us he him his she her hers they them their do does
did done doing have has had having will would can could should may might must
not no nor now just about into over under before after again more most one two
three what which who whom when where why how all any both each few other some
such only own same than too very
""".split())


@dataclass
class Goal:
    id: str
    text: str
    kind: str = "task"          # reach_out | task | maintenance
    priority: float = 0.5
    due: str | None = None      # ISO — when this becomes time-sensitive
    commitment: str = "single-minded"
    provenance: str = "user"    # source[:detail]
    state: str = "pending"      # pending | active | waiting | done | abandoned
    created: str = ""
    meta: dict = field(default_factory=dict)

    def is_due(self, clock: Clock, horizon_hours: float = 12.0) -> bool:
        if self.due is None:
            return False
        return (ts_of_iso(self.due) - clock.now()) / 3600 <= horizon_hours

    def is_stale(self, clock: Clock) -> bool:
        """Past due — whether it is defended or dropped is the commitment's call."""
        return self.due is not None and ts_of_iso(self.due) < clock.now()

    @property
    def steps(self) -> int:
        """How many working ticks this goal has already had (SPEC §22).

        The horizon lives in `meta` rather than in a column because it is
        bookkeeping the *loop* keeps, not something a person reading
        `goals.md` as a checklist needs a heading for.
        """
        try:
            return int(self.meta.get("steps", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def dispatched(self) -> dict:
        """The tool call this goal is waiting on, or `{}` (SPEC §22, §7.3).

        Written when a `tool_step` starts work the loop will not await, read
        when `task_completion` comes back to decide which goal it belongs to.
        """
        d = self.meta.get("dispatched")
        return d if isinstance(d, dict) else {}


def _echo_words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]+", (text or "").lower())
            if len(word) >= 3 and word not in _ECHO_STOP}


def night_owned(text: str) -> bool:
    """True when the text is DREAM's job, not a daytime desk task (SPEC §22.1b).

    Strategy used to refile the `maintenance:dream` leftover as "list diary
    vs kept-memory", and she then spent days on `list_notes` looking for a
    folder that is not on the desk. Consolidation writes
    `memory/semantic/facts.md`; `workspace/diary/` is a separate night job.
    A standing research job writes the morning brief. None of those is a hand.
    """
    t = " ".join((text or "").lower().split())
    if "kept-memory" in t or "kept memory" in t:
        return True
    if "unconsolidated" in t:
        return True
    if "consolidat" in t and any(
            w in t for w in ("night", "nights", "diary", "memory")):
        return True
    if any(p in t for p in ("morning brief", "market brief", "overnight brief",
                            "market-brief")):
        return True
    if "wake" in t and any(
            p in t for p in ("spot price", "s&p", "bitcoin", "gold",
                             "market number")):
        return True
    return False


def echoes(text: str, existing: Iterable[Goal]) -> Goal | None:
    """Return the open goal that expresses the same content, if one exists."""
    words = _echo_words(text)
    for goal in existing:
        if getattr(goal, "text", "").strip().lower() == (text or "").strip().lower():
            return goal
    # One content word ("see what's there") is too little evidence for a
    # repository-wide semantic merge. Exact equality above still deduplicates it.
    if len(words) < 2:
        return None
    for goal in existing:
        theirs = _echo_words(getattr(goal, "text", ""))
        if len(theirs) < 2:
            continue
        shared = words & theirs
        overlap = len(shared) / min(len(words), len(theirs))
        if len(shared) >= 3 and overlap >= GOAL_ECHO_THRESHOLD:
            return goal
    return None


LINE_RE = re.compile(r"^- \[(?P<done>[ x~])\] \((?P<id>[\w-]+)\) (?P<text>.*?)"
                     r"(?P<fields>(?: \| \w[\w-]*: [^|]*)*)$")


def trim(text: str, limit: int = 200) -> str:
    """One line, short enough that `goals.md` still reads as a checklist —
    the meta field rides on the goal's own line."""
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


class GoalStore:
    def __init__(self, vault: MindVault, clock: Clock):
        self.vault = vault
        self.clock = clock

    # ------------------------------------------------------------------ parse

    def all(self) -> list[Goal]:
        goals = []
        for line in self.vault.read("goals.md").splitlines():
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            fields = dict(re.findall(r"\| (\w[\w-]*): ([^|]*)", m.group("fields")))
            state = fields.get("state", "").strip()
            if not state:
                state = {"x": "done", "~": "abandoned"}.get(m.group("done"), "pending")
            goals.append(Goal(
                id=m.group("id"), text=m.group("text").strip(),
                kind=fields.get("kind", "task").strip(),
                priority=float(fields.get("priority", 0.5)),
                due=fields.get("due", "").strip() or None,
                commitment=fields.get("commit", "single-minded").strip(),
                provenance=fields.get("from", "user").strip(),
                state=state, created=fields.get("created", "").strip(),
                meta=_parse_meta(fields.get("meta", ""))))
        return goals

    def open_goals(self) -> list[Goal]:
        return [g for g in self.all() if g.state in ("pending", "active", "waiting")]

    def get(self, goal_id: str) -> Goal | None:
        return next((g for g in self.all() if g.id == goal_id), None)

    # ------------------------------------------------------------------ write

    def _render(self, goals: list[Goal]) -> str:
        lines = ["# Goals", ""]
        for g in goals:
            box = {"done": "x", "abandoned": "~"}.get(g.state, " ")
            parts = [f"- [{box}] ({g.id}) {g.text}",
                     f"kind: {g.kind}", f"priority: {g.priority}"]
            if g.due:
                parts.append(f"due: {g.due}")
            parts += [f"commit: {g.commitment}", f"from: {g.provenance}",
                      f"state: {g.state}", f"created: {g.created}"]
            if g.meta:
                parts.append(f"meta: {json.dumps(g.meta, ensure_ascii=False)}")
            lines.append(" | ".join(parts))
        return "\n".join(lines) + "\n"

    def _save(self, goals: list[Goal]) -> None:
        self.vault.write("goals.md", self._render(goals))

    def add(self, text: str, *, kind: str = "task", priority: float = 0.5,
            due: str | None = None, commitment: str = "single-minded",
            provenance: str = "user", meta: dict | None = None) -> Goal:
        goals = self.all()
        open_goals = [g for g in goals
                      if g.state in ("pending", "active", "waiting")]
        existing = echoes(text, open_goals)
        if isinstance(existing, Goal):
            return existing
        g = Goal(id=new_id("g"), text=text, kind=kind, priority=priority, due=due,
                 commitment=commitment, provenance=provenance,
                 created=iso_of(self.clock.now()), meta=meta or {})
        goals.append(g)
        self._save(goals)
        return g

    def set_state(self, goal_id: str, state: str) -> None:
        self.update(goal_id, state=state)

    def update(self, goal_id: str, *, state: str | None = None,
               meta: dict | None = None, priority: float | None = None,
               due: str | None = None) -> Goal | None:
        """One read-modify-write over `goals.md`, for everything a working tick
        changes about a goal at once.

        `set_state` used to be the only mutator, which is why the lifecycle was
        never used: advancing a goal means moving its state *and* recording what
        step it is on *and* often when to look at it again, and three separate
        rewrites of the checklist would be three chances to lose one of them.
        `meta` is merged, not replaced — the provenance a dispatched tool call
        leaves behind must survive the next step's step-counter bump.
        """
        goals = self.all()
        found: Goal | None = None
        for g in goals:
            if g.id != goal_id:
                continue
            found = g
            if state is not None:
                g.state = state
            if meta:
                g.meta = {**g.meta, **meta}
            if priority is not None:
                g.priority = priority
            if due is not None:
                g.due = due
        if found is not None:
            self._save(goals)
        return found

    def reconsider(self) -> list[Goal]:
        """Apply commitment strategies to stale goals (SPEC §22.2): blind is
        defended, open-minded drops the moment it stops being timely.

        Returns the goals *this* pass let go of, not every goal that happens to
        be abandoned. The difference is the whole value of the return: the day
        rollover journals a line per goal it gets back (`loop._day_rollover`),
        so the wider reading re-announced "let go of: X (the moment for it
        passed)" every morning for every goal ever dropped — including ones you
        let go of yourself, hours earlier, by hand.
        """
        goals = self.all()
        dropped = []
        for g in goals:
            if g.state not in ("pending", "waiting"):
                continue
            if g.is_stale(self.clock) and g.commitment == "open-minded":
                g.state = "abandoned"
                dropped.append(g)
        if dropped:
            self._save(goals)
        return dropped


def _parse_meta(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


# --- REFLECT's promise scan (SPEC §22.1) ---------------------------------------
#
# A promise is not only "I'll". Live, the commitments that went unfiled were the
# softer ones — "let me look that up", "I can dig into that tonight", "I want to
# read more about it before I answer" — which is exactly the register a warm
# companion makes most of her commitments in. So the scan is a small family of
# openers rather than one, with three rules holding it steady:
#
#   * **the negation guard is shared.** "I'll never leave", "let me not push",
#     "I can't look that up" are not promises and must never become goals; the
#     guard is written once, in `_NEGATED`, and every opener consults it.
#   * **the cap and the dedupe stay.** At most three per exchange (a reply that
#     enumerates six things is not six commitments), and the same commitment
#     phrased twice in one reply is one goal.
#   * **conditionals are left alone.** "if you want, I can…" is an offer she is
#     waiting on an answer to, not something she has taken on — filing it makes
#     her chase work nobody asked for.
PROMISE_RES = (
    # "I'll look into it", "I will read it tonight"
    re.compile(r"\bI(?:'ll| will| shall)\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "let me look that up" — an offer she has already started making good on
    re.compile(r"\blet me\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "I can look that up", "I could dig into it" — capability offered as intent
    re.compile(r"\bI(?: can| could| should)\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "I want to read up on it first", "I'd like to think about it"
    re.compile(r"\bI(?: want to| need to|'d like to| mean to)\s+"
               r"(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "I'm going to sit with that"
    re.compile(r"\bI(?:'m| am) going to\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
)
REMIND_RE = re.compile(r"\bremind me to\s+(.{4,100}?)(?:[.!?\n]|$)", re.I)

#: What the opener may not be followed by. `not`/`never` invert the promise;
#: the modal "have to"/"had to" turns "I can" into a report rather than an offer.
_NEGATED = re.compile(r"^(?:never|not|n't|no\b|hardly|barely)\b", re.I)

#: An opener sitting inside a conditional is a question, not a commitment. Only
#: the clause the opener starts is inspected, so "I'll read it if you send it"
#: still files — she committed and named a precondition, which is different from
#: "if you like, I can read it".
_CONDITIONAL = re.compile(r"\b(?:if|unless|whether)\b[^.!?\n]{0,60}$", re.I)

#: …and the same offer, phrased with the condition on the end. "if you send it"
#: is a precondition on work she took on; "if you want" is her asking. Only the
#: second is excluded, and only because it is the one she is waiting on an
#: answer to.
_TRAILING_OFFER = re.compile(
    r",?\s*\b(?:if|unless)\s+(?:you|you'd|you would)\s+"
    r"(?:want|like|wish|prefer|care)\b.*$", re.I)


def _clean(text: str) -> str:
    return text.strip().rstrip(",;:").strip()


@dataclass(frozen=True)
class PromiseCandidate:
    """One possible commitment, preserving enough source for semantic review."""

    index: int
    text: str
    provenance: str
    source: str
    start: int
    confidence: str                 # explicit | strong | soft

    def as_dict(self) -> dict:
        return {"index": self.index, "text": self.text,
                "provenance": self.provenance, "source": self.source,
                "start": self.start, "confidence": self.confidence}


@dataclass(frozen=True)
class PromiseDecision:
    """The one canonical unresolved objective accepted from an exchange."""

    text: str
    kind: str
    rationale: str
    success: str
    candidates: tuple[int, ...]


class PromiseReviewError(ValueError):
    """A utility completion did not satisfy the promise-review contract."""


PROMISE_REVIEW_SYSTEM = """\
You are reviewing possible commitments extracted from one completed conversation.
Decide whether the character left any NEW, UNRESOLVED work after the reply ended.
Conversational framing, principles, feelings, capabilities, hypotheticals, and work
already completed in the same reply are not goals. Merge clauses that are steps of
one intention. Judge only the supplied candidates and user request. Return at most
one concrete, standalone objective grounded in their subject and object. Never copy
another task or invent work. If every candidate is framing, return null.

The final state at the END of the assistant reply controls. If the reply later gives
the result, confirmation, answer, artifact, or explicitly says the work is complete,
return null even when an earlier sentence said "I will" or "let me" do that work.

Return exactly one JSON object and no prose:
{"goal": null}
or
{"goal":{"text":"imperative objective","kind":"task|reach_out",\
"rationale":"why it remains owed","success":"observable completion",\
"candidates":[0]}}
"""

PROMISE_REVIEW_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "promise_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["goal"],
            "properties": {
                "goal": {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "kind", "rationale", "success",
                                         "candidates"],
                            "properties": {
                                "text": {"type": "string"},
                                "kind": {"type": "string",
                                         "enum": ["task", "reach_out"]},
                                "rationale": {"type": "string"},
                                "success": {"type": "string"},
                                "candidates": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                            },
                        },
                    ],
                },
            },
        },
    },
}


def discover_promise_candidates(reply: str, user_msg: str) -> list[PromiseCandidate]:
    """Find possible commitments in source order; make no semantic decision."""
    raw: list[tuple[int, str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def take(start: int, text: str, provenance: str, source: str,
             confidence: str) -> None:
        if _TRAILING_OFFER.search(text):
            return
        text = _clean(text)
        key = (text.lower(), provenance)
        if not text or _NEGATED.match(text) or key in seen:
            return
        seen.add(key)
        raw.append((start, text, provenance, source.strip(), confidence))

    # Explicit reminders are already decisions by the user. They remain first
    # for compatibility with the old three-item cap and bypass model review.
    for match in REMIND_RE.finditer(user_msg or ""):
        take(match.start(), match.group(1), "user:remind-me", match.group(0),
             "explicit")

    patterns = (
        (PROMISE_RES[0], "strong"),
        (PROMISE_RES[1], "soft"),
        (PROMISE_RES[2], "soft"),
        (PROMISE_RES[3], "soft"),
        (PROMISE_RES[4], "strong"),
    )
    found: list[tuple[int, str, str, str, str]] = []
    for pattern, confidence in patterns:
        for match in pattern.finditer(reply or ""):
            if _CONDITIONAL.search((reply or "")[:match.start()].rsplit("\n", 1)[-1]):
                continue
            found.append((match.start(), match.group(1),
                          "promise:her-own-words", match.group(0), confidence))
    for item in sorted(found, key=lambda value: value[0]):
        take(*item)

    # User reminders precede assistant candidates; each group retains source order.
    explicit = [item for item in raw if item[2].startswith("user:")]
    assistant = sorted((item for item in raw if item[2].startswith("promise:")),
                       key=lambda value: value[0])
    ordered = [*explicit, *assistant][:6]
    return [PromiseCandidate(index=i, start=item[0], text=item[1],
                             provenance=item[2], source=item[3],
                             confidence=item[4])
            for i, item in enumerate(ordered)]


def fallback_promises(candidates: list[PromiseCandidate]) -> list[PromiseCandidate]:
    """High-precision behavior when semantic review is unavailable."""
    return [candidate for candidate in candidates
            if candidate.confidence in ("explicit", "strong")][:1]


def promise_review_messages(*, user_text: str, reply: str,
                            candidates: list[dict], capabilities: list[str]) -> list[dict]:
    payload = {
        "user_request": user_text,
        "assistant_reply": reply,
        "candidates": candidates,
        "available_capabilities": capabilities,
    }
    return [{"role": "system", "content": PROMISE_REVIEW_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _json_payload(raw: str) -> dict:
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", raw or "", flags=re.S)
    fence = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, flags=re.S | re.I)
    if fence:
        text = fence.group(1)
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PromiseReviewError("promise review was not JSON") from exc
    if not isinstance(value, dict) or set(value) != {"goal"}:
        raise PromiseReviewError("promise review must contain only goal")
    return value


def parse_promise_review(raw: str, *, candidate_count: int) -> PromiseDecision | None:
    """Parse the strict utility contract; `None` is a valid no-goal decision."""
    value = _json_payload(raw)
    goal = value["goal"]
    if goal is None:
        return None
    required = {"text", "kind", "rationale", "success", "candidates"}
    if not isinstance(goal, dict) or set(goal) != required:
        raise PromiseReviewError("promise goal fields do not match the contract")
    text = " ".join(str(goal["text"]).split()).strip()
    rationale = " ".join(str(goal["rationale"]).split()).strip()
    success = " ".join(str(goal["success"]).split()).strip()
    kind = goal["kind"]
    indexes = goal["candidates"]
    if (not text or len(text) > 240 or "|" in text
            or kind not in ("task", "reach_out")
            or not rationale or len(rationale) > 400
            or not success or len(success) > 400
            or not isinstance(indexes, list) or not indexes
            or any(type(index) is not int for index in indexes)
            or len(set(indexes)) != len(indexes)
            or any(index < 0 or index >= candidate_count for index in indexes)):
        raise PromiseReviewError("invalid canonical promise goal")
    return PromiseDecision(text=text, kind=kind, rationale=rationale,
                           success=success, candidates=tuple(indexes))


def promise_decision_grounded(decision: PromiseDecision, candidates: list[dict],
                              user_text: str) -> bool:
    """A canonical objective must retain subject matter from its source exchange."""
    source = " ".join([
        user_text,
        *(str(candidates[index].get("text", "")) for index in decision.candidates),
        *(str(candidates[index].get("source", "")) for index in decision.candidates),
    ])
    return bool(_echo_words(decision.text) & _echo_words(source))


#: Telling verbs: the promise whose whole substance is that *they* hear it.
#:
#: Anchored at the front of the captured predicate on purpose. "I'll read it and
#: let you know" is work with a report on the end, and the work is the half she
#: has to do first — so it files as a `task`, and finishing it is what schedules
#: the telling (`_offer_to_tell`). "I'll let you know once I've read it" leads
#: with the report and files as a `reach_out`. The leading verb is the promise;
#: everything after it is when.
_TELLING_RE = re.compile(
    # optional throat-clearing before the verb — "probably tell you", "try to
    # let you know", "go and ask you". Deliberately a closed list: a wildcard
    # here would eat the verb it is supposed to be standing in front of.
    r"^(?:(?:probably|definitely|certainly|then|also|just|first|soon|quickly|"
    r"actually|maybe)\s+|(?:try|make sure|be sure|remember)\s+to\s+|"
    r"(?:go|come)\s+(?:and\s+)?)*"
    r"(?:"
    r"(?:tell|remind|update|warn|notify|ping|message|text|email|call|show|"
    r"send|ask|nudge|alert|answer)\s+you\b"
    r"|let\s+you\s+know"
    r"|keep\s+you\s+(?:posted|updated|informed|in\s+the\s+loop)"
    r"|fill\s+you\s+in"
    r"|(?:get|come|circle)\s+back\s+to\s+you"
    r"|(?:report|check|circle)\s+back\b"
    r"|follow\s+up\s+with\s+you"
    r"|run\s+(?:it|this|that|them)\s+(?:by|past)\s+you"
    r"|share\s+(?:it|this|that|them)\s+with\s+you"
    r")", re.I)


def promise_kind(text: str, provenance: str) -> str:
    """`task` or `reach_out` — which half of a promise this one is (§22.1).

    Only her own words are ever a `task`. An explicit "remind me to…" is a
    `reach_out` by definition however it is phrased: the entire thing they asked
    for is to be told, and doing it quietly would be doing the opposite.

    Anything else defaults to `reach_out`, which is the behaviour every goal had
    before this existed — an unrecognised provenance should not silently gain a
    capability, it should keep the one it had.
    """
    if not provenance.startswith("promise:"):
        return "reach_out"
    return "reach_out" if _TELLING_RE.match(_clean(text)) else "task"


def extract_promises(reply: str, user_msg: str) -> list[tuple[str, str]]:
    """Scan an exchange for commitments: hers, in her own words, and the
    user's explicit remind-me asks. Returns (text, provenance) pairs.

    Hers first and capped at three, because a reply that offers a list is one
    conversation, not a day's work — and a companion who files everything she
    ever said "I could" about ends up with a to-do list she can only fail."""
    return [(candidate.text, candidate.provenance)
            for candidate in discover_promise_candidates(reply, user_msg)[:3]]
