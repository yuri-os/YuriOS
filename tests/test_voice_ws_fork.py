"""The forked /ws/voice route (SPEC §2.2, §8.4, §13) — B2's behaviour preserved
(greeting-once, noise-drop, barge-in) plus the one fork: ambient injection.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                    # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain, _wordish   # noqa: E402
from yurios.world.main import create_app                              # noqa: E402


class AmbientBrain(FakeBrain):
    """FakeBrain + the stream_ambient seam the fork calls (SPEC §8.3), and an
    optional mid-reply hold so a test can pin 'a turn is in flight'."""

    def __init__(self):
        super().__init__()
        self.ambient_cues: list[str] = []
        self.hold: asyncio.Event | None = None

    async def stream_reply(self, session_id: str, text: str):
        for tok in _wordish("[happy] One moment. "):
            yield tok
            await asyncio.sleep(0)
        if self.hold is not None:
            await self.hold.wait()          # …the turn stays in flight until released
        for tok in _wordish("Okay, all done now."):
            yield tok
            await asyncio.sleep(0)

    async def stream_ambient(self, session_id: str, cue: str):
        self.ambient_cues.append(cue)
        for tok in _wordish("[relaxed] The rain hasn't let up at all."):
            yield tok
            await asyncio.sleep(0)


@pytest.fixture
def rig(cfg, controller):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    brain = AmbientBrain()
    app = create_app(cfg, brain=brain, controller=controller)
    with TestClient(app) as client:
        yield client, app.state.rt, brain


def handshake(ws) -> str:
    """The session id, skipping the cold-start notice. The first client into a
    cold room is told `warming` *before* its session id — that notice is what
    shuts the composer, so it has to come out ahead of everything (§9.9). The
    matching `ready` lands after and is harmless to `drain`."""
    while True:
        m = ws.receive_json()
        if m["type"] == "session":
            return m["session_id"]
        assert m["type"] == "warming", f"unexpected {m['type']} before the session"


def drain(ws, cap=60):
    kinds, texts = [], []
    for _ in range(cap):
        m = ws.receive_json()
        kinds.append(m["type"])
        if m["type"] in ("audio", "filler") and m.get("text"):
            texts.append(m["text"])
        if m["type"] in ("done", "error", "cancelled"):
            break
    return kinds, texts


def test_b2_behaviour_preserved_greeting_once_noise_drop(rig):
    client, rt, brain = rig
    speech = np.full(512, 0.2, dtype=np.float32).tobytes()
    noise = np.full(512, 0.03, dtype=np.float32).tobytes()

    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        sid = handshake(ws)
        kinds, texts = drain(ws)                        # she greets first (B2 §7)
        assert kinds[-1] == "done" and any("there you are" in t for t in texts)

        for _ in range(6):                              # keyboard clatter: no turn
            ws.send_bytes(noise)
        ws.send_json({"type": "endpoint"})
        for _ in range(6):                              # real speech: a turn
            ws.send_bytes(speech)
        ws.send_json({"type": "endpoint"})
        kinds, _ = drain(ws)
        assert kinds[-1] == "done"
    assert len(brain.persist_calls) == 1                # only the speech persisted

    with client.websocket_connect("/ws/voice") as ws:   # reconnect ≠ new arrival
        ws.send_json({"type": "hello", "session_id": sid})
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "text", "text": "hi again"})
        _, texts = drain(ws)
        assert texts and not any("there you are" in t for t in texts)


def test_ambient_injection_reaches_the_client_and_is_not_persisted(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)                                       # the greeting
        persisted_before = len(brain.persist_calls)

        # the mind speaks (from the server side, through the fork's seam)
        fut = asyncio.run_coroutine_threadsafe(
            rt.speak_ambient("((say something about the rain))"), rt.loop)
        assert fut.result(timeout=5) is True

        kinds, texts = drain(ws)
        assert kinds[-1] == "done"
        assert any("rain hasn't let up" in t for t in texts)   # it reached the client
    assert brain.ambient_cues == ["((say something about the rain))"]
    assert len(brain.persist_calls) == persisted_before        # never persisted (§8.3)


def test_ambient_refused_while_a_turn_is_in_flight(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)                                       # greeting done

        brain.hold = asyncio.Event()                    # the reply will park mid-turn
        ws.send_json({"type": "text", "text": "talk to me"})
        while ws.receive_json()["type"] != "audio":     # her turn is audibly in flight
            pass
        fut = asyncio.run_coroutine_threadsafe(
            rt.speak_ambient("((cue))"), rt.loop)
        busy_result = fut.result(timeout=5)
        rt.loop.call_soon_threadsafe(brain.hold.set)    # release; the turn finishes
        drain(ws)
        assert busy_result is False                     # she was busy (§8.4)
        assert brain.ambient_cues == []


def test_disconnect_unregisters_the_ambient_injector(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)
        assert rt._ambient                              # registered while live
    for _ in range(50):
        if not rt._ambient:
            break
        import time
        time.sleep(0.05)
    assert not rt._ambient                              # gone on disconnect
    fut = asyncio.run_coroutine_threadsafe(rt.speak_ambient("((cue))"), rt.loop)
    assert fut.result(timeout=5) is False               # nobody to speak through


# ---- the transcript tee + one expression lane (forks #4/#5, SPEC §2.6/§10) ----

def wait_for(pred, timeout=5.0):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_turns_commit_to_the_transcript_greeting_is_proactive(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)                                       # the greeting
        assert wait_for(lambda: len(rt.transcript) == 1)
        greet = rt.transcript[0]
        assert greet["role"] == "assistant" and greet["proactive"] is True
        assert "there you are" in greet["text"]         # tag stripped, text kept
        assert greet["channel"] == "voice"

        ws.send_json({"type": "text", "text": "talk to me",
                      "client_id": "browser-1"})
        accepted = ws.receive_json()
        assert accepted["type"] == "accepted"
        assert accepted["message"]["client_id"] == "browser-1"
        while True:
            done = ws.receive_json()
            if done["type"] == "done":
                break
        assert wait_for(lambda: len(rt.transcript) == 3)
        assert [m["role"] for m in rt.transcript] == ["assistant", "user", "assistant"]
        assert rt.transcript[1]["text"] == "talk to me"
        assert rt.transcript[1]["channel"] == "voice"
        assert rt.transcript[1]["client_id"] == "browser-1"
        assert "all done now" in rt.transcript[2]["text"]
        assert rt.transcript[2]["channel"] == "voice"
        assert not rt.transcript[2].get("proactive")    # a reply, not an approach
        # The voice socket is the typed turn's transport when sound is enabled.
        # Its terminal frame must carry the committed line too, so a stale SSE
        # connection cannot make an otherwise successful spoken reply textless.
        assert done["client_id"] == "browser-1"
        assert done["message"] == rt.transcript[2]


def test_the_cold_open_is_committed_as_written_not_as_spoken(rig):
    """A cold open is a *scene* (§5.4). The pipeline strips `*narration*` on the
    way to TTS — right for a voice, ruinous for the card's first message: a
    transcript built from that residue opened the relationship with the handful
    of noises that happened to be in quotes."""
    client, rt, brain = rig
    scene = ('*She looks up from the window, and the rain goes quiet.* '
             '"You found me." *A pause, and something eases in her.* '
             '"I hoped you would."')
    brain.cold = scene
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        _kinds, texts = drain(ws)
        assert wait_for(lambda: len(rt.transcript) == 1)
        greet = rt.transcript[0]
        assert greet["text"] == scene, "the room shows the card's text, whole"
        assert greet["proactive"] is True

        spoken = " ".join(texts)                        # what went to TTS
        assert "You found me." in spoken
        assert "looks up from the window" not in spoken  # a voice reads no stage directions


def test_a_spoken_turn_puts_the_stt_transcript_in_the_chat(rig):
    client, rt, brain = rig
    speech = np.full(512, 0.2, dtype=np.float32).tobytes()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)                                       # greeting
        for _ in range(6):
            ws.send_bytes(speech)
        ws.send_json({"type": "endpoint", "client_id": "spoken-1"})
        drain(ws)
    assert wait_for(lambda: any(
        m["role"] == "user" and m["text"] == "hey, i'm back" and
        m.get("client_id") == "spoken-1"             # the FakeSTT script + receipt
        for m in rt.transcript))


def test_bargein_drops_the_draft_and_commits_nothing(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)                                       # greeting (1 entry)
        brain.hold = asyncio.Event()                    # park the reply mid-turn
        ws.send_json({"type": "text", "text": "talk to me"})
        while ws.receive_json()["type"] != "audio":     # audibly in flight
            pass
        ws.send_json({"type": "bargein"})
        rt.loop.call_soon_threadsafe(brain.hold.set)
        kinds, _ = drain(ws)
        assert kinds[-1] == "cancelled"
    # the user's turn is in the chat; her cut-off reply is not (no trace, B2 §4.4)
    assert wait_for(lambda: len(rt.transcript) == 2)
    assert [m["role"] for m in rt.transcript] == ["assistant", "user"]
    # …and the pump rolled the half-written turn out of her memory, so the next
    # prompt won't carry the question as still unanswered (test_integration
    # pins what that rollback actually does over the real brain)
    assert wait_for(lambda: brain.abandon_calls)
    assert not brain.persist_calls                     # nothing was committed


def test_expressions_ride_the_puppet_lane_not_the_wire(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        kinds, _ = drain(ws)                            # the greeting ([happy] …)
        assert "expression" not in kinds                # off the audio wire (§10)
    # …and onto the hub, with voice-turn hold semantics (reset 0, B2 §6)
    assert wait_for(lambda: any(
        c["type"] == "expression" and c["name"] == "happy" and c["reset_ms"] == 0
        for c in rt.controller.commands))


def test_ambient_lines_commit_as_proactive(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)                                   # session (past any warm notice)
        drain(ws)                                       # greeting
        fut = asyncio.run_coroutine_threadsafe(
            rt.speak_ambient("((the rain))"), rt.loop)
        assert fut.result(timeout=5) is True
        drain(ws)
    assert wait_for(lambda: any(
        m.get("proactive") and "rain hasn't let up" in m["text"]
        for m in rt.transcript))
