"""The event hub + /api/events (SPEC §4, §10, §2.6) — the one outbound bus,
over the unit surface and the real SSE route."""
from __future__ import annotations

import asyncio
import json

import pytest

from yurios.world.hub import EventHub

pytest.importorskip("fastapi")
from starlette.testclient import TestClient           # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.main import create_app                     # noqa: E402


# ---- the hub itself --------------------------------------------------------

async def test_publish_reaches_every_subscriber_typed():
    hub = EventHub()
    q1, q2 = hub.subscribe(), hub.subscribe()
    hub.publish("message", {"role": "user", "text": "hi"})
    for q in (q1, q2):
        ev = q.get_nowait()
        assert ev == {"type": "message", "role": "user", "text": "hi"}


async def test_sticky_recorded_before_any_subscriber_then_replayed():
    hub = EventHub()
    # nobody listening yet (startup: set_rain runs before the first page opens)
    hub.publish("avatar", {"op": "rain", "intensity": 0.6}, sticky=("rain", ""))
    q = hub.subscribe()
    assert q.get_nowait() == {"type": "avatar", "op": "rain", "intensity": 0.6}


async def test_sticky_last_write_per_key_wins():
    hub = EventHub()
    hub.subscribe()
    hub.publish("avatar", {"op": "rain", "intensity": 0.2}, sticky=("rain", ""))
    hub.publish("avatar", {"op": "rain", "intensity": 0.9}, sticky=("rain", ""))
    q = hub.subscribe()
    replayed = [q.get_nowait() for _ in range(q.qsize())]
    assert replayed == [{"type": "avatar", "op": "rain", "intensity": 0.9}]


async def test_unsubscribe_stops_delivery_and_counts():
    hub = EventHub()
    q = hub.subscribe()
    assert hub.subscribers == 1
    hub.unsubscribe(q)
    assert hub.subscribers == 0
    hub.publish("message", {"text": "x"})
    assert q.empty()


# ---- the SSE route over the real app ---------------------------------------
# Neither starlette's TestClient nor httpx's ASGITransport can read a response
# that never ends (both buffer to completion), so the route is exercised with
# the stop flag pre-set: the stream yields the hello + the sticky replay and
# terminates cleanly — exactly the shutdown discipline the route promises.
# Live fan-out (publish → every subscriber queue) is the hub tests above; the
# whole wire is driven for real by scripts/demo_avatar.py and the live run.

@pytest.fixture
def client(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        c.app = app
        yield c


def sse_events(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in body.splitlines() if line.startswith("data: ")]


def test_stream_opens_with_hello_then_sticky_and_honours_the_stop_flag(client):
    rt = client.app.state.rt
    rt.loop.call_soon_threadsafe(rt.stopping.set)   # shutdown began: stream must end
    r = client.get("/api/events")
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert events[0] == {"type": "hello", "character": "yuri"}
    assert {"type": "avatar", "op": "rain",       # startup scene, replayed sticky
            "intensity": rt.cfg.rain_intensity} in events
    assert rt.hub.subscribers == 0                 # unsubscribed on the way out


def test_a_message_posted_before_attach_is_in_the_sticky_free_backfill(client):
    """Chat history is /api/history's job, not the stream's: a late page gets
    hello + sticky scene only, and backfills the transcript over HTTP."""
    rt = client.app.state.rt
    rt.loop.call_soon_threadsafe(rt.post_message, "user", "before attach")
    rt.loop.call_soon_threadsafe(rt.stopping.set)
    events = sse_events(client.get("/api/events").text)
    assert all(e["type"] != "message" for e in events)
    assert any(m["text"] == "before attach"
               for m in client.get("/api/history").json()["messages"])


def test_history_backfills_the_transcript_ring(client):
    rt = client.app.state.rt
    rt.loop.call_soon_threadsafe(rt.post_message, "user", "hello there")
    rt.loop.call_soon_threadsafe(
        lambda: rt.post_message("assistant", "hi~", proactive=True))
    for _ in range(50):
        if len(rt.transcript) == 2:
            break
        import time
        time.sleep(0.02)
    d = client.get("/api/history").json()
    roles = [(m["role"], m.get("proactive", False)) for m in d["messages"]]
    assert roles == [("user", False), ("assistant", True)]
    assert all(m["id"] and m["ts"] for m in d["messages"])   # dedup + clock keys


def test_transcript_ring_is_bounded(client):
    rt = client.app.state.rt

    def flood():
        for i in range(250):
            rt.post_message("user", f"m{i}")
    rt.loop.call_soon_threadsafe(flood)
    for _ in range(100):
        if len(rt.transcript) == 200 and rt.transcript[-1]["text"] == "m249":
            break
        import time
        time.sleep(0.02)
    assert len(rt.transcript) == 200                    # the ring holds


def test_selfie_route_404s_outside_the_flat_dir(client):
    assert client.get("/selfies/nope.png").status_code == 404
    assert client.get("/selfies/..%2F.env").status_code in (400, 404)
