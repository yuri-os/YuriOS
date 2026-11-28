"""Her connection watch: boot, the banner's event, failed turns, recovery
(SPEC §31.5). The servers are fakes behind reach's transport and launcher
seams; conftest's `no_model_servers` refuses everything a case does not
replace."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from yurios.app.config import Config
from yurios.app.providers import reach
from yurios.desktop.voice.backends.fakes import FakeBrain
from yurios.kernel.hub import EventHub
from yurios.world.boot import BootBoard
from yurios.world.connection import ConnectionWatch
from yurios.world.main import create_app


class Machine:
    """One LM Studio on this box: closed until the launcher runs."""

    def __init__(self, up: bool = False):
        self.up = up
        self.starts = 0

    def launch(self, argv, env, detach):
        self.starts += 1
        self.up = True

    def transport(self):
        def handle(request):
            if not self.up:
                raise httpx.ConnectError("closed", request=request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "qwen/qwen3-14b"}]})
            raise httpx.ConnectError("no developer api", request=request)
        return httpx.MockTransport(handle)


@pytest.fixture
def machine(monkeypatch):
    m = Machine()
    monkeypatch.setattr(reach, "default_transport", m.transport)
    monkeypatch.setattr(reach, "default_launcher", lambda: m.launch)
    monkeypatch.setattr(reach, "_lms_binary", lambda: "/opt/lms")
    monkeypatch.setattr("yurios.world.connection.START_WAIT_S", 1.0)
    return m


def cfg(**kw) -> Config:
    base = dict(chat_model="lm_studio/qwen/qwen3-14b", utility_enabled=False,
                embed_backend="sentence_tf")
    base.update(kw)
    return Config(_env_file=None, **base)


def sticky(hub: EventHub) -> dict:
    return hub.sticky["connection"]


def test_boot_starts_a_closed_local_server_before_anything_is_built(machine):
    hub, board = EventHub(), BootBoard()
    watch = ConnectionWatch(cfg(), hub)
    watch.boot(chat=True, utility=True, embed=True, board=board, autostart=True)
    assert machine.starts == 1
    assert sticky(hub)["state"] == "ok"
    line = next(s for s in board.snapshot()["services"] if s["key"] == "server")
    assert line["state"] == "ready" and "LM Studio" in line["detail"]


def test_boot_with_autostart_off_only_reports(machine):
    hub, board = EventHub(), BootBoard()
    watch = ConnectionWatch(cfg(), hub, fallback=lambda: "on the GGUF runner")
    watch.boot(chat=True, utility=True, embed=True, board=board, autostart=False)
    assert machine.starts == 0
    snap = sticky(hub)
    assert snap["state"] == "down"
    assert "isn't answering at localhost:1234" in snap["detail"]
    assert snap["links"][0]["startable"] is True
    assert snap["fallback"] == "on the GGUF runner"
    line = next(s for s in board.snapshot()["services"] if s["key"] == "server")
    assert line["state"] == "failed"
    assert watch.degradations() and "model server" in watch.degradations()[0]


def test_injected_models_reach_no_server(machine):
    hub = EventHub()
    watch = ConnectionWatch(cfg(), hub)
    watch.boot(chat=False, utility=False, embed=False, autostart=True)
    assert machine.starts == 0 and not watch.tracking
    assert sticky(hub)["state"] == "ok"


class ModelError(Exception):
    pass


ModelError.__module__ = "litellm.exceptions"


async def test_a_failed_turn_is_said_and_a_good_one_clears_it(machine):
    machine.up = True
    hub = EventHub()
    watch = ConnectionWatch(cfg(), hub)
    watch.boot(chat=True, utility=False, embed=False, autostart=False)
    watch.turn_failed(ValueError("a tool broke"))       # not the model's doing
    assert sticky(hub)["state"] == "ok"
    watch.turn_failed(ModelError("model 'qwen' not loaded"))
    snap = watch.snapshot()
    assert snap["state"] == "failed"
    assert "her last reply failed: model 'qwen' not loaded" in snap["detail"]
    watch.turn_ok()
    assert sticky(hub)["state"] == "ok"


def test_a_failure_wrapped_in_another_is_still_the_models(machine):
    machine.up = True
    watch = ConnectionWatch(cfg(), EventHub())
    watch.boot(chat=True, utility=False, embed=False, autostart=False)
    try:
        try:
            raise ModelError("connection refused")
        except ModelError as e:
            raise RuntimeError("turn failed") from e
    except RuntimeError as outer:
        watch.turn_failed(outer)
    snap = watch.snapshot()
    assert snap["state"] == "failed"
    assert snap["detail"] == "her last reply failed: connection refused"


async def test_a_down_server_is_watched_until_it_answers(machine):
    hub = EventHub()
    recovered = []

    async def on_back(links):
        recovered.append([li.provider for li in links])

    watch = ConnectionWatch(cfg(), hub, on_recovered=on_back, interval_s=0.01)
    watch.boot(chat=True, utility=False, embed=False, autostart=False)
    assert sticky(hub)["state"] == "down"
    watch.bind(asyncio.get_running_loop())
    await asyncio.sleep(0.03)
    machine.up = True                       # someone opened LM Studio by hand
    for _ in range(100):
        if recovered:
            break
        await asyncio.sleep(0.01)
    assert recovered == [["lmstudio"]]
    assert sticky(hub)["state"] == "ok"
    await asyncio.sleep(0.03)
    assert watch._watch is None or watch._watch.done()    # nothing left to watch
    await watch.stop()


async def test_the_start_button_starts_it_and_recovers(machine):
    hub = EventHub()
    recovered = []

    async def on_back(links):
        recovered.append(True)

    watch = ConnectionWatch(cfg(), hub, on_recovered=on_back)
    watch.boot(chat=True, utility=False, embed=False, autostart=False)
    snap = await watch.start()
    assert machine.starts == 1 and snap["state"] == "ok" and recovered == [True]
    assert snap["busy"] == ""
    await watch.stop()


async def test_a_retune_onto_another_server_is_asked_about_that_one(machine):
    machine.up = True
    c = cfg()
    watch = ConnectionWatch(c, EventHub())
    watch.boot(chat=True, utility=False, embed=False, autostart=False)
    c.chat_model = "ollama/qwen3"           # the live Config is mutated in place (§31.4)
    snap = await watch.recheck()
    assert snap["links"][0]["provider"] == "ollama"
    assert snap["state"] == "down"
    await watch.stop()


async def test_recovery_moves_her_off_the_gguf_fallback(monkeypatch):
    """The runtime's half: a provider built while LM Studio was away is the
    fallback, and stays one unless something rebuilds it."""
    from yurios.world import main as world_main
    from yurios.world import rewire

    class GGUFChatModel:
        pass

    rebuilt, pinned = [], []
    state = SimpleNamespace(chat=GGUFChatModel())
    fake = SimpleNamespace(
        _owns_models=True, brain=SimpleNamespace(state=state),
        cfg=SimpleNamespace(chat_model="lm_studio/qwen", lmstudio_preload=True,
                            embed_backend="sentence_tf"),
        context=None, boot=BootBoard())
    fake._fallback_line = lambda: world_main.Runtime._fallback_line(fake)
    monkeypatch.setattr(rewire, "rebuild",
                        lambda st, c, meter=None: rebuilt.append(True))
    monkeypatch.setattr("yurios.app.main._preload_lmstudio",
                        lambda c, chat, embed: pinned.append((chat, embed)) or ["qwen"])
    monkeypatch.setattr(world_main.runtime, "probe_context_window", lambda rt, c: "")
    assert fake._fallback_line()
    await world_main.Runtime._server_recovered(fake, [reach.Link("lmstudio", "x")])
    assert rebuilt == [True] and pinned == [(True, False)]


# ---- the routes -----------------------------------------------------------------

@pytest.fixture
def client(cfg, machine):
    app = create_app(cfg.model_copy(update={"tools_backend": "off",
                                            "mind_enabled": False,
                                            "chat_model": "lm_studio/qwen/qwen3-14b"}),
                     brain=FakeBrain())
    with TestClient(app) as c:
        rt = c.app.state.rt
        rt.connection.boot(chat=True, utility=False, embed=False, autostart=False)
        yield c


def test_health_names_a_down_server(client):
    body = client.get("/api/health").json()
    assert body["ok"] is False
    assert any(d.startswith("model server:") for d in body["degraded"])
    assert body["connection"]["state"] == "down"


def test_the_routes_check_and_start(client, machine):
    assert client.get("/api/connection").json()["state"] == "down"
    assert client.post("/api/connection/check").json()["state"] == "down"
    snap = client.post("/api/connection/start").json()
    assert machine.starts == 1 and snap["state"] == "ok"
    assert client.get("/api/health").json()["connection"]["state"] == "ok"


def test_starting_is_the_owners_alone(client, monkeypatch):
    monkeypatch.setattr("yurios.desktop.routes.settings.owner_or_loopback",
                        lambda request: False)
    assert client.post("/api/connection/start").status_code == 403
    assert client.post("/api/connection/check").status_code == 200
