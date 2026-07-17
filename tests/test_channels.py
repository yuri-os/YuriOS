"""The channel layer (SPEC §10.5): the shared TextTurns runner over POST
/api/chat, and the Telegram adapter against a scripted Bot API — all offline,
like everything else in the suite."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("fastapi")
import httpx                                                  # noqa: E402
from starlette.testclient import TestClient                   # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.channels.manager import ChannelManager      # noqa: E402
from yurios.world.channels.telegram import TelegramChannel    # noqa: E402
from yurios.world.hub import EventHub                         # noqa: E402
from yurios.world.main import create_app                      # noqa: E402

STRIPPED = "Hey, you made it back. I missed you today."       # FakeBrain, tags gone


def make_app(cfg, brain=None):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    return create_app(cfg, brain=brain or FakeBrain())


def signal_types(rt) -> list[tuple[str, str]]:
    batch, _ = rt.signals.next(0, limit=64)
    return [(s.type, s.source) for s in batch]


# ---- POST /api/chat — the TextTurns runner over HTTP ------------------------

def test_api_chat_runs_one_committed_turn(cfg):
    brain = FakeBrain()
    app = make_app(cfg, brain)
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "hi there", "channel": "cli"})
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == "0" * 32
        assert data["message"]["role"] == "assistant"
        assert data["message"]["text"] == STRIPPED       # tags drive the face, not the text
        assert data["message"]["channel"] == "cli"

        rt = app.state.rt
        roles = [(m["role"], m.get("channel")) for m in rt.transcript]
        assert roles == [("user", "cli"), ("assistant", "cli")]
        # the verbatim reply (tags kept) persisted — B2's corpus rule
        assert brain.persisted is not None
        assert "[happy]" in brain.persisted[2]
        # the mind's tee: preempt + REFLECT share, stamped with the medium
        assert ("user_message", "cli") in signal_types(rt)
        assert ("turn_committed", "cli") in signal_types(rt)


def test_api_chat_session_continues_and_rejects_noise(cfg):
    app = make_app(cfg)
    with TestClient(app) as c:
        first = c.post("/api/chat", json={"text": "hello"}).json()
        again = c.post("/api/chat", json={
            "text": "still me", "session_id": first["session_id"]}).json()
        assert again["session_id"] == first["session_id"]
        # a punctuation-only line is not a turn (B2 §3.2, held on this path too)
        assert c.post("/api/chat", json={"text": ". . ."}).status_code == 422


def test_api_chat_midstream_failure_leaves_no_reply_trace(cfg):
    class BrokenBrain(FakeBrain):
        async def stream_reply(self, session_id, text):
            yield "[happy] one "
            raise RuntimeError("model died")

    brain = BrokenBrain()
    app = make_app(cfg, brain)
    with TestClient(app) as c:
        assert c.post("/api/chat", json={"text": "hi"}).status_code == 502
        rt = app.state.rt
        assert [m["role"] for m in rt.transcript] == ["user"]   # her half never landed
        assert brain.persisted is None
        assert ("turn_committed", "api") not in signal_types(rt)


# ---- the Telegram adapter ---------------------------------------------------

class ScriptedTelegram(httpx.AsyncBaseTransport):
    """A scripted Bot API: records every call, serves getMe/getUpdates, and
    sleeps on an empty poll so the long-poll loop idles instead of spinning."""

    def __init__(self, updates: list[dict] | None = None):
        self.updates = list(updates or [])
        self.calls: list[tuple[str, dict]] = []

    async def handle_async_request(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        raw = await request.aread()              # multipart bodies stream
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:                       # multipart (sendPhoto)
            body = {"_multipart": True}
        self.calls.append((method, body))
        if method == "getMe":
            result = {"username": "yuri_bot"}
        elif method == "getUpdates":
            if self.updates:
                result = [self.updates.pop(0)]
            else:
                await asyncio.sleep(0.05)
                result = []
        else:
            result = {"message_id": 1}
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, method: str) -> list[dict]:
        return [b for m, b in self.calls if m == method]


class StubTurns:
    def __init__(self):
        self.calls: list[tuple[str, str, str | None]] = []

    async def run(self, text, *, channel, session_id=None):
        self.calls.append((text, channel, session_id))
        return {"session_id": "s" * 32,
                "message": {"id": "m1", "role": "assistant", "text": "hi"}}


class StubRT:
    def __init__(self):
        self.hub = EventHub()
        self.turns = StubTurns()


def tg(transport, chat_id="42", **kw) -> TelegramChannel:
    ch = TelegramChannel("tok", chat_id, transport=transport, **kw)
    ch.rt = StubRT()
    ch._client = httpx.AsyncClient(base_url="https://x/bot-tok",
                                   transport=transport)
    return ch


def update(chat_id=42, text="hello") -> dict:
    msg: dict = {"chat": {"id": chat_id}}
    if text is not None:
        msg["text"] = text
    return {"update_id": 1, "message": msg}


async def test_telegram_message_becomes_a_text_turn_with_typing():
    tr = ScriptedTelegram()
    ch = tg(tr)
    await ch._handle_update(update())
    assert ch.rt.turns.calls == [("hello", "telegram", None)]
    assert tr.sent("sendChatAction") == [{"chat_id": "42", "action": "typing"}]
    # the session sticks for the next message from the same chat
    await ch._handle_update(update(text="again"))
    assert ch.rt.turns.calls[1] == ("again", "telegram", "s" * 32)
    await ch._client.aclose()


async def test_telegram_strangers_are_ignored_and_unset_id_pairs():
    tr = ScriptedTelegram()
    ch = tg(tr)                                  # bound to chat 42
    await ch._handle_update(update(chat_id=99))
    assert ch.rt.turns.calls == []               # not a turn
    assert tr.sent("sendMessage") == []          # not even a reply

    tr2 = ScriptedTelegram()
    ch2 = tg(tr2, chat_id="")                    # pairing mode
    await ch2._handle_update(update(chat_id=99))
    assert ch2.rt.turns.calls == []
    (pairing,) = tr2.sent("sendMessage")
    assert "TELEGRAM_CHAT_ID=99" in pairing["text"]
    await ch._client.aclose()
    await ch2._client.aclose()


async def test_telegram_non_text_gets_the_stock_line():
    tr = ScriptedTelegram()
    ch = tg(tr)
    await ch._handle_update(update(text=None))   # a sticker, a photo…
    assert ch.rt.turns.calls == []
    (note,) = tr.sent("sendMessage")
    assert "only read text" in note["text"]
    await ch._client.aclose()


async def test_telegram_delivers_assistant_lines_only_and_chunks(tmp_path):
    tr = ScriptedTelegram()
    ch = tg(tr, selfie_dir=tmp_path)
    await ch._deliver_event({"type": "message", "role": "user", "text": "me"})
    await ch._deliver_event({"type": "draft", "text": "half a"})
    await ch._deliver_event({"type": "avatar", "op": "rain"})
    assert tr.sent("sendMessage") == []          # none of that is hers to forward
    await ch._deliver_event({"type": "message", "role": "assistant",
                             "text": "x" * 5000, "proactive": True})
    sent = tr.sent("sendMessage")
    assert [len(b["text"]) for b in sent] == [4096, 904]   # the 4096 cap, split
    await ch._client.aclose()


async def test_telegram_sends_the_selfie_file_itself(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG fake")
    tr = ScriptedTelegram()
    ch = tg(tr, selfie_dir=tmp_path)
    await ch._deliver_event({"type": "message", "role": "assistant",
                             "text": "took one!", "image_url": "/selfies/shot.png"})
    assert tr.sent("sendMessage") == []          # the photo carried the caption
    assert len(tr.sent("sendPhoto")) == 1
    await ch._client.aclose()


# ---- the whole wire: server ↔ Telegram, through the real Runtime ------------

def test_telegram_end_to_end_over_the_running_app(cfg):
    tr = ScriptedTelegram(updates=[update(chat_id=42, text="hey you")])
    app = make_app(cfg)
    rt = app.state.rt
    rt.channels = ChannelManager([TelegramChannel(
        "tok", "42", transport=tr, selfie_dir=cfg.selfie_dir)])
    with TestClient(app) as c:
        for _ in range(100):                     # the poll task runs on the app loop
            time.sleep(0.05)
            if tr.sent("sendMessage"):
                break
        assert c.get("/api/health").json()["channels"] == "telegram · @yuri_bot"
        msgs = c.get("/api/history").json()["messages"]
        assert [(m["role"], m.get("channel")) for m in msgs] == [
            ("user", "telegram"), ("assistant", "telegram")]
        (delivered,) = tr.sent("sendMessage")
        assert delivered == {"chat_id": "42", "text": STRIPPED}
