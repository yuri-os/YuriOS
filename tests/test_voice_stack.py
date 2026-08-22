"""Her voice, loaded only while somebody is in the room (SPEC §9.9).

A node with a registry starts every autostart character at boot (world/host.py),
and each runtime used to warm its own Kokoro/whisper/silero on the way up — a
dashboard of six characters paid for six voice stacks nobody was listening to.
These pin the seam that fixed it: the stack is cold until a `/ws/voice` client
arrives, it is held up by a count of listeners, and the last one out frees it.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from yurios.world import voicestack
from yurios.world.boot import BootBoard
from yurios.world.voicestack import UNLOADED, VoiceStack

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                       # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain         # noqa: E402
from yurios.world.main import create_app                          # noqa: E402


async def settle(stack: VoiceStack) -> None:
    """Let a scheduled unload actually happen. `release` only *schedules* it —
    the freeing itself hops a worker thread so a gc pause never lands on the
    event loop — so a test that wants the after-state has to wait for the task."""
    task = stack._unload_task
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def stack(cfg):
    """A stack over the fake backends: warms instantly, frees like the real one."""
    return VoiceStack(cfg.model_copy(update={"voice_unload_after_s": 0.0}),
                      BootBoard())


# ---- the stack itself ------------------------------------------------------

def test_cold_until_someone_arrives(stack):
    assert stack.loaded is False and stack.listeners == 0
    assert stack.tts is stack.stt is stack.vad is None
    assert stack.tts_name == stack.stt_name == stack.vad_name == UNLOADED
    assert stack.status()["ready"] is False


def test_first_listener_loads_it_and_the_last_one_out_frees_it(stack):
    async def scenario():
        await stack.acquire()
        assert stack.loaded and stack.listeners == 1
        assert stack.tts is not None and stack.tts_name == "fake"
        stack.release()
        await settle(stack)

    asyncio.run(scenario())
    assert stack.loaded is False and stack.listeners == 0
    assert stack.tts is None and stack.tts_name == UNLOADED
    assert stack.loads == 1                    # warmed exactly once


def test_a_second_listener_keeps_it_up(stack):
    async def scenario():
        await stack.acquire()
        await stack.acquire()                  # the second room, same runtime
        assert stack.loads == 1                # joined the warm one, didn't re-warm
        stack.release()
        await settle(stack)
        assert stack.loaded is True            # one client still listening
        assert stack.listeners == 1
        stack.release()
        await settle(stack)

    asyncio.run(scenario())
    assert stack.loaded is False


def test_a_client_arriving_mid_warm_never_waits_on_the_load(stack, monkeypatch):
    """A cold load holds its lock for up to a minute. Nothing the event loop
    touches may wait on that lock, or the second person into the house freezes
    the server until the first one's models land."""
    started, go = threading.Event(), threading.Event()
    real = voicestack.build_tts

    def slow(cfg):
        started.set()
        go.wait(5)
        return real(cfg)

    monkeypatch.setattr(voicestack, "build_tts", slow)

    async def scenario():
        first = asyncio.create_task(stack.acquire())
        await asyncio.to_thread(started.wait, 5)     # mid-warm, lock held
        second = asyncio.create_task(stack.acquire())
        await asyncio.sleep(0)                       # one step is all it may need
        assert stack.listeners == 2                  # counted, not blocked
        go.set()
        await asyncio.gather(first, second)
        assert stack.loads == 1                      # one warm, two listeners
        stack.release()
        stack.release()
        await settle(stack)

    asyncio.run(scenario())


def test_grace_period_survives_a_reload(cfg):
    """A page reload is a disconnect too: the stack waits VOICE_UNLOAD_AFTER_S
    before letting go, and a client back inside that window keeps it warm."""
    stack = VoiceStack(cfg.model_copy(update={"voice_unload_after_s": 30.0}),
                       BootBoard())

    async def scenario():
        await stack.acquire()
        stack.release()                        # F5
        await asyncio.sleep(0)
        assert stack.loaded is True            # still holding it
        await stack.acquire()                  # …and they're back
        assert stack.loaded is True and stack.loads == 1
        stack.release()

    asyncio.run(scenario())


def test_negative_delay_never_unloads(cfg):
    stack = VoiceStack(cfg.model_copy(update={"voice_unload_after_s": -1.0}),
                       BootBoard())

    async def scenario():
        await stack.acquire()
        stack.release()
        await settle(stack)

    asyncio.run(scenario())
    assert stack.loaded is True                # kept resident on purpose


def test_a_listener_arriving_during_the_unload_keeps_her_loaded(stack):
    """The check that matters is inside the lock: a connection that lands while
    the unload is in flight must never end up with a stack that's been freed."""
    async def scenario():
        await stack.acquire()
        stack.listeners += 1                   # somebody is still in the room
        stack.unload()                         # …so this is a no-op
        assert stack.loaded is True
        stack.listeners = 0
        stack.unload()
        assert stack.loaded is False

    asyncio.run(scenario())


def test_close_frees_everything(stack):
    async def scenario():
        await stack.acquire()
        await stack.close()
        assert stack.loaded is False
        await stack.acquire()                  # a closed stack never warms again
        assert stack.loaded is False

    asyncio.run(scenario())


def test_boot_panel_says_on_demand_then_narrates_the_load(stack):
    def states():
        return {s["key"]: (s["state"], s["detail"])
                for s in stack.boot.snapshot()["services"]}

    assert states()["tts"] == ("skipped", "on demand")
    assert stack.boot.snapshot()["done"] is True      # the gate never waits on it

    async def scenario():
        await stack.acquire()
        assert states()["tts"] == ("ready", "fake")
        stack.release()
        await settle(stack)

    asyncio.run(scenario())
    assert states()["tts"] == ("skipped", UNLOADED)


# ---- a warm that does not land ---------------------------------------------

def test_a_failed_warm_stays_cold_and_is_retried(stack, monkeypatch):
    """Marking a failed warm `loaded` made `acquire` short-circuit ever after,
    so one bad build handed every later connection a stack whose tts is None —
    a permanent mute from a transient failure."""
    def boom(cfg):
        raise RuntimeError("no espeak-ng")

    monkeypatch.setattr(voicestack, "build_tts", boom)

    async def scenario():
        with pytest.raises(RuntimeError):
            await stack.acquire()
        assert stack.loaded is False           # cold, so the next one tries again
        assert stack.ready.is_set() is False
        assert stack.tts is stack.stt is stack.vad is None
        assert stack.tts_name == UNLOADED

        monkeypatch.undo()
        await stack.acquire()
        assert stack.loaded is True and stack.tts is not None
        stack.release()
        await settle(stack)

    asyncio.run(scenario())


def test_a_failed_warm_releases_the_listener_it_counted(stack, monkeypatch):
    """`acquire` counts the listener before it warms anything, so the caller's
    release has to cover the raise too. Leak one and `unload` returns early
    forever — her weights pinned by a connection that never got into the room."""
    monkeypatch.setattr(voicestack, "build_tts",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("nope")))

    async def scenario():
        try:                                   # exactly what world/routes/voice_ws does
            await stack.acquire()
        except RuntimeError:
            pass
        finally:
            stack.release()

    asyncio.run(scenario())
    assert stack.listeners == 0


def test_the_socket_releases_a_listener_when_the_warm_raises(cfg, monkeypatch):
    """…and through the real route, because the guard is the route's to get
    right: the acquire has to sit inside the try that releases."""
    monkeypatch.setattr(voicestack, "build_tts",
                        lambda c: (_ for _ in ()).throw(RuntimeError("no espeak-ng")))
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as client:
        rt = app.state.rt
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_json({"type": "hello"})
                ws.receive_json()
                ws.receive_json()
        assert rt.voice.listeners == 0


# ---- through the runtime and the socket ------------------------------------

def test_runtime_holds_no_voice_until_a_client_opens_the_socket(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "voice_unload_after_s": 0.0})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        rt = c.app.state.rt
        assert rt.voice.loaded is False
        assert c.get("/api/health").json()["voice"] == {
            "ready": False, "loaded": False, "listeners": 0, "loads": 0,
            "stt": UNLOADED, "tts": UNLOADED, "vad": UNLOADED}

        with c.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            # `warming` comes FIRST, ahead of even the session id: it is what
            # shuts the client's composer, and everything after it is sent from
            # a server that has stopped reading the wire (web/js/voice.js).
            assert ws.receive_json()["type"] == "warming"   # cold: she says so
            assert ws.receive_json()["type"] == "session"
            assert ws.receive_json()["type"] == "ready"     # …and the all-clear
            while ws.receive_json()["type"] != "done":      # drain the greeting
                pass
            voice = c.get("/api/health").json()["voice"]
            assert voice["loaded"] is True and voice["listeners"] == 1
            assert voice["tts"] == "fake"

        # left the room: the socket's release schedules the unload on the server's
        # loop, so give it a moment to land rather than racing it
        deadline = time.monotonic() + 5
        while rt.voice.loaded and time.monotonic() < deadline:
            time.sleep(0.02)
        assert rt.voice.loaded is False
        assert c.get("/api/health").json()["voice"]["tts"] == UNLOADED
    assert rt.voice.loaded is False             # …and shutdown frees it regardless


def test_the_all_clear_says_which_voice_actually_landed(cfg):
    """A seam that fell back to the fake looks *exactly* like a working one from
    the room: the turn runs, her line lands in the transcript, and nothing plays.
    That is a real state and not a rare one — kokoro refusing to load (no
    espeak-ng, a GPU with nothing left on it) degrades to the fake so she boots
    at all. Until `ready` carried the name, the only account of it was one
    WARNING at boot, and "I unmuted her and she said nothing" was unanswerable
    from inside the app. web/js/voice.js captions `tts === "fake"`."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        with c.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            frames = {}
            while "ready" not in frames:
                frame = ws.receive_json()
                frames[frame["type"]] = frame
            assert frames["ready"]["tts"] == "fake"   # the suite's voice IS the fake
            while ws.receive_json()["type"] != "done":
                pass


def test_a_house_full_of_characters_warms_no_voices(tmp_path, monkeypatch):
    """The bug this exists for: `start_all` brings up every autostart character
    at boot (world/host.py), and each one used to warm its own Kokoro on the way
    up. Two characters, both running, zero voice stacks resident."""
    from yurios.characters import CharacterRegistry
    from yurios.world.config import Config as WorldConfig
    from yurios.world.host import create_host_app
    from tests.test_host import record

    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    registry.add(record(tmp_path, "mika"))

    def character_app(character_cfg, **kwargs):
        return create_app(character_cfg, brain=FakeBrain())

    monkeypatch.setattr("yurios.world.host.create_app", character_app)
    base = WorldConfig(_env_file=None, data_dir=tmp_path, tools_backend="off",
                       mind_enabled=False, selfie_backend="off",
                       tts_backend="fake", stt_backend="fake", vad_backend="fake",
                       telegram_bot_token="", telegram_chat_id="")
    app = create_host_app(base, registry)

    with TestClient(app) as client:
        host = app.state.host
        assert set(host.apps) == {"yuri", "mika"}          # both really running
        for character_id in ("yuri", "mika"):
            assert host.runtime(character_id).voice.loaded is False
            voice = client.get(f"/api/characters/{character_id}/health").json()["voice"]
            assert voice == {"ready": False, "loaded": False, "listeners": 0,
                             "loads": 0, "stt": UNLOADED, "tts": UNLOADED,
                             "vad": UNLOADED}

        # …and one of them getting a visitor warms only hers
        with client.websocket_connect("/ws/characters/yuri/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            while ws.receive_json()["type"] != "done":
                pass
            assert host.runtime("yuri").voice.loaded is True
            assert host.runtime("mika").voice.loaded is False


def test_the_text_room_never_warms_her_voice(cfg):
    """/api/chat mirrors the voice contract minus the audio (§10) — and minus the
    voice stack: a typed turn must not drag a gigabyte of TTS into memory."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "are you there?"})
        assert r.status_code == 200
        assert c.app.state.rt.voice.loaded is False


def test_a_rewarmed_session_gets_its_all_clear_without_a_greeting(cfg):
    """The lockout the explicit `ready` exists for: she greets a session once
    (world/main.py), so a client that reconnects to a *cold* stack on a session
    she already greeted is warmed, told so — and then nothing else is ever sent.
    A composer shut on `warming` and reopened by "the next message" would stay
    shut for the rest of the visit."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "voice_unload_after_s": 0.0})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        rt = c.app.state.rt
        with c.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            assert ws.receive_json()["type"] == "warming"
            session_id = ws.receive_json()["session_id"]
            assert ws.receive_json()["type"] == "ready"
            while ws.receive_json()["type"] != "done":      # she greets, once
                pass

        deadline = time.monotonic() + 5                    # let the unload land
        while rt.voice.loaded and time.monotonic() < deadline:
            time.sleep(0.02)
        assert rt.voice.loaded is False                    # cold again

        with c.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": session_id})
            assert ws.receive_json()["type"] == "warming"
            assert ws.receive_json()["session_id"] == session_id
            # no greeting is coming — this is the only thing that reopens the room
            assert ws.receive_json()["type"] == "ready"


def test_a_warm_stack_never_shuts_the_composer(cfg):
    """No wait, no notice: the second client into a warm room must not be told
    to sit on its hands."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "voice_unload_after_s": -1.0})   # stay loaded
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        with c.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            assert ws.receive_json()["type"] == "warming"
            ws.receive_json()                              # session
            assert ws.receive_json()["type"] == "ready"
            while ws.receive_json()["type"] != "done":
                pass

        with c.websocket_connect("/ws/voice") as ws:       # already warm
            ws.send_json({"type": "hello", "session_id": None})
            # The session id comes first and alone: no notice means the composer
            # is never shut, which is the whole point of only warning when cold.
            assert ws.receive_json()["type"] == "session"
