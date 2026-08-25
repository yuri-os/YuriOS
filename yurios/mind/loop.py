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

import asyncio
import logging
import random
from typing import Awaitable, Callable

from yurios.kernel import correlate
from yurios.world.avatar.controller import VrmController
from yurios.world.brain_protocol import AutonomousBrain
from yurios.kernel.clock import Clock
from yurios.kernel.hub import EventHub
from yurios.world.tools.timers import TimerBoard
from yurios.world.vram import ParkGate

from .budget import BudgetGovernor
from .dream import DreamConsolidator
from .dreamjobs import SELF_GOAL, DreamRunner
from .goals import (Goal, GoalStore, discover_promise_candidates,
                    fallback_promises, trim)
from .hands import (Hands, build_guard)
from .journal import Journal
from .knowledge import KnowledgeStore
from .promptlog import PromptLog
from . import acts, goalwork, housekeeping, prompts
from .policy import (DREAM, ENGAGED, IDLE, ActivityController, Appraisal,
                     appraise_goal, appraise_signal)
from .selfedit import SelfEdit
from .signals import Signal, SignalBus
from .trace import TickTrace
from .util import new_id, read_json, ts_of_iso, write_json
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
            deliver_report=lambda *a, **k: acts.deliver_report(self, *a, **k),
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
        """The persona blocks her private prompts open with (mind/prompts.py)."""
        return prompts.soul_text(self, full=full)

    def _soul_drives(self) -> list[str]:
        """Current durable motives, loaded live like every other SOUL surface."""
        return prompts.soul_drives(self)

    async def _utility(self, messages: list[dict], *, soul: bool = False,
                       **params) -> str:
        """Local-tier utility call, debited against the governor (mind/prompts.py).

        Kept as a method because it is a *seam*: `__init__` hands this bound
        method to the knowledge store, the dream consolidator and the goal-work
        step as their `utility`, and five callers hold the reference.
        """
        return await prompts.utility(self, messages, soul=soul, **params)

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
                    note = acts.file_promise_candidate(
                        self, candidate, user_text=user_text, reply=reply)
                    if note:
                        reflect_notes.append(note)
                if assistant and acts.promise_review_available(self):
                    known = {str(review.get("signal_id", ""))
                             for review in self.promise_reviews}
                    if sig.id not in known:
                        normalized = [
                            {**candidate.as_dict(), "index": index}
                            for index, candidate in enumerate(assistant)]
                        self.promise_reviews.append({
                            "signal_id": sig.id,
                            "user_text": trim(user_text, 1000),
                            "reply": trim(reply, 3000),
                            "candidates": normalized,
                            "attempts": 0,
                        })
                        # The signal bus is not replayed after restart. Persist
                        # before advancing its offset at the end of this tick.
                        self._persist()
                elif assistant:
                    for candidate in fallback_promises(assistant):
                        note = acts.file_promise_candidate(
                            self, candidate, user_text=user_text, reply=reply)
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
                note = acts.wake_goal(self, str(sig.payload.get("goal", "")))
                if note:
                    reflect_notes.append(note)
            elif sig.type == "task_completion":
                # Bookkeeping first — the goal that dispatched this comes back
                # from `waiting` here, where it cannot starve behind another
                # intention — and then on to APPRAISE, which is where the
                # journal line about it is decided (the branch in ACT).
                note = acts.land_dispatched(self, sig)
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
        eligible_reviews = acts.eligible_promise_reviews(self)
        if eligible_reviews and acts.promise_review_available(self):
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
        # git is a subprocess: on the loop it stalls every room this host is
        # holding open, including the other characters' (SPEC §2.2)
        await asyncio.to_thread(
            self.vault.commit_if_dirty,
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
            return await acts.announce(self)
        if chosen.subject == "self_talk":
            return await acts.self_talk(self)
        if chosen.subject == "ingest":
            return await acts.ingest(self)
        if chosen.subject == "dream":
            return await acts.dream(self)
        if chosen.subject == "promise_review":
            return await acts.promise_review(self, offer)
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
                return await acts.reach_out(self, goal)
            # `tool_step` is reachable ONLY from here (§26, as amended): a hand
            # she reaches for is a step of an open goal or it does not happen.
            return await self._act_goal_work(
                goal, offer if chosen.kind == "tool_step" else None)
        return ({"what": None, "result": "rest"}, {}, [])










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
        await asyncio.to_thread(self.vault.commit_if_dirty,
                                f"dream (by hand): {report.summary[:60]}")
        return report

    # ---- initiative: gate 2 lives here (SPEC §18.2–§18.3) -----------------------


    # ---- the goal lifecycle's two return paths (SPEC §22, §16) ----------------




    #: Where a goal's working notes live (§34.1, §22). One file per goal, in her
    #: own workspace, so "what did she work out about this" is a file a person
    #: can open — and so the next tick can read back what the last one concluded
    #: instead of starting from the goal's one-line text every time.
    GOAL_DESK = "goals/{id}.md"





    def _goal_context(self, goal: Goal) -> str:
        """What a working step is given to work from (mind/goalwork.py)."""
        return goalwork.context(self, goal)

    async def _act_goal_work(self, goal: Goal,
                             offer=None) -> tuple[dict, dict, list[str]]:
        """Work one goal for one step (mind/goalwork.py) — the tick's most
        expensive act, and the only one that may reach for a hand (§26)."""
        return await goalwork.goal_work(self, goal, offer)

    #: The words a step uses when it means "this goal is finished". Deliberately
    #: a phrase she has to *choose*, not a heuristic over the note's contents: a
    #: goal closing itself because a paragraph sounded conclusive is how a
    #: standing commitment quietly disappears.
    DONE_MARK = "goal complete"





    # ---- the hands, in one tick (SPEC §26, as amended) -------------------------


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
        """Local midnight: close yesterday, file what it left (mind/housekeeping.py)."""
        return housekeeping.day_rollover(self, now)

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
        return housekeeping.leftover(self, which)

    def _file_maintenance(self) -> list[str]:
        """The maintenance goals this morning's Vault implies (mind/housekeeping.py)."""
        return housekeeping.file_maintenance(self)

    def _bootstrap_handoff(self, now: float) -> list[str]:
        """Once, and then never again (mind/housekeeping.py)."""
        return housekeeping.bootstrap_handoff(self, now)

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
