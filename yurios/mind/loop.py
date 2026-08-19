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

from yurios.world import correlate
from yurios.world.avatar.controller import VrmController
from yurios.world.clock import Clock
from yurios.world.hub import EventHub
from yurios.world.tools.timers import TimerBoard
from yurios.world.vram import PATIENT_WAIT_S, ParkGate

from .budget import BudgetGovernor
from .dream import DreamConsolidator
from .dreamjobs import DreamRunner
from .goals import Goal, GoalStore, extract_promises
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
        if hasattr(brain, "set_world"):
            brain.set_world(self.world)        # the §19.2 seam swap: every prompt
                                               # now carries the store's stage
        self.knowledge = KnowledgeStore(
            self.vault, state.embedder, clock,
            utility=self._utility if cfg.utility_enabled and state.utility else None,
            min_score=cfg.knowledge_min_score)
        if hasattr(brain, "set_knowledge"):
            brain.set_knowledge(self.knowledge)  # §20.2: the shelf joins the
                                                 # prompt's knowledge slot
        # her desk and her skills (§34). Built before the dream runner, which
        # gives jobs a place to write, and before the brain seam below.
        self._desk_notes: list[str] = []       # drained by REFLECT, see below
        self.workspace = (Workspace(cfg.vault_dir / "workspace")
                          if cfg.workspace_enabled else None)
        self.skills = (SkillStore(cfg.vault_dir / "skills")
                       if cfg.skills_enabled else None)
        if hasattr(brain, "set_workspace"):
            brain.set_workspace(self.workspace, self.skills,
                                on_write=self._desk_written)
        self.goals = GoalStore(self.vault, clock)
        self.selfedit = SelfEdit(self.vault, clock)
        self.journal = Journal(self.vault, clock, hub, store=state.store)
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
            # A night's desk writes and its camera dispatch land in the same
            # audit as her daytime hands (§7.3), so the Tools page answers
            # "what did this actually do to the vault" for DREAM too.
            audit=(brain.guard.audit
                   if getattr(brain, "guard", None) is not None else None))
        state_dir = cfg.vault_dir / "state"
        self.activity = ActivityController(state_dir, clock, cfg,
                                           trace_dir=cfg.trace_dir)
        self.budget = BudgetGovernor(state_dir, clock,
                                     daily_tokens=cfg.mind_daily_tokens)
        self.trace = TickTrace(cfg.trace_dir, clock, max_bytes=cfg.mind_trace_max_bytes)
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
        only other model use is inside deliberate ACT speech (SPEC §17.3).

        One instrumentation point covers five callers: the knowledge store, the
        dream consolidator and the goal-work step are all handed *this method*
        as their `utility` seam (see __init__), so the prompt each of them sends
        is recorded here, labelled by whichever ACT opened the scope."""
        utility = self.brain.state.utility
        if utility is None:
            return ""
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
            text = await utility.complete(messages)
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
                           "score_to_act": round(a.score, 3)} for a in appraisals],
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
        stopped. What changed is only how many kinds of work that covers.
        """
        if not self.cfg.dream_enabled or not self.cfg.utility_enabled:
            return ({"what": "dream", "result": "DREAM disabled"}, {}, [])
        with correlate.scope(kind=correlate.DREAM):
            report = await self.dreams.run(
                token_budget=self.cfg.mind_dream_tick_tokens)
        return ({"what": "dream", "result": report.summary,
                 "jobs": [j.as_dict() for j in report.jobs]}, {}, report.notes)

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

    async def _act_goal_work(self, goal: Goal) -> tuple[dict, dict, list[str]]:
        """Advance a task/maintenance goal with one bounded local-tier step —
        a working note in the journal, never a message to the user."""
        with correlate.scope(kind=correlate.GOAL_WORK):
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
            "dream_backlog": self.dreams.backlog(),
            "dream_jobs": self.dreams.status(),
            "workspace": (self.workspace.digest(limit=10)
                          if self.workspace else ""),
            "skills": [s.as_dict() for s in self.skills.all()]
                      if self.skills else [],
        }
