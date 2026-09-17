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


# ---- a promise outlives the process (§7.5) -----------------------------------
#
# The board used to be a list and nothing else, so every pending timer died with
# the server. At a ceiling of three hours that was a small loss; at a day
# (TIMER_MAX_MINUTES) "wake me at seven" set at midnight has a whole night to be
# restarted through, so the board is written to `<vault>/state/timers.json` and
# read back. A restored timer needs no catch-up logic: `due` is a wall epoch, so
# one that came due while she was down is simply already due.

from yurios.kernel.clock import VirtualClock                      # noqa: E402
from yurios.world.tools.timers import STALE_AFTER_S, TimerBoard   # noqa: E402


def board(vault, clock):
    return TimerBoard(clock, vault=vault)


def test_a_pending_timer_survives_the_process(tmp_path, clock):
    board(tmp_path, clock).add(id="t1", label="the roast", seconds=3600)

    restarted = board(tmp_path, VirtualClock(start=clock.now()))
    assert [(t.id, t.label) for t in restarted.pending()] == [("t1", "the roast")]


def test_a_timer_that_came_due_while_she_was_down_still_lands(tmp_path, clock):
    """The whole point. Nothing catches up: the due time simply passed."""
    board(tmp_path, clock).add(id="t1", label="wake him", seconds=7 * 3600)

    later = VirtualClock(start=clock.now() + 8 * 3600)       # restarted an hour late
    restarted = board(tmp_path, later)
    assert [t.id for t in restarted.poll()] == ["t1"]
    assert restarted.pending() == []


def test_a_landed_timer_is_not_restored_and_announced_twice(tmp_path, clock):
    """Off the file the moment it is on the queue — a promise kept twice reads
    as her losing track, not as diligence."""
    first = board(tmp_path, clock)
    first.add(id="t1", label="tea", seconds=60)
    clock.advance(61)
    assert [t.id for t in first.poll()] == ["t1"]

    restarted = board(tmp_path, VirtualClock(start=clock.now()))
    assert restarted.pending() == [] and restarted.poll() == []


def test_a_timer_older_than_the_longest_one_settable_is_dropped(tmp_path, clock):
    """A fortnight of countdowns announced at boot is noise, not delivery."""
    board(tmp_path, clock).add(id="t1", label="ancient", seconds=60)

    stale = VirtualClock(start=clock.now() + STALE_AFTER_S + 120)
    assert board(tmp_path, stale).pending() == []
    # …but one inside the window is still owed
    fresh = VirtualClock(start=clock.now() + STALE_AFTER_S - 120)
    assert [t.id for t in board(tmp_path, fresh).pending()] == ["t1"]


def test_the_board_is_untracked(tmp_path, clock):
    """It changes on every countdown, and what she *said* when one finished is
    already in the journal (world/inbox.py's reasoning, same file)."""
    b = board(tmp_path, clock)
    b.add(id="t1", label="tea", seconds=60)
    assert "timers.json" in (tmp_path / "state" / ".gitignore").read_text()


def test_it_shares_the_ignore_file_with_the_inbox(tmp_path, clock):
    """Both write into `state/.gitignore`; the second must append, not clobber."""
    from yurios.world.inbox import Inbox
    Inbox(tmp_path).add({"id": "i1", "ts": "2026-08-21T04:00:00", "text": "hey"})
    board(tmp_path, clock).add(id="t1", label="tea", seconds=60)
    ignored = (tmp_path / "state" / ".gitignore").read_text()
    assert "inbox.json" in ignored and "timers.json" in ignored


def test_an_unreadable_board_is_an_empty_one(tmp_path, clock):
    """Refusing to start her because a scheduling file was hand-edited would be
    the worse bug."""
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "timers.json").write_text("{not json at all", encoding="utf-8")
    b = board(tmp_path, clock)
    assert b.pending() == []
    b.add(id="t1", label="tea", seconds=60)                  # …and the next add repairs it
    assert [t.id for t in board(tmp_path, clock).pending()] == ["t1"]


def test_no_vault_is_a_working_in_memory_board(clock):
    """The bare-runtime tests and a config with no Vault, exactly as before."""
    b = TimerBoard(clock)
    b.add(id="t1", label="tea", seconds=60)
    assert [t.id for t in b.pending()] == ["t1"] and b.path is None
