"""What a model request says about the app sending it: nothing (SPEC §3.1).

No route names YuriOS or files its usage under an app page — not the chat and
utility models on any route, not her camera. And the `openai_compatible/` route
speaks plain OpenAI to whatever server it is given, carrying that server's key
and no other. Offline — the transports are captured, or answered by a socket on
loopback; nothing here leaves the machine."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from yurios.app.providers.openrouter import LiteLLMChatModel, LiteLLMUtilityModel
from yurios.forge.backends.openrouter import OpenRouterBackend
from yurios.forge.types import GenRequest

#: Headers that file a request under an app, in any spelling OpenRouter reads.
_ATTRIBUTION = ("http-referer", "x-title", "x-openrouter-title", "x-openrouter-categories")


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


def _loopback(seen: dict) -> HTTPServer:
    """An OpenAI-shaped server on 127.0.0.1 that records the last request's headers."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            seen.clear()
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
    return server


async def _ask(model: str, *, api_key: str = "") -> dict:
    seen: dict = {}
    server = _loopback(seen)
    try:
        utility = LiteLLMUtilityModel(
            model, api_key=api_key,
            api_base=f"http://127.0.0.1:{server.server_port}/v1")
        assert await utility.complete([{"role": "user", "content": "hi"}]) == "ok"
    finally:
        server.shutdown()
    return seen


# ---- no attribution ---------------------------------------------------------

async def test_openrouter_calls_blank_litellm_s_attribution(acompletion):
    """LiteLLM merges these over its own `litellm.ai` / `liteLLM` defaults; empty
    is the only value that takes them off (it refuses None)."""
    await LiteLLMUtilityModel("some/model").complete([{"role": "user", "content": "hi"}])
    assert acompletion.kwargs["extra_headers"] == {"HTTP-Referer": "", "X-Title": ""}


async def test_local_routes_send_no_headers_at_all(acompletion):
    await LiteLLMUtilityModel("lm_studio/gemma").complete([{"role": "user", "content": "hi"}])
    assert "extra_headers" not in acompletion.kwargs


async def test_the_streaming_voice_is_blank_too(acompletion):
    """The reply voice is where the tokens actually are (§3)."""
    model = LiteLLMChatModel("some/model")
    assert [chunk async for chunk in model.stream([])] == []
    assert acompletion.kwargs["extra_headers"] == {"HTTP-Referer": "", "X-Title": ""}


async def test_nothing_names_the_app_on_the_openrouter_wire():
    """Not a monkeypatch, because the hazard lives BELOW our call: LiteLLM writes
    its own referer and title onto every openrouter/… request, and blanks
    winning over them is its behaviour, not a contract. A version bump that
    changed the merge would put `litellm.ai` back on the wire with every
    mock-based test above still green. Loopback only."""
    seen = await _ask("openrouter/test/model", api_key="sk-test")
    for name in _ATTRIBUTION:
        assert not seen.get(name), f"{name}: {seen.get(name)!r}"
    assert "yurios" not in seen.get("user-agent", "").lower()


def test_her_camera_names_no_app(monkeypatch):
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
    assert not set(_ATTRIBUTION) & set(lower)
    assert "yurios" not in lower.get("user-agent", "").lower()
    assert lower["authorization"] == "Bearer sk-or-test"


# ---- the openai_compatible/ route --------------------------------------------

async def test_a_gateway_id_rides_litellm_s_plain_openai_client(acompletion):
    model = LiteLLMUtilityModel("openai_compatible/venice-uncensored", api_key="sk-gw",
                                api_base="https://gw.example/v1")
    await model.complete([{"role": "user", "content": "hi"}])
    assert acompletion.kwargs["model"] == "hosted_vllm/venice-uncensored"
    assert acompletion.kwargs["api_base"] == "https://gw.example/v1"
    assert acompletion.kwargs["api_key"] == "sk-gw"
    assert "extra_headers" not in acompletion.kwargs


async def test_the_gateway_wire_carries_its_key_and_no_platform_report():
    """`openai/` would ride the OpenAI SDK, which signs every request with the
    OS, CPU and Python version (`x-stainless-*`)."""
    seen = await _ask("openai_compatible/some/model", api_key="sk-gw")
    assert seen["authorization"] == "Bearer sk-gw"
    assert not [name for name in seen if name.startswith("x-stainless")]
    for name in _ATTRIBUTION:
        assert name not in seen


async def test_a_keyless_gateway_is_never_sent_another_server_s_key(monkeypatch):
    """With no key of its own, `openai/` sends OPENAI_API_KEY and `hosted_vllm/`
    sends HOSTED_VLLM_API_KEY — to whatever server the url names. Neither may
    leave: the gateway gets the placeholder."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-leak")
    monkeypatch.setenv("HOSTED_VLLM_API_KEY", "sk-vllm-leak")
    seen = await _ask("openai_compatible/some/model")
    assert seen["authorization"] == "Bearer none"


async def test_a_gateway_thinks_off_without_touching_her_prompt(acompletion):
    """`/no_think` is a Qwen idiom; a gateway's model is anyone's guess, so it
    gets the OpenAI switch and her system message is left alone (SPEC §2.4)."""
    await _one_stream(LiteLLMChatModel("openai_compatible/some/model", thinking=False))
    assert acompletion.kwargs["extra_body"] == {"reasoning_effort": "none"}
    assert acompletion.kwargs["messages"][0]["content"] == "be her"


async def test_a_gateway_is_not_asked_for_stream_usage(acompletion):
    """`stream_options` is an allowlist: a server that rejects it fails the turn."""

    class Meter:
        def note_prompt(self, messages):
            pass

        def note_usage(self, tokens):
            pass

    await _one_stream(LiteLLMChatModel("openai_compatible/some/model", meter=Meter()))
    assert "stream_options" not in acompletion.kwargs


def test_the_route_s_base_and_key_are_its_own():
    from yurios.app.main import model_api_base, model_api_key
    from yurios.world.config import Config

    cfg = Config(_env_file=None, openrouter_api_key="sk-or", lmstudio_api_key="sk-lms",
                 openai_compatible_base_url="https://gw.example/v1",
                 openai_compatible_api_key="sk-gw")
    assert model_api_base(cfg, "openai_compatible/m") == "https://gw.example/v1"
    assert model_api_key(cfg, "openai_compatible/m") == "sk-gw"


# ---- thinking-off (SPEC §2.4) ------------------------------------------------

async def _one_stream(model):
    async for _ in model.stream([{"role": "system", "content": "be her"},
                                 {"role": "user", "content": "hi"}]):
        pass


async def test_openrouter_thinking_off_is_the_native_switch(acompletion):
    await _one_stream(LiteLLMChatModel(
        "openrouter/deepseek/deepseek-v4.1-flash", thinking=False,
        reasoning_effort="medium"))
    assert acompletion.kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    assert "/no_think" not in acompletion.kwargs["messages"][0]["content"]


async def test_openrouter_chat_reasoning_effort_reaches_the_raw_body(acompletion):
    await _one_stream(LiteLLMChatModel(
        "openrouter/deepseek/deepseek-v4.1-flash", thinking=True,
        reasoning_effort="medium"))
    assert acompletion.kwargs["extra_body"] == {"reasoning_effort": "medium"}


async def test_local_thinking_off_keeps_the_belt_and_braces(acompletion):
    await _one_stream(LiteLLMChatModel("lm_studio/some/qwen", thinking=False))
    assert acompletion.kwargs["extra_body"] == {"reasoning_effort": "none"}
    assert acompletion.kwargs["messages"][0]["content"].endswith("/no_think")


async def test_openrouter_utility_thinking_off_is_native_too(acompletion):
    model = LiteLLMUtilityModel("openrouter/deepseek/deepseek-v4.1-flash", thinking=False)
    await model.complete([{"role": "system", "content": "extract"},
                         {"role": "user", "content": "hi"}])
    assert acompletion.kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    assert "/no_think" not in acompletion.kwargs["messages"][0]["content"]
