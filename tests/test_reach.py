"""Reaching her model's server, and starting a local one (SPEC §31.5).

Every probe here runs on an httpx.MockTransport and every start on a fake
launcher — conftest's `no_model_servers` already refuses the real ones, so a
case that forgot its transport fails as "down" rather than asking this
machine's LM Studio."""
from __future__ import annotations

import httpx
import pytest

from yurios.app.config import Config
from yurios.app.providers import reach
from yurios.app.providers.reach import DOWN, MISSING, OK, REFUSED, UNCHECKED, Link


def cfg(**kw) -> Config:
    base = dict(chat_model="lm_studio/qwen/qwen3-14b",
                utility_model="lm_studio/qwen/qwen3-14b",
                utility_enabled=True, embed_backend="sentence_tf")
    base.update(kw)
    return Config(_env_file=None, **base)


def server(routes: dict[str, tuple[int, dict]]):
    """A transport answering *routes* by path; anything else is no server."""
    def handle(request: httpx.Request) -> httpx.Response:
        hit = routes.get(request.url.path)
        if hit is None:
            raise httpx.ConnectError("nothing there", request=request)
        status, body = hit
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handle)


LMSTUDIO_UP = {"/v1/models": (200, {"data": [{"id": "qwen/qwen3-14b"}]}),
               "/api/v1/models": (200, {"models": [{"key": "qwen3-14b",
                                                     "publisher": "qwen"}]})}


# ---- which servers ----------------------------------------------------------

def test_chat_and_utility_on_one_server_are_one_dependency():
    links = reach.links_for(cfg())
    assert len(links) == 1
    assert links[0].provider == "lmstudio"
    assert links[0].roles == ["chat", "utility"]
    assert links[0].models == ["qwen/qwen3-14b"]


def test_a_server_backed_embedder_joins_its_server():
    links = reach.links_for(cfg(embed_backend="lm_studio",
                                embed_model="text-embedding-nomic-embed-text-v1.5"))
    assert links[0].roles == ["chat", "utility", "memory"]
    assert "text-embedding-nomic-embed-text-v1.5" in links[0].models


def test_routes_with_no_server_of_their_own_are_not_asked():
    assert reach.links_for(cfg(chat_model="NONE", utility_model="NONE")) == []
    assert reach.links_for(cfg(chat_model="gguf/some/repo", utility_enabled=False)) == []


def test_only_the_seams_being_built_are_asked():
    """An injected model reaches no server — a test injecting one asks none."""
    assert reach.links_for(cfg(), chat=False, utility=False, embed=False) == []


def test_a_bare_id_is_openrouter_and_a_vendor_route_is_unchecked():
    [link] = reach.links_for(cfg(chat_model="z-ai/glm-5", utility_enabled=False,
                                 openrouter_api_key="sk-or"))
    assert (link.provider, link.models, link.api_key) == ("openrouter", ["z-ai/glm-5"], "sk-or")
    [link] = reach.links_for(cfg(chat_model="anthropic/claude-x", utility_enabled=False))
    assert reach.probe(link).state == UNCHECKED


# ---- asking -----------------------------------------------------------------

def test_a_running_lm_studio_with_her_model_is_ok():
    [link] = reach.links_for(cfg())
    assert reach.probe(link, transport=server(LMSTUDIO_UP)).state == OK


def test_a_closed_local_server_says_it_is_not_running():
    [link] = reach.links_for(cfg())
    got = reach.probe(link, transport=server({}))
    assert got.state == DOWN
    assert "isn't answering at localhost:1234" in got.detail
    assert "isn't running" in got.detail


def test_a_401_is_a_key_not_a_missing_server():
    """§31.1: a server asking for a key is there."""
    [link] = reach.links_for(cfg())
    got = reach.probe(link, transport=server({"/v1/models": (401, {})}))
    assert got.state == REFUSED and "requires an API key" in got.detail
    [keyed] = reach.links_for(cfg(lmstudio_api_key="wrong"))
    got = reach.probe(keyed, transport=server({"/v1/models": (403, {})}))
    assert got.state == REFUSED and "refused the API key" in got.detail


def test_a_model_the_server_does_not_have_is_named():
    [link] = reach.links_for(cfg(chat_model="lm_studio/nobody/nothing",
                                 utility_enabled=False))
    got = reach.probe(link, transport=server(LMSTUDIO_UP))
    assert got.state == MISSING and "'nobody/nothing'" in got.detail


def test_lm_studio_models_on_disk_count_though_not_loaded():
    """With JIT off /v1/models lists only what is loaded — and nothing is,
    before the pin. The developer catalog is what is on disk."""
    [link] = reach.links_for(cfg())
    routes = {"/v1/models": (200, {"data": [{"id": "something-else"}]}),
              "/api/v1/models": (200, {"models": [{"key": "qwen3-14b"}]})}
    assert reach.probe(link, transport=server(routes)).state == OK


def test_an_empty_listing_is_not_a_missing_model():
    [link] = reach.links_for(cfg())
    routes = {"/v1/models": (200, {"data": []})}
    assert reach.probe(link, transport=server(routes)).state == OK


def test_ollama_matches_through_latest():
    [link] = reach.links_for(cfg(chat_model="ollama/qwen3", utility_enabled=False))
    routes = {"/api/tags": (200, {"models": [{"name": "qwen3:latest"}]})}
    assert reach.probe(link, transport=server(routes)).state == OK


def test_openrouter_checks_the_key():
    [link] = reach.links_for(cfg(chat_model="openrouter/x/y", utility_enabled=False))
    assert reach.probe(link).state == REFUSED            # no key at all
    keyed = Link(provider="openrouter", endpoint="", models=["x/y"], api_key="sk")
    assert reach.probe(keyed, transport=server({"/api/v1/key": (200, {"data": {}})})).state == OK
    assert reach.probe(keyed, transport=server({"/api/v1/key": (401, {})})).state == REFUSED
    assert reach.probe(keyed, transport=server({})).state == DOWN


def test_the_wire_shape_never_carries_the_key():
    link = Link(provider="lmstudio", endpoint="http://localhost:1234/v1", api_key="secret")
    out = link.as_dict()
    assert "secret" not in str(out)
    assert out["where"] == "localhost:1234" and out["local"] is True


def test_worst_orders_down_before_missing():
    links = [Link("ollama", "x", state=MISSING), Link("lmstudio", "y", state=DOWN)]
    assert reach.worst(links) == DOWN
    assert reach.worst([]) == OK


# ---- starting -----------------------------------------------------------------

class Machine:
    """A launcher that 'starts' the server: after it runs, the transport answers."""

    def __init__(self, routes=LMSTUDIO_UP, fail: str = ""):
        self.up = False
        self.calls: list[tuple[list[str], dict, bool]] = []
        self.fail = fail
        self.routes = routes

    def launch(self, argv, env, detach):
        self.calls.append((argv, env, detach))
        if self.fail:
            raise RuntimeError(self.fail)
        self.up = True

    @property
    def transport(self):
        def handle(request):
            hit = self.routes.get(request.url.path) if self.up else None
            if hit is None:
                raise httpx.ConnectError("closed", request=request)
            return httpx.Response(hit[0], json=hit[1])
        return httpx.MockTransport(handle)


@pytest.fixture
def lms(monkeypatch):
    monkeypatch.setattr(reach, "_lms_binary", lambda: "/opt/lms")
    monkeypatch.setattr(reach.shutil, "which",
                        lambda name: "/usr/bin/ollama" if name == "ollama" else None)


def test_a_local_lm_studio_is_started_on_its_own_port(lms):
    machine = Machine()
    [link] = reach.links_for(cfg(lmstudio_base_url="http://127.0.0.1:4321/v1"))
    assert link.startable
    got = reach.start(link, launcher=machine.launch, transport=machine.transport,
                      sleep=lambda s: None)
    assert got.state == OK
    argv, _, detach = machine.calls[0]
    assert argv == ["/opt/lms", "server", "start", "--port", "4321"]
    assert detach is False


def test_ollama_serve_is_detached_on_the_configured_host(lms):
    machine = Machine(routes={"/api/tags": (200, {"models": [{"name": "qwen3"}]})})
    [link] = reach.links_for(cfg(chat_model="ollama/qwen3", utility_enabled=False,
                                 ollama_base_url="http://localhost:11500"))
    got = reach.start(link, launcher=machine.launch, transport=machine.transport,
                      sleep=lambda s: None)
    assert got.state == OK
    argv, env, detach = machine.calls[0]
    assert argv == ["/usr/bin/ollama", "serve"] and detach is True
    assert env["OLLAMA_HOST"] == "localhost:11500"


def test_a_remote_server_is_never_started(lms):
    machine = Machine()
    [link] = reach.links_for(cfg(lmstudio_base_url="http://10.0.0.5:1234/v1"))
    assert not link.startable
    got = reach.start(link, launcher=machine.launch, transport=machine.transport)
    assert machine.calls == []
    assert got.state == DOWN and "not on this machine" in got.detail


def test_no_command_found_says_so(monkeypatch):
    monkeypatch.setattr(reach, "_lms_binary", lambda: None)
    [link] = reach.links_for(cfg())
    assert not link.startable
    got = reach.start(link, transport=server({}))
    assert "no command to start the LM Studio" in got.detail


def test_a_start_that_fails_says_why(lms):
    machine = Machine(fail="LM Studio is not installed")
    [link] = reach.links_for(cfg())
    got = reach.start(link, launcher=machine.launch, transport=machine.transport)
    assert got.state == DOWN and "would not start: LM Studio is not installed" in got.detail


def test_a_server_that_never_answers_gives_up_at_the_deadline(lms):
    clock = [0.0]
    [link] = reach.links_for(cfg())
    got = reach.start(link, launcher=lambda *a: None, transport=server({}),
                      wait_s=5, poll_s=1, sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                      monotonic=lambda: clock[0])
    assert got.state == DOWN and "was started but still isn't answering" in got.detail


def test_a_server_already_up_is_not_started_again(lms):
    machine = Machine()
    machine.up = True
    [link] = reach.links_for(cfg())
    assert reach.start(link, launcher=machine.launch,
                       transport=machine.transport).state == OK
    assert machine.calls == []
