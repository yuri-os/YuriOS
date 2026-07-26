"""App attribution (https://openrouter.ai/docs/app-attribution).

OpenRouter files usage under the app whose `HTTP-Referer` it saw. These tests pin
the two things that make YuriOS's usage show up on one page instead of nowhere:
every billed OpenRouter caller sends the identity, and all of them send the SAME
url. Offline — the transports are captured, or answered by a socket on loopback;
nothing here leaves the machine."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from yurios import attribution
from yurios.app.providers.openrouter import LiteLLMChatModel, LiteLLMUtilityModel
from yurios.forge.backends.openrouter import OpenRouterBackend
from yurios.forge.types import GenRequest


class FakeCompletion:
    """Stand-in for litellm.acompletion: records the call, answers emptily —
    streaming or not, since the two model classes take different exits."""

    def __init__(self):
        self.kwargs: dict = {}

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            return _no_chunks()

        class Msg:
            content = "ok"

        class Choice:
            message = Msg()

        return type("R", (), {"choices": [Choice()]})()


async def _no_chunks():
    return
    yield                                       # pragma: no cover — empty stream


@pytest.fixture
def acompletion(monkeypatch) -> FakeCompletion:
    fake = FakeCompletion()
    monkeypatch.setattr("litellm.acompletion", fake)
    return fake


async def test_the_chat_path_names_the_app(acompletion):
    await LiteLLMUtilityModel("some/model").complete([{"role": "user", "content": "hi"}])
    headers = acompletion.kwargs["extra_headers"]
    assert headers["HTTP-Referer"] == attribution.APP_URL
    assert headers["X-OpenRouter-Title"] == "YuriOS"
    # the legacy name too: LiteLLM's own `X-Title: liteLLM` default would ride
    # along otherwise and the app could come back labelled as LiteLLM
    assert headers["X-Title"] == "YuriOS"


async def test_the_user_agent_puts_yurios_before_its_plumbing(acompletion):
    """`YuriOS/0.1.0 litellm/1.93.0`: our name first, the library that actually
    holds the socket second. Unset, a provider's logs say only `litellm/…`."""
    await LiteLLMUtilityModel("some/model").complete([{"role": "user", "content": "hi"}])
    agent = acompletion.kwargs["extra_headers"]["User-Agent"]
    assert agent.startswith(f"YuriOS/{attribution.APP_VERSION} ")
    assert agent.split()[1].startswith("litellm/")


async def test_local_routes_send_no_attribution(acompletion):
    """ollama / lm_studio are someone's own machine — nothing to attribute."""
    await LiteLLMUtilityModel("lm_studio/gemma").complete([{"role": "user", "content": "hi"}])
    assert "extra_headers" not in acompletion.kwargs


async def test_the_streaming_voice_carries_it_too(acompletion):
    """The reply voice is where the tokens actually are (§3)."""
    model = LiteLLMChatModel("some/model")
    assert [chunk async for chunk in model.stream([])] == []
    assert acompletion.kwargs["extra_headers"]["HTTP-Referer"] == attribution.APP_URL


def test_her_camera_is_attributed_to_the_same_app(monkeypatch):
    """Selfies are billed OpenRouter calls; unattributed, the image spend never
    reaches the app page — and a *different* url would open a second one."""
    seen: dict = {}

    class FakeResponse:                         # all json.load() wants is .read()
        def read(self):
            return json.dumps({"choices": [{"message": {"images": [
                {"image_url": {"url": "data:image/png;base64,aGk="}}]}}]}).encode()

    def fake_urlopen(req, timeout=None):
        seen.update(req.headers)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    OpenRouterBackend(api_key="sk-or-test").generate(GenRequest(prompt="a selfie"))

    # urllib title-cases header names on the way in
    lower = {k.lower(): v for k, v in seen.items()}
    assert lower["http-referer"] == attribution.APP_URL
    assert lower["x-openrouter-title"] == "YuriOS"
    assert "image-gen" in lower["x-openrouter-categories"]
    # urllib would otherwise sign the request `Python-urllib/3.12` alone
    assert lower["user-agent"].startswith("YuriOS/")
    assert "Python-urllib/" in lower["user-agent"]


def test_categories_stay_inside_openrouter_s_per_request_limit():
    """Two per request is the documented cap; a third is not silently sent."""
    sent = attribution.headers(categories=("roleplay", "personal-agent", "game"))
    assert sent["X-OpenRouter-Categories"].split(",") == ["roleplay", "personal-agent"]


async def test_the_headers_survive_litellm_all_the_way_to_the_socket():
    """The one test that isn't a monkeypatch, because the two hazards it covers
    both live BELOW our call: LiteLLM writes its own `X-Title: liteLLM` and its own
    `user-agent: litellm/…` onto every openrouter/… request, and `extra_headers`
    winning over both is its behaviour, not a contract. A version bump that changed
    the precedence would revert us silently — the app relabelled, our name gone from
    the logs — with every mock-based test above still green. Loopback only."""
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            seen.update({k.lower(): v for k, v in self.headers.items()})
            body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant",
                                                        "content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):           # keep pytest's output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        model = LiteLLMUtilityModel(
            "openrouter/test/model", api_key="sk-test",
            api_base=f"http://127.0.0.1:{server.server_port}/v1")
        assert await model.complete([{"role": "user", "content": "hi"}]) == "ok"
    finally:
        server.shutdown()

    assert seen["http-referer"] == attribution.APP_URL
    assert seen["x-openrouter-title"] == "YuriOS"
    assert seen["x-title"] == "YuriOS"              # not "liteLLM"
    assert seen["user-agent"].startswith("YuriOS/") # not "litellm/1.93.0"
