"""The mind's hands (SPEC §26, as amended) — policy for a tool call nobody asked for.

SPEC §26 used to end with "the mind never *initiates* tool calls; a tool-bearing
autonomous act needs the broker that comes with the workshop". The broker was
already here: `ToolBrain._execute` does allowlist → rate bucket → dedupe →
timeout → truncate → audit → host realisation, never raises, and has no
dependency on the streaming loop that calls it. What §26 actually deferred was
not the mechanism but the **policy** — five questions conversation answers
implicitly and a tick cannot:

  1. **Which hands?** The conversational allowlist is every discovered tool.
     This one is named explicitly, in `MIND_TOOL_ALLOWLIST`, and is empty even
     when the capability is switched on. Which puts a debt on `HANDS` below:
     names that live only in this file are names nobody can allowlist, so the
     table carries a phrase per hand and both settings surfaces publish it.
  2. **Where does the product land?** In the Vault. A contract built here is
     stamped `_deliver: "vault"`; `Researcher` and `SelfieLab` honour it by
     shelving the product and posting nothing. Delivery to the user is Gate 2's
     decision and Gate 2's alone — a product that posts itself is Gate 2
     bypassed by the back door.
  3. **What stops a repeat?** `Guard.turn()` is one dedupe scope per *reply*,
     and the mind has ticks, not turns. So the fingerprint ledger below is
     persistent and per-tool: research is cooled down for days, the desk for
     minutes.
  4. **Who pays?** A second `Guard` with its own buckets, so a night of
     autonomous work cannot leave the morning's request rate-limited; a hard
     daily call cap that refuses *before* dispatch; and, for the expensive
     class, budget pressure as a **precondition** rather than the post-hoc
     estimate `MIND_DAILY_TOKENS` is.
  5. **How does the answer come back?** `task_completion`, which the loop was
     designed for and which the two off-turn workers now post.

Two switches, in series, never one overriding the other (the §18.4.6 notify
pattern): `MIND_TOOLS_ENABLED` says whether anything on this machine may act
unasked; `LoopSwitches.hands` says whether *she* is one of the ones that may.
Off means the hands are not described to her at all — `SEARCH_BACKEND=off`'s
rule, generalised: a hand she may not use is not advertised to her.

What stays omitted from §26 is the workshop: no code execution, no shell. That
is a different capability with a different threat model, and it is the one that
genuinely needs a sandbox.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from yurios.kernel import correlate
from yurios.kernel.clock import Clock
from yurios.world.tools.guard import Guard, _fingerprint

from .policy import DORMANT, DREAM, ENGAGED
from .util import day_of

log = logging.getLogger("mind.hands")


@dataclass(frozen=True)
class Hand:
    """One hand she has, in the four facts every surface needs of it.

    `does` is written for somebody filling in `MIND_TOOL_ALLOWLIST` — the
    variable is a list of names and nothing else, so the panel and
    `yurios settings MIND_TOOL_ALLOWLIST` read these phrases out beside the
    names. `args` is written for her: one example argument object, kept here
    rather than read off the MCP spec because the tool schema is JSON Schema
    and this has to fit on one line of a prompt.
    """
    #: "cheap" | "expensive" — the whole of the permission model's
    #: read/write/internet axis in this first cut.
    klass: str
    #: One phrase, for whoever is choosing which hands she may have.
    does: str
    #: One example argument object, so the line she writes is shaped right.
    args: str
    #: The `.env` backend this hand is nothing without, or "". A hand whose
    #: backend is off is dropped from the allowlist and never advertised.
    needs: str = ""


#: Every hand she has. ONE table rather than the several keyed by the same names
#: that this used to be: a hand added to the cost class and forgotten in the
#: argument hints is a malformed line in the prompt she answers, and there was
#: no column at all for the thing anybody configuring her needs — what a name
#: means. The classes and the backend requirements are read off it below.
#:
#: Cheap is a step *in* goal work, not a whole tick's intention: local, and with
#: no outside party on the other end. Allowed wherever she isn't mid-conversation.
#: Expensive is one whole tick's intention — money, somebody else's server, or
#: her GPU — and therefore a pressure ceiling, a stricter state rule, and a
#: cooldown in days.
HANDS: dict[str, Hand] = {
    "write_note": Hand(
        "cheap", "start a new note in her Vault",
        '{"path": "notes/x.md", "text": "..."}'),
    "append_note": Hand(
        "cheap", "add to the end of a note she already has",
        '{"path": "notes/x.md", "text": "..."}'),
    "edit_note": Hand(
        "cheap", "replace a passage inside one of her notes",
        '{"path": "notes/x.md", "old_text": "...", "new_text": "..."}'),
    "delete_note": Hand(
        "cheap", "throw one of her own notes away",
        '{"path": "notes/x.md"}'),
    "read_note": Hand(
        "cheap", "read one of her notes back",
        '{"path": "notes/x.md"}'),
    "list_notes": Hand(
        "cheap", "see what is on her desk",
        '{"folder": ""}'),
    "count_note_lines": Hand(
        "cheap", "check how long a note has got",
        '{"path": "notes/x.md"}'),
    "read_skill": Hand(
        "cheap", "read back something she wrote down how to do",
        '{"name": "a-skill"}'),
    "write_skill": Hand(
        "cheap", "write down how to do something, for next time",
        '{"name": "a-skill", "description": "...", "instructions": "..."}'),
    "set_timer": Hand(
        "cheap", "leave herself a reminder to come back to this",
        '{"minutes": 10, "label": "..."}'),
    "research": Hand(
        "expensive", "read several pages on a topic and write up what she found",
        '{"topic": "...", "depth": 3}', needs="SEARCH_BACKEND"),
    "read_page": Hand(
        "expensive", "read one web page she has the address of",
        '{"url": "https://..."}', needs="SEARCH_BACKEND"),
    "web_search": Hand(
        "expensive", "search the web and read what comes back",
        '{"query": "..."}', needs="SEARCH_BACKEND"),
    "take_selfie": Hand(
        "expensive", "make a picture of herself, onto her gallery shelf",
        '{"look": "..."}', needs="SELFIE_BACKEND"),
    "show_picture": Hand(
        "expensive", "make a picture of something she is thinking about",
        '{"subject": "..."}', needs="SELFIE_BACKEND"),
}

CHEAP = tuple(n for n, h in HANDS.items() if h.klass == "cheap")
EXPENSIVE = tuple(n for n, h in HANDS.items() if h.klass == "expensive")

#: Web hands need `SEARCH_BACKEND != off`; camera hands need a camera. Read off
#: the table rather than tested for inline so the "off means unadvertised" rule
#: is one column instead of three conditionals.
NEEDS_WEB = tuple(n for n, h in HANDS.items() if h.needs == "SEARCH_BACKEND")
NEEDS_CAMERA = tuple(n for n, h in HANDS.items() if h.needs == "SELFIE_BACKEND")

#: Tools that answer `{"status": "started"}` and finish off-tick (§7.6). A goal
#: that dispatches one goes to `waiting` and is woken by `task_completion`.
START_DONT_AWAIT = ("research", "take_selfie", "show_picture")

#: `needs` -> the config attribute that says whether that backend is on.
_BACKEND_ATTR = {"SEARCH_BACKEND": "search_backend",
                 "SELFIE_BACKEND": "selfie_backend"}


def klass(tool: str) -> str:
    """"cheap" | "expensive" | "" — the cost class, which is the whole of the
    permission model's read/write/internet axis in this first cut."""
    hand = HANDS.get(tool)
    return hand.klass if hand else ""


def available(tool: str, cfg: object) -> bool:
    """Whether this installation can offer this hand at all — its backend is on.

    Separate from the allowlist because the two answer different questions:
    the allowlist is what you asked for, this is what the machine can do. The
    settings surfaces show a hand whose backend is off rather than hiding it,
    with the reason beside it — "there is no such hand" and "you have not turned
    its backend on" are different sentences and only one of them is fixable.
    """
    hand = HANDS.get(tool)
    if hand is None:
        return False
    if not hand.needs:
        return True
    attr = _BACKEND_ATTR[hand.needs]
    return str(getattr(cfg, attr, "off") or "off") != "off"


def describe_hands(cfg: object | None = None) -> list[dict]:
    """The whole vocabulary `MIND_TOOL_ALLOWLIST` is written in.

    This exists because the variable is a comma-separated list of names and
    nothing on either surface could say what the names were: a text box with no
    catalogue behind it asks you to allowlist things you have never been shown.
    The settings panel renders this as tick-boxes and `yurios settings` prints
    it; both therefore say what a hand does, what it costs, and — given a
    `cfg` — whether this machine can offer it at all.
    """
    return [{"name": name, "klass": hand.klass, "does": hand.does,
             "needs": hand.needs,
             "available": True if cfg is None else available(name, cfg)}
            for name, hand in HANDS.items()]


@dataclass(frozen=True)
class Offer:
    """What her hands can reach for on this tick, and why not when they can't.

    `reason` is non-empty exactly when `tools` is empty, and it is written to be
    read in the tick trace: a blocked hand should show up as a runner-up with a
    sentence on it, not as an exception inside ACT.
    """
    tools: tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.tools)


@dataclass
class Hands:
    """The mind's tool policy: the allowlist, the ledger, the caps, the guard.

    Built even when the capability is off — `enabled` is then False, `offer()`
    returns nothing, and nothing about the tools reaches a prompt. That keeps
    the loop free of `if self.hands is not None` at six call sites, and keeps
    the default-off proof to one property rather than an absence.
    """

    cfg: object
    clock: Clock
    guard: Guard | None = None
    #: () -> ToolRunner | None. A getter, not a reference: the runner is wired
    #: onto the brain at start-up and can be replaced (a failed MCP start leaves
    #: None), and the mind is built either side of that.
    runner: object = None
    #: The live per-character switch. Revoking it denies every subsequent call
    #: and says so in the audit; it cancels nothing already dispatched, because
    #: a kill switch that pretends to recall a running request is lying.
    granted: bool = True

    #: fingerprint -> the clock time it was last dispatched (persistent, §22).
    ledger: dict = field(default_factory=dict)
    #: {"date": "YYYY-MM-DD", "count": n} — rolls at local midnight the same way
    #: `interrupts` does, and for the same reason: her day is a local day.
    spent: dict = field(default_factory=lambda: {"date": "", "count": 0})

    #: Names already complained about. `allowlist` is read on every tick, and a
    #: misconfiguration that logged once a minute forever would be the loudest
    #: line in the file and still tell you nothing the first one didn't.
    _warned: set = field(default_factory=set, repr=False)

    # ------------------------------------------------------------- the switches

    @property
    def allowlist(self) -> tuple[str, ...]:
        """Exactly the names configured, filtered to hands that exist here.

        No wildcard and no inheritance from the conversational set: a tool the
        house has never heard of is not a hand, and a tool the *config* has
        turned off (no camera, no search backend) is not one either. Both drops
        say so in the log, because a typo in this variable is the difference
        between "she may write notes" and "she may do nothing" and both of
        those are quiet — and because "I ticked research and she never
        researches" has an answer (`SEARCH_BACKEND=off`) that is nowhere near
        this variable.
        """
        names: list[str] = []
        for raw in str(getattr(self.cfg, "mind_tool_allowlist", "") or "").split(","):
            name = raw.strip()
            if not name:
                continue
            hand = HANDS.get(name)
            if hand is None:
                self._warn(name, "%r is not a hand she has — known names are %s",
                           name, ", ".join(HANDS))
                continue
            if not available(name, self.cfg):
                self._warn(name, "%s needs %s, which is off — dropping it",
                           name, hand.needs)
                continue
            names.append(name)
        return tuple(dict.fromkeys(names))

    def _warn(self, name: str, message: str, *args) -> None:
        """One line per bad name, whatever the tick rate."""
        if name in self._warned:
            return
        self._warned.add(name)
        log.warning("MIND_TOOL_ALLOWLIST: " + message, *args)

    @property
    def enabled(self) -> bool:
        """Both switches, an allowlist with something in it, and a way to call.

        `mind_tools_enabled` is already the two switches multiplied together by
        the time a character runtime sees it (host.config_for_character), which
        is what keeps "in series" from becoming "whichever was checked last".
        """
        return bool(getattr(self.cfg, "mind_tools_enabled", False)
                    and self.granted
                    and self.allowlist
                    and self.guard is not None
                    and self._runner() is not None)

    def _runner(self):
        try:
            return self.runner() if callable(self.runner) else self.runner
        except Exception:       # noqa: BLE001 — a brain mid-rebuild
            return None

    # ------------------------------------------------------------- the ledger

    def load(self, state: dict) -> None:
        """Rehydrate from `state/engine.json` (§15.4). A restart resumes its
        cooldowns and its daily count; forgetting them is how a restart loop
        becomes an unmetered one."""
        ledger = state.get("hand_calls")
        self.ledger = dict(ledger) if isinstance(ledger, dict) else {}
        spent = state.get("hand_spend")
        if isinstance(spent, dict) and "count" in spent:
            self.spent = {"date": str(spent.get("date", "")),
                          "count": int(spent.get("count", 0))}

    def snapshot(self) -> dict:
        return {"hand_calls": dict(self.ledger), "hand_spend": dict(self.spent)}

    def roll(self) -> None:
        """Roll the day and forget cooldowns that have fully expired.

        Called from REGULATE. The pruning is not tidiness: the ledger is
        persisted in the engine snapshot every tick, and an always-on mind that
        never forgot a fingerprint would rewrite a growing file forever.
        """
        today = day_of(self.clock.now())
        if self.spent.get("date") != today:
            self.spent = {"date": today, "count": 0}
        now = self.clock.now()
        longest = max(self._cooldown_s(t) for t in (CHEAP + EXPENSIVE))
        self.ledger = {fp: at for fp, at in self.ledger.items()
                       if now - float(at) < longest}

    def _cooldown_s(self, tool: str) -> float:
        """Per-tool if the override names it, else the class default — and never
        shorter than the goal's own re-consider gap.

        That floor is the whole difference between a cooldown and a decoration.
        A goal is re-appraised every `MIND_CONSIDER_COOLDOWN_S`; if a hand's
        fingerprint has expired by then, the goal comes back round, the ledger
        has forgotten, and she makes the identical call again — hourly, forever.
        Applied to the per-tool override too, because a knob that can be set
        below the floor is a knob that will be.
        """
        floor = float(getattr(self.cfg, "mind_consider_cooldown_s", 0.0) or 0.0)
        for raw in str(getattr(self.cfg, "mind_tool_cooldown_s", "") or "").split(","):
            name, _, seconds = raw.partition("=")
            if name.strip() == tool and seconds.strip():
                try:
                    return max(floor, float(seconds))
                except ValueError:
                    log.warning("MIND_TOOL_COOLDOWN_S: %r is not a number", seconds)
        if klass(tool) == "expensive":
            return max(floor, float(
                getattr(self.cfg, "mind_tool_cooldown_expensive_s", 172_800)))
        return max(floor, float(getattr(self.cfg, "mind_tool_cooldown_cheap_s", 21_600)))

    def cooling(self, tool: str, args: dict | None) -> float:
        """Seconds left on this exact call's cooldown, or 0.0.

        Exact, like `Guard._fingerprint`: `cozy` and `bare` are two photos she
        may well have meant, so only a byte-identical repeat is a repeat.
        """
        at = self.ledger.get(_fingerprint(tool, args))
        if at is None:
            return 0.0
        left = self._cooldown_s(tool) - (self.clock.now() - float(at))
        return max(0.0, left)

    # ------------------------------------------------------- the preconditions

    def offer(self, *, state: str, pressure: float,
              user_present: bool) -> Offer:
        """Which hands are reachable on this tick — DECIDE's question (§18.1).

        Checked here rather than inside ACT so a blocked hand shows up in the
        trace with its score and its reason, instead of as an exception behind
        a decision that already committed.
        """
        if not getattr(self.cfg, "mind_tools_enabled", False):
            return Offer(reason="")             # off means invisible, trace included
        if not self.granted:
            return Offer(reason="her hands are switched off for this character")
        if not self.allowlist:
            return Offer(reason="no hand is on MIND_TOOL_ALLOWLIST")
        if self.guard is None or self._runner() is None:
            return Offer(reason="no tool server is running")
        if state == ENGAGED:
            return Offer(reason="she is mid-conversation — those are the "
                                "conversational hands' turn")
        cap = int(getattr(self.cfg, "mind_tool_calls_per_day", 8))
        if self._count() >= cap:
            return Offer(reason=f"today's {cap} autonomous calls are spent")
        ceiling = float(getattr(self.cfg, "mind_tool_pressure_ceiling", 0.5))
        expensive_ok = (pressure < ceiling
                        and (state in (DORMANT, DREAM) or not user_present))
        tools = tuple(t for t in self.allowlist
                      if klass(t) == "cheap" or expensive_ok)
        if not tools:
            if pressure >= ceiling:
                return Offer(reason=f"budget pressure {pressure:.2f} is over "
                                    f"the {ceiling:g} ceiling for expensive hands")
            return Offer(reason="expensive hands wait for the room to be empty")
        return Offer(tools=tools)

    def check(self, tool: str, args: dict | None, *, state: str,
              pressure: float, user_present: bool) -> tuple[bool, str]:
        """The one call's own preconditions, re-checked at dispatch.

        `offer()` says what she may reach for; this says whether *this* reach is
        allowed. Both, because the offer is computed before the model chooses
        and the switch can be revoked in between — which is exactly what the
        kill switch has to survive.
        """
        available = self.offer(state=state, pressure=pressure,
                               user_present=user_present)
        if not available:
            return False, available.reason or "her hands are off"
        if tool not in available.tools:
            if tool in self.allowlist:
                return False, f"{tool} is not available in {state}"
            return False, "not a hand she may use on her own"
        left = self.cooling(tool, args)
        if left > 0:
            return False, f"she already did this — {left / 60:.0f} min of cooldown left"
        return True, ""

    def _count(self) -> int:
        if self.spent.get("date") != day_of(self.clock.now()):
            return 0
        return int(self.spent.get("count", 0))

    def spend(self, tool: str, args: dict | None) -> None:
        """Book the call: the fingerprint into the ledger, one off the day's cap.

        Called after a call is *allowed*, not after it succeeds. A research run
        that errored still went and asked somebody's server, and re-dispatching
        it on the next tick because it failed is precisely the loop the ledger
        exists to stop.
        """
        self.ledger[_fingerprint(tool, args)] = self.clock.now()
        today = day_of(self.clock.now())
        if self.spent.get("date") != today:
            self.spent = {"date": today, "count": 0}
        self.spent["count"] = int(self.spent.get("count", 0)) + 1

    # ------------------------------------------------------------ the dispatch

    async def execute(self, tool: str, args: dict, *, timeout_s: float) -> str:
        """Guard → MCP → audit. Never raises: a failed hand is a sentence in the
        journal, not a dead heartbeat.

        Deliberately the same shape as `ToolBrain._execute`, with one difference
        that matters and one that doesn't. The one that matters: the guard is
        the mind's own instance, with its own buckets, so this can never spend
        conversation's. The one that doesn't: there is no `Turn`, because there
        is no turn — the persistent ledger above is the mind's dedupe scope.

        The audit line lands in the SAME `calls.jsonl` as her conversational
        hands, because there must be exactly one honest record of what her hands
        did. The `mind_tool` correlate kind is what tells the two apart, and it
        gives the debug page "what did she reach for on her own" for free.
        """
        assert self.guard is not None
        runner = self._runner()
        t0 = self.guard.clock.now()
        ok, reason = self.guard.check(tool, args)
        if not ok or runner is None:
            reason = reason or "no tool server is running"
            self.guard.audit(tool, args, f"denied: {reason}", 0.0, "")
            return f"denied ({reason})"
        try:
            text = await asyncio.wait_for(runner.call(tool, args),
                                          timeout=timeout_s)
        except Exception as e:                 # timeout, tool error, transport
            dt = (self.guard.clock.now() - t0) * 1000
            self.guard.audit(tool, args, "error", dt, str(e))
            return f"error ({e})"
        dt = (self.guard.clock.now() - t0) * 1000
        self.guard.audit(tool, args, "ok", dt, self.guard.truncate(text))
        return text

    def deny(self, tool: str, args: dict, reason: str) -> None:
        """Audit a call the preconditions refused before it was ever dispatched.

        A denial that leaves no line is the failure mode the whole audit exists
        against: "what did she try to do at 4am" has to be answerable in the
        same file as "what did she do".
        """
        if self.guard is None:
            return
        self.guard.audit(tool, args, f"denied: {reason}", 0.0, "")

    # --------------------------------------------------------------- the prompt

    def catalog(self, tools: tuple[str, ...]) -> str:
        """How the offered hands are described to her, or "" for none.

        Only the offered ones — off means invisible (principle 9). She is asked
        for one line of intent, not for a tool-marker grammar: the conversational
        parser exists because a reply is a stream she is talking through, and a
        tick is not. One structured line is easier for a 12B model to get right
        and easier for this file to refuse.
        """
        if not tools:
            return ""
        rows = "\n".join(
            f"  use {t} {HANDS[t].args if t in HANDS else '{...}'}" for t in tools)
        return (
            "You may take ONE action this tick, and only one. Answer with one "
            "line, in one of these two forms:\n\n"
            "  think <a short note to yourself about this goal>\n"
            f"{rows}\n\n"
            # She writes the reason beside the call anyway; asking for it is
            # cheaper than parsing around it, and the note is what the next tick
            # reads off the desk.
            "If you reach for a hand, put a `think` line above it saying why — "
            "the result alone won't tell you next time.\n\n"
            "…where the part after the tool name is one line of JSON. Prefer "
            "`think` — most steps are thinking. Reach for a hand only when the "
            "step genuinely needs it, and never for something you already did. "
            "Whatever a hand produces is kept for you, not sent to anyone: "
            "nothing you do here reaches them until you decide, separately, to "
            "say so.")


@dataclass(frozen=True)
class Intent:
    """One parsed line of the answer above: a thought, or a reach."""
    kind: str               # "think" | "use"
    #: Her words either way. On a `think` that is the whole note; on a `use` it
    #: is the reason she gave for reaching, which she writes beside the call far
    #: more often than not — and which is the half the *next* tick needs, since
    #: a tool result says what happened and never why she wanted it.
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)


def parse_intent(reply: str, *, allowed: tuple[str, ...]) -> Intent:
    """Read her one line back. Anything unparseable is a thought, not an error.

    Failing safe *towards thinking* is the whole point: a malformed reach does
    nothing and journals the words, which is the same thing a quiet tick does.
    The conversational path can afford to ask her to retry a broken marker
    because somebody is waiting; here nobody is, and the next tick will come.
    """
    text = (reply or "").strip()
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip().lstrip("-• ").strip()
        if not line.lower().startswith("use "):
            continue
        rest = line[4:].strip()
        tool, _, raw = rest.partition(" ")
        tool = tool.strip().strip("`\"'")
        if tool not in allowed:
            continue
        raw = raw.strip()
        start, end = raw.find("{"), raw.rfind("}")
        args: dict = {}
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start:end + 1])
                args = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                # She named the hand and fumbled the JSON. An empty argument
                # object is a call the *server* will refuse with a sentence she
                # can read next tick, which beats guessing what she meant.
                args = {}
        return Intent("use", tool=tool, args=args,
                      text=_thought(lines[:index] + lines[index + 1:]))
    # everything else — including a plain paragraph — is her thinking
    return Intent("think", text=_thought(lines))


def _thought(lines: list[str]) -> str:
    """Her prose, with the `think` marker taken off the front if she wrote one.

    Kept separate from the reach because a 12B model reliably answers with both
    — a sentence of reasoning and then the call — and the reasoning is the part
    that has to survive to the next tick. Dropping it was how a goal ended up
    redoing on step 3 the thing step 2 had already done.
    """
    note = "\n".join(lines).strip()
    if note.lower().startswith("think "):
        note = note[6:].strip()
    return note.strip()


def stamp_contract(contract: dict, *, goal_id: str) -> dict:
    """The two fields the mind puts on every contract it builds.

    `_deliver` is the landing rule (SPEC §18, principle 8) and `_goal_id` is
    principle 7 — every autonomous call names the goal that wanted it, so
    `goals.md` stays the complete, readable list of what her hands might do and
    `task_completion` knows which goal to wake.
    """
    return {**contract, "_deliver": "vault", "_goal_id": goal_id}


def build_guard(cfg, clock: Clock) -> Guard | None:
    """The mind's own Guard: its own buckets, the shared audit (SPEC §7.3).

    `rates_per_min` doubles as the allowlist, which is exactly the property
    wanted here — a hand missing from this dict does not exist to the mind,
    whatever the conversational guard happens to allow. Returns None when there
    is nothing on the allowlist, so "she has no hands" is an absence rather than
    an empty object that looks like a capability.
    """
    hands = Hands(cfg=cfg, clock=clock)
    names = hands.allowlist
    if not names:
        return None
    rates = {}
    for name in names:
        if name in NEEDS_WEB:
            rates[name] = int(getattr(cfg, "tool_rate_mind_web", 1))
        elif name in NEEDS_CAMERA:
            rates[name] = int(getattr(cfg, "tool_rate_mind_camera", 1))
        elif klass(name) == "cheap":
            rates[name] = int(getattr(cfg, "tool_rate_mind_desk", 4))
        else:
            rates[name] = int(getattr(cfg, "tool_rate_mind_other", 1))
    return Guard(rates_per_min=rates, log_dir=cfg.tool_log_dir, clock=clock,
                 max_bytes=getattr(cfg, "tool_log_max_bytes", None))


#: The correlate kind every mind-initiated call is stamped with, re-exported so
#: callers don't have to reach across into `kernel.correlate` for one constant.
MIND_TOOL = correlate.MIND_TOOL
