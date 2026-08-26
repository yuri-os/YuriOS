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
    p.gate.close(p)                              # as the lab shuts it: on her behalf
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


# ---- one card, four characters ----------------------------------------------
# A host runs every character in one process, on one GPU, against one LM Studio
# server. The gate has to be process-wide or it guards only the character whose
# own camera is rendering — which is how a night lost two of four dreamt
# selfies with every park in the log reporting success.

def test_every_runtime_gets_the_same_door(cfg):
    from yurios.world.vram import shared_gate
    assert shared_gate() is shared_gate()
    assert make_parker(cfg, 5.0).gate is not shared_gate()   # standalone keeps its own


def test_one_characters_park_holds_anothers_caller_at_the_door(cfg, spied,
                                                                monkeypatch):
    """Adia's render, Yuri's dream call: the door is the same door."""
    from yurios.world.vram import shared_gate

    async def scenario():
        gate = shared_gate()
        gate.bind(asyncio.get_running_loop())
        adia = make_parker(cfg, 5.0, selfie_backend="diffusers")
        adia.gate = gate
        monkeypatch.setattr(adia, "_await_free", lambda before: None)
        with adia.parked():                       # adia has the card
            yuri_call = asyncio.create_task(gate.wait(timeout_s=5))
            await asyncio.sleep(0)
            held = not yuri_call.done()           # yuri's dream call queues
        return held, await asyncio.wait_for(yuri_call, 1)

    assert asyncio.run(scenario()) == (True, True)


def test_a_second_camera_deciding_not_to_park_leaves_the_first_door_shut(
        cfg, spied, monkeypatch):
    """The bug an unowned door would have: B opens the window A is standing in."""
    from yurios.world.vram import shared_gate

    gate = shared_gate()
    adia = make_parker(cfg, 5.0)                  # short card → parks
    iris = make_parker(cfg, 14.0)                 # roomy card → does not
    adia.gate = iris.gate = gate
    monkeypatch.setattr(adia, "_await_free", lambda before: None)
    with adia.parked() as borrowed:
        assert borrowed is True
        with iris.parked() as also:
            assert also is False                  # nothing to park for
        assert gate._open.is_set() is False       # adia's window is still hers
    assert gate._open.is_set() is True


def test_two_cameras_do_not_render_on_one_card_at_once(cfg, spied, monkeypatch):
    """The park lock is the card's, not the parker's."""
    import threading
    from yurios.world.vram import shared_gate

    adia, iris = make_parker(cfg, 5.0), make_parker(cfg, 5.0)
    adia.gate = iris.gate = shared_gate()
    for p in (adia, iris):
        monkeypatch.setattr(p, "_await_free", lambda before: None)
    inside = threading.Event()
    overlapped = []

    def second():
        with iris.parked():
            overlapped.append(inside.is_set())

    with adia.parked():
        inside.set()
        t = threading.Thread(target=second)
        t.start()
        t.join(0.3)                               # blocked on the card, not done
        assert t.is_alive()
        inside.clear()
    t.join(2)
    assert overlapped == [False]                  # it only got in after adia left


# ---- the other half of the door: callers that are not turns ------------------
# The mind loop reaches her brain off-turn (dream jobs, consolidation), at the
# hour the camera is busiest. Both directions of the gate have to know about it.

def test_a_patient_caller_waits_longer_than_a_turn_would():
    """A dream job outlasts the conversational cap: nobody is watching it."""
    async def scenario():
        gate = ParkGate(timeout_s=0.05)          # what a turn would settle for
        gate.bind(asyncio.get_running_loop())
        gate.close()
        waiter = asyncio.create_task(gate.wait(timeout_s=5))
        await asyncio.sleep(0.1)                 # a turn would have given up here
        assert not waiter.done()
        gate.open()
        return await asyncio.wait_for(waiter, 1)

    assert asyncio.run(scenario()) is True


def test_a_park_waits_for_an_off_turn_model_call_to_finish():
    """The mirror of wait_turns_idle: don't evict under a live utility call."""
    async def scenario():
        gate = ParkGate()
        gate.bind(asyncio.get_running_loop())
        async with gate.hold():
            quiet = asyncio.create_task(gate.wait_idle(timeout_s=5))
            await asyncio.sleep(0)
            assert not quiet.done()              # the call is still running
        return await asyncio.wait_for(quiet, 1)

    assert asyncio.run(scenario()) is True


def test_nested_off_turn_calls_only_go_quiet_when_the_last_one_ends():
    async def scenario():
        gate = ParkGate()
        gate.bind(asyncio.get_running_loop())
        outer = gate.hold()
        inner = gate.hold()
        await outer.__aenter__()
        await inner.__aenter__()
        await inner.__aexit__(None, None, None)
        quiet = asyncio.create_task(gate.wait_idle(timeout_s=5))
        await asyncio.sleep(0)
        assert not quiet.done()                  # one is still in flight
        await outer.__aexit__(None, None, None)
        return await asyncio.wait_for(quiet, 1)

    assert asyncio.run(scenario()) is True


def test_a_wedged_off_turn_call_does_not_mean_no_selfie_ever_renders():
    async def scenario():
        gate = ParkGate()
        gate.bind(asyncio.get_running_loop())
        async with gate.hold():                  # never finishes in time
            return await gate.wait_idle(timeout_s=0.05)

    assert asyncio.run(scenario()) is False      # parks under it, and says so


def test_an_idle_gate_never_makes_a_park_wait():
    async def scenario():
        gate = ParkGate()
        gate.bind(asyncio.get_running_loop())
        return await asyncio.wait_for(gate.wait_idle(timeout_s=5), 1)

    assert asyncio.run(scenario()) is True


# ---- the warm pipeline: whose card is it between renders? --------------------
# The bug this section exists for is quiet, because the render that causes it
# is never the render that fails. See LLMParker.can_keep_pipeline_warm.

def test_a_warm_pipeline_may_stay_when_her_brain_still_fits_beside_it(cfg):
    """Free VRAM measured with the pipeline loaded: room for her brain to come
    home means the 25 seconds of warmth are free."""
    assert make_parker(cfg, 9.0).can_keep_pipeline_warm() is True


def test_a_warm_pipeline_is_dropped_when_it_would_fill_the_card(cfg):
    """The failure from the log: the pipeline stays warm, her brain reloads
    beside it, and the NEXT render parks into a card that is already full."""
    assert make_parker(cfg, 1.4).can_keep_pipeline_warm() is False


def test_the_headroom_follows_the_configured_brain(cfg):
    p = make_parker(cfg, 6.5, selfie_warm_headroom_gib=8.0)
    assert p.brain_headroom == 8.0
    assert p.can_keep_pipeline_warm() is False   # 6.5 < 8.0


def test_no_local_brain_leaves_the_pipeline_warm(cfg):
    """A hosted or mock camera has no brain on this card to make room for, so
    this measurement has nothing to say. It is NOT "nothing competes for the
    card" — another character's camera does, and `claim_card` is what settles
    that. Reading it the other way is what cost the night of 2026-08-26."""
    p = make_parker(cfg, 0.2, selfie_backend="mock")
    assert p.applicable() is False
    assert p.can_keep_pipeline_warm() is True


def test_an_unmeasurable_card_keeps_the_pipeline_warm(cfg):
    """No torch, no CUDA: don't take the speed away over a number we can't read."""
    p = LLMParker(cfg.model_copy(update={"selfie_backend": "diffusers"}),
                  free_probe=lambda: None, resident_free_gib=_RESIDENT_FLOOR_GIB)
    assert p.can_keep_pipeline_warm() is True


# ---- the wait: one flat reading is a pause, not the end of an unload --------

def test_await_free_does_not_mistake_a_pause_for_a_finished_unload(cfg, monkeypatch):
    """llama.cpp releases in stages. The old single-poll exit returned after
    ~1 s with the card still full, silently — which is why a failed park and a
    park that never ran looked identical in the log."""
    readings = [5.4, 5.4, 8.9, 8.9, 8.9, _RESIDENT_FLOOR_GIB + 1.0]
    probe = iter(readings)
    p = LLMParker(cfg.model_copy(update={"selfie_backend": "diffusers"}),
                  free_probe=lambda: next(probe, 99.0),
                  resident_free_gib=_RESIDENT_FLOOR_GIB)
    monkeypatch.setattr("yurios.world.vram.time.sleep", lambda s: None)
    assert p._await_free(before=5.2) is True     # waited through both plateaus


def test_await_free_reports_that_it_gave_up_short(cfg, monkeypatch, caplog):
    p = make_parker(cfg, 8.8)                    # never reaches the floor
    monkeypatch.setattr("yurios.world.vram.time.sleep", lambda s: None)
    with caplog.at_level("WARNING"):
        assert p._await_free(before=8.6) is False
    assert "below the" in caplog.text and "floor" in caplog.text


# ---- the card's one warm pipeline, across characters ------------------------

def test_two_cameras_cannot_both_hold_the_card():
    """The night this exists for: an OpenRouter brain means no local model to
    park, so `can_keep_pipeline_warm` says yes to everyone and every
    character's camera keeps its own copy of the same checkpoint resident.
    Yuri's sat warm for nine hours; YuriQuant's load OOM'd behind it."""
    from yurios.world.vram import claim_card

    handed_back: list[str] = []
    first, second = object(), object()

    claim_card(first, lambda: handed_back.append("first"))
    assert handed_back == []                    # nobody had it
    claim_card(second, lambda: handed_back.append("second"))
    assert handed_back == ["first"]             # …and now it's been handed over


def test_re_claiming_the_card_you_hold_tears_nothing_down():
    """The warm case, and the whole point of keying on the backend rather than
    the camera: two characters sharing one pipeline share one claim, and a
    second render on it must not pay a reload to take what it already has."""
    from yurios.world.vram import claim_card

    handed_back: list[str] = []
    holder = object()
    claim_card(holder, lambda: handed_back.append("holder"))
    claim_card(holder, lambda: handed_back.append("holder"))
    assert handed_back == []


def test_a_neighbours_failed_teardown_does_not_stop_this_render(caplog):
    """A render that refuses to start because someone *else's* cleanup raised
    is worse than one that tries: the worst case is the OOM we already had."""
    from yurios.world.vram import claim_card

    def boom() -> None:
        raise RuntimeError("cuda is having a day")

    claim_card(object(), boom)
    with caplog.at_level("ERROR"):
        claim_card(object(), lambda: None)      # must not raise
    assert "couldn't release" in caplog.text


def test_releasing_the_card_leaves_nothing_to_hand_back(caplog):
    from yurios.world.vram import claim_card, release_card

    handed_back: list[str] = []
    holder = object()
    claim_card(holder, lambda: handed_back.append("holder"))
    release_card(holder)
    with caplog.at_level("INFO"):
        claim_card(object(), lambda: None)
    assert handed_back == []
    assert "still resident" not in caplog.text


def test_the_render_lock_belongs_to_the_card():
    from yurios.world.vram import shared_render_lock

    assert shared_render_lock() is shared_render_lock()
