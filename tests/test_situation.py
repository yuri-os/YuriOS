"""The situation block (SPEC §2.5) — time, body, weather, timers, in sim time.

The two failures this block exists to prevent are pinned here as law: she can
state the clock (it is the injected clock, never the wall clock), and the
embodiment truth is present verbatim — knowing she runs as an AI never licenses
"I have no body."
"""
from __future__ import annotations

import datetime

from yurios.world.avatar.controller import VrmController
from yurios.world.clock import VirtualClock
from yurios.world.situation import EMBODIMENT, render_situation
from yurios.world.tools.timers import TimerBoard


def _render(clock, controller=None, timers=None, user="you"):
    return render_situation(clock, controller=controller or VrmController(),
                            timers=timers or TimerBoard(clock), user_name=user)


def test_clock_line_is_the_injected_clock():
    clock = VirtualClock(start=1_000_000.0)
    now = datetime.datetime.fromtimestamp(clock.now())
    text = _render(clock)
    assert now.strftime("%A") in text
    assert now.strftime("%Y-%m-%d") in text
    assert now.strftime("%H:%M") in text
    # advancing the virtual clock moves the stated time — no wall-clock reads
    clock.advance(3600)
    later = datetime.datetime.fromtimestamp(clock.now())
    assert later.strftime("%H:%M") in _render(clock)


def test_embodiment_truth_is_present_and_names_the_user():
    text = _render(VirtualClock(), user="Grant")
    assert EMBODIMENT.replace("{user}", "Grant") in text
    # the law itself, not a paraphrase
    assert "Never say you have no body" in text
    assert "You know you run as an AI" in text
    # and the answer-shaped law: "can you blink?" is answered yes, not denied
    assert "the answer is always yes" in text
    assert "never call it pretending" in text


def test_scene_state_follows_the_sticky_commands():
    clock = VirtualClock()
    c = VrmController()
    # nothing set yet → no weather or music status lines
    text = _render(clock, controller=c)
    assert "is falling" not in text and "The window is dry" not in text
    c.set_rain(0.6)
    assert "A steady rain is falling" in _render(clock, controller=c)
    c.set_rain(0.9)
    assert "A heavy rain is falling" in _render(clock, controller=c)
    c.set_rain(0.0)
    assert "The window is dry" in _render(clock, controller=c)
    c.music("play", track="night_piano")
    assert '"night_piano" ambience is playing' in _render(clock, controller=c)
    c.music("stop")
    assert "ambience is playing" not in _render(clock, controller=c)


def test_timers_are_listed_with_time_left():
    clock = VirtualClock()
    board = TimerBoard(clock)
    board.add(id="t1", label="tea", seconds=600)
    board.add(id="t2", label="laundry", seconds=7200)
    text = _render(clock, timers=board)
    assert '"tea" (about 10 minutes left)' in text
    assert '"laundry" (about 2 hours left)' in text
    # an elapsed timer leaves the board and the block
    clock.advance(700)
    board.poll()
    text = _render(clock, timers=board)
    assert "tea" not in text
    assert "laundry" in text


def test_no_timers_no_timer_line():
    assert "Timers you have running" not in _render(VirtualClock())
