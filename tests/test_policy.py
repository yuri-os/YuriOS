"""Activity states + the two salience gates (SPEC §17, §18) — the make-or-break
component, unit-tested. The scenario battery proves the behaviour; these pin
the mechanics: the preempt, the drift, the quiet-hours gate, the daily cap.
"""
from __future__ import annotations

import pytest

from yurios.mind.policy import (DORMANT, DREAM, ENGAGED, IDLE, ActivityController,
                         appraise_signal, score_interrupt)
from yurios.mind.signals import Signal
from yurios.mind.util import jsonl_tail
from yurios.kernel.clock import VirtualClock
from yurios.world.config import Config

from .conftest import SIM_START


@pytest.fixture
def clock():
    return VirtualClock(start=SIM_START.timestamp())      # Monday 09:00 local


@pytest.fixture
def activity(tmp_path, clock):
    cfg = Config(_env_file=None)
    return ActivityController(tmp_path / "state", clock, cfg)


def test_preempt_wins_from_any_state(activity, clock):
    activity.state = DORMANT
    activity.preempt_engaged()
    assert activity.state == ENGAGED
    assert activity.cadence() == 2.0


def test_drift_down_the_cost_ladder(activity, clock):
    activity.preempt_engaged()
    clock.advance(200)                                     # > engaged_timeout
    assert activity.update(dream_backlog=False, budget_pressure=0) == IDLE
    clock.advance(3700)                                    # > idle_timeout
    assert activity.update(dream_backlog=False, budget_pressure=0) == DORMANT
    assert activity.cadence() == 900.0


def test_dream_only_from_dormant_inside_the_window(activity, clock):
    activity.state = DORMANT
    # 09:00 — outside the 02:00–06:00 window
    assert activity.update(dream_backlog=True, budget_pressure=0) == DORMANT
    clock.advance(18 * 3600)                               # Tuesday 03:00
    assert activity.update(dream_backlog=True, budget_pressure=0) == DREAM
    # backlog done → back to DORMANT
    assert activity.update(dream_backlog=False, budget_pressure=0) == DORMANT


def test_budget_pressure_sheds_idle(activity, clock):
    activity.preempt_engaged()
    clock.advance(200)
    activity.update(dream_backlog=False, budget_pressure=0.5)
    assert activity.state == IDLE
    assert activity.update(dream_backlog=False, budget_pressure=1.2) == DORMANT


def test_restart_resumes_in_place(tmp_path, clock):
    cfg = Config(_env_file=None)
    a = ActivityController(tmp_path / "state", clock, cfg)
    a.preempt_engaged()
    b = ActivityController(tmp_path / "state", clock, cfg)  # a fresh process
    assert b.state == ENGAGED
    assert b.last_user_msg == a.last_user_msg


# --- the transition log: where has she been (traces/activity.jsonl) ------------

@pytest.fixture
def logged(tmp_path, clock):
    """A controller that keeps a timeline, which is what the debug page reads."""
    cfg = Config(_env_file=None)
    return ActivityController(tmp_path / "state", clock, cfg,
                              trace_dir=tmp_path / "traces")


def rows(controller):
    return jsonl_tail(controller.log_path, 100)


def test_the_timeline_opens_with_where_she_started(logged):
    assert [(r["from"], r["to"], r["reason"]) for r in rows(logged)] \
        == [(None, IDLE, "boot")]


def test_a_quiet_tick_writes_nothing(logged, clock):
    """`_persist` runs every tick and rewrites activity.json every time. If the
    log followed it, the timeline would be one row per heartbeat and would say
    nothing at all — which is the reason it is a separate file."""
    logged.preempt_engaged()
    settled = len(rows(logged))
    for _ in range(10):                      # 100 s total, well inside the 180 s
        clock.advance(10)                    # ENGAGED timeout — she does not move
        logged.update(dream_backlog=False, budget_pressure=0)
    assert logged.state == ENGAGED
    assert len(rows(logged)) == settled, "ten heartbeats, nothing happened"


def test_each_rung_of_the_ladder_names_why_it_fired(logged, clock):
    logged.preempt_engaged()
    clock.advance(200)
    logged.update(dream_backlog=False, budget_pressure=0)     # → IDLE
    clock.advance(3700)
    logged.update(dream_backlog=False, budget_pressure=0)     # → DORMANT
    assert [(r["from"], r["to"], r["reason"]) for r in rows(logged)] == [
        (None, IDLE, "boot"),
        (IDLE, ENGAGED, "user_turn"),
        (ENGAGED, IDLE, "engaged_timeout"),
        (IDLE, DORMANT, "idle_timeout"),
    ]


def test_budget_pressure_is_recorded_as_its_own_reason(logged, clock):
    logged.preempt_engaged()
    clock.advance(200)
    logged.update(dream_backlog=False, budget_pressure=0)
    logged.update(dream_backlog=False, budget_pressure=1.2)
    assert rows(logged)[-1]["reason"] == "budget_pressure"


def test_the_dream_window_round_trip_is_two_rows(logged, clock):
    logged.state = DORMANT
    clock.advance(18 * 3600)                                  # 03:00, in window
    logged.update(dream_backlog=True, budget_pressure=0)
    logged.update(dream_backlog=False, budget_pressure=0)
    assert [(r["from"], r["to"], r["reason"]) for r in rows(logged)][-2:] == [
        (IDLE, DREAM, "dream_window"),      # `from` is the last state we logged
        (DREAM, DORMANT, "dream_done"),
    ]


def test_a_row_carries_what_the_timeline_draws_with(logged, clock):
    logged.preempt_engaged()
    row = rows(logged)[-1]
    assert row["to"] == ENGAGED
    assert row["cadence_s"] == 2.0
    assert row["at"] == clock.now() and row["ts"].startswith("20")
    assert row["last_user_msg"] == clock.now()


def test_a_restart_in_place_is_not_a_transition(tmp_path, clock):
    cfg = Config(_env_file=None)
    kw = {"trace_dir": tmp_path / "traces"}
    a = ActivityController(tmp_path / "state", clock, cfg, **kw)
    a.preempt_engaged()
    before = len(rows(a))
    b = ActivityController(tmp_path / "state", clock, cfg, **kw)  # fresh process
    b.update(dream_backlog=False, budget_pressure=0)              # still ENGAGED
    assert len(rows(b)) == before, "resuming where she was is not a state change"


def test_without_a_trace_dir_there_is_simply_no_log(activity, clock):
    """The mind can run without one (every direct construction in these tests
    does), and that must not be an error path."""
    assert activity.log_path is None
    activity.preempt_engaged()
    activity.update(dream_backlog=False, budget_pressure=0)


# --- gate 1 --------------------------------------------------------------------

def test_nothing_outranks_the_person_speaking(clock):
    msg = Signal(id="1", type="user_message", ts="", payload={})
    fs = Signal(id="2", type="fs_event", ts="", payload={})
    assert appraise_signal(msg).score > appraise_signal(fs).score
    assert appraise_signal(msg).score == 1.0


def test_surprise_raises_salience(clock):
    ev = Signal(id="3", type="fs_event", ts="", payload={})
    assert appraise_signal(ev, surprise=1.0).score > appraise_signal(ev).score


# --- gate 2 --------------------------------------------------------------------

def _score(clock, **kw):
    args = dict(clock=clock, relevance=0.8, time_sensitivity=1.0,
                last_contact_out=None, interrupts_today=0,
                max_interrupts_per_day=3, threshold=0.75)
    args.update(kw)
    return score_interrupt(**args)


def test_a_due_relevant_goal_speaks_in_daytime(clock):
    d = _score(clock)                                      # Monday 09:00
    assert d.outcome == "SPEAK" and d.score >= d.threshold


def test_quiet_hours_are_a_gate_not_a_weight(clock):
    clock.advance(17 * 3600)                               # Tuesday 02:00
    d = _score(clock)
    assert d.outcome == "SILENT", "3am is silent no matter the score"


def test_the_daily_cap_is_hard(clock):
    d = _score(clock, interrupts_today=3)
    assert d.score == 0.0 and d.outcome == "SILENT"


def test_low_time_sensitivity_stays_silent(clock):
    d = _score(clock, time_sensitivity=0.2, relevance=0.5,
               last_contact_out=clock.now() - 3600)        # spoke an hour ago
    assert d.outcome == "SILENT"
    assert d.factors["contact_license"] < 0.1              # recent contact, no license


def test_the_factors_are_shown(clock):
    d = _score(clock)
    for k in ("relevance", "time_sensitivity", "contact_license",
              "availability", "welcome"):
        assert k in d.factors                              # auditable, not vibes
