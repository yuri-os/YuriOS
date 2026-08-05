"""Parking the LLM for a local render (world/vram.py) — the VRAM lender.

Offline, no torch, no LM Studio: the free-VRAM probe is injected, and the two
provider calls (evict / ensure_resident) are spied at their module.
"""
from __future__ import annotations

import asyncio

import pytest

from yurios.app.providers import gguf, lmstudio
from yurios.forge.backends.diffusers import DiffusersBackend
from yurios.forge.backends.krea2 import Krea2Backend
from yurios.world.vram import LLMParker, ParkGate, _DEFAULT_FLOOR_GIB

_RESIDENT_FLOOR_GIB = _DEFAULT_FLOOR_GIB

# What the runtime passes in: the floor of the backend it actually built. A
# hosted or mock camera keeps nothing on the card and so declares None.
_FLOORS = {"diffusers": DiffusersBackend.RESIDENT_FREE_GIB,
           "krea2": Krea2Backend.RESIDENT_FREE_GIB}


def make_parker(cfg, free, **over):
    # Fresh YuriOS installs intentionally have no LLM. These parking tests need
    # the explicit LM Studio configuration whose residency they exercise.
    cfg = cfg.model_copy(update={"chat_model": "lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
                                 "utility_model": "lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
                                 "selfie_backend": "diffusers", **over})
    return LLMParker(cfg, free_probe=lambda: free,
                     resident_free_gib=_FLOORS.get(cfg.selfie_backend))


# ---- applicability: is there anything to park, for a backend that benefits? ----

def test_applicable_for_a_local_camera_on_an_lmstudio_brain(cfg):
    assert make_parker(cfg, 5.0).applicable() is True


def test_not_applicable_to_hosted_or_mock_cameras(cfg):
    assert make_parker(cfg, 5.0, selfie_backend="openrouter").applicable() is False
    assert make_parker(cfg, 5.0, selfie_backend="mock").applicable() is False


def test_not_applicable_when_the_user_disabled_parking(cfg):
    assert make_parker(cfg, 5.0, selfie_llm_park=False).applicable() is False


def test_not_applicable_when_her_brain_is_elsewhere(cfg):
    p = make_parker(cfg, 5.0, chat_model="ollama/gemma", utility_model="ollama/gemma",
                    embed_backend="ollama")
    assert p._ids() == []
    assert p.applicable() is False


# ---- the direct-GGUF brain: llama.cpp contexts in this process ----

def make_gguf_parker(cfg, free, resident=1, **over):
    # A gguf/ chat route with `resident` llama contexts already in process.
    cfg = cfg.model_copy(update={"chat_model": "gguf/HauhauCS/Gemma-4-E4B-GGUF",
                                 "utility_model": "gguf/HauhauCS/Gemma-4-E4B-GGUF",
                                 "selfie_backend": "diffusers", **over})
    return LLMParker(cfg, free_probe=lambda: free,
                     resident_free_gib=_FLOORS.get(cfg.selfie_backend))


@pytest.fixture
def gguf_spied(monkeypatch):
    from types import SimpleNamespace

    state = SimpleNamespace(calls=[], resident=1)
    monkeypatch.setattr(gguf, "resident_count", lambda: state.resident)
    monkeypatch.setattr(gguf, "park",
                        lambda: state.calls.append(("gguf-park",)) or [("m", "cfg")])
    monkeypatch.setattr(gguf, "unpark",
                        lambda handles: state.calls.append(
                            ("gguf-unpark", tuple(handles))))
    return state


def test_applicable_for_a_local_camera_on_a_gguf_brain(cfg, gguf_spied):
    assert make_gguf_parker(cfg, 5.0).applicable() is True


def test_not_applicable_when_no_gguf_context_is_resident(cfg, gguf_spied):
    gguf_spied.resident = 0                     # nothing loaded yet, nothing to park
    p = make_gguf_parker(cfg, 5.0)
    assert p._ids() == []
    assert p.applicable() is False


def test_a_short_card_parks_the_gguf_brain_and_restores_it(cfg, gguf_spied,
                                                           monkeypatch):
    p = make_gguf_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)

    with p.parked() as borrowed:
        gguf_spied.calls.append(("render",))
    assert borrowed is True
    assert gguf_spied.calls == [("gguf-park",), ("render",),
                                ("gguf-unpark", (("m", "cfg"),))]


def test_a_failed_render_still_restores_the_gguf_brain(cfg, gguf_spied, monkeypatch):
    p = make_gguf_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)

    with pytest.raises(RuntimeError, match="render exploded"):
        with p.parked():
            raise RuntimeError("render exploded")
    assert gguf_spied.calls == [("gguf-park",), ("gguf-unpark", (("m", "cfg"),))]
    assert p.gate._open.is_set() is True


def test_an_lmstudio_and_gguf_brain_are_parked_together(cfg, spied, gguf_spied,
                                                        monkeypatch):
    # lm_studio/ ids that landed in-process via the LM-Studio-down fallback:
    # both loans go out, both come back.
    p = make_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)

    with p.parked():
        spied.append(("render",))
    kinds = [c[0] for c in spied] + [c[0] for c in gguf_spied.calls]
    assert kinds == ["evict", "render", "restore", "gguf-park", "gguf-unpark"]


# ---- the decision: park only when it buys a resident render ----

def test_needs_park_only_below_the_resident_floor(cfg):
    assert make_parker(cfg, _RESIDENT_FLOOR_GIB - 1).needs_park() is True
    assert make_parker(cfg, _RESIDENT_FLOOR_GIB + 1).needs_park() is False
    assert make_parker(cfg, None).needs_park() is False   # no torch → don't touch


def test_the_floor_is_the_backends_own_not_a_constant(cfg):
    # Krea 2 keeps more on the card than SDXL, so the same free-VRAM reading
    # decides differently for the two — which is the point of taking the floor
    # from the backend that was actually built.
    between = (DiffusersBackend.RESIDENT_FREE_GIB
               + Krea2Backend.RESIDENT_FREE_GIB) / 2
    assert make_parker(cfg, between, selfie_backend="diffusers").needs_park() is False
    assert make_parker(cfg, between, selfie_backend="krea2").needs_park() is True


def test_a_backend_that_keeps_nothing_resident_never_parks(cfg):
    # floor None (mock/openrouter/degraded-to-mock) → the parker stays out of
    # the way entirely, however little VRAM is free.
    p = LLMParker(cfg.model_copy(update={"selfie_backend": "diffusers"}),
                  free_probe=lambda: 0.1, resident_free_gib=None)
    assert p.applicable() is False
    assert p.needs_park() is False


# ---- the loan itself ----

@pytest.fixture
def spied(monkeypatch):
    calls = []
    monkeypatch.setattr(lmstudio, "evict",
                        lambda base, ids, **kw: calls.append(("evict", base, tuple(ids))))
    monkeypatch.setattr(lmstudio, "ensure_resident",
                        lambda base, ids, **kw: calls.append(("restore", base, tuple(ids))))
    return calls


def test_a_short_card_parks_renders_and_restores(cfg, spied, monkeypatch):
    p = make_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)   # don't sleep in tests

    with p.parked() as borrowed:
        spied.append(("render",))
    assert borrowed is True                       # the caller must release the pipe

    kinds = [c[0] for c in spied]
    assert kinds == ["evict", "render", "restore"]
    assert spied[0][1] == cfg.lmstudio_base_url
    assert any("Gemma-4-E4B-Uncensored" in m for m in spied[0][2])


def test_a_failed_render_still_restores_her_brain(cfg, spied, monkeypatch):
    p = make_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)

    with pytest.raises(RuntimeError, match="render exploded"):
        with p.parked():
            raise RuntimeError("render exploded")
    assert [c[0] for c in spied] == ["evict", "restore"]


def test_an_empty_card_parks_nothing(cfg, spied):
    with make_parker(cfg, 14.0).parked() as borrowed:   # plenty of room
        spied.append(("render",))
    assert borrowed is False
    assert [c[0] for c in spied] == ["render"]


def test_an_unmeasurable_card_parks_nothing(cfg, spied):
    with make_parker(cfg, None).parked() as borrowed:   # no torch/CUDA to ask
        spied.append(("render",))
    assert borrowed is False
    assert [c[0] for c in spied] == ["render"]


def test_nothing_is_parked_for_a_non_local_backend(cfg, spied):
    with make_parker(cfg, 1.0, selfie_backend="mock").parked():
        spied.append(("render",))
    assert [c[0] for c in spied] == ["render"]


# ---- the wait-for-free poll ----

def test_await_free_stops_once_the_floor_is_crossed(cfg, monkeypatch):
    probe = iter([5.0, 8.0, _RESIDENT_FLOOR_GIB + 1.0])
    p = LLMParker(cfg.model_copy(update={"selfie_backend": "diffusers"}),
                  free_probe=lambda: next(probe, 99.0),
                  resident_free_gib=_RESIDENT_FLOOR_GIB)
    monkeypatch.setattr("yurios.world.vram.time.sleep", lambda s: None)
    p._await_free(before=5.0)                    # must return promptly, not burn budget


def test_await_free_gives_up_within_budget(cfg, monkeypatch):
    p = make_parker(cfg, 5.0)                    # VRAM never comes back
    monkeypatch.setattr("yurios.world.vram.time.sleep", lambda s: None)
    p._await_free(before=5.0)                    # returns anyway — the render goes ahead


# ---- the gate: a turn that arrives mid-park waits instead of reloading ----

def test_the_gate_starts_open_so_an_ordinary_turn_never_waits(cfg):
    gate = ParkGate()
    assert asyncio.run(gate.wait()) is True


def test_a_park_shuts_the_gate_and_reopens_it(cfg, spied, monkeypatch):
    p = make_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)
    seen = []

    with p.parked():
        seen.append(p.gate._open.is_set())        # shut for the render's duration
    seen.append(p.gate._open.is_set())
    assert seen == [False, True]


def test_a_failed_render_reopens_the_gate(cfg, spied, monkeypatch):
    """The leak this guards against is silence: a gate stuck shut is a
    companion who stops answering, which is worse than the OOM it prevents."""
    p = make_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)

    with pytest.raises(RuntimeError):
        with p.parked():
            raise RuntimeError("render exploded")
    assert p.gate._open.is_set() is True


def test_a_failed_restore_still_reopens_the_gate(cfg, monkeypatch):
    """LM Studio will JIT-load her on the next turn — that fallback is what the
    gate postpones, not what it forbids."""
    p = make_parker(cfg, 5.0)
    monkeypatch.setattr(p, "_await_free", lambda before: None)
    monkeypatch.setattr(lmstudio, "evict", lambda base, ids, **kw: None)
    monkeypatch.setattr(lmstudio, "ensure_resident",
                        lambda base, ids, **kw: (_ for _ in ()).throw(
                            RuntimeError("LM Studio is gone")))

    with pytest.raises(RuntimeError, match="LM Studio is gone"):
        with p.parked():
            pass
    assert p.gate._open.is_set() is True


def test_a_render_that_decides_not_to_park_reopens_a_pre_shut_gate(cfg, spied):
    """The lab shuts the gate before waiting for a quiet moment, and the card
    can free up in between — that decision must not strand queued turns."""
    p = make_parker(cfg, 14.0)                   # plenty of room by now
    p.gate.close()
    with p.parked() as borrowed:
        pass
    assert borrowed is False
    assert p.gate._open.is_set() is True


def test_a_turn_waits_at_a_shut_gate_and_proceeds_when_it_opens():
    gate = ParkGate()

    async def scenario():
        gate.bind(asyncio.get_running_loop())
        gate.close()
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0)                   # let it reach the gate
        assert not waiter.done()                 # held, not let through
        gate.open()
        return await asyncio.wait_for(waiter, 1)

    assert asyncio.run(scenario()) is True


def test_a_wedged_gate_lets_the_turn_through_rather_than_going_mute():
    async def scenario():
        gate = ParkGate(timeout_s=0.05)          # nobody is coming to open it
        gate.bind(asyncio.get_running_loop())
        gate.close()
        return await gate.wait()

    assert asyncio.run(scenario()) is False      # failed open, on purpose


def test_the_gate_can_be_shut_from_the_render_worker_thread():
    """The parker runs under asyncio.to_thread; the flag has to cross back."""
    async def scenario():
        gate = ParkGate(timeout_s=0.05)
        gate.bind(asyncio.get_running_loop())
        await asyncio.to_thread(gate.close)
        blocked = await gate.wait()              # times out → the close landed
        await asyncio.to_thread(gate.open)
        await asyncio.sleep(0)                   # let the threadsafe callback run
        return blocked, await gate.wait()

    assert asyncio.run(scenario()) == (False, True)
