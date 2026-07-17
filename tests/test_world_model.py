"""WorldModelStore (SPEC §19) — the present tense as a store, not a rendering.

The Build #4 situation assertions survive verbatim (the block's place is the
seam that survived); what's new is what only a store can do: presence
arithmetic, expectations that score as surprise, and point-in-time queries.
"""
from __future__ import annotations

import datetime

import pytest

from yurios.mind.signals import Signal
from yurios.mind.util import iso_of
from yurios.mind.vaultio import MindVault
from yurios.mind.world import WorldModelStore
from yurios.world.clock import VirtualClock
from yurios.world.situation import EMBODIMENT
from yurios.world.tools.timers import TimerBoard

from .conftest import SIM_START, SpyController


@pytest.fixture
def world(tmp_path):
    clock = VirtualClock(start=SIM_START.timestamp())
    vault = MindVault(tmp_path / "vault")
    w = WorldModelStore(vault, clock, controller=SpyController(),
                        timers=TimerBoard(clock), user_name="Grant")
    return w, clock


def _sig(type_, clock, **payload):
    return Signal(id="s1", type=type_, ts=iso_of(clock.now()), payload=payload)


def test_situation_carries_the_host_lines_and_presence(world):
    w, clock = world
    text = w.situation()
    # the Build #4 lines survive: the injected clock's time, the embodiment law
    assert datetime.datetime.fromtimestamp(clock.now()).strftime("%H:%M") in text
    assert EMBODIMENT.replace("{user}", "Grant") in text
    assert "Grant hasn't spoken yet." in text
    # …and the snapshot is a file in the Vault
    assert (w.vault.vault / "world" / "situation.md").read_text() == text


def test_presence_and_away_arithmetic(world):
    w, clock = world
    w.observe(_sig("user_message", clock, text="hi"))
    assert "Grant is here right now." in w.situation()
    w.observe(_sig("user_absent", clock))
    clock.advance(5 * 3600)
    assert "away about 5 hours" in w.situation()
    clock.advance(48 * 3600)
    assert "away about 2 days" in w.situation()


def test_expectation_met_resolves_quietly(world):
    w, clock = world
    w.expect("they'll mention the interview", keys=["interview"],
             due_ts=clock.now() + 48 * 3600)
    assert "half-expect" in w.situation()
    res = w.observe(_sig("user_message", clock, text="the interview went great!"))
    assert res.resolved == ["they'll mention the interview"]
    assert res.surprises == []
    assert "half-expect" not in w.situation()


def test_expectation_past_due_scores_as_surprise(world):
    w, clock = world
    w.expect("they'll be back by evening", due_ts=clock.now() + 3600)
    clock.advance(2 * 3600)
    res = w.observe(_sig("user_absent", clock))
    assert len(res.surprises) == 1                 # prediction-error = salience
    assert res.surprises[0]["text"] == "they'll be back by evening"


def test_query_at_is_point_in_time(world):
    w, clock = world
    w.observe(_sig("user_message", clock, text="I got the job"))
    cutoff = iso_of(clock.now())
    clock.advance(3600)
    w.observe(_sig("user_message", clock, text="I quit the job"))
    all_beliefs = w.query("job")
    assert len(all_beliefs) == 2
    then = w.query("job", at=cutoff)
    assert len(then) == 1 and "got the job" in then[0].belief


def test_threads_and_contact_out(world):
    w, clock = world
    w.add_thread("researching cat names", task="task-1")
    assert "In progress: researching cat names" in w.situation()
    w.observe(_sig("task_completion", clock, task="task-1"))
    assert "In progress" not in w.situation()
    assert w.snapshot()["last_contact_out"] is None
    w.note_contact_out()
    assert w.snapshot()["last_contact_out"] is not None
