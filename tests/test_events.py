"""The event hub + /api/events (SPEC §4, §10, §2.6) — the one outbound bus,
over the unit surface and the real SSE route."""
from __future__ import annotations

import asyncio
import json

import pytest

from yurios.kernel.hub import EventHub

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


async def test_a_drain_is_not_a_viewer():
    """Telegram and notify subscribe for delivery. They are not company."""
    hub = EventHub()
    drain = hub.subscribe()
    page = hub.subscribe(viewer=True)
    assert hub.subscribers == 2
    assert hub.viewers == 1
    hub.unsubscribe(page)
    assert hub.viewers == 0
    assert hub.subscribers == 1
    hub.unsubscribe(drain)
    assert hub.subscribers == 0


async def test_turns_idle_gates_on_the_turn_lifecycle(cfg):
    """Runtime.wait_turns_idle — the selfie parker's quiet gate (§7.6): set
    while no turn is in flight, cleared from turn_started until the matching
    turn_ended, counting overlapping turns (a reply + her ambient line)."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    assert rt.turns_idle.is_set()
    rt.turn_started()
    assert not rt.turns_idle.is_set()
    rt.turn_started()
    rt.turn_ended()
    assert not rt.turns_idle.is_set()          # one turn still in flight
    rt.turn_ended()
    assert rt.turns_idle.is_set()
    await asyncio.wait_for(rt.wait_turns_idle(), 1)   # returns at once when idle


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


def _types(rt) -> list[str]:
    return [s.type for s in rt.signals.next(0, 1000)[0]]


class _Req:
    """All `/api/events` reads off a request: the runtime, and the uvicorn
    server that TestClient does not have."""

    def __init__(self, app):
        self.app = app


def test_shutdown_does_not_post_user_absent(client):
    """`stopping` is what ends the stream loop, so the `finally` under it runs on
    every shutdown. Posting there wrote `traces/signals.jsonl` after the runtime
    had stopped, for a mind already cancelled — and since archive moves the
    character's directory the moment `host.stop` returns, the write recreated it
    and left a phantom folder for a character no longer in the registry.
    """
    rt = client.app.state.rt
    rt.loop.call_soon_threadsafe(rt.stopping.set)
    client.get("/api/events")
    assert rt.hub.subscribers == 0            # it still tidied up after itself
    assert "user_present" in _types(rt)       # …and the arrival still counted
    assert "user_absent" not in _types(rt)


async def test_the_last_page_leaving_still_posts_user_absent(client):
    """The signal the guard must not cost. A page closing is not a shutdown: the
    generator is closed under it, which is a different way out of the same
    `finally`, and her world model still needs to hear the room emptied (§16.2).

    Driven directly rather than over HTTP — under TestClient the stream only
    ends when `stopping` is set, which is the case this one is not about.
    """
    from yurios.world.routes.events import events as events_route

    rt = client.app.state.rt
    response = await events_route(_Req(client.app))
    stream = response.body_iterator
    assert "hello" in await stream.__anext__()
    assert rt.hub.subscribers == 1
    await stream.aclose()                     # the page went away

    assert rt.hub.subscribers == 0
    assert not rt.stopping.is_set()
    assert "user_absent" in _types(rt)


async def test_a_channel_adapter_does_not_keep_the_room_occupied(client):
    """Found live: Telegram + notify held hub.subscribers ≥ 1, so closing the
    tab never posted user_absent and expensive hands stayed gated."""
    from yurios.world.routes.events import events as events_route

    rt = client.app.state.rt
    drain = rt.hub.subscribe()
    assert rt.hub.viewers == 0
    response = await events_route(_Req(client.app))
    stream = response.body_iterator
    assert "hello" in await stream.__anext__()
    assert rt.hub.viewers == 1
    assert rt.hub.subscribers == 2
    present = [s for s in rt.signals.next(0, 1000)[0] if s.type == "user_present"]
    assert present[-1].payload.get("viewers") == 1
    await stream.aclose()
    assert rt.hub.viewers == 0
    assert rt.hub.subscribers == 1
    assert not rt.stopping.is_set()
    assert "user_absent" in _types(rt)
    rt.hub.unsubscribe(drain)
