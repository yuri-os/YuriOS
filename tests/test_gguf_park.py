"""The in-process half of the VRAM loan (app/providers/gguf.py) — no llama.cpp.

The mechanism under test is a race, so the model build is replaced by a stub
that records who is on the card and, crucially, is *slow* enough to interleave.
What these prove is one sentence: between `park()` and `unpark()`, nothing this
process does may put a llama context back onto the card.
"""
from __future__ import annotations

import threading
import time

import pytest

from yurios.app.providers import gguf


class _Card:
    """The GPU, as far as these tests care: what is resident, and the high-water
    mark of how many contexts were on it at once."""

    def __init__(self) -> None:
        self.resident = 0
        self.peak_during_park = 0
        self.lock = threading.Lock()


@pytest.fixture
def card(monkeypatch, tmp_path):
    """A stubbed llama.cpp: `get_model` builds these instead of real contexts."""
    state = _Card()
    path = tmp_path / "brain.gguf"
    path.write_bytes(b"GGUF" + b"\0" * 4096)

    class _Stub:
        def __init__(self, *_a, **kw):
            self.options = gguf._Options(context_length=8192, gpu_layers=-1,
                                         threads=0, flash_attn=True)
            self.context_length = 8192
            self.llama = self
            self.no_think_handler = None
            self.lock = threading.Lock()
            self.closed = False
            time.sleep(0.05)             # a real load is slow; so is this one
            with state.lock:
                state.resident += 1
                if getattr(gguf, "_parked", False):
                    state.peak_during_park = max(state.peak_during_park,
                                                 state.resident)

        def close(self):
            with state.lock:
                state.resident -= 1

    # The HF round trip the real resolver makes is exactly the window the old
    # race lived in — keep it slow, and keep it off the lock.
    def _resolve(model, cfg):
        time.sleep(0.05)
        return path

    monkeypatch.setattr(gguf, "_LoadedModel", _Stub)
    monkeypatch.setattr(gguf, "resolve_model_file", _resolve)
    monkeypatch.setattr(gguf, "_probe_model",
                        lambda p, requested: requested)
    monkeypatch.setattr(gguf, "_models", {})
    monkeypatch.setattr(gguf, "_registry", {})
    monkeypatch.setattr(gguf, "_probed", {})
    monkeypatch.setattr(gguf, "_parked", False, raising=False)
    gguf._load_gate.set()
    yield state
    gguf._load_gate.set()
    monkeypatch.setattr(gguf, "_parked", False, raising=False)


def _get(cfg):
    return gguf.get_model("gguf/test/Brain", cfg)


def test_park_closes_the_resident_context(cfg, card):
    _get(cfg)
    assert card.resident == 1
    handles = gguf.park()
    assert card.resident == 0
    assert handles == [("gguf/test/Brain", cfg)]
    gguf.unpark(handles)
    assert card.resident == 1


def test_a_load_that_passed_the_gate_cannot_reload_during_the_park(cfg, card):
    """The OOM from the failure log, as a test.

    A mind tick calls `get_model` and is inside the slow HF resolution when the
    render parks. It used to sail on and build a fresh 5.7 GiB context into the
    render's window — "park: lending the GPU (8.9 GiB free)" and, eleven seconds
    later, the render still seeing 8.9 GiB free. It must wait instead.
    """
    _get(cfg)                                   # her brain is on the card
    started = threading.Event()
    done = threading.Event()

    def tick():
        started.set()
        _get(cfg)                               # the load that used to win
        done.set()

    caller = threading.Thread(target=tick, daemon=True)
    caller.start()
    started.wait(2)
    handles = gguf.park()                       # the render takes the card

    # The render's window: nothing may come back onto the card during it.
    assert card.resident == 0
    time.sleep(0.4)                             # longer than a resolve + build
    assert card.resident == 0, "her brain came back mid-render"
    assert not done.is_set(), "the queued load did not wait for the render"

    gguf.unpark(handles)                        # the render gives it back
    assert done.wait(5), "the queued load never woke up"
    caller.join(5)
    assert card.resident == 1                   # one brain, not two
    assert card.peak_during_park == 0


def test_the_gate_never_wedges_a_caller_forever(cfg, card, monkeypatch):
    """A park that never ends must not leave her permanently mute: the load goes
    through late and says so, the same trade world/vram.py's ParkGate makes."""
    monkeypatch.setattr(gguf, "_PARK_WAIT_SECONDS", 0.2, raising=False)
    gguf.park()                                 # ...and never unpark
    assert _get(cfg) is not None
    assert card.resident == 1
