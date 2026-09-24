"""The signal bus holds what is unread, not what happened (SPEC §16.4)."""
from __future__ import annotations

from yurios.kernel.clock import VirtualClock
from yurios.mind import signals
from yurios.mind.signals import SignalBus


def _bus() -> SignalBus:
    return SignalBus(VirtualClock(start=0.0))


def test_reading_releases_what_was_read_and_keeps_offsets_absolute():
    bus = _bus()
    for i in range(5):
        bus.post("timer", {"i": i})
    batch, offset = bus.next(0, limit=3)
    assert [s.payload["i"] for s in batch] == [0, 1, 2] and offset == 3
    batch, offset = bus.next(offset)
    assert [s.payload["i"] for s in batch] == [3, 4] and offset == 5
    bus.post("timer", {"i": 5})
    batch, offset = bus.next(offset)
    assert [s.payload["i"] for s in batch] == [5] and offset == 6
    bus.next(offset)
    # nothing read is still held, and the end offset still counts it all
    assert bus._signals == [] and len(bus) == 6


def test_a_tick_that_failed_asks_again_and_loses_nothing():
    bus = _bus()
    bus.post("user_message", {"text": "hi"})
    first, _ = bus.next(0)                  # the tick died before committing
    again, offset = bus.next(0)
    assert again == first and offset == 1


def test_a_mind_rebuilt_on_the_live_bus_resumes_at_its_cursor():
    bus = _bus()
    for i in range(4):
        bus.post("timer", {"i": i})
    _, offset = bus.next(0, limit=2)
    bus.next(offset)                        # the old mind's last tick
    # the rebuilt mind restores the same persisted cursor
    batch, _ = bus.next(offset)
    assert [s.payload["i"] for s in batch] == [2, 3]


def test_an_undrained_bus_is_bounded_and_a_late_reader_resumes_at_the_oldest(
        monkeypatch):
    monkeypatch.setattr(signals, "MAX_HELD", 3)
    bus = _bus()
    for i in range(10):
        bus.post("timer", {"i": i})
    assert len(bus._signals) == 3 and len(bus) == 10
    batch, offset = bus.next(0)
    assert [s.payload["i"] for s in batch] == [7, 8, 9] and offset == 10


def test_an_offset_from_another_bus_reads_everything_held():
    bus = _bus()
    bus.post("user_message", {"text": "after a restart"})
    batch, offset = bus.next(40)
    assert [s.type for s in batch] == ["user_message"] and offset == 1
