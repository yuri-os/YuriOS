"""The boot board + /api/boot (SPEC §6.4) — the wake-up log the enter gate polls.

The board is what turns a minute of cold-model loading into visible progress
instead of a blank gate; these pin its state machine and the endpoint shape.
"""
from __future__ import annotations

import pytest

from yurios.world.boot import BootBoard

pytest.importorskip("fastapi")
from starlette.testclient import TestClient           # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.main import create_app                     # noqa: E402


# ---- the board itself ------------------------------------------------------

def test_pending_service_keeps_boot_unfinished():
    b = BootBoard()
    b.declare("tts", "voice · TTS")
    b.declare("selfies", "camera", state="ready", detail="mock")
    snap = b.snapshot()
    assert snap["done"] is False                       # tts still pending
    assert [s["key"] for s in snap["services"]] == ["tts", "selfies"]  # order kept


def test_stage_records_state_and_timing():
    ticks = iter([0.0, 0.0, 2.5, 2.5])                 # t0, start, done, snapshot
    b = BootBoard(clock=lambda: next(ticks))
    b.declare("tts", "voice · TTS")
    b.start("tts", detail="kokoro")
    b.done("tts", detail="kokoro")
    (svc,) = b.snapshot()["services"]
    assert svc["state"] == "ready" and svc["detail"] == "kokoro"
    assert svc["seconds"] == 2.5
    assert "_start" not in svc                          # internals never serialised


def test_done_true_only_when_every_service_is_terminal():
    b = BootBoard()
    b.declare("a", "a"); b.declare("b", "b")
    b.done("a", state="ready")
    assert b.snapshot()["done"] is False
    b.done("b", state="failed")                         # failed still counts as settled
    assert b.snapshot()["done"] is True


def test_unresolved_finds_only_pending_or_loading():
    b = BootBoard()
    b.declare("tts", "t"); b.declare("stt", "s"); b.declare("vad", "v")
    b.start("stt"); b.done("vad", state="ready")
    assert b.unresolved(("tts", "stt", "vad", "missing")) == ["tts", "stt"]


def test_empty_board_is_not_done():
    assert BootBoard().snapshot()["done"] is False


# ---- the endpoint, wired through the real Runtime --------------------------

def test_api_boot_reaches_done_with_every_service_settled(cfg):
    """Fake voice backends warm in a blink; once the thread lands, /api/boot
    reports done and no service is left pending (SPEC §6.4)."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        assert c.app.state.rt.voice_ready.wait(timeout=10)  # warm thread finished
        snap = c.get("/api/boot").json()
        assert snap["done"] is True
        states = {s["key"]: s["state"] for s in snap["services"]}
        assert states["tts"] == states["stt"] == states["vad"] == "ready"
        assert states["tools"] == "skipped"            # backend off
        assert not any(s["state"] in ("pending", "loading")
                       for s in snap["services"])
