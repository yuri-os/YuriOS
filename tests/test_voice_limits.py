from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import time
import types

import numpy as np
import pytest

from yurios.desktop.config import Config as VoiceConfig
from yurios.desktop.voice.backends.fakes import FakeBrain, FakeSTT
from yurios.desktop.voice.ws_limits import (
    MAX_SESSION_ID_BYTES, MAX_TYPED_TEXT_BYTES, TIMEOUT_CLOSE,
    VoiceConnectionLimiter, VoiceSocketClosed, VoiceSocketGuard, bounded_text,
    uvicorn_ws_options,
)


def test_stt_sessions_do_not_share_buffers():
    engine = FakeSTT("heard")
    first = engine.create_session()
    second = engine.create_session()

    first.feed(np.zeros(8, dtype=np.float32), 16000)
    first.feed(np.zeros(8, dtype=np.float32), 16000)
    second.feed(np.zeros(8, dtype=np.float32), 16000)
    second.reset()

    assert first.frames == 2
    assert second.frames == 0
    assert engine.frames == 0


def test_whisper_serializes_consumption_of_lazy_segments(monkeypatch):
    class Segment:
        text = " heard"
        no_speech_prob = 0.0

    class Model:
        def __init__(self, *args, **kwargs):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def transcribe(self, *args, **kwargs):
            def lazy_segments():
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.03)
                    yield Segment()
                finally:
                    with self.lock:
                        self.active -= 1
            return lazy_segments(), object()

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=Model))
    from yurios.desktop.voice.backends.stt_whisper import WhisperSTT

    engine = WhisperSTT()
    sessions = [engine.create_session(), engine.create_session()]
    for session in sessions:
        session.feed(np.zeros(16, dtype=np.float32), 16000)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda session: session.final(), sessions))

    assert results == ["heard", "heard"]
    assert engine._model.max_active == 1


def test_connection_limiter_is_nonblocking_and_balanced():
    limiter = VoiceConnectionLimiter(2)
    assert limiter.try_acquire()
    assert limiter.try_acquire()
    assert not limiter.try_acquire()
    limiter.release()
    assert limiter.try_acquire()
    limiter.release()
    limiter.release()
    limiter.release()
    assert limiter.active == 0


def test_audio_frames_and_utterances_are_bounded_and_resettable():
    cfg = VoiceConfig(_env_file=None, voice_ws_max_frame_bytes=16,
                      voice_ws_max_utterance_s=0.0005)
    guard = VoiceSocketGuard(object(), cfg)

    guard.accept_audio(bytes(16))
    guard.accept_audio(bytes(16))
    with pytest.raises(OverflowError):
        guard.accept_audio(bytes(16))
    guard.reset_utterance()
    guard.accept_audio(bytes(16))
    with pytest.raises(ValueError):
        guard.accept_audio(bytes(3))
    with pytest.raises(ValueError):
        guard.accept_audio(bytes(20))


def test_websocket_text_and_session_fields_are_bounded():
    assert bounded_text("hello", maximum=MAX_TYPED_TEXT_BYTES, field="text") == "hello"
    assert bounded_text(None, maximum=MAX_SESSION_ID_BYTES,
                        field="session_id", optional=True) is None
    with pytest.raises(ValueError):
        bounded_text("x" * (MAX_TYPED_TEXT_BYTES + 1),
                     maximum=MAX_TYPED_TEXT_BYTES, field="text")
    with pytest.raises(ValueError):
        bounded_text("s" * (MAX_SESSION_ID_BYTES + 1),
                     maximum=MAX_SESSION_ID_BYTES, field="session_id")


@pytest.mark.asyncio
async def test_initial_timeout_closes_the_socket():
    class Socket:
        def __init__(self):
            self.closed = None

        async def receive(self):
            await asyncio.sleep(1)

        async def close(self, *, code, reason):
            self.closed = (code, reason)

    socket = Socket()
    cfg = VoiceConfig(_env_file=None, voice_ws_initial_timeout_s=0.01)
    with pytest.raises(VoiceSocketClosed):
        await VoiceSocketGuard(socket, cfg).receive_initial()
    assert socket.closed[0] == TIMEOUT_CLOSE


@pytest.mark.asyncio
async def test_steady_socket_sends_heartbeat_then_accepts_pong():
    released = asyncio.Event()

    class Socket:
        async def receive(self):
            await released.wait()
            return {"type": "websocket.receive", "text": '{"type":"pong"}'}

    async def send(message):
        assert message == {"type": "ping"}
        released.set()
        return True

    cfg = VoiceConfig(_env_file=None, voice_ws_heartbeat_s=0.01,
                      voice_ws_idle_timeout_s=0.1)
    message = await VoiceSocketGuard(Socket(), cfg).receive(send)
    assert "pong" in message["text"]


def test_uvicorn_transport_options_are_bounded():
    cfg = VoiceConfig(_env_file=None, voice_ws_max_message_bytes=12345,
                      voice_ws_max_queue=7, voice_ws_heartbeat_s=3,
                      voice_ws_idle_timeout_s=9)
    options = uvicorn_ws_options(cfg)
    assert options == {
        "ws_max_size": 12345,
        "ws_max_queue": 7,
        "ws_ping_interval": 3.0,
        "ws_ping_timeout": 9.0,
        "ws_per_message_deflate": False,
    }


def test_two_world_sockets_get_independent_stt_sessions(cfg, controller, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient
    from yurios.world import voicestack
    from yurios.world.main import create_app

    class Session:
        def __init__(self):
            self.frames = 0

        def feed(self, frame, sample_rate):
            self.frames += 1

        def final(self):
            return f"heard {self.frames} frames"

        def reset(self):
            self.frames = 0

    class Engine:
        def __init__(self):
            self.sessions = []

        def create_session(self):
            session = Session()
            self.sessions.append(session)
            return session

    engine = Engine()
    monkeypatch.setattr(voicestack, "build_stt", lambda _cfg: (engine, "counting"))
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "vad_confirm": False,
                                 "voice_unload_after_s": -1.0})
    app = create_app(cfg, brain=FakeBrain(), controller=controller)
    frame = np.zeros(512, dtype=np.float32).tobytes()

    def handshake_and_greet(ws, session_id):
        ws.send_json({"type": "hello", "session_id": session_id})
        while ws.receive_json()["type"] != "done":
            pass

    def drain(ws):
        while ws.receive_json()["type"] not in ("done", "error", "cancelled"):
            pass

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice") as first:
            handshake_and_greet(first, "1" * 32)
            with client.websocket_connect("/ws/voice") as second:
                handshake_and_greet(second, "2" * 32)
                first.send_bytes(frame)
                second.send_bytes(frame)
                second.send_bytes(frame)
                first.send_json({"type": "endpoint"})
                second.send_json({"type": "endpoint"})
                drain(first)
                drain(second)

    user_lines = [item["text"] for item in app.state.rt.transcript
                  if item["role"] == "user"]
    assert sorted(user_lines) == ["heard 1 frames", "heard 2 frames"]
    assert len(engine.sessions) == 2


def test_host_connection_cap_is_shared_across_characters(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient
    from yurios.characters import CharacterRegistry
    from yurios.world.config import Config
    from yurios.world.host import create_host_app
    from yurios.world.main import create_app
    from tests.test_host import record

    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    registry.add(record(tmp_path, "mika"))

    def character_app(character_cfg, **kwargs):
        return create_app(character_cfg, brain=FakeBrain(), **kwargs)

    monkeypatch.setattr("yurios.world.host.hosting.create_app", character_app)
    cfg = Config(_env_file=None, data_dir=tmp_path, tools_backend="off",
                 mind_enabled=False, selfie_backend="off", tts_backend="fake",
                 stt_backend="fake", vad_backend="fake", voice_ws_max_connections=1,
                 voice_unload_after_s=-1.0, telegram_bot_token="",
                 telegram_chat_id="")
    app = create_host_app(cfg, registry)

    with TestClient(app) as client:
        host = app.state.host
        assert app.state.http_boundaries_installed is True
        assert not hasattr(host.apps["yuri"].state, "http_boundaries_installed")
        assert host.runtime("yuri").voice_ws_limiter is host.runtime("mika").voice_ws_limiter
        with client.websocket_connect("/ws/characters/yuri/voice") as first:
            first.send_json({"type": "hello", "session_id": "1" * 32})
            while first.receive_json()["type"] != "done":
                pass
            with client.websocket_connect("/ws/characters/mika/voice") as second:
                assert second.receive_json() == {
                    "type": "error", "message": "voice connection limit reached"}
                closed = second.receive()
                assert closed["type"] == "websocket.close"
                assert closed["code"] == 4429
