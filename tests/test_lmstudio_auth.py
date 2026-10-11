"""LM Studio with "Require API key" on (SPEC §2.4) — every call carries LMSTUDIO_API_KEY.

A keyed LM Studio answers a bare request with 401, and every caller here is
best-effort, so a missing header never fails loudly: the context probe returns
zeros (she budgets for CONTEXT_LENGTH instead of the window she is loaded in),
pinning is skipped, the embedder falls back, and the availability check reads
the server as down — which, with llama.cpp installed, moves her turns off LM
Studio altogether. So each test runs against a server that refuses anything
without the right bearer, and asserts on what came back as well as what was sent.
"""
from __future__ import annotations

import json

import httpx
import pytest

from yurios.app.config import Config
from yurios.app.providers.lmstudio import (LMStudioEmbedder, auth_headers,
                                           ensure_resident, evict, probe_context)

KEY = "sk-lm-studio"
BASE = "http://lms.test:1234/v1"
QWEN = {"key": "qwen3.8-27b", "publisher": "qwen", "type": "llm",
        "max_context_length": 131072,
        "loaded_instances": [{"id": "qwen3.8-27b", "config": {"context_length": 22272}}]}


def keyed_server(models=(QWEN,)):
    """LM Studio with a key required: 401 without `Bearer KEY`, the developer
    API and /v1 otherwise. Records every request's path and Authorization."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization", "")))
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": "api key required"})
        path = request.url.path
        if path == "/api/v1/models":
            return httpx.Response(200, json={"models": list(models)})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m["key"]} for m in models]})
        if path == "/v1/embeddings":
            n = len(json.loads(request.content)["input"])
            return httpx.Response(200, json={"data": [
                {"index": i, "embedding": [0.0] * 4} for i in range(n)]})
        if path.endswith("/unload"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status": "loaded", "load_time_seconds": 1.0})

    t = httpx.MockTransport(handler)
    t.seen = seen
    return t


def test_no_key_means_no_header():
    assert auth_headers("") == {}
    assert auth_headers(KEY) == {"Authorization": f"Bearer {KEY}"}


def test_the_context_probe_reads_the_loaded_window_with_the_key():
    t = keyed_server()
    assert probe_context(BASE, "qwen3.8-27b", transport=t) == {"loaded": 0, "max": 0}
    assert probe_context(BASE, "qwen3.8-27b", api_key=KEY, transport=t) == \
        {"loaded": 22272, "max": 131072}


def test_pinning_lists_and_loads_with_the_key():
    unloaded = {**QWEN, "loaded_instances": []}
    t = keyed_server([unloaded])
    assert ensure_resident(BASE, ["qwen3.8-27b"], transport=t) == []
    t.seen.clear()
    assert ensure_resident(BASE, ["qwen3.8-27b"], api_key=KEY, transport=t) == \
        ["qwen3.8-27b"]
    assert ("/api/v1/models/load", f"Bearer {KEY}") in t.seen
    assert all(auth == f"Bearer {KEY}" for _, auth in t.seen)


def test_eviction_unloads_with_the_key():
    t = keyed_server()
    assert evict(BASE, ["qwen3.8-27b"], transport=t) == []
    assert evict(BASE, ["qwen3.8-27b"], api_key=KEY, transport=t) == ["qwen3.8-27b"]
    assert ("/api/v1/models/unload", f"Bearer {KEY}") in t.seen


def test_the_embedder_sends_the_key():
    t = keyed_server()
    with pytest.raises(httpx.HTTPStatusError):
        LMStudioEmbedder("nomic", 4, BASE, transport=t).embed(["x"])
    assert LMStudioEmbedder("nomic", 4, BASE, api_key=KEY, transport=t).embed(["x"]) == \
        [[0.0] * 4]


def _route_httpx_to(monkeypatch, t):
    """The two callers below build their own client; hand it the fake server."""
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=t, **kw))


def test_a_keyed_server_is_available_not_down(monkeypatch):
    """The check that decides whether her turns go to llama.cpp instead."""
    from yurios.app import main

    _route_httpx_to(monkeypatch, keyed_server())
    cfg = Config(_env_file=None, lmstudio_base_url=BASE)
    assert main._lmstudio_available(cfg) is False
    cfg.lmstudio_api_key = KEY
    assert main._lmstudio_available(cfg) is True


def test_the_model_check_authenticates_and_names_the_fix(monkeypatch):
    from yurios.models import probe_model

    _route_httpx_to(monkeypatch, keyed_server())
    cfg = Config(_env_file=None, lmstudio_base_url=BASE)
    refused = probe_model(cfg, "lm_studio/qwen3.8-27b")
    assert refused.ok is False and "LMSTUDIO_API_KEY" in refused.detail
    cfg.lmstudio_api_key = KEY
    assert probe_model(cfg, "lm_studio/qwen3.8-27b").ok is True


def test_her_chat_route_carries_the_key():
    from yurios.app.main import model_api_key

    cfg = Config(_env_file=None, lmstudio_api_key=KEY, openrouter_api_key="sk-or",
                 connection_api_key="sk-ollama-profile")
    assert model_api_key(cfg, "lm_studio/qwen3.8-27b") == KEY
    assert model_api_key(cfg, "ollama/llama3") == "sk-ollama-profile"
    assert model_api_key(cfg, "openrouter/x") == "sk-or"
