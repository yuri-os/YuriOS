"""The /ws/voice handshake, asserted against BOTH bodies (SPEC §9, §10).

There are two voice routes — the browser's and the native window's — and they
are different on purpose. The wire underneath them is not: the connection cap,
the "say hello first" rule, the size ceilings on the first frame, and the rule
that a session id is something the client *asks* for and the brain grants.

Those live in `desktop/voice/ws_session.py` now, and every test here runs
twice, once against each app. A fork that drifts on any of them fails on the
side that drifted, which is exactly what the previous arrangement — the same
handshake typed out in two files — could not do.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                          # noqa: E402
from starlette.websockets import WebSocketDisconnect                 # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain           # noqa: E402
from yurios.desktop.voice.ws_limits import (                        # noqa: E402
    CAPACITY_CLOSE, LIMIT_CLOSE, MAX_SESSION_ID_BYTES,
)

GRANTED = "a-session-the-server-chose"


class RecordingBrain(FakeBrain):
    """A brain that records what was asked for and hands back its own id.

    `resolve_session` is the seam that stops a client naming somebody else's
    conversation, so a fake that echoes the request (FakeBrain's default) can't
    tell whether the route consulted it at all.
    """

    def __init__(self):
        super().__init__()
        self.requested: list[str | None] = []

    def resolve_session(self, session_id: str | None) -> str:
        self.requested.append(session_id)
        return GRANTED


def _make(kind: str, cfg, controller, brain):
    if kind == "world":
        from yurios.world.main import create_app
        return create_app(
            cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False}),
            brain=brain, controller=controller)
    from yurios.desktop.main import create_app
    return create_app(cfg, brain=brain)


@pytest.fixture(params=["world", "desktop"])
def rig(request, cfg, controller):
    brain = RecordingBrain()
    with TestClient(_make(request.param, cfg, controller, brain)) as client:
        yield client, brain


def session_frame(ws) -> dict:
    """The `session` frame, skipping the browser's cold-voice notice (§9.9)."""
    for _ in range(4):
        m = ws.receive_json()
        if m["type"] == "session":
            return m
        assert m["type"] in ("warming", "ready"), f"unexpected {m['type']}"
    raise AssertionError("no session frame")


def expect_rejected(ws, code: int = LIMIT_CLOSE) -> str:
    """Drain to the guard's error frame, then assert the close code it used.

    Draining matters: she greets on arrival, so on the paths that reject *after*
    the handshake there is a turn already streaming down the same wire.
    """
    message = None
    for _ in range(80):
        try:
            frame = ws.receive_json()
        except WebSocketDisconnect as exc:                  # closed without a word
            assert exc.code == code
            return message or ""
        if frame["type"] == "error":
            message = frame["message"]
            break
    else:
        raise AssertionError("no error frame")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        ws.receive_json()
    assert exc_info.value.code == code
    return message


def test_the_first_frame_must_be_a_hello(rig):
    """No hello, no room — and the brain is never even asked for a session."""
    client, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "text", "text": "let me in"})
        assert expect_rejected(ws) == "voice hello required"
    assert brain.requested == []


def test_the_session_id_is_the_brains_to_give_not_the_clients(rig):
    """The client asks; `resolve_session` decides. That is the whole binding."""
    client, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": "someone-elses-session"})
        assert session_frame(ws)["session_id"] == GRANTED
    assert brain.requested == ["someone-elses-session"]


def test_an_oversized_session_id_never_reaches_the_brain(rig):
    client, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello",
                      "session_id": "x" * (MAX_SESSION_ID_BYTES + 1)})
        assert "session_id" in expect_rejected(ws)
    assert brain.requested == []


def test_an_oversized_hello_frame_is_refused_before_it_is_parsed(rig, cfg):
    """The size check runs on the raw text, ahead of `json.loads` — a frame that
    breaks the ceiling costs a length, not a parse."""
    client, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello",
                      "session_id": "x" * (cfg.voice_ws_max_message_bytes + 64)})
        assert expect_rejected(ws) == "voice text frame is too large"
    assert brain.requested == []


def test_a_misaligned_audio_frame_is_refused(rig):
    """Float32 or nothing: a length that isn't a multiple of 4 is not audio, and
    is thrown out before a single sample is copied."""
    client, _brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        session_frame(ws)
        ws.send_bytes(b"\x00" * 7)
        assert expect_rejected(ws) == "invalid float32 audio frame"


@pytest.mark.parametrize("kind", ["world", "desktop"])
def test_the_connection_cap_refuses_the_next_socket(kind, cfg, controller):
    """The slot is taken before the socket is even accepted — and the refusal is
    a completed handshake with a close code, so a browser can caption it."""
    capped = cfg.model_copy(update={"voice_ws_max_connections": 1})
    brain = RecordingBrain()
    with TestClient(_make(kind, capped, controller, brain)) as client:
        with client.websocket_connect("/ws/voice") as first:
            first.send_json({"type": "hello", "session_id": None})
            session_frame(first)
            with client.websocket_connect("/ws/voice") as second:
                assert expect_rejected(second, CAPACITY_CLOSE) == \
                    "voice connection limit reached"


def test_the_slot_is_given_back_when_the_socket_closes(rig):
    """Every exit releases the count — otherwise a cap of eight becomes a cap of
    zero after eight reloads."""
    import time
    client, _brain = rig
    limiter = client.app.state.rt.voice_ws_limiter
    for _ in range(3):
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            session_frame(ws)
        for _ in range(100):                # the release runs on the server's loop
            if limiter.active == 0:
                break
            time.sleep(0.02)
        assert limiter.active == 0
