"""The timer board (SPEC §7.5) — host-side scheduling, in sim time (§13)."""
from __future__ import annotations


def test_timer_lands_on_the_due_queue_when_the_clock_passes_it(clock, timers):
    timers.add(id="t1", label="tea", seconds=600)
    assert timers.poll() == []                 # not yet
    assert [t.id for t in timers.pending()] == ["t1"]

    clock.advance(599)
    assert timers.poll() == []
    clock.advance(1)
    landed = timers.poll()
    assert [t.id for t in landed] == ["t1"]
    assert timers.due.get_nowait().label == "tea"
    assert timers.pending() == []


def test_multiple_timers_land_in_due_order(clock, timers):
    timers.add(id="b", label="oven", seconds=120)
    timers.add(id="a", label="tea", seconds=60)
    clock.advance(200)
    timers.poll()
    assert timers.due.get_nowait().id == "a"   # nearer deadline announces first
    assert timers.due.get_nowait().id == "b"


async def test_run_loop_polls_on_the_virtual_clock(clock, timers):
    """One pass of the production loop moves an elapsed timer without a real
    sleep — the VirtualClock advances instead of waiting."""
    import asyncio
    timers.add(id="t", label="tea", seconds=30)
    task = asyncio.create_task(timers.run())
    for _ in range(5):                          # a few loop iterations
        await asyncio.sleep(0)
    task.cancel()
    # the loop's clock.sleep calls advanced virtual time past the deadline
    assert clock.now() >= 1_000_030
    assert not timers.due.empty()
