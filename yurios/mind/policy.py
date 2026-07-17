"""Activity states + the two salience gates (SPEC §17, §18).

ENGAGED · IDLE · DORMANT · DREAM govern cadence and the permitted model tier —
the single most important cost control in an always-on mind. The preempt
overrides everything: a user turn pulls the loop straight to ENGAGED from any
state. Everything else is a slow drift *down* the cost ladder.

Gate 1 (salience-to-act) is crossed often and cheaply — pure heuristics, never
a model call, because it runs every tick. Gate 2 (salience-to-interrupt) is
crossed rarely, and only after deliberate work has already happened. Collapsing
the two gates is precisely Clippy (→ ch. 18).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from yurios.world.clock import Clock

from .util import dt_of, read_json, write_json

ENGAGED, IDLE, DORMANT, DREAM = "ENGAGED", "IDLE", "DORMANT", "DREAM"


class ActivityController:
    """SPEC §17.1: the state ladder, persisted so a restart resumes in place."""

    def __init__(self, state_dir: Path, clock: Clock, cfg):
        self.clock = clock
        self.cfg = cfg
        self.path = state_dir / "activity.json"
        st = read_json(self.path, None) or {}
        self.state: str = st.get("state", IDLE)
        self.last_user_msg: float | None = st.get("last_user_msg")

    def _persist(self) -> None:
        write_json(self.path, {"state": self.state,
                               "cadence_s": self.cadence(),
                               "last_user_msg": self.last_user_msg})

    def preempt_engaged(self) -> None:
        """A user turn, from ANY state, mid-tick if necessary (SPEC §17.2)."""
        self.state = ENGAGED
        self.last_user_msg = self.clock.now()
        self._persist()

    def update(self, *, dream_backlog: bool, budget_pressure: float) -> str:
        """REGULATE's half: drift down the cost ladder (SPEC §17.1)."""
        now = self.clock.now()
        since_msg = (now - self.last_user_msg) if self.last_user_msg else float("inf")

        if self.state == ENGAGED and since_msg > self.cfg.mind_engaged_timeout_s:
            self.state = IDLE
        if self.state == IDLE and since_msg > self.cfg.mind_idle_timeout_s:
            self.state = DORMANT
        if self.state == DORMANT and dream_backlog and self._in_dream_window(now):
            self.state = DREAM
        if self.state == DREAM and not dream_backlog:
            self.state = DORMANT
        # budget pressure sheds the expensive states (SPEC §17.3 → REGULATE)
        if budget_pressure >= 1.0 and self.state == IDLE:
            self.state = DORMANT
        self._persist()
        return self.state

    def _in_dream_window(self, now: float) -> bool:
        h = dt_of(now).hour
        lo, hi = self.cfg.mind_dream_start_hour, self.cfg.mind_dream_end_hour
        return lo <= h < hi if lo <= hi else (h >= lo or h < hi)

    def cadence(self) -> float:
        return {ENGAGED: self.cfg.mind_engaged_cadence_s,
                IDLE: self.cfg.mind_idle_cadence_s,
                DORMANT: self.cfg.mind_dormant_cadence_s,
                DREAM: self.cfg.mind_dream_cadence_s}[self.state]


# --- Gate 1: salience-to-act (SPEC §18.1) --------------------------------------

@dataclass
class Appraisal:
    subject: object          # Signal | Goal | str ("dream", "self_talk", …)
    kind: str                # 'signal' | 'goal' | 'dream' | 'impulse'
    score: float
    why: str


SIGNAL_BASE = {
    "user_message": 1.0,      # nothing is more salient than the person speaking
    "turn_committed": 0.85,   # her own conversation, to reflect over
    "user_present": 0.95,
    "selfedit_decision": 0.9,
    "task_completion": 0.8,
    "suspend_gap": 0.75,
    "timer": 0.9,             # a timer is a promise — it preempts rest
    "wakeup": 0.5,
    "fs_event": 0.45,
    "user_absent": 0.2,
}


def appraise_signal(sig, *, surprise: float = 0.0) -> Appraisal:
    base = SIGNAL_BASE.get(sig.type, 0.3)
    score = min(1.0, base + 0.3 * surprise)
    return Appraisal(sig, "signal", score, f"{sig.type} (surprise={surprise:.2f})")


def appraise_goal(goal, clock: Clock) -> Appraisal:
    score = goal.priority * 0.6
    why = f"priority {goal.priority}"
    if goal.is_due(clock):
        score += 0.35
        why += ", due soon"
    if goal.is_stale(clock) and goal.commitment == "blind":
        score += 0.2
        why += ", blind commitment defends it"
    return Appraisal(goal, "goal", min(1.0, score), why)


# --- Gate 2: salience-to-interrupt (SPEC §18.2) ---------------------------------

@dataclass
class InterruptDecision:
    score: float
    threshold: float
    outcome: str             # SILENT | SUGGEST | SPEAK
    factors: dict = field(default_factory=dict)


def score_interrupt(*, clock: Clock,
                    relevance: float,
                    time_sensitivity: float,
                    last_contact_out: float | None,
                    interrupts_today: int,
                    max_interrupts_per_day: int,
                    threshold: float) -> InterruptDecision:
    now = clock.now()
    hours_since_contact = ((now - last_contact_out) / 3600
                           if last_contact_out else 48.0)
    contact_license = min(1.0, hours_since_contact / 24.0)  # quiet → more license

    h = dt_of(now).hour
    availability = 1.0 if 9 <= h < 22 else 0.15             # don't ping at 3am
    welcome = max(0.0, 1.0 - interrupts_today / max(1, max_interrupts_per_day))

    score = (0.30 * relevance + 0.35 * time_sensitivity
             + 0.10 * contact_license + 0.15 * availability + 0.10 * welcome)
    if interrupts_today >= max_interrupts_per_day:
        score = 0.0                                          # the hard daily cap

    if score < threshold or availability < 0.5:
        outcome = "SILENT"   # THE DEFAULT — and quiet hours are a gate, not a weight
    elif score < threshold + 0.1:
        outcome = "SUGGEST"  # a soft line in the journal/chat, never spoken aloud
    else:
        outcome = "SPEAK"
    return InterruptDecision(round(score, 3), threshold, outcome, {
        "relevance": relevance, "time_sensitivity": time_sensitivity,
        "contact_license": round(contact_license, 2),
        "availability": availability, "welcome": round(welcome, 2),
        "interrupts_today": interrupts_today,
    })
