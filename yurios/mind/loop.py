"""The cognitive tick loop (SPEC §15) — SENSE → APPRAISE → DECIDE → ACT →
REFLECT → REGULATE, forever.

This file replaces Build #4's `world/idle.py` as the caller of everything the
host owns — the same `VrmController` strings, the same ambient-speech seam, the
same timer board — which was the entire point of that build's seams: nothing
below this file changed, the strings just got a real puppeteer. Where the idle
machine had states and dice, this loop has signals, salience, goals, and a
journal.

Three design rules keep it legible instead of chaotic (all normative):

  * **One intention per tick.** DECIDE commits to exactly one thing or to
    resting — the majority of all ticks. An agent that does one thing per
    heartbeat can be read like a diary; runaway fan-out cannot start.
  * **APPRAISE is cheap by construction.** It runs every tick, so it is pure
    heuristics — never a model call. The model is invoked only inside ACT, for
    work the loop has already decided is worth it. This one rule is what makes
    continuous presence economically possible.
  * **Everything is journaled.** The journal is simultaneously the audit trail
    and the product — the "what I did while you were out" surface.

Where conversation lives (SPEC §15.3): the reply itself stays on Build #2's
turn pipeline — the voice socket's sub-second reactive path, which no tick
cadence should ever sit in front of. The loop is that path's *observer and
consequence*: a user turn preempts it to ENGAGED from any state, and the
committed exchange arrives as a `turn_committed` signal that REFLECT folds in —
the world model updates, promises she made become goals she must keep. One
mind at two cadences; the loop owns everything between turns.
"""
from __future__ import annotations

import logging
import random
from typing import Awaitable, Callable

from yurios.app.core.assemble import age_tag, soul_preamble
from yurios.kernel import correlate
from yurios.world.avatar.controller import VrmController
from yurios.world.brain_protocol import AutonomousBrain
from yurios.kernel.clock import Clock
from yurios.kernel.hub import EventHub
from yurios.world.tools.timers import TimerBoard
from yurios.world.vram import PATIENT_WAIT_S, ParkGate

from .budget import BudgetGovernor
from .dream import DreamConsolidator
from .dreamjobs import SELF_GOAL, DreamRunner
from .goals import (Goal, GoalStore, PromiseCandidate,
                    PROMISE_REVIEW_RESPONSE_FORMAT,
                    discover_promise_candidates, fallback_promises,
                    parse_promise_review, promise_decision_grounded,
                    promise_kind, promise_review_messages)
from .hands import (START_DONT_AWAIT, Hands, build_guard, klass, parse_intent,
                    stamp_contract)
from .journal import Journal
from .knowledge import KnowledgeStore
from .promptlog import PromptLog
from .policy import (DREAM, ENGAGED, IDLE, ActivityController, Appraisal,
                     appraise_goal, appraise_signal, score_interrupt)
from .selfedit import SelfEdit
from .signals import Signal, SignalBus
from .trace import TickTrace
from .util import day_of, iso_of, new_id, read_json, ts_of_iso, write_json
from .vaultio import MindVault
from .workspace import SkillStore, Workspace
from .world import WorldModelStore

log = logging.getLogger("mind.loop")

SUSPEND_GAP_S = 2 * 3600.0

# scene canon, carried over from the idle machine it replaced (SPEC §15.5):
# when she rain-gazes, this is the window the scene builds — the corner glass on
# the −x wall (web/js/stage/SanctuaryScene.js), a step inside the pane. The wide
# window is behind her; this one she can actually turn her head to.
WINDOW_TARGET = {"x": -1.55, "y": 1.45, "z": 1.2}

ANNOUNCE_CUE = (
    "((The timer for “{label}” just finished. Tell {user} it's done — one "
    "short, warm spoken line, nothing else.))")

SELF_TALK_CUES = (
    "((It's been quiet for a while. Murmur one short line to yourself about "
    "the rain on the window — a private thought said softly aloud, not "
    "expecting an answer.))",
    "((A quiet stretch. One soft spoken line to yourself about this room — "
    "the lamp, the plant, the window seat. Half to yourself.))",
    "((The room is quiet. Let one small remembered thing about {user} "
    "surface, and say one gentle line to yourself about it.))",
)

REACH_OUT_CUE = (
    "((You decided, on your own, to reach out first about this: {goal}. Say "
    "the one short, warm, specific spoken message you'd open with — no "
    "preamble, no explaining that you decided to speak.))")


def _with_soul(messages: list[dict], preamble: str) -> list[dict]:
    """Prepend the persona blocks to a prompt's system message (SPEC §22.4).

    Fused onto the existing system message rather than added as a second one:
    a chat template renders two system turns however it likes, and half the
    local models this runs on silently drop the second. The instruction the
    caller wrote stays *after* the persona, because it is the part that says
    what this particular call is for and the last thing read wins ties.
    """
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = f"{preamble}\n\n{m.get('content', '')}".strip()
            return out
    return [{"role": "system", "content": preamble}, *out]


class MindLoop:
    """The autonomy engine, assembled over the host's existing surfaces."""

    def __init__(self, cfg, clock: Clock, *,
                 bus: SignalBus,
                 brain: AutonomousBrain,                   # a ToolBrain, in practice
                 controller: VrmController,
                 timers: TimerBoard,
                 hub: EventHub,
                 speak: Callable[[str], Awaitable[bool]],  # Runtime.speak_ambient
                 post_message,                             # Runtime.post_message
                 park_gate=None,                           # Runtime.park_gate
                 rng: random.Random | None = None):
        self.cfg = cfg
        self.clock = clock
        self.bus = bus
        self.brain = brain
        self.controller = controller
        self.timers = timers
        self.hub = hub
        self.speak = speak
        self.post_message = post_message
        # The camera's VRAM door (§7.6, world/vram.py). Her turns already wait
        # at it; so must everything down here, because a dream job asking the
        # utility model is exactly as good at loading a parked chat model back
        # onto a busy card as a turn is. One of our own by default, so a
        # MindLoop built without a host (tests, scripts) has a door that is
        # simply always open rather than a None to test for at every call.
        self.park_gate = park_gate if park_gate is not None else ParkGate()
        self.rng = rng or random.Random(cfg.mind_seed or None)

        # the mind's home: the same Vault the brain already keeps (SPEC §15.2)
        self.vault = MindVault(cfg.vault_dir)
        state = brain.state                    # the Build #1 AppState
        self.world = WorldModelStore(self.vault, clock, controller=controller,
                                     timers=timers, user_name=cfg.user_name)
        brain.set_world(self.world)            # the §19.2 seam swap: every prompt
                                               # now carries the store's stage
        self.knowledge = KnowledgeStore(
            self.vault, state.embedder, clock,
            utility=self._utility if cfg.utility_enabled and state.utility else None,
            min_score=cfg.knowledge_min_score)
        brain.set_knowledge(self.knowledge)    # §20.2: the shelf joins the
                                               # prompt's knowledge slot
        # her desk and her skills (§34). Built before the dream runner, which
        # gives jobs a place to write, and before the brain seam below.
        self._desk_notes: list[str] = []       # drained by REFLECT, see below
        self.workspace = (Workspace(cfg.vault_dir / "workspace")
                          if cfg.workspace_enabled else None)
        self.skills = (SkillStore(cfg.vault_dir / "skills")
                       if cfg.skills_enabled else None)
        brain.set_workspace(self.workspace, self.skills,
                            on_write=self._desk_written)
        self.goals = GoalStore(self.vault, clock)
        #: (key, rendered, when) for `_soul_text` — see there.
        self._soul_cache: tuple = ((None, None), "", -1e18)
        self.selfedit = SelfEdit(self.vault, clock)
        brain.set_goals(self.goals)            # §22: her standing list joins the
                                               # conversational prompt, so the
                                               # talking-self and the intending-
                                               # self stop being two people
        brain.set_selfedit(self.selfedit)      # §23: `propose_edit` gets a door
        self.journal = Journal(self.vault, clock, hub, store=state.store)
        # The same memory the conversation recalls from (§22.4). Kept rather
        # than passed along, because goal work needs to ask it questions too:
        # semantic facts told her *what* is true of you, and nothing told her
        # when any of it happened.
        self.store = state.store
        self.dream = DreamConsolidator(self.vault, state.store, clock,
                                       utility=self._utility
                                       if state.utility else None)
        # …and the pipeline it is now the first job of (§21.2). `self.dream`
        # stays the consolidator: it is still what §21 means and what the tests
        # pin, and the runner wraps rather than replaces it.
        self.dreams = DreamRunner(
            self.vault, state.store, clock, cfg,
            consolidator=self.dream, goals=self.goals,
            workspace=self.workspace, skills=self.skills,
            utility=self._utility if state.utility else None,
            selfie=(brain.selfies.start
                    if getattr(brain, "selfies", None) is not None else None),
            # The night's web hands (§7.7 applied to §21.2). The `Researcher` is
            # handed over whole rather than as two loose callables: it already
            # owns the search provider, the fetcher and `shelve()`, and keeping
            # them together is what makes "what she reads she keeps" true of the
            # unattended hours too.
            research=getattr(brain, "research", None),
            deliver_report=self._deliver_report,
            # A night's desk writes and its camera dispatch land in the same
            # audit as her daytime hands (§7.3), so the Tools page answers
            # "what did this actually do to the vault" for DREAM too.
            soul_text=self._soul_text,
            drives=self._soul_drives,
            audit=(brain.guard.audit
                   if getattr(brain, "guard", None) is not None else None))
        state_dir = cfg.vault_dir / "state"
        self.activity = ActivityController(state_dir, clock, cfg,
                                           trace_dir=cfg.trace_dir)
        self.budget = BudgetGovernor(state_dir, clock,
                                     daily_tokens=cfg.mind_daily_tokens)
        self.trace = TickTrace(cfg.trace_dir, clock, max_bytes=cfg.mind_trace_max_bytes)
        # Her hands, in the loop (SPEC §26, as amended — mind/hands.py). Built
        # unconditionally and off by default: `enabled` is one property to read
        # rather than a None to test for at six call sites, and the guard is a
        # SECOND instance with its own buckets so a night of autonomous work can
        # never leave the morning's request rate-limited.
        self.hands = Hands(cfg=cfg, clock=clock,
                           guard=build_guard(cfg, clock),
                           runner=lambda: getattr(self.brain, "runner", None))
        # Share the runtime's sink when there is one, so her conversational
        # prompts and her private ones land in one file with one rotation state.
        # A test brain has none; build our own rather than lose the record.
        self.prompt_log = (getattr(brain, "prompt_log", None)
                           or PromptLog.from_config(cfg, clock))

        # rehydration snapshot (SPEC §15.4): a restart resumes, not forgets
        self.state_path = state_dir / "engine.json"
        st = read_json(self.state_path, None) or {}
        self.offset: int = st.get("bus_offset", 0)
        self.interrupts: dict = st.get("interrupts", {"date": "", "count": 0})
        self.considered: dict = st.get("considered", {})
        self.last_tick_ts: float | None = st.get("last_tick_ts")
        # goal id -> when to consider it next (SPEC §16, the `wakeup` signal).
        # A goal that decided "look at this again after lunch" said something the
        # hourly consider cooldown cannot express, and a restart that forgot it
        # would quietly turn a scheduled follow-up into an hourly one.
        self.wakeups: dict = st.get("wakeups", {})
        # Which day's reconsideration has already run (§22.2). Commitment
        # strategies used to apply only after a suspend gap — i.e. only when the
        # machine had slept two hours — so a goal that went stale while she was
        # awake was defended forever by a strategy that never got asked.
        self.reconsidered_on: str = st.get("reconsidered_on", "")
        # The one-shot §5.4 handoff, below. A date rather than a bool so the
        # journal line and the goal it files can be traced back to the day.
        self.bootstrapped_on: str = st.get("bootstrapped_on", "")
        reviews = st.get("promise_reviews", [])
        self.promise_reviews: list[dict] = [
            review for review in reviews
            if isinstance(review, dict)
            and isinstance(review.get("candidates"), list)
        ] if isinstance(reviews, list) else []
        self.hands.load(st)

        self._session: str | None = None       # lazy brain session for her own words
        self._pending_announce: list = []      # timer promises awaiting delivery
        self._turns_in_flight = 0
        self._last_turn_end = clock.now()
        self._next_self_talk = clock.now() + self._uniform(
            cfg.idle_talk_min_s, cfg.idle_talk_max_s)
        self._next_body_act = 0.0
        self._gaze_until = 0.0
        self._tick_id = ""

    # ------------------------------------------------------------- host seams

    def _soul_text(self, *, full: bool = True) -> str:
        """The persona blocks her private prompts open with (SPEC §22.4, §7.1).

        Everything the mind sent a model used to be characterless. `_utility`
        below is the seam for the knowledge store, every DREAM job and the
        goal-work step, and it passed the caller's messages through untouched —
        so her diary's entire character content was the string "You are
        {char}", and two vaults with completely different cards produced
        byte-identical prompts for their private thinking. The one mind call
        that *did* sound like her was `_compose`, because a reach-out borrows
        the conversational assembler on its way out.

        Cached, because `SoulLoader.load()` re-reads the whole soul directory
        on every call by design (§5) — correct for a turn, wasteful for a night
        that makes ten of them. Keyed on the newest mtime in `soul/`, so an
        edit she made through the self-edit gate is picked up without a
        restart and without a signal, the way `KnowledgeStore.search` watches
        its index.

        Never raises. A missing or mangled soul costs the block and not the
        call, which is §20.2's rule for the shelf applied to the self.
        """
        mode = str(getattr(self.cfg, "mind_soul_in_prompts", "full") or "full").lower()
        if mode == "off":
            return ""
        full = full and mode != "brief"
        try:
            soul_dir = self.cfg.vault_dir / "soul"
            stamp = max((p.stat().st_mtime for p in
                         [*soul_dir.glob("*.md"), soul_dir / "soul.yaml"]
                         if p.is_file()),
                         default=0.0)
        except OSError:
            stamp = 0.0
        key = (stamp, full)
        cached_key, cached_text, cached_at = self._soul_cache
        ttl = float(getattr(self.cfg, "mind_soul_cache_s", 300.0) or 0.0)
        if cached_key == key and (self.clock.now() - cached_at) < ttl:
            return cached_text
        text = ""
        try:
            loader = getattr(self.brain.state, "soul_loader", None)
            if loader is not None:
                text = soul_preamble(
                    loader.load(),
                    user_md=self.vault.read("soul/USER.md"),
                    user_name=self.cfg.user_name, full=full)
        except Exception:  # noqa: BLE001 — an absent self is not a dead tick
            log.warning("soul preamble unavailable", exc_info=True)
            text = ""
        self._soul_cache = (key, text, self.clock.now())
        return text

    def _soul_drives(self) -> list[str]:
        """Current durable motives, loaded live like every other SOUL surface."""
        try:
            loader = getattr(self.brain.state, "soul_loader", None)
            return list(loader.load().drives) if loader is not None else []
        except Exception:  # noqa: BLE001 — absent drives cost context, not a night
            log.warning("character drives unavailable", exc_info=True)
            return []

    async def _utility(self, messages: list[dict], *, soul: bool = False,
                       **params) -> str:
        """Local-tier utility call, debited against the governor. The loop's
        only other model use is inside deliberate ACT speech (SPEC §17.3).

        One instrumentation point covers five callers: the knowledge store, the
        dream consolidator and the goal-work step are all handed *this method*
        as their `utility` seam (see __init__), so the prompt each of them sends
        is recorded here, labelled by whichever ACT opened the scope.

        `soul` opts a caller into the persona blocks (§22.4). It defaults off
        rather than on because the two callers that reach this method *without*
        going through a job flag — the knowledge store's chunk blurbs and the
        consolidator — are the two doing mechanical extraction, and a shelf
        blurb written in character is a shelf that lies about what it read.
        Everything that thinks as *her* passes `soul=True` explicitly.

        `params` are the provider's, not this method's, and they are handed
        over untouched — `thinking`, `reasoning_effort`, `max_tokens`,
        `timeout`. This used to take `soul` and nothing else, and a seam that
        accepts no parameters is a seam that *rejects* them: a DREAM research
        round asking for `thinking=False` did not get a thoughtless call, it
        got a `TypeError` and a job that failed every night. Nothing here reads
        them, and that is the point — the knob a job turns is the provider's
        knob, and this method's job is to not be in the way of it.
        """
        utility = self.brain.state.utility
        if utility is None:
            return ""
        if soul:
            preamble = self._soul_text()
            if preamble:
                messages = _with_soul(messages, preamble)
        # A selfie may be holding her brain's VRAM right now (§7.6). Wait at
        # the door rather than JIT-loading the chat model back onto a card the
        # render hasn't finished with — the OOM that killed half of one night's
        # dreamt selfies, because the DREAM selfie job starts a render and then
        # immediately asks the model about the next day. Patiently: nothing
        # here is a person waiting for an answer. Then `hold`, so a park that
        # starts now waits for this call instead of evicting under it — and in
        # that order, or the park's quiet wait would be waiting for a call
        # that is waiting for the park.
        await self.park_gate.wait(timeout_s=PATIENT_WAIT_S)
        async with self.park_gate.hold():
            text = await utility.complete(messages, **params)
        self.budget.debit("".join(m.get("content", "") for m in messages), text)
        origin = correlate.current()
        self.prompt_log.record(
            kind=(origin.kind if origin and origin.kind != correlate.TICK
                  else correlate.UTILITY),
            messages=messages, completion=text, model=self.cfg.utility_model,
            tier="utility")
        return text

    def _desk_written(self, tool: str, data: dict, *, notify: bool = True) -> None:
        """A desk tool changed a file, from the tool server's process (§34.2).

        The journal is the whole consequence. `workspace/` is not versioned
        (§34.1), so there is nothing to commit — but a note she wrote for
        herself is exactly what "what did she do while I was out" means, and
        with the file itself outside `git log` the journal line is the *only*
        record that it happened.

        Skills are versioned, so those do dirty the Vault. `commit_if_dirty`
        already no-ops when nothing is staged, so this stays a one-line rule
        rather than a special case per tool.
        """
        if tool in ("write_skill", "delete_skill"):
            self.vault.mark_dirty()
        what = data.get("path") or data.get("name") or "something"
        verb = {"write_note": "wrote myself a note",
                "append_note": "added to a note",
                "edit_note": "edited a note",
                "delete_note": "cleared a note off my desk",
                "write_skill": "wrote down how to do something",
                "delete_skill": "let go of a skill"}.get(tool, "changed")
        self._desk_notes.append(f"{verb}: {what}")
        action = {"write_note": "write", "append_note": "append",
                  "edit_note": "edit", "delete_note": "delete"}.get(tool)
        if action and notify:
            event = {"action": action, "path": what}
            if "bytes" in data:
                event["bytes"] = data["bytes"]
            self.hub.publish("workspace", event)

    def workspace_written(self, path: str) -> None:
        """Record a human editor write with the same journal semantics as a tool."""
        self._desk_written("write_note", {"path": path}, notify=False)

    def _brain_session(self) -> str:
        if self._session is None:
            self._session = self.brain.resolve_session(None)
        return self._session

    async def _compose(self, cue: str) -> str:
        """One line in her own voice, without the voice pipeline — used when a
        reach-out finds no page open to speak through (SPEC §18.3)."""
        out: list[str] = []
        with correlate.scope(kind=correlate.COMPOSE):
            async for tok in self.brain.stream_ambient(self._brain_session(), cue):
                out.append(tok)
        text = "".join(out).strip()
        self.budget.debit(cue, text)
        # strip any leading [expression] tag — this line lands as chat text
        if text.startswith("[") and "]" in text[:24]:
            text = text.split("]", 1)[1].strip()
        return text

    # ---- notifications from the voice route (same surface the idle machine had)

    def turn_started(self, proactive: bool = False) -> None:
        self._turns_in_flight += 1              # bookkeeping: always, both kinds
        if not proactive:
            self.activity.preempt_engaged()     # only a real user turn forces ENGAGED
        self.bus.wake.set()

    def turn_ended(self) -> None:
        self._turns_in_flight = max(0, self._turns_in_flight - 1)
        self._last_turn_end = self.clock.now()
        self.bus.wake.set()

    def _engaged_now(self) -> bool:
        return (self._turns_in_flight > 0
                or (self.clock.now() - self._last_turn_end) < self.cfg.idle_settle_s)

    def _uniform(self, lo: float, hi: float) -> float:
        return self.rng.uniform(lo, hi)

    # ------------------------------------------------------------------- tick

    async def tick(self) -> dict:
        """One full pass. Returns the trace record (the tests assert over these)."""
        self._tick_id = new_id("t")
        # Everything this tick reaches for — a model call, a tool, a prompt —
        # gets stamped with this id, so the debug page can put the tick trace
        # next to the prompt that phrased it and the audit line that ran it
        # (world/correlate.py). The ACT methods below refine `kind` in place.
        with correlate.scope(kind=correlate.TICK, tick_id=self._tick_id):
            return await self._tick()

    async def _tick(self) -> dict:
        now = self.clock.now()

        # ---- SENSE -----------------------------------------------------------
        # the machine sleeps too: a real gap gets ONE catch-up appraisal,
        # not a pile of stale reactions (SPEC §15.4)
        if self.last_tick_ts and (now - self.last_tick_ts) > max(
                SUSPEND_GAP_S, self.cfg.mind_dormant_cadence_s * 2):
            self.bus.post("suspend_gap",
                          {"hours": (now - self.last_tick_ts) / 3600},
                          source="mind")
        while not self.timers.due.empty():     # landed countdowns become signals
            t = self.timers.due.get_nowait()
            self.bus.post("timer", {"label": t.label, "id": t.id}, source="host")
        # …and the wakes the loop scheduled for itself (SPEC §16). A goal that
        # said "look at this again after lunch" said something the hourly
        # consider cooldown cannot express, and a run she dispatched and never
        # heard back from needs a floor under how long it may strand its goal.
        # Posted as signals rather than slept on, so `signals.jsonl` still
        # answers "what woke her at 3am" for this reason too.
        for goal_id, at in sorted(self.wakeups.items(), key=lambda kv: kv[1]):
            if now >= float(at):
                self.wakeups.pop(goal_id, None)
                self.bus.post("wakeup", {"goal": goal_id}, source="mind")

        batch, new_offset = self.bus.next(self.offset)
        surprise = 0.0
        for sig in batch:
            upd = self.world.observe(sig)
            surprise += 0.5 * len(upd.surprises)

        # bookkeeping signals fold into state during SENSE — they are internal
        # updates, never intentions, so they can't starve behind anything
        reflect_notes: list[str] = []
        actionable: list[Signal] = []
        for sig in batch:
            if sig.type == "user_message":
                self.activity.preempt_engaged()
            elif sig.type == "turn_committed":
                user_text = str(sig.payload.get("text", ""))
                reply = str(sig.payload.get("reply", ""))
                candidates = discover_promise_candidates(reply, user_text)
                explicit = [candidate for candidate in candidates
                            if candidate.confidence == "explicit"]
                assistant = [candidate for candidate in candidates
                             if candidate.confidence != "explicit"]
                for candidate in explicit:
                    note = self._file_promise_candidate(
                        candidate, user_text=user_text, reply=reply)
                    if note:
                        reflect_notes.append(note)
                if assistant and self._promise_review_available():
                    known = {str(review.get("signal_id", ""))
                             for review in self.promise_reviews}
                    if sig.id not in known:
                        normalized = [
                            {**candidate.as_dict(), "index": index}
                            for index, candidate in enumerate(assistant)]
                        self.promise_reviews.append({
                            "signal_id": sig.id,
                            "user_text": self._trim(user_text, 1000),
                            "reply": self._trim(reply, 3000),
                            "candidates": normalized,
                            "attempts": 0,
                        })
                        # The signal bus is not replayed after restart. Persist
                        # before advancing its offset at the end of this tick.
                        self._persist()
                elif assistant:
                    for candidate in fallback_promises(assistant):
                        note = self._file_promise_candidate(
                            candidate, user_text=user_text, reply=reply)
                        if note:
                            reflect_notes.append(note)
            elif sig.type == "selfedit_decision":
                res = self.selfedit.decide(sig.payload.get("id", ""),
                                           bool(sig.payload.get("approve")))
                if res:
                    reflect_notes.append(
                        f"you {res.outcome} my edit to {res.surface}")
            elif sig.type == "goal_decision":
                goal = self.goals.get(str(sig.payload.get("id", "")))
                if (goal is not None and sig.payload.get("abandon")
                        and goal.state not in ("done", "abandoned")):
                    self.goals.set_state(goal.id, "abandoned")
                    self.wakeups.pop(goal.id, None)
                    reflect_notes.append(
                        f"you let go of: {goal.text}")
            elif sig.type == "suspend_gap":
                self.goals.reconsider()        # ONE catch-up over the whole gap
                reflect_notes.append(
                    f"the machine slept ~{sig.payload.get('hours', 0):.1f}h; "
                    "I caught up on what expired and what still matters")
            elif sig.type == "timer":
                self._pending_announce.append(sig.payload)
            elif sig.type == "wakeup":
                # The wake IS the effect: clearing the consider cooldown is what
                # lets APPRAISE look at that goal again on this very tick. It is
                # bookkeeping, not an intention — "she woke up" was never a thing
                # for her to do.
                note = self._wake_goal(str(sig.payload.get("goal", "")))
                if note:
                    reflect_notes.append(note)
            elif sig.type == "task_completion":
                # Bookkeeping first — the goal that dispatched this comes back
                # from `waiting` here, where it cannot starve behind another
                # intention — and then on to APPRAISE, which is where the
                # journal line about it is decided (the branch in ACT).
                note = self._land_dispatched(sig)
                if note:
                    reflect_notes.append(note)
                actionable.append(sig)
            elif sig.type in ("user_present", "user_absent"):
                pass                           # observed above; greeting is the
                                               # voice route's job in this build
            else:
                actionable.append(sig)

        # REGULATE's daily duties, performed here rather than after ACT because
        # both of their outputs belong to *this* tick: the goals a rollover
        # retires or files are goals APPRAISE has to see now, and the lines it
        # writes are lines REFLECT has to journal now. Idempotent and dated, so
        # a restart at 23:59 does not do the day twice.
        reflect_notes.extend(self._day_rollover(now))
        reflect_notes.extend(self._bootstrap_handoff(now))

        # ---- APPRAISE (cheap by construction: heuristics, no model) ----------
        appraisals: list[Appraisal] = [
            appraise_signal(s, surprise=surprise) for s in actionable]
        if self._pending_announce and not self._engaged_now():
            appraisals.append(Appraisal("announce", "impulse", 0.9,
                                        "a timer landed — a promise due"))
        eligible_reviews = self._eligible_promise_reviews()
        if eligible_reviews and self._promise_review_available():
            appraisals.append(Appraisal(
                "promise_review", "promise_review", 0.86,
                f"{eligible_reviews} committed exchange(s) awaiting review"))
        # Which of her hands, if any, are reachable this tick (§26, as amended).
        # Computed in APPRAISE and checked again at dispatch, so a hand that is
        # blocked shows up in the trace as a runner-up with its reason rather
        # than as an exception inside an act that already committed.
        offer = self.hands.offer(
            state=self.activity.state,
            pressure=self.budget.pressure(),
            user_present=bool(self.world.snapshot().get("user_present")))
        for g in self.goals.open_goals():
            if g.state == "waiting":
                continue
            last = self.considered.get(g.id)
            if last and (now - last) < self.cfg.mind_consider_cooldown_s:
                continue                       # don't re-chew one goal every tick
            a = appraise_goal(g, self.clock)
            if offer and g.kind != "reach_out":
                # Same goal, same score — the difference is only whether the
                # step she takes may be a reach as well as a thought (principle
                # 7: a tool call is a step of an open goal, never free-floating).
                a = Appraisal(g, "tool_step", a.score,
                              f"{a.why}; hands: {', '.join(offer.tools)}")
            appraisals.append(a)
        if offer.reason:
            # Configured, and blocked for a nameable reason. Scored 0.0, which
            # is below every threshold there is: this exists to be *read* in the
            # trace, not to compete. With the house switch off there is no
            # reason string at all — off means invisible (principle 9).
            appraisals.append(Appraisal("tool_step", "impulse", 0.0, offer.reason))
        if self.knowledge.pending_docs():
            appraisals.append(Appraisal("ingest", "impulse", 0.55,
                                        "new document on the shelf"))
        if (self.cfg.dream_enabled and self.cfg.utility_enabled
                and self.activity.state == DREAM and self.dreams.backlog()):
            appraisals.append(Appraisal("dream", "dream", 0.6, "DREAM backlog"))
        if (self.activity.state == IDLE and not self._engaged_now()
                and self.world.snapshot().get("user_present")
                and now >= self._next_self_talk):
            appraisals.append(Appraisal(
                "self_talk", "impulse", self.cfg.mind_act_threshold + 0.05,
                "a long quiet stretch, with someone in the room"))

        # ---- DECIDE: exactly one intention, or REST ---------------------------
        appraisals.sort(key=lambda a: a.score, reverse=True)
        chosen = next((a for a in appraisals
                       if a.score >= self.cfg.mind_act_threshold), None)
        decided = {"intention": self._describe(chosen),
                   "runners_up": [self._describe(a) for a in appraisals[1:4]],
                   # what her hands could reach for, and why not when they
                   # couldn't — the "what did she do at 4am" question, answered
                   # in the same record as the decision that answered it
                   "hands": {"available": list(offer.tools),
                             "blocked": offer.reason}}
        # more than one thing worth doing? one intention per tick still holds —
        # the runners-up just shorten the next heartbeat instead of piling into
        # this one (the DREAM chunking discipline, generalised)
        self._backlog = (sum(
            1 for a in appraisals if a.score >= self.cfg.mind_act_threshold) > 1
            or eligible_reviews > 1)

        # ---- ACT: at most one act, through the host's own surfaces ------------
        acted: dict = {"what": None, "result": "rest"}
        interrupt: dict = {}
        if chosen is not None:
            try:
                acted, interrupt, act_notes = await self._act(chosen, offer)
                reflect_notes.extend(act_notes)
            except Exception as e:  # noqa: BLE001 — a failed act never kills the loop
                log.exception("ACT failed")
                acted = {"what": "error", "result": f"error: {e}"}

        # ---- REFLECT: journal + trace, always ----------------------------------
        # Desk writes happened on the *turn's* task, outside this tick entirely
        # (§34.2). Drain them here so they are journalled in tick order with
        # everything else, rather than racing the journal from another task.
        while self._desk_notes:
            reflect_notes.append(self._desk_notes.pop(0))
        for note in reflect_notes:
            self.journal.write(note)
        trace_rec = {
            "activity_state": self.activity.state,
            "sensed": [{"type": s.type, "id": s.id} for s in batch],
            "appraised": [{"what": self._describe(a),
                           "score_to_act": round(a.score, 3),
                           "why": a.why} for a in appraisals],
            "decided": decided, "acted": acted, "interrupt": interrupt,
        }
        self.trace.record(tick_id=self._tick_id, **trace_rec)

        # ---- REGULATE -----------------------------------------------------------
        self.activity.update(dream_backlog=(self.cfg.dream_enabled
                                            and self.cfg.utility_enabled
                                            and bool(self.dreams.backlog())),
                             budget_pressure=self.budget.pressure())
        self._body_reflexes(now)
        # persist the cursor BEFORE the commit, not after: `git add -A` stages
        # the whole tree, so a tick that persisted afterwards left its own state
        # for the *next* commit to sweep up and mislabel
        self.offset = new_offset
        self.last_tick_ts = now
        self._persist()
        self.vault.commit_if_dirty(
            f"tick {self._tick_id}: {decided['intention'][:60]}")
        self.hub.publish("mind", {"state": self.activity.state,
                                  "tick": self._tick_id,
                                  "intention": decided["intention"]})
        return {"tick_id": self._tick_id, **trace_rec}

    def _describe(self, a: Appraisal | None) -> str:
        if a is None:
            return "REST"
        if a.kind == "signal":
            return f"signal:{a.subject.type}"
        if a.kind == "goal":
            return f"goal:{a.subject.text[:50]}"
        if a.kind == "tool_step":
            return f"tool_step:{a.subject.text[:50]}"
        return str(a.subject)

    # --------------------------------------------------------------------- ACT

    async def _act(self, chosen: Appraisal,
                   offer=None) -> tuple[dict, dict, list[str]]:
        if chosen.subject == "announce":
            return await self._act_announce()
        if chosen.subject == "self_talk":
            return await self._act_self_talk()
        if chosen.subject == "ingest":
            return await self._act_ingest()
        if chosen.subject == "dream":
            return await self._act_dream()
        if chosen.subject == "promise_review":
            return await self._act_promise_review(offer)
        if chosen.kind == "signal":
            sig: Signal = chosen.subject
            if sig.type == "task_completion":
                return ({"what": "noted", "result": f"task done: "
                         f"{sig.payload.get('task', '?')}"}, {},
                        [f"finished something I'd started: "
                         f"{sig.payload.get('task', 'a task')}"])
            return ({"what": "noted", "result": f"noted {sig.type}"}, {}, [])
        if chosen.kind in ("goal", "tool_step"):
            goal: Goal = chosen.subject
            self.considered[goal.id] = self.clock.now()
            if goal.kind == "reach_out":
                return await self._act_reach_out(goal)
            # `tool_step` is reachable ONLY from here (§26, as amended): a hand
            # she reaches for is a step of an open goal or it does not happen.
            return await self._act_goal_work(
                goal, offer if chosen.kind == "tool_step" else None)
        return ({"what": None, "result": "rest"}, {}, [])

    def _promise_review_available(self) -> bool:
        return bool(self.cfg.utility_enabled and self.brain.state.utility is not None)

    def _eligible_promise_reviews(self) -> int:
        now = self.clock.now()
        return sum(1 for review in self.promise_reviews
                   if float(review.get("retry_at", 0) or 0) <= now)

    def _file_promise_candidate(self, candidate: PromiseCandidate, *,
                                user_text: str, reply: str,
                                text: str | None = None,
                                kind: str | None = None,
                                rationale: str = "",
                                success: str = "",
                                sources: list[dict] | None = None) -> str:
        objective = self._trim(text or candidate.text, 240)
        goal_kind = kind or promise_kind(objective, candidate.provenance)
        before = {goal.id for goal in self.goals.open_goals()}
        meta = {
            "about": self._trim(user_text),
            "source": self._trim(candidate.source, 300),
            "reply": self._trim(reply, 600),
        }
        if rationale:
            meta["rationale"] = self._trim(rationale, 400)
        if success:
            meta["success"] = self._trim(success, 400)
        if sources:
            meta["candidate_sources"] = sources
        goal = self.goals.add(
            objective, kind=goal_kind,
            priority=0.6 if goal_kind == "reach_out" else 0.7,
            due=(iso_of(self.clock.now() + 24 * 3600)
                 if goal_kind == "reach_out" else None),
            commitment="single-minded", provenance=candidate.provenance,
            meta=meta)
        if goal.id in before:
            return ""
        return (f"I promised: {goal.text}" if goal_kind == "reach_out"
                else f"I took that on: {goal.text}")

    async def _act_promise_review(self, offer=None) -> tuple[dict, dict, list[str]]:
        now = self.clock.now()
        index = next((index for index, review in enumerate(self.promise_reviews)
                      if float(review.get("retry_at", 0) or 0) <= now), -1)
        if index < 0:
            return ({"what": "promise_review", "result": "review is backing off"}, {}, [])
        review = self.promise_reviews.pop(index)
        candidates = review.get("candidates", [])
        messages = promise_review_messages(
            user_text=str(review.get("user_text", "")),
            reply=str(review.get("reply", "")), candidates=candidates,
            capabilities=list(offer.tools) if offer else [])
        try:
            with correlate.scope(kind=correlate.UTILITY):
                raw = await self._utility(
                    messages, soul=False, thinking=True, reasoning_effort="low",
                    max_tokens=1200,
                    response_format=PROMISE_REVIEW_RESPONSE_FORMAT)
            decision = parse_promise_review(raw, candidate_count=len(candidates))
        except Exception:  # noqa: BLE001 — a malformed review must not kill the tick
            log.warning("promise review failed; retaining it", exc_info=True)
            attempts = int(review.get("attempts", 0)) + 1
            review["attempts"] = attempts
            review["retry_at"] = now + min(3600.0, 30.0 * (2 ** (attempts - 1)))
            self.promise_reviews.append(review)
            return ({"what": "promise_review",
                     "result": "invalid review retained for retry"}, {}, [])

        if decision is None:
            return ({"what": "promise_review", "result": "no unresolved promise"}, {}, [])
        if not promise_decision_grounded(
                decision, candidates, str(review.get("user_text", ""))):
            log.warning("promise review returned an ungrounded objective; discarded")
            return ({"what": "promise_review", "result": "ungrounded goal discarded"}, {}, [])
        selected = [candidates[index] for index in decision.candidates]
        primary = selected[0]
        candidate = PromiseCandidate(
            index=int(primary.get("index", 0)),
            text=str(primary.get("text", "")),
            provenance=str(primary.get("provenance", "promise:her-own-words")),
            source=str(primary.get("source", "")),
            start=int(primary.get("start", 0)),
            confidence=str(primary.get("confidence", "soft")))
        sources = [{"text": self._trim(str(item.get("source", "")), 200),
                    "candidate": int(item.get("index", 0))}
                   for item in selected]
        note = self._file_promise_candidate(
            candidate, user_text=str(review.get("user_text", "")),
            reply=str(review.get("reply", "")), text=decision.text,
            kind=decision.kind, rationale=decision.rationale,
            success=decision.success, sources=sources)
        return ({"what": "promise_review",
                 "result": "filed one canonical goal" if note else
                           "matched an existing goal"}, {}, [note] if note else [])

    async def _act_announce(self) -> tuple[dict, dict, list[str]]:
        """A landed timer — a promise, so it queues until deliverable (the
        Build #4 rule, kept verbatim)."""
        t = self._pending_announce[0]
        self.controller.set_expression("surprised", 0.6, reset_ms=4000)
        cue = ANNOUNCE_CUE.format(label=t.get("label", "your timer"),
                                  user=self.cfg.user_name)
        with correlate.scope(kind=correlate.AMBIENT):
            spoken = await self.speak(cue)
        if spoken:
            self._pending_announce.pop(0)
            return ({"what": "speak", "result": "announced the timer"}, {},
                    [f"told them the “{t.get('label')}” timer finished"])
        return ({"what": "speak", "result": "announce queued (nobody to tell)"},
                {}, [])

    async def _act_self_talk(self) -> tuple[dict, dict, list[str]]:
        """The Ukagaka murmur, now decided rather than diced — ambient, never
        persisted, dropped if she can't be heard (SPEC §15.5)."""
        cue = self.rng.choice(SELF_TALK_CUES).format(user=self.cfg.user_name)
        with correlate.scope(kind=correlate.AMBIENT):
            delivered = await self.speak(cue)
        self._next_self_talk = self.clock.now() + self._uniform(
            self.cfg.idle_talk_min_s, self.cfg.idle_talk_max_s)
        return ({"what": "speak",
                 "result": "murmured to herself" if delivered else
                           "let the murmur go (busy or alone)"}, {}, [])

    async def _act_ingest(self) -> tuple[dict, dict, list[str]]:
        with correlate.scope(kind=correlate.KNOWLEDGE):
            results = await self.knowledge.scan()
        notes = [f"read and shelved {r.doc} ({r.chunks} passages"
                 + (", too long to keep word-for-word, so notes)" if r.digested
                    else ")")
                 for r in results]
        return ({"what": "knowledge.ingest",
                 "result": f"ingested {len(results)} doc(s)"}, {}, notes)

    async def _act_dream(self) -> tuple[dict, dict, list[str]]:
        """One DREAM tick: the whole pipeline, one shared budget (§21.2).

        The night is chunked exactly as consolidation alone used to be — this
        tick spends what it may and yields, the ladder stays in DREAM while any
        job still has a backlog, and the next tick picks up where this one
        stopped. What changed is only how many kinds of work that covers — and
        that research carries its own budget, so a night of reading neither
        starves the roster nor is starved by it (§21.2).
        """
        if not self.cfg.dream_enabled or not self.cfg.utility_enabled:
            return ({"what": "dream", "result": "DREAM disabled"}, {}, [])
        with correlate.scope(kind=correlate.DREAM):
            report = await self.dreams.run(
                token_budget=self.cfg.mind_dream_tick_tokens,
                research_budget=self.cfg.mind_dream_research_tokens)
        return ({"what": "dream", "result": report.summary,
                 "jobs": [j.as_dict() for j in report.jobs]}, {}, report.notes)

    def _deliver_report(self, *, title: str, path: str, summary: str,
                        job: str) -> None:
        """File one night's report where they will find it (§18.2a).

        Not Gate 2. Gate 2 asks whether *she* should interrupt, and spends one
        of a handful of daily interrupts when the answer is yes; this is a
        standing instruction its owner wrote into a job file, which is the
        difference between her deciding to reach for you and you having asked
        for a thing to be ready in the morning. So it costs no interrupt and
        argues with no threshold — it only puts a named artifact in the inbox
        and lets them find it when they next look.

        `unheard=True` for the same reason `_act_reach_out`'s SUGGEST does it: a
        briefing is by definition waiting for the next time they look, so it
        belongs in the inbox whether or not a page is open at four in the
        morning (`world/inbox.py`).
        """
        try:
            self.post_message("assistant", summary, proactive=True,
                              unheard=True, report_path=path,
                              report_title=title, report_job=job)
        except Exception:  # noqa: BLE001 — a night's work is not lost to delivery
            log.exception("DREAM: couldn't deliver %s", path)

    async def dream_now(self, **kw):
        """Run the pipeline off-cadence — the debug page's trigger (§21.3).

        Deliberately the same call the tick makes, with the same correlate
        scope, so a hand-triggered night is indistinguishable from a real one
        in the prompt log and the trace. What it does NOT do is move the
        activity ladder: a night you asked for is not evidence she drifted into
        one, and a DREAM state written by a button would be a lie the timeline
        then shows you forever.
        """
        with correlate.scope(kind=correlate.DREAM, tick_id=self._tick_id):
            report = await self.dreams.run(**kw)
        if report.dry_run:
            return report          # a rehearsal leaves no journal and no commit
        for note in report.notes:
            self.journal.write(note)
        self.vault.commit_if_dirty(f"dream (by hand): {report.summary[:60]}")
        return report

    # ---- initiative: gate 2 lives here (SPEC §18.2–§18.3) -----------------------

    async def _act_reach_out(self, goal: Goal) -> tuple[dict, dict, list[str]]:
        today = day_of(self.clock.now())       # her day rolls at local midnight
        if self.interrupts.get("date") != today:
            self.interrupts = {"date": today, "count": 0}
        world = self.world.snapshot()
        last_out = world.get("last_contact_out")
        decision = score_interrupt(
            clock=self.clock,
            relevance=goal.priority,
            time_sensitivity=1.0 if goal.is_due(self.clock, 6) else 0.2,
            last_contact_out=ts_of_iso(last_out) if last_out else None,
            interrupts_today=self.interrupts["count"],
            max_interrupts_per_day=self.cfg.mind_max_interrupts_per_day,
            threshold=self.cfg.mind_interrupt_threshold)
        interrupt = {"score": decision.score, "threshold": decision.threshold,
                     "outcome": decision.outcome, "factors": decision.factors,
                     "goal": goal.text}

        if decision.outcome == "SILENT":
            # THE DEFAULT: do it silently and journal it
            if goal.is_stale(self.clock) and goal.commitment != "blind":
                self.goals.set_state(goal.id, "abandoned")
                note = f"let it go quietly: {goal.text} (the moment passed)"
            else:
                note = f"thought about {goal.text}; chose not to interrupt"
            return ({"what": None, "result": "stayed quiet"}, interrupt, [note])

        if decision.outcome == "SUGGEST":
            # a soft line in the chat — waiting when they next look, never spoken
            text = await self._compose(REACH_OUT_CUE.format(goal=goal.text))
            if text:
                # `unheard`: a SUGGEST is *by definition* a line waiting for the
                # next time they look, so it belongs in the inbox whether or not
                # a page happens to be open right now (world/inbox.py).
                self.post_message("assistant", text, proactive=True, unheard=True)
            self.world.note_contact_out()
            self.interrupts["count"] += 1
            self.goals.set_state(goal.id, "done")
            return ({"what": "chat", "result": f"left a quiet note: {goal.text}"},
                    interrupt, [f"left them a note about {goal.text}"])

        # SPEAK: aloud through the ambient seam if a page is open (the full turn
        # pipeline — voice, face, barge-in); as a chat line if the room is empty
        cue = REACH_OUT_CUE.format(goal=goal.text)
        with correlate.scope(kind=correlate.COMPOSE):
            spoken = await self.speak(cue)
        if not spoken:
            # `speak` said no: there is no page to say it through, so this is the
            # case the inbox exists for — she spent an interrupt on an empty room.
            text = await self._compose(cue)
            if text:
                self.post_message("assistant", text, proactive=True, unheard=True)
        self.world.note_contact_out()
        self.interrupts["count"] += 1
        self.goals.set_state(goal.id, "done")
        return ({"what": "speak", "result": f"reached out: {goal.text}"},
                interrupt, [f"reached out first about {goal.text}"])

    # ---- the goal lifecycle's two return paths (SPEC §22, §16) ----------------

    def _wake_goal(self, goal_id: str) -> str:
        """A `wakeup` the loop scheduled has landed.

        Two things it can mean, and the goal's own state says which: a parked
        goal is due another look (clear the consider cooldown and let APPRAISE
        see it), or a goal has been sitting in `waiting` on work that never
        posted its `task_completion` (unstrand it, and say so — a goal invisible
        to every gate she has is worse than one that failed).
        """
        goal = self.goals.get(goal_id) if goal_id else None
        if goal is None or goal.state not in ("pending", "active", "waiting"):
            return ""
        self.considered.pop(goal.id, None)
        if goal.state != "waiting":
            return ""
        dispatched = goal.dispatched
        self.goals.update(goal.id, state="active", meta={"dispatched": {}})
        if dispatched:
            return (f"the {dispatched.get('tool', 'work')} I started for "
                    f"“{goal.text}” never came back; picking it up myself")
        return f"came back to: {goal.text}"

    def _land_dispatched(self, sig: Signal) -> str:
        """`task_completion` → the goal that dispatched it returns to `active`.

        This is the whole reason the signal type existed and was never posted:
        the loop was built for a return path and the return path was a stub.
        Bookkeeping, done in SENSE, so a busy tick cannot leave a finished run's
        goal stranded in `waiting` behind some louder intention.
        """
        goal_id = str(sig.payload.get("goal_id") or "")
        goal = self.goals.get(goal_id) if goal_id else None
        if goal is None or goal.state != "waiting":
            return ""
        self.goals.update(goal.id, state="active", meta={"dispatched": {}})
        self.considered.pop(goal.id, None)     # workable again on this very tick
        self.wakeups.pop(goal.id, None)        # the safety net is not needed now
        what = sig.payload.get("kind") or "work"
        where = ("it's in the vault, not in the chat"
                 if sig.payload.get("deliver") == "vault" else "it's in the chat")
        return (f"the {what} I started for “{goal.text}” came back — {where}")

    async def _act_maintenance(self, goal: Goal,
                               auto: str) -> tuple[dict, dict, list[str]]:
        """A `maintenance:*` goal that stands for a standing leftover (§22).

        It does not get a paragraph written about it — it gets the leftover
        done, through the same act the cheap impulse path uses, and it closes
        itself the moment there is nothing left. That is what keeps the goals
        page from filling with to-dos nobody can finish by reading them.
        """
        if auto == "shelf":
            acted, interrupt, notes = await self._act_ingest()
            left = bool(self.knowledge.pending_docs())
        elif self.activity.state != DREAM:
            # Defence, not a path she is expected to take: the dream goal
            # carries no due time precisely so it never outranks the window
            # that owns this decision (§21). If somebody raises its priority by
            # hand, it still waits for the night rather than starting one.
            self.goals.update(goal.id, meta={"last_step": iso_of(self.clock.now())})
            return ({"what": None, "result": "the backlog waits for tonight"},
                    {}, [f"still to do tonight: {goal.text}"])
        else:
            acted, interrupt, notes = await self._act_dream()
            left = bool(self.dreams.backlog())
        state = "active" if left else "done"
        if state == "done":
            notes.append(f"cleared: {goal.text}")
        self.goals.update(goal.id, state=state,
                          meta={"steps": goal.steps + 1,
                                "last_step": iso_of(self.clock.now())})
        return ({**acted, "goal": goal.id, "state": state}, interrupt, notes)

    #: Where a goal's working notes live (§34.1, §22). One file per goal, in her
    #: own workspace, so "what did she work out about this" is a file a person
    #: can open — and so the next tick can read back what the last one concluded
    #: instead of starting from the goal's one-line text every time.
    GOAL_DESK = "goals/{id}.md"

    def _goal_desk_path(self, goal: Goal) -> str:
        return self.GOAL_DESK.format(id=goal.id)

    def _goal_desk_read(self, goal: Goal, *, limit: int = 3000) -> str:
        """What she has already worked out about this goal, newest at the end."""
        if self.workspace is None:
            return ""
        try:
            text = self.workspace.read(self._goal_desk_path(goal), default="") or ""
        except Exception:  # noqa: BLE001 — a desk file is never worth a dead tick
            log.warning("goal desk read failed", exc_info=True)
            return ""
        return text[-limit:]

    def _goal_desk_write(self, goal: Goal, line: str) -> None:
        """Append one step's conclusion to the goal's desk file.

        Append rather than replace: the value of the file is the trail. The
        workspace already caps file and tree size and jails the path
        (mind/workspace.py), so an unbounded trail is a caught error rather than
        a full disk — and the per-tick single-step rule bounds the rate.
        """
        if self.workspace is None or not line.strip():
            return
        stamp = iso_of(self.clock.now())
        try:
            self.workspace.append(self._goal_desk_path(goal),
                                  f"\n## {stamp}\n\n{line.strip()}\n")
        except Exception:  # noqa: BLE001
            log.warning("goal desk write failed", exc_info=True)
            return
        # …and the same journal line a desk tool would have produced, so the
        # inner-life page shows the write whichever hand made it (§34.2).
        self._desk_notes.append(f"wrote up where I got to: "
                                f"{self._goal_desk_path(goal)}")
        self.vault.mark_dirty()

    def _goal_memories(self, goal: Goal, facts: str) -> list:
        """The episodic half of §22.4 — what she can remember about this goal.

        `facts.md` above is the *semantic* residue: what DREAM decided is still
        true in a month, with the evenings it came from burned off. Working
        alone she had only that, so she knew you had a sister and not that you
        had mentioned her on Tuesday sounding tired. This asks the same index
        the conversation asks (`store.recall`), probed from the goal rather
        than from a message, the way the greeting probes from state.

        Anything already sitting in the facts block is dropped: the same
        sentence under two headings reads as two pieces of evidence.
        """
        probe = " ".join(p for p in (goal.text,
                                     str(goal.meta.get("about") or "").strip())
                         if p)
        try:
            mems = self.store.recall(probe, self.cfg.retrieval_k)
        except Exception:  # noqa: BLE001 — a cold index is not a reason to
            log.debug("goal work: no recall", exc_info=True)   # skip the step
            return []
        seen = facts.lower()
        return [m for m in mems if m.text.strip().lower() not in seen]

    def _goal_context(self, goal: Goal) -> str:
        """Everything the *conversational* prompt would have given her, minus
        the conversation (SPEC §7.1, §34.3, §19.2).

        She was measurably dumber alone than she is talking to you: chat gets
        the desk digest, the skills catalog, the situation and the shelf, and
        goal work got the goal's one-line text and nothing else. That is
        backwards for a project whose whole thesis is the inner life, and it is
        why every private step read like a fortune cookie.
        """
        parts: list[str] = [f"THE GOAL\n\n{goal.text}"]
        meta = [f"kind: {goal.kind}", f"state: {goal.state}",
                f"step {goal.steps + 1} of {self.cfg.mind_goal_max_steps}",
                f"why you have it: {goal.provenance}"]
        if goal.due:
            meta.append(f"due: {goal.due}")
        parts.append("ABOUT IT\n\n" + "\n".join(f"- {m}" for m in meta))
        # The exchange that made it, when it was made by one. A promise is
        # scanned as the *predicate* after "I'll", so "which of the two kettles
        # boils faster" survives only as "find out which one is faster for you"
        # — and a working step handed that alone invents a subject for it with
        # complete confidence. It only became visible when tasks started being
        # worked; as `reach_out` goals these never got a step at all.
        about = str(goal.meta.get("about") or "").strip()
        if about:
            parts.append(f"WHERE THIS CAME FROM\n\nThey said: “{about}”")
        desk = self._goal_desk_read(goal)
        if desk.strip():
            parts.append("WHAT YOU HAVE ALREADY WORKED OUT ON THIS\n\n" + desk.strip())
        try:
            parts.append("THE SITUATION RIGHT NOW\n\n" + self.world.situation())
        except Exception:  # noqa: BLE001
            log.debug("goal work: no situation", exc_info=True)
        if self.workspace is not None:
            digest = self.workspace.digest(limit=12)
            if digest:
                parts.append("YOUR DESK (paths only — `read_note` opens one)"
                             "\n\n" + digest)
        if self.skills is not None:
            catalog = self.skills.catalog(limit=12)
            if catalog:
                parts.append("SKILLS YOU HAVE WRITTEN DOWN\n\n" + catalog)
        facts = self.vault.read("memory/semantic/facts.md")[-1200:].strip()
        if facts:
            parts.append("WHAT YOU KNOW ABOUT THEM\n\n" + facts)
        recalled = self._goal_memories(goal, facts)
        if recalled:
            parts.append("THINGS THAT MAY BE RELEVANT\n\n" + "\n".join(
                f"- ({age_tag(m)}) {m.text}" for m in recalled))
        other = [g.text for g in self.goals.open_goals() if g.id != goal.id][-8:]
        if other:
            parts.append("YOUR OTHER OPEN GOALS (do not work these now)\n\n"
                         + "\n".join(f"- {t}" for t in other))
        return "\n\n".join(parts)

    async def _act_goal_work(self, goal: Goal,
                             offer=None) -> tuple[dict, dict, list[str]]:
        """Advance one task/maintenance goal by exactly one step (SPEC §22).

        The lifecycle is the point. A goal used to be created `pending`, worked
        once, and marked `done` — which meant "she does things while you're
        gone" was one paragraph of a local model and a tick. Now:

            pending → active     on the first step
            active  → waiting    when it is blocked on the user, or on work it
                                 dispatched and will not await (§7.6)
            active  → done       when the step says the work is finished
            active  → waiting/abandoned  when the step budget runs out, by
                                 whichever the commitment strategy says

        One step is one utility call that may emit **one** intent: a thought, or
        — when her hands are offered — one tool call. Never both, never two;
        that is "one intention per tick" applied one level down.

        Nothing here ever speaks. The product of a step lands on her desk and in
        her journal; reaching the user is Gate 2's decision, made about a
        `reach_out` goal, on some later tick.
        """
        notes: list[str] = []
        # A maintenance goal that only *stands for* a leftover does not get a
        # paragraph written about it — it gets the leftover done. The impulses
        # are still the cheap path (§21, §20.1); this is what makes the standing
        # record of them something more than a line in a checklist.
        auto = goal.meta.get("auto")
        if auto in ("shelf", "dream"):
            return await self._act_maintenance(goal, auto)

        if goal.state == "pending":
            self.goals.update(goal.id, state="active")
            goal.state = "active"
        step = goal.steps + 1
        last = step >= max(1, int(self.cfg.mind_goal_max_steps))

        with correlate.scope(kind=correlate.GOAL_WORK):
            reply = await self._utility([
                {"role": "system", "content": self._work_system(goal, offer, last)},
                {"role": "user", "content": self._goal_context(goal)}],
                soul=True)

        intent = parse_intent(reply, allowed=tuple(offer.tools) if offer else ())
        used: dict = {}
        if intent.kind == "use":
            used, note = await self._tool_step(goal, intent, offer)
            notes.append(note)
        else:
            note = (intent.text or "").strip() or \
                f"(sat with it; nothing new yet on: {goal.text})"
            self._goal_desk_write(goal, note)
            notes.append(f"worked on: {goal.text} — {note[:160]}")

        meta: dict = {"steps": step, "last_step": iso_of(self.clock.now())}
        if used.get("dispatched"):
            # Start-don't-await: the answer comes back as `task_completion`, and
            # until it does there is nothing to think about (§7.6, §16).
            meta["dispatched"] = used["dispatched"]
            state = "waiting"
            self.wakeups[goal.id] = (self.clock.now()
                                     + float(self.cfg.mind_dispatch_timeout_s))
            notes.append("…and I'm waiting on it before I go further")
        elif self._finished(intent.text):
            state = "done"
            notes.append(f"finished: {goal.text}")
            notes += self._offer_to_tell(goal)
        elif last:
            # The horizon (§22): three steps and it either waits for something to
            # change or the commitment strategy lets it go. Without this a goal
            # loops forever, which is the failure a lifecycle exists to prevent.
            if goal.commitment == "open-minded" and goal.is_stale(self.clock):
                state = "abandoned"
                notes.append(f"let go of: {goal.text} (I gave it what I had)")
            else:
                state = "waiting"
                self.wakeups[goal.id] = self.clock.now() + 12 * 3600
                notes.append(f"parked: {goal.text} — I've taken it as far as I "
                             "can on my own for now")
        else:
            state = "active"

        self.goals.update(goal.id, state=state, meta=meta)
        did = (f"{used['tool']} ({used['verdict']})" if used
               else "thought about it")
        return ({"what": "tool_step" if used else "goal_work",
                 "result": f"step {step}: {did}", "goal": goal.id,
                 "state": state,
                 **({"tool": used["tool"], "verdict": used["verdict"]}
                    if used else {})},
                {}, notes)

    #: The words a step uses when it means "this goal is finished". Deliberately
    #: a phrase she has to *choose*, not a heuristic over the note's contents: a
    #: goal closing itself because a paragraph sounded conclusive is how a
    #: standing commitment quietly disappears.
    DONE_MARK = "goal complete"

    @staticmethod
    def _trim(text: str, limit: int = 200) -> str:
        """One line, short enough that `goals.md` still reads as a checklist —
        the meta field rides on the goal's own line."""
        one = " ".join((text or "").split())
        return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"

    def _offer_to_tell(self, goal: Goal) -> list[str]:
        """A promise she has now kept becomes something to say (§18.2, §22.1).

        Splitting a promise into work and news is what makes her do the work at
        all — but the news half has to be filed by something, or the split just
        loses it, and "I'll look into that" becomes a thing she quietly does and
        never mentions. That is a worse companion than the one who talked about
        everything and did none of it.

        Only her own promises. Work you planted is on the desk where you left
        it, maintenance is nobody's business but hers, and a `followup` never
        gets a follow-up of its own — reaching out is already the act.

        `open-minded` on purpose: news has a shelf life. If Gate 2 never finds a
        moment inside a day, letting it go is better company than opening with
        something she finished the day before yesterday.
        """
        if goal.kind != "task" or not goal.provenance.startswith("promise:"):
            return []
        self.goals.add(
            f"tell them what came of “{goal.text}” — it's in "
            f"{self.GOAL_DESK.format(id=goal.id)}",
            kind="reach_out", priority=0.6,
            due=iso_of(self.clock.now() + 24 * 3600),
            commitment="open-minded", provenance=f"followup:{goal.id}")
        return [f"…and they should hear what came of it: {goal.text}"]

    def _finished(self, note: str) -> bool:
        return self.DONE_MARK in (note or "").lower()

    def _work_system(self, goal: Goal, offer, last: bool) -> str:
        """The instruction half of a working step."""
        lines = [
            # The persona blocks arrive above this, fused on by `_utility`
            # (§22.4). So this opens by saying what the moment *is* rather than
            # who she is — the two used to be the same sentence, and with no
            # card behind it "you" pointed at nobody and every character wrote
            # the same note.
            "This is you, alone, between conversations — quietly advancing one "
            "of your own goals. Nobody is waiting on this and nothing you write "
            "here is sent to anyone: it goes on your own desk, for you to pick "
            "up next time. Think it through as yourself, not as an assistant "
            "reporting on a task.",
            "",
        ]
        if offer:
            lines.append(self.hands.catalog(tuple(offer.tools)))
        else:
            lines.append(
                "Write a short working note (<=80 words) of what you concluded "
                "or want to try next. Just the note.")
        lines += [
            "",
            # "in your note" was ambiguous the moment she had hands: she read it
            # as the note file she was writing and put the words inside
            # `append_note`, where nothing reads them, and a finished goal parked
            # for twelve hours instead of closing. Name the line instead.
            f'When the goal is genuinely finished, write "{self.DONE_MARK}" '
            + ("on your `think` line — and only then. Words inside a tool call "
               "are not read." if offer else "in your note — and only then."),
        ]
        if last:
            lines.append(
                "This is the last step you get on this goal for now, so make it "
                "the one that leaves the clearest trail for next time.")
        return "\n".join(line for line in lines if line is not None)

    # ---- the hands, in one tick (SPEC §26, as amended) -------------------------

    async def _tool_step(self, goal: Goal, intent,
                         offer) -> tuple[dict, str]:
        """One mind-initiated tool call: check → dispatch → realise → journal.

        Every precondition is checked again here, not because DECIDE's check was
        wrong but because the switch can be revoked between the two — which is
        exactly what a kill switch has to survive. A denial is audited and
        becomes a working note; it is never an exception, and it never costs the
        goal its step.
        """
        args = dict(intent.args)
        ok, why = self.hands.check(
            intent.tool, args, state=self.activity.state,
            pressure=self.budget.pressure(),
            user_present=bool(self.world.snapshot().get("user_present")))
        if not ok:
            self.hands.deny(intent.tool, args, why)
            note = f"wanted to {intent.tool} for “{goal.text}” but didn't: {why}"
            self._goal_desk_write(goal, note)
            # A refused reach is still a reach, and the trace should say so:
            # "she thought about it" and "she tried to look it up and the cap
            # was spent" are different ticks, and only one of them is a reason
            # to go and change a knob.
            return ({"tool": intent.tool, "verdict": "denied", "why": why,
                     "class": klass(intent.tool), "dispatched": {}}, note)

        # Principle 7: every autonomous call names the goal that wanted it, so
        # `goals.md` stays the complete, readable list of what her hands might do.
        self.hands.spend(intent.tool, args)
        with correlate.scope(kind=correlate.MIND_TOOL):
            result = await self.hands.execute(
                intent.tool, args, timeout_s=self.cfg.tool_timeout_s)
            # Host-side realisation (§7.5) — the timer actually scheduled, the
            # render actually started. The stamp is what makes the product land
            # in the Vault instead of in the chat (§18, principle 8).
            realise = getattr(self.brain, "realise", None)
            if callable(realise):
                try:
                    realise(intent.tool, result,
                            extra=stamp_contract({}, goal_id=goal.id))
                except Exception:  # noqa: BLE001 — realisation is not the call
                    log.exception("mind tool realisation failed")

        dispatched: dict = {}
        if intent.tool in START_DONT_AWAIT and '"started"' in result:
            dispatched = {"tool": intent.tool, "at": iso_of(self.clock.now())}
        short = result[:160].replace("\n", " ")
        note = f"reached for {intent.tool} on “{goal.text}” → {short}"
        # Her reason first, the result under it. A desk that records only what a
        # hand returned reads, three ticks later, as a list of things that
        # happened to her rather than steps she took — and she re-does them.
        why = (intent.text or "").strip()
        self._goal_desk_write(goal, f"{why}\n\n{note}" if why else note)
        return ({"tool": intent.tool, "verdict": "ok",
                 "class": klass(intent.tool), "dispatched": dispatched}, note)

    # ----------------------------------------------------------- body reflexes

    def _body_reflexes(self, now: float) -> None:
        """REGULATE's cheap aliveness (SPEC §15.5): the idle machine's micro-acts,
        kept as reflexes — gaze drift, a small expression pulse, rain-gazing.
        Reflexes, not intentions: no model, no journal, seeded RNG, and silent
        the moment she's engaged or the room is empty."""
        if self._engaged_now() or self.hub.subscribers == 0:
            return
        if self.activity.state not in (IDLE, ENGAGED):
            return                              # DORMANT/DREAM: the body rests
        if self._gaze_until and now >= self._gaze_until:
            self.controller.look_at_camera()
            self._gaze_until = 0.0
        if now < self._next_body_act:
            return
        act = self.rng.choice(("gaze_drift", "pulse", "posture", "recenter",
                               "rain_gaze"))
        if act == "gaze_drift":
            self.controller.look_at(self._uniform(-0.9, 0.9),
                                    self._uniform(0.9, 1.6),
                                    self._uniform(-2.0, -0.5))
        elif act == "pulse":
            self.controller.set_expression(
                self.rng.choice(("relaxed", "happy", "thinking")),
                self._uniform(0.35, 0.6), reset_ms=5000)
        elif act == "posture":
            self.controller.reset_bone()
            self.controller.set_bone("head", x=self._uniform(-2.0, 2.0),
                                     z=self._uniform(-3.0, 3.0))
        elif act == "recenter":
            self.controller.reset_bone()
            self.controller.look_at_camera()
        else:                                   # rain_gaze
            self.controller.look_at(WINDOW_TARGET["x"], WINDOW_TARGET["y"],
                                    WINDOW_TARGET["z"])
            self.controller.set_expression("relaxed", 0.5, reset_ms=0)
            self._gaze_until = now + self._uniform(5.0, 15.0)
        self._next_body_act = now + self._uniform(self.cfg.idle_act_min_s,
                                                  self.cfg.idle_act_max_s)

    # ------------------------------------------------------------------- misc

    def _persist(self) -> None:
        write_json(self.state_path, {
            "bus_offset": self.offset, "interrupts": self.interrupts,
            "considered": self.considered, "last_tick_ts": self.last_tick_ts,
            "wakeups": self.wakeups, "reconsidered_on": self.reconsidered_on,
            "bootstrapped_on": self.bootstrapped_on,
            "promise_reviews": self.promise_reviews,
            # the fingerprint ledger and the daily call count, beside
            # `interrupts` and rolling at the same local midnight (§26, amended)
            **self.hands.snapshot()})

    # ---- REGULATE's daily duties ----------------------------------------------

    def _day_rollover(self, now: float) -> list[str]:
        """Once per local day: apply commitment strategies, roll the hands'
        caps, and file what has been left standing (SPEC §22.2, §26).

        `reconsider()` used to run on `suspend_gap` alone — i.e. only after the
        machine had slept two hours — so a goal that went stale while she was
        awake was defended forever by a strategy nobody ever asked. Cheap and
        idempotent, which is why once a day is enough and more would be noise.
        """
        today = day_of(now)
        if self.reconsidered_on == today:
            return []
        self.reconsidered_on = today
        self.hands.roll()
        notes = [f"let go of: {g.text} (the moment for it passed)"
                 for g in self.goals.reconsider()]
        return notes + self._file_maintenance()

    #: The two standing leftovers that become goals rather than staying impulses
    #: (SPEC §22, provenance `maintenance:*`). Fixed text, because the dedupe in
    #: `GoalStore.add` is by text and a count in the title would file a new goal
    #: every morning.
    #: `(which, provenance, text, due_hours)`. The due time is what decides
    #: whether the goal is a *record* or a *plan*, because `appraise_goal` adds
    #: 0.35 once a goal is due and that is the difference between scoring under
    #: the ingest impulse and scoring over it:
    #:
    #:   * the shelf gets one. A drop she has not read for a day has stopped
    #:     being something the cheap impulse keeps declining to reach and
    #:     started being something she should go and do.
    #:   * DREAM does not. The night IS the schedule (§21, §17.1), and a goal
    #:     that outranked the dream window would be one half of the system
    #:     arguing with the other about when to consolidate.
    MAINTENANCE = (
        ("shelf", "maintenance:shelf",
         "read what's still sitting unread on my shelf", 24.0),
        ("dream", "maintenance:dream",
         "catch up on the nights I haven't consolidated yet", None),
    )

    def _leftover(self, which: str) -> bool:
        if which == "shelf":
            return bool(self.knowledge.pending_docs())
        return bool(self.cfg.dream_enabled and self.cfg.utility_enabled
                    and self.dreams.backlog())

    def _file_maintenance(self) -> list[str]:
        """Standing leftovers become goals; cleared ones close themselves.

        Ingest and DREAM stay impulses — that is the cheap path and it is
        right. What was missing is that a shelf nobody got round to for three
        days is a *commitment*, and the goals page is where commitments are
        visible. Documented in `goals.py` as `maintenance:*` since the store was
        written, and never created until now.
        """
        notes: list[str] = []
        # Keyed on `meta.auto`, which only this method ever writes — NOT on the
        # provenance, which a person or a dream job may reasonably reuse. A
        # reconciler that closed every goal wearing a familiar label would close
        # work she filed herself, which is the opposite of a maintenance sweep.
        open_now = {g.meta.get("auto"): g for g in self.goals.open_goals()
                    if g.meta.get("auto")}
        for which, provenance, text, due_hours in self.MAINTENANCE:
            existing = open_now.get(which)
            if self._leftover(which):
                if existing is not None:
                    continue
                self.goals.add(
                    text, kind="maintenance", priority=0.4,
                    due=(iso_of(self.clock.now() + due_hours * 3600)
                         if due_hours else None),
                    # `single-minded`, not `open-minded`, and the due time is
                    # why: `reconsider()` abandons an open-minded goal the
                    # moment it goes stale, which for a due-dated one is the
                    # very tick it becomes worth doing. A leftover drops when it
                    # is moot, and the reconciler above is what decides that —
                    # the shelf is empty, so the goal closes `done`.
                    commitment="single-minded", provenance=provenance,
                    meta={"auto": which})
                notes.append(f"put it on my own list: {text}")
            elif existing is not None:
                self.goals.set_state(existing.id, "done")
                notes.append(f"cleared: {existing.text}")
        return notes

    def _bootstrap_handoff(self, now: float) -> list[str]:
        """The first session's handoff, once (SPEC §5.4, `soul-src/BOOTSTRAP.md`).

        The bootstrap file names three things the runtime should be left holding
        when it retires: a seeded `USER.md`, a first `MEMORY.md` line, and one
        standing `reach_out` in `goals.md`. The first two are the post-turn
        pipeline's, and it already does them (`app/memory/partner.py`,
        `store.remember`). The third was prose and nothing else — so the very
        first thing she ever learned about you produced no intention at all.

        Detected here rather than pushed from the retirement site because
        retirement happens in three places across two servers, none of which can
        reach a GoalStore without the app layer importing the mind layer. File
        presence is the flag, exactly as it is for the greeting; the date is
        persisted so this is a one-shot, not a thing that re-files whenever the
        goal is completed.
        """
        if self.bootstrapped_on:
            return []
        soul = self.cfg.vault_dir / "soul"
        if (soul / "BOOTSTRAP.md").is_file():
            return []                          # she has not met you yet
        if not (soul / "onboarded" / "BOOTSTRAP.done.md").is_file():
            # No bootstrap was ever installed (an imported card, a hand-built
            # vault). Nothing to hand off, and nothing to keep checking for.
            self.bootstrapped_on = day_of(now)
            return []
        self.bootstrapped_on = day_of(now)
        user_model = self.vault.read("soul/USER.md")
        ongoing = ""
        in_ongoing = False
        for line in user_model.splitlines():
            if line.strip().lower() == "## ongoing":
                in_ongoing = True
                continue
            if in_ongoing and line.startswith("## "):
                break
            if in_ongoing and line.lstrip().startswith("-"):
                candidate = line.lstrip()[1:].strip()
                if candidate and not candidate.startswith("_("):
                    ongoing = candidate
                    break
        if not ongoing:
            return ["our first conversation is done; no concrete follow-up was left open"]
        goal = self.goals.add(
            f"ask {self.cfg.user_name} about this ongoing thread: {ongoing}",
            kind="reach_out", priority=0.55,
            due=iso_of(now + 36 * 3600), commitment="open-minded",
            provenance="bootstrap:first-session", meta={"about": ongoing})
        return [f"our first conversation is done; I want to follow it up: {goal.text}"]

    def set_hands_enabled(self, enabled: bool) -> None:
        """The live kill switch (SPEC §26, as amended, and §32).

        Revoking cancels nothing already dispatched — a research run that is
        halfway through somebody's website cannot be recalled and pretending
        otherwise would be a lie — but every subsequent call is denied, and the
        denial is audited like any other. Granting takes effect on the next tick
        without a restart, which is the property that makes this a switch rather
        than a setting.
        """
        self.hands.granted = bool(enabled)

    def set_goal_filing_enabled(self, enabled: bool) -> None:
        """The same switch, for goals she files herself (§22.1).

        Mutates the live config rather than a flag of its own, because the
        config object *is* the live one — `world/rewire.py` already changes
        models under a running brain the same way — and `DreamContext.file_goal`
        reads it at the moment it files. So switching off between two nights
        takes effect on the next one with no restart, and nothing already filed
        is disturbed: the goals she has are hers to finish or let go of, and
        withdrawing the permission is not the same act as deleting the work.
        """
        self.cfg.mind_goal_filing_enabled = bool(enabled)

    def cadence(self) -> float:
        """REGULATE's other half: how long until the next heartbeat — the
        activity state's cadence, shortened if a goal comes due sooner or a
        backlog of actionable appraisals is waiting its turn."""
        if getattr(self, "_backlog", False):
            return 5.0                         # drain one intention at a time
        delay = self.activity.cadence()
        now = self.clock.now()
        for g in self.goals.open_goals():
            if g.due and g.state == "pending":
                delay = max(1.0, min(delay, ts_of_iso(g.due) - now))
        # …and a wake she scheduled for herself (§16). Without this a follow-up
        # set for twenty minutes' time waits out a DORMANT cadence of fifteen
        # minutes and lands whenever the heartbeat happens to fall.
        for at in self.wakeups.values():
            delay = max(1.0, min(delay, float(at) - now))
        for review in self.promise_reviews:
            retry_at = float(review.get("retry_at", 0) or 0)
            if retry_at > now:
                delay = max(1.0, min(delay, retry_at - now))
        return delay

    async def run(self) -> None:
        """Production loop: tick, then sleep the regulated cadence — woken early
        by any new signal (the bus wake) so a user turn never waits on DORMANT."""
        self.bus.bind_loop()
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — the heartbeat must never stop
                log.exception("tick failed")
            self.bus.wake.clear()
            await self.clock.sleep(self.cadence(), wake=self.bus.wake)

    # ---- the inner-life snapshot the /api/mind route serves (SPEC §24.3) ------

    def snapshot(self) -> dict:
        return {
            "state": self.activity.state,
            "cadence_s": self.activity.cadence(),
            "budget": self.budget.snapshot(),
            "goals": [{"id": g.id, "text": g.text, "kind": g.kind,
                       "state": g.state, "due": g.due,
                       "provenance": g.provenance}
                      for g in self.goals.all()][-30:],
            "pending_edits": self.selfedit.pending(),
            # Goals of her own (§22.1), as the inner-life panel reads them.
            # `open` counts only hers against the cap, so the panel can say
            # "2 of 3" rather than leaving you to work out which of the goals
            # on the list were her idea.
            "goal_filing": {
                "enabled": bool(getattr(self.cfg,
                                        "mind_goal_filing_enabled", True)),
                "open": sum(1 for g in self.goals.open_goals()
                            if str(g.provenance or "").startswith(SELF_GOAL)),
                "max": int(getattr(self.cfg, "mind_self_goals_max", 3) or 0)},
            "shelf": self.knowledge.shelf(),
            "interrupts_today": self.interrupts.get("count", 0),
            "dream_backlog": self.dreams.backlog(),
            "dream_jobs": self.dreams.status(),
            "workspace": (self.workspace.digest(limit=10)
                          if self.workspace else ""),
            # her hands, as the switchboard and the inner-life page read them
            # (§26, as amended). `available` is empty and `granted` irrelevant
            # while the house switch is off — which is what "off means
            # invisible" looks like from outside.
            "hands": {"enabled": self.hands.enabled,
                      "granted": self.hands.granted,
                      "allowlist": list(self.hands.allowlist),
                      "calls_today": self.hands.spent.get("count", 0),
                      "calls_per_day": self.cfg.mind_tool_calls_per_day},
            "skills": [s.as_dict() for s in self.skills.all()]
                      if self.skills else [],
        }
