"""evict() — the mirror of ensure_resident, unloading LM Studio models so the
local camera can borrow the GPU. Offline: httpx.MockTransport plays the server.
"""
from __future__ import annotations

import httpx

from yurios.app.providers.lmstudio import evict

BASE = "http://localhost:1234/v1"

CATALOG = {
    "models": [
        {"key": "gemma-4-e4b-uncensored-hauhaucs-aggressive",
         "publisher": "HauhauCS", "type": "llm",
         "loaded_instances": [{"id": "inst-chat-1"}, {"id": "inst-chat-2"}]},
        {"key": "text-embedding-nomic-embed-text-v1.5",
         "publisher": "nomic-ai", "type": "embedding",
         "loaded_instances": [{"id": "inst-embed"}]},
        {"key": "never-loaded-model", "publisher": "x", "type": "llm",
         "loaded_instances": []},
    ]
}


def server(catalog, posts):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": catalog})
        if request.url.path == "/api/v1/models/unload":
            import json
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": "unknown route"})
    return httpx.MockTransport(handler)


def test_every_loaded_instance_goes():
    posts = []
    out = evict(BASE, ["HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
                       "text-embedding-nomic-embed-text-v1.5"],
                transport=server(CATALOG["models"], posts))
    assert {p["instance_id"] for p in posts} == {
        "inst-chat-1", "inst-chat-2", "inst-embed"}
    assert set(out) == {"gemma-4-e4b-uncensored-hauhaucs-aggressive",
                        "text-embedding-nomic-embed-text-v1.5"}


def test_the_chat_id_resolves_by_its_publisher_stripped_tail():
    """CHAT_MODEL carries the publisher (`HauhauCS/gemma-4-…`); the catalog key
    doesn't. evict must match the way ensure_resident does."""
    posts = []
    out = evict(BASE, ["gemma-4-e4b-uncensored-hauhaucs-aggressive"],
                transport=server(CATALOG["models"], posts))
    assert [p["instance_id"] for p in posts] == ["inst-chat-1", "inst-chat-2"]
    assert out == ["gemma-4-e4b-uncensored-hauhaucs-aggressive"]


def test_a_model_with_nothing_loaded_is_a_skip_not_an_error():
    posts = []
    out = evict(BASE, ["never-loaded-model"], transport=server(CATALOG["models"], posts))
    assert posts == [] and out == []


def test_an_unknown_model_is_a_warning_not_a_crash(caplog):
    posts = []
    with caplog.at_level("WARNING"):
        out = evict(BASE, ["no/such-model"], transport=server(CATALOG["models"], posts))
    assert posts == [] and out == []
    assert any("no model matching" in r.message for r in caplog.records)


def test_an_unreachable_server_means_nothing_to_evict(caplog):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with caplog.at_level("WARNING"):
        out = evict(BASE, ["whatever"], transport=httpx.MockTransport(boom))
    assert out == []
    assert any("unavailable" in r.message for r in caplog.records)


def test_a_failed_unload_is_logged_and_the_rest_carry_on(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": CATALOG["models"]})
        return httpx.Response(500, text="stuck instance")

    with caplog.at_level("WARNING"):
        out = evict(BASE, ["text-embedding-nomic-embed-text-v1.5"],
                    transport=httpx.MockTransport(handler))
    assert out == []
    assert any("would not unload" in r.message for r in caplog.records)
