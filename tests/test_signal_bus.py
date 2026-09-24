"""The signal bus holds what is unacknowledged, not what happened (SPEC §16.4)."""
from __future__ import annotations

from yurios.kernel.clock import VirtualClock
from yurios.mind import signals
from yurios.mind.signals import SignalBus


def _bus() -> SignalBus:
    return SignalBus(VirtualClock(start=0.0))


def test_reading_releases_nothing_until_it_is_acked():
    bus = _bus()
    for i in range(5):
        bus.post("timer", {"i": i})
    batch, offset = bus.next(0, limit=3)
    assert [s.payload["i"] for s in batch] == [0, 1, 2] and offset == 3
    assert len(bus._signals) == 5            # a read is only a look
    bus.ack(offset)
    assert [s.payload["i"] for s in bus._signals] == [3, 4]
    batch, offset = bus.next(offset)
    assert [s.payload["i"] for s in batch] == [3, 4] and offset == 5
    bus.ack(offset)
    # nothing acked is still held, and the end offset still counts it all
    assert bus._signals == [] and len(bus) == 5
    bus.post("timer", {"i": 5})
    batch, offset = bus.next(offset)
    assert [s.payload["i"] for s in batch] == [5] and offset == 6


def test_a_tick_that_died_before_its_ack_reads_the_batch_again():
    bus = _bus()
    bus.post("user_message", {"text": "hi"})
    first, _ = bus.next(0)                   # the tick died before committing
    again, offset = bus.next(0)
    assert again == first and offset == 1


def test_a_second_look_further_on_does_not_cost_the_reader_anything():
    bus = _bus()
    for i in range(4):
        bus.post("timer", {"i": i})
    bus.next(3)                              # someone else peeking ahead
    batch, _ = bus.next(0)
    assert [s.payload["i"] for s in batch] == [0, 1, 2, 3]


def test_an_ack_behind_the_window_or_past_the_end_is_harmless():
    bus = _bus()
    for i in range(3):
        bus.post("timer", {"i": i})
    bus.ack(2)
    bus.ack(1)                               # behind: nothing more to drop
    assert [s.payload["i"] for s in bus._signals] == [2]
    bus.ack(99)                              # past the end: drops what is held
    assert bus._signals == [] and len(bus) == 3
    bus.post("timer", {"i": 3})
    batch, offset = bus.next(3)
    assert [s.payload["i"] for s in batch] == [3] and offset == 4


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
