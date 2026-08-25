"""Reading one of her lines back out — the replay button (SPEC §9.11).

The feature is one frame on the voice socket (`speak`, carrying a message id)
and the four rules that keep it from being something else:

  1. it says what she said — resolved out of the transcript server-side, so the
     wire can ask her to repeat herself and cannot put new words in her mouth;
  2. it is not a turn — nothing is generated, committed, teed or remembered, and
     pressing the button twice leaves the transcript exactly as it was;
  3. it is her voice, not yours — a `speak` naming one of *your* lines is
     refused, and so is one naming a line that has fallen off the ring;
  4. it never costs a live turn — she is mid-reply, a barge-in commits nothing
     (§4.4), so the button waits rather than throwing her answer away. The
     other way round it yields at once: talking over a replay silences it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                          # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain, _wordish  # noqa: E402
from yurios.world.main import create_app                             # noqa: E402


class HoldableBrain(FakeBrain):
    """FakeBrain with a mid-reply hold, so a test can pin 'a turn is in flight'."""

    def __init__(self):
        super().__init__()
        self.hold: asyncio.Event | None = None

    async def stream_reply(self, session_id: str, text: str, image: str | None = None):
        for tok in _wordish("[happy] One moment. "):
            yield tok
            await asyncio.sleep(0)
        if self.hold is not None:
            await self.hold.wait()
        for tok in _wordish("Okay, all done now."):
            yield tok
            await asyncio.sleep(0)


@pytest.fixture
def rig(cfg, controller):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    brain = HoldableBrain()
    app = create_app(cfg, brain=brain, controller=controller)
    with TestClient(app) as client:
        yield client, app.state.rt, brain


def handshake(ws) -> str:
    while True:
        m = ws.receive_json()
        if m["type"] == "session":
            return m["session_id"]
        assert m["type"] == "warming", f"unexpected {m['type']} before the session"


def drain(ws, cap=60):
    """Pump until a turn ends. Returns (kinds, spoken sentence texts)."""
    kinds, texts = [], []
    for _ in range(cap):
        m = ws.receive_json()
        kinds.append(m["type"])
        if m["type"] in ("audio", "filler") and m.get("text"):
            texts.append(m["text"])
        if m["type"] in ("done", "error", "cancelled"):
            break
    return kinds, texts


def read_out(ws, cap=60):
    """Pump one replay. Returns (its sentence texts, the closing `spoken` frame)."""
    texts = []
    for _ in range(cap):
        m = ws.receive_json()
        if m["type"] == "audio" and m.get("text"):
            texts.append(m["text"])
        if m["type"] == "spoken":
            return texts, m
    raise AssertionError("the replay never ended")


def wait_for(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_replay_says_her_line_again_and_commits_nothing(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)                                       # the greeting
        assert wait_for(lambda: len(rt.transcript) == 1)
        greeting = rt.transcript[0]
        before = list(rt.transcript)
        persists = len(brain.persist_calls)

        ws.send_json({"type": "speak", "message_id": greeting["id"]})
        assert ws.receive_json() == {"type": "speaking",
                                     "message_id": greeting["id"]}
        texts, spoken = read_out(ws)
        # what came back is the line itself, sentence by sentence…
        assert texts and " ".join(texts) in greeting["text"]
        # …and it closed cleanly: `spoken` with no reason, because it happened
        assert spoken == {"type": "spoken", "message_id": greeting["id"]}

    # A replay is not a turn (§9.11): the transcript is byte-for-byte what it
    # was, nothing was teed to the mind, and her memory never heard about it.
    assert rt.transcript == before
    assert len(brain.persist_calls) == persists


def test_it_strips_what_she_must_not_read_aloud(rig):
    """The committed text is already clean, but the lines the mind's other
    surfaces post are not guaranteed to be — she must never say the word
    "happy", nor read a stage direction out (emotion.py's whole job)."""
    client, rt, _ = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)
        entry = rt.post_message(
            "assistant", "[happy] There you are. *she leans in* Sit with me.",
            proactive=True)

        ws.send_json({"type": "speak", "message_id": entry["id"]})
        assert ws.receive_json()["type"] == "speaking"
        texts, _ = read_out(ws)
        said = " ".join(texts)
        assert "There you are." in said and "Sit with me." in said
        assert "happy" not in said and "leans in" not in said


def test_her_lines_only_and_only_ones_still_in_the_ring(rig):
    client, rt, _ = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)
        mine = rt.post_message("user", "read this back to me", channel="web")

        for message_id in (mine["id"], "nosuchid"):
            ws.send_json({"type": "speak", "message_id": message_id})
            refusal = ws.receive_json()
            assert refusal["type"] == "spoken"
            assert refusal["message_id"] == message_id
            assert refusal["message"]                    # …and it says why


def test_a_replay_never_costs_a_live_turn(rig):
    client, rt, brain = rig
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)
        greeting = rt.transcript[0]

        brain.hold = asyncio.Event()                     # her reply parks mid-turn
        ws.send_json({"type": "text", "text": "talk to me"})
        while ws.receive_json()["type"] != "audio":      # …audibly in flight
            pass
        ws.send_json({"type": "speak", "message_id": greeting["id"]})
        refused = ws.receive_json()
        assert refused["type"] == "spoken"               # refused, not accepted
        assert refused["message_id"] == greeting["id"]
        assert refused["message"]

        rt.loop.call_soon_threadsafe(brain.hold.set)     # release; the turn lands
        kinds, _ = drain(ws)
        assert kinds[-1] == "done"
    # the turn she was in the middle of committed normally
    assert wait_for(lambda: [m["role"] for m in rt.transcript]
                    == ["assistant", "user", "assistant"])


def test_talking_over_a_replay_silences_it(rig):
    client, rt, _ = rig
    long_line = " ".join(f"Sentence number {i}." for i in range(40))
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)
        entry = rt.post_message("assistant", long_line, proactive=True)

        ws.send_json({"type": "speak", "message_id": entry["id"]})
        assert ws.receive_json()["type"] == "speaking"
        while ws.receive_json()["type"] != "audio":      # she is reading it out
            pass
        ws.send_json({"type": "bargein"})

        heard = 0
        for _ in range(80):
            m = ws.receive_json()
            if m["type"] == "audio":
                heard += 1
            if m["type"] == "spoken":
                break
        else:
            raise AssertionError("the replay never ended")
        # it stopped short — a forty-sentence line was not read to the end
        assert heard < 40

        # …and the floor is genuinely free again: a turn runs straight after
        ws.send_json({"type": "text", "text": "sorry, go on"})
        kinds, _ = drain(ws)
        assert kinds[-1] == "done"


def test_the_mind_will_not_talk_over_a_replay(rig):
    client, rt, _ = rig
    long_line = " ".join(f"Sentence number {i}." for i in range(40))
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)
        entry = rt.post_message("assistant", long_line, proactive=True)

        ws.send_json({"type": "speak", "message_id": entry["id"]})
        assert ws.receive_json()["type"] == "speaking"
        while ws.receive_json()["type"] != "audio":
            pass
        fut = asyncio.run_coroutine_threadsafe(
            rt.speak_ambient("((say something))"), rt.loop)
        assert fut.result(timeout=5) is False            # one mouth (§8.4)
        ws.send_json({"type": "bargein"})
        read_out(ws, cap=120)


def test_a_line_that_outlived_the_ring_is_still_hers_to_read(rig):
    """The ring is memory; her inbox is the disk copy of what she said into an
    empty room (§18.4). After a restart that file is the only copy a page is
    still showing, so it is the second place a replay looks."""
    client, rt, _ = rig
    entry = rt.post_message("assistant", "I left the light on for you.",
                            proactive=True, unheard=True)
    assert entry["id"] in {e["id"] for e in rt.inbox.entries()}
    rt.transcript.clear()                                # …as a restart leaves it

    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "hello", "session_id": None})
        handshake(ws)
        drain(ws)                                        # the greeting
        rt.transcript.clear()                            # …and again, past it

        ws.send_json({"type": "speak", "message_id": entry["id"]})
        assert ws.receive_json()["type"] == "speaking"
        texts, spoken = read_out(ws)
        assert " ".join(texts) == "I left the light on for you."
        assert "message" not in spoken                   # it happened
