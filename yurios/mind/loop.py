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

from yurios.world.avatar.controller import VrmController
from yurios.world.clock import Clock
from yurios.world.hub import EventHub
from yurios.world.tools.timers import TimerBoard

from .budget import BudgetGovernor
from .dream import DreamConsolidator
from .goals import Goal, GoalStore, extract_promises
from .journal import Journal
from .knowledge import KnowledgeStore
from .policy import (DREAM, ENGAGED, IDLE, ActivityController, Appraisal,
                     appraise_goal, appraise_signal, score_interrupt)
from .selfedit import SelfEdit
from .signals import Signal, SignalBus
from .trace import TickTrace
from .util import day_of, iso_of, new_id, read_json, ts_of_iso, write_json
from .vaultio import MindVault
from .world import WorldModelStore

log = logging.getLogger("mind.loop")

SUSPEND_GAP_S = 2 * 3600.0

# scene canon, carried over from the idle machine it replaced (SPEC §15.5):
# when she rain-gazes, this is the window the scene builds.
WINDOW_TARGET = {"x": -1.4, "y": 1.45, "z": 0.6}

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


class MindLoop:
    """The autonomy engine, assembled over the host's existing surfaces."""

    def __init__(self, cfg, clock: Clock, *,
                 bus: SignalBus,
                 brain,                                    # the ToolBrain
                 controller: VrmController,
                 timers: TimerBoard,
                 hub: EventHub,
                 speak: Callable[[str], Awaitable[bool]],  # Runtime.speak_ambient
                 post_message,                             # Runtime.post_message
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
        self.rng = rng or random.Random(cfg.mind_seed or None)

        # the mind's home: the same Vault the brain already keeps (SPEC §15.2)
        self.vault = MindVault(cfg.vault_dir)
        state = brain.state                    # the Build #1 AppState
        self.world = WorldModelStore(self.vault, clock, controller=controller,
                                     timers=timers, user_name=cfg.user_name)
        if hasattr(brain, "set_world"):
            brain.set_world(self.world)        # the §19.2 seam swap: every prompt
                                               # now carries the store's stage
        self.knowledge = KnowledgeStore(self.vault, state.embedder, clock,
                                        utility=self._utility)
        self.goals = GoalStore(self.vault, clock)
        self.selfedit = SelfEdit(self.vault, clock)
        self.journal = Journal(self.vault, clock, hub, store=state.store)
        self.dream = DreamConsolidator(self.vault, state.store, clock,
                                       utility=self._utility
                                       if state.utility else None)
        state_dir = cfg.vault_dir / "state"
        self.activity = ActivityController(state_dir, clock, cfg)
        self.budget = BudgetGovernor(state_dir, clock,
                                     daily_tokens=cfg.mind_daily_tokens)
        self.trace = TickTrace(cfg.trace_dir, clock)

        # rehydration snapshot (SPEC §15.4): a restart resumes, not forgets
        self.state_path = state_dir / "engine.json"
        st = read_json(self.state_path, None) or {}
        self.offset: int = st.get("bus_offset", 0)
        self.interrupts: dict = st.get("interrupts", {"date": "", "count": 0})
        self.considered: dict = st.get("considered", {})
        self.last_tick_ts: float | None = st.get("last_tick_ts")

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

    async def _utility(self, messages: list[dict]) -> str:
        """Local-tier utility call, debited against the governor. The loop's
        only other model use is inside deliberate ACT speech (SPEC §17.3)."""
        utility = self.brain.state.utility
        if utility is None:
            return ""
        text = await utility.complete(messages)
        self.budget.debit("".join(m.get("content", "") for m in messages), text)
        return text

    def _brain_session(self) -> str:
        if self._session is None:
            self._session = self.brain.resolve_session(None)
        return self._session

    async def _compose(self, cue: str) -> str:
        """One line in her own voice, without the voice pipeline — used when a
        reach-out finds no page open to speak through (SPEC §18.3)."""
        out: list[str] = []
        async for tok in self.brain.stream_ambient(self._brain_session(), cue):
            out.append(tok)
        text = "".join(out).strip()
        self.budget.debit(cue, text)
        # strip any leading [expression] tag — this line lands as chat text
        if text.startswith("[") and "]" in text[:24]:
            text = text.split("]", 1)[1].strip()
        return text

    # ---- notifications from the voice route (same surface the idle machine had)

    def turn_started(self) -> None:
        self._turns_in_flight += 1
        self.activity.preempt_engaged()        # the preempt wins, from ANY state
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
                # the reactive path already replied (SPEC §15.3); REFLECT's share
                # is the promise scan — her own words become goals she must keep
                for text, prov in extract_promises(
                        sig.payload.get("reply", ""), sig.payload.get("text", "")):
                    g = self.goals.add(
                        text, kind="reach_out", priority=0.6,
                        due=iso_of(self.clock.now() + 24 * 3600),
                        commitment="single-minded", provenance=prov)
                    reflect_notes.append(f"I promised: {g.text}")
            elif sig.type == "selfedit_decision":
                res = self.selfedit.decide(sig.payload.get("id", ""),
                                           bool(sig.payload.get("approve")))
                if res:
                    reflect_notes.append(
                        f"you {res.outcome} my edit to {res.surface}")
            elif sig.type == "suspend_gap":
                self.goals.reconsider()        # ONE catch-up over the whole gap
                reflect_notes.append(
                    f"the machine slept ~{sig.payload.get('hours', 0):.1f}h; "
                    "I caught up on what expired and what still matters")
            elif sig.type == "timer":
                self._pending_announce.append(sig.payload)
            elif sig.type in ("user_present", "user_absent"):
                pass                           # observed above; greeting is the
                                               # voice route's job in this build
            else:
                actionable.append(sig)

        # ---- APPRAISE (cheap by construction: heuristics, no model) ----------
        appraisals: list[Appraisal] = [
            appraise_signal(s, surprise=surprise) for s in actionable]
        if self._pending_announce and not self._engaged_now():
            appraisals.append(Appraisal("announce", "impulse", 0.9,
                                        "a timer landed — a promise due"))
        for g in self.goals.open_goals():
            if g.state == "waiting":
                continue
            last = self.considered.get(g.id)
            if last and (now - last) < self.cfg.mind_consider_cooldown_s:
                continue                       # don't re-chew one goal every tick
            appraisals.append(appraise_goal(g, self.clock))
        if self.knowledge.pending_docs():
            appraisals.append(Appraisal("ingest", "impulse", 0.55,
                                        "new document on the shelf"))
        if self.activity.state == DREAM and self.dream.backlog():
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
                   "runners_up": [self._describe(a) for a in appraisals[1:4]]}
        # more than one thing worth doing? one intention per tick still holds —
        # the runners-up just shorten the next heartbeat instead of piling into
        # this one (the DREAM chunking discipline, generalised)
        self._backlog = sum(
            1 for a in appraisals if a.score >= self.cfg.mind_act_threshold) > 1

        # ---- ACT: at most one act, through the host's own surfaces ------------
        acted: dict = {"what": None, "result": "rest"}
        interrupt: dict = {}
        if chosen is not None:
            try:
                acted, interrupt, act_notes = await self._act(chosen)
                reflect_notes.extend(act_notes)
            except Exception as e:  # noqa: BLE001 — a failed act never kills the loop
                log.exception("ACT failed")
                acted = {"what": "error", "result": f"error: {e}"}

        # ---- REFLECT: journal + trace, always ----------------------------------
        for note in reflect_notes:
            self.journal.write(note)
        trace_rec = {
            "activity_state": self.activity.state,
            "sensed": [{"type": s.type, "id": s.id} for s in batch],
            "appraised": [{"what": self._describe(a),
                           "score_to_act": round(a.score, 3)} for a in appraisals],
            "decided": decided, "acted": acted, "interrupt": interrupt,
        }
        self.trace.record(tick_id=self._tick_id, **trace_rec)

        # ---- REGULATE -----------------------------------------------------------
        self.activity.update(dream_backlog=bool(self.dream.backlog()),
                             budget_pressure=self.budget.pressure())
        self._body_reflexes(now)
        self.vault.commit_if_dirty(
            f"tick {self._tick_id}: {decided['intention'][:60]}")
        self.offset = new_offset
        self.last_tick_ts = now
        self._persist()
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
        return str(a.subject)

    # --------------------------------------------------------------------- ACT

    async def _act(self, chosen: Appraisal) -> tuple[dict, dict, list[str]]:
        if chosen.subject == "announce":
            return await self._act_announce()
        if chosen.subject == "self_talk":
            return await self._act_self_talk()
        if chosen.subject == "ingest":
            return await self._act_ingest()
        if chosen.subject == "dream":
            return await self._act_dream()
        if chosen.kind == "signal":
            sig: Signal = chosen.subject
            if sig.type == "task_completion":
                return ({"what": "noted", "result": f"task done: "
                         f"{sig.payload.get('task', '?')}"}, {},
                        [f"finished something I'd started: "
                         f"{sig.payload.get('task', 'a task')}"])
            return ({"what": "noted", "result": f"noted {sig.type}"}, {}, [])
        if chosen.kind == "goal":
            goal: Goal = chosen.subject
            self.considered[goal.id] = self.clock.now()
            if goal.kind == "reach_out":
                return await self._act_reach_out(goal)
            return await self._act_goal_work(goal)
        return ({"what": None, "result": "rest"}, {}, [])

    async def _act_announce(self) -> tuple[dict, dict, list[str]]:
        """A landed timer — a promise, so it queues until deliverable (the
        Build #4 rule, kept verbatim)."""
        t = self._pending_announce[0]
        self.controller.set_expression("surprised", 0.6, reset_ms=4000)
        cue = ANNOUNCE_CUE.format(label=t.get("label", "your timer"),
                                  user=self.cfg.user_name)
        if await self.speak(cue):
            self._pending_announce.pop(0)
            return ({"what": "speak", "result": "announced the timer"}, {},
                    [f"told them the “{t.get('label')}” timer finished"])
        return ({"what": "speak", "result": "announce queued (nobody to tell)"},
                {}, [])

    async def _act_self_talk(self) -> tuple[dict, dict, list[str]]:
        """The Ukagaka murmur, now decided rather than diced — ambient, never
        persisted, dropped if she can't be heard (SPEC §15.5)."""
        cue = self.rng.choice(SELF_TALK_CUES).format(user=self.cfg.user_name)
        delivered = await self.speak(cue)
        self._next_self_talk = self.clock.now() + self._uniform(
            self.cfg.idle_talk_min_s, self.cfg.idle_talk_max_s)
        return ({"what": "speak",
                 "result": "murmured to herself" if delivered else
                           "let the murmur go (busy or alone)"}, {}, [])

    async def _act_ingest(self) -> tuple[dict, dict, list[str]]:
        results = await self.knowledge.scan()
        notes = [f"read and shelved {r.doc} ({r.chunks} passages)"
                 for r in results]
        return ({"what": "knowledge.ingest",
                 "result": f"ingested {len(results)} doc(s)"}, {}, notes)

    async def _act_dream(self) -> tuple[dict, dict, list[str]]:
        report = await self.dream.consolidate(
            token_budget=self.cfg.mind_dream_tick_tokens)
        msg = (f"DREAM: consolidated {len(report.days_processed)} day(s), "
               f"{report.facts_added} fact(s)"
               + (", budget spent — backlog remains"
                  if report.exhausted_budget else ""))
        notes = []
        if report.days_processed:
            notes.append(f"slept on it: folded {', '.join(report.days_processed)} "
                         "into what I keep")
        return ({"what": "dream", "result": msg}, {}, notes)

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
                self.post_message("assistant", text, proactive=True)
            self.world.note_contact_out()
            self.interrupts["count"] += 1
            self.goals.set_state(goal.id, "done")
            return ({"what": "chat", "result": f"left a quiet note: {goal.text}"},
                    interrupt, [f"left them a note about {goal.text}"])

        # SPEAK: aloud through the ambient seam if a page is open (the full turn
        # pipeline — voice, face, barge-in); as a chat line if the room is empty
        cue = REACH_OUT_CUE.format(goal=goal.text)
        if not await self.speak(cue):
            text = await self._compose(cue)
            if text:
                self.post_message("assistant", text, proactive=True)
        self.world.note_contact_out()
        self.interrupts["count"] += 1
        self.goals.set_state(goal.id, "done")
        return ({"what": "speak", "result": f"reached out: {goal.text}"},
                interrupt, [f"reached out first about {goal.text}"])

    async def _act_goal_work(self, goal: Goal) -> tuple[dict, dict, list[str]]:
        """Advance a task/maintenance goal with one bounded local-tier step —
        a working note in the journal, never a message to the user."""
        note = await self._utility([
            {"role": "system",
             "content": "You are quietly advancing one of your own goals. "
                        "Write a short working note (<=80 words) of what you "
                        "concluded or want to try next. Just the note."},
            {"role": "user", "content": f"The goal: {goal.text}"}])
        note = (note or "").strip() or f"(sat with it; nothing new yet on: {goal.text})"
        self.goals.set_state(goal.id, "done")
        return ({"what": "goal_work", "result": "worked the goal; journaled"},
                {}, [f"worked on: {goal.text} — {note[:120]}"])

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
            "considered": self.considered, "last_tick_ts": self.last_tick_ts})

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
            "shelf": self.knowledge.shelf(),
            "interrupts_today": self.interrupts.get("count", 0),
            "dream_backlog": self.dream.backlog(),
        }
