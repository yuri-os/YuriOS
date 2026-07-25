"""Keeping both LM Studio models resident (SPEC §3.1) — pinned over MockTransport.

The bug these guard against: chat and embeddings share one LM Studio server, and
its JIT loader unloads the previously JIT-loaded model to serve the next request,
so every turn evicted the other seam's model and paid a full reload. The cure is
an explicit, TTL-less load through the developer API — so what matters here is
WHICH calls go out, not just that something happened.
"""
from __future__ import annotations

import json

import httpx

from yurios.app.providers.lmstudio import (_api_root, _resolve_key,
                                           ensure_resident, probe_context)

GEMMA = {"key": "gemma-4-e4b-uncensored-hauhaucs-aggressive", "publisher": "HauhauCS",
         "type": "llm", "loaded_instances": []}
NOMIC = {"key": "text-embedding-nomic-embed-text-v1.5", "publisher": "nomic-ai",
         "type": "embedding", "loaded_instances": []}
QAT = {"key": "google/gemma-4-12b-qat", "publisher": "google", "type": "llm",
       "loaded_instances": []}


def transport(models):
    """The LM Studio developer API, recording every call it is asked to make."""
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": models})
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/unload"):
            return httpx.Response(200, json={"instance_id": body["instance_id"]})
        return httpx.Response(200, json={"status": "loaded", "load_time_seconds": 1.0})

    t = httpx.MockTransport(handler)
    t.calls = calls
    return t


# ---- the id alphabets -------------------------------------------------------

def test_resolve_key_accepts_every_id_shape():
    catalog = [GEMMA, NOMIC, QAT]
    # /v1/chat/completions takes the publisher-prefixed id; /models/load does not
    assert _resolve_key(catalog, "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive") \
        == GEMMA["key"]
    assert _resolve_key(catalog, "text-embedding-nomic-embed-text-v1.5") == NOMIC["key"]
    assert _resolve_key(catalog, "google/gemma-4-12b-qat") == QAT["key"]  # key IS prefixed
    assert _resolve_key(catalog, "gemma-4-12b-qat") == QAT["key"]
    assert _resolve_key(catalog, "not-downloaded") is None


def test_api_root_strips_the_openai_suffix():
    # LMSTUDIO_BASE_URL points at /v1; the developer API is a sibling, not a child
    assert _api_root("http://localhost:1234/v1") == "http://localhost:1234"
    assert _api_root("http://localhost:1234/v1/") == "http://localhost:1234"
    assert _api_root("http://localhost:1234") == "http://localhost:1234"


# ---- what actually goes over the wire ---------------------------------------

def test_unloaded_model_is_loaded_with_no_ttl():
    t = transport([GEMMA, NOMIC])
    ensure_resident("http://lms/v1", ["text-embedding-nomic-embed-text-v1.5"],
                    transport=t)
    assert t.calls == [("/api/v1/models/load", {"model": NOMIC["key"]})]
    # no ttl_seconds is the whole point: a TTL'd load expires and comes back
    # through the evicting JIT path
    assert "ttl_seconds" not in t.calls[0][1]


def test_pinned_model_is_left_alone():
    pinned = {**NOMIC, "loaded_instances": [{"id": NOMIC["key"], "config": {}}]}
    t = transport([pinned])
    ensure_resident("http://lms/v1", ["text-embedding-nomic-embed-text-v1.5"],
                    transport=t)
    assert t.calls == []                       # already resident, nothing to do


def test_ttl_instance_is_traded_for_a_pinned_one():
    jit = {**GEMMA, "loaded_instances": [
        {"id": GEMMA["key"], "config": {}, "remaining_ttl_seconds": 3559}]}
    t = transport([jit])
    ensure_resident("http://lms/v1", ["HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive"],
                    transport=t)
    # unload FIRST — loading on top would pay for the same 6 GB of weights twice
    assert t.calls == [("/api/v1/models/unload", {"instance_id": GEMMA["key"]}),
                       ("/api/v1/models/load", {"model": GEMMA["key"]})]


def test_chat_and_utility_being_the_same_model_loads_it_once():
    t = transport([GEMMA, NOMIC])
    same = "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive"
    ensure_resident("http://lms/v1", [same, same, NOMIC["key"]], transport=t)
    assert [c[1]["model"] for c in t.calls] == [GEMMA["key"], NOMIC["key"]]


# ---- the context window (SPEC §11) ------------------------------------------
#
# The bug THESE guard: LM Studio loads a model in its own per-model default
# window, which is routinely smaller than the model can do — so a long enough
# conversation dies on "Context size has been exceeded". CONTEXT_LENGTH is the
# cure, and it only works if the number actually reaches /models/load.

def test_context_length_rides_the_load():
    t = transport([GEMMA])
    ensure_resident("http://lms/v1", [GEMMA["key"]], context_length=32768,
                    transport=t)
    assert t.calls == [("/api/v1/models/load",
                        {"model": GEMMA["key"], "context_length": 32768})]


def test_no_context_length_asks_for_nothing():
    """0 = leave it to LM Studio: the old behaviour, byte for byte."""
    t = transport([GEMMA])
    ensure_resident("http://lms/v1", [GEMMA["key"]], transport=t)
    assert t.calls == [("/api/v1/models/load", {"model": GEMMA["key"]})]


def test_an_embedder_never_gets_a_chat_window():
    """An embedding model has no conversation to hold — its own window is right."""
    t = transport([NOMIC])
    ensure_resident("http://lms/v1", [NOMIC["key"]], context_length=32768,
                    transport=t)
    assert t.calls == [("/api/v1/models/load", {"model": NOMIC["key"]})]


def test_a_model_pinned_too_small_is_reloaded_bigger():
    # LM Studio reports the loaded window inside the instance's `config` block
    small = {**GEMMA, "loaded_instances": [
        {"id": GEMMA["key"], "config": {"context_length": 4096}}]}
    t = transport([small])
    ensure_resident("http://lms/v1", [GEMMA["key"]], context_length=32768,
                    transport=t)
    assert t.calls == [("/api/v1/models/unload", {"instance_id": GEMMA["key"]}),
                       ("/api/v1/models/load",
                        {"model": GEMMA["key"], "context_length": 32768})]


def test_a_model_already_big_enough_is_left_alone():
    big = {**GEMMA, "loaded_instances": [
        {"id": GEMMA["key"], "config": {"context_length": 65536}}]}
    t = transport([big])
    ensure_resident("http://lms/v1", [GEMMA["key"]], context_length=32768,
                    transport=t)
    assert t.calls == []          # reloading 6 GB of weights to shrink is madness


def test_a_server_that_does_not_report_the_window_is_not_churned():
    """No context_length in the instance = an older LM Studio. Reloading on every
    boot to chase a number it will never report would be a permanent tax."""
    t = transport([{**GEMMA, "loaded_instances": [{"id": GEMMA["key"], "config": {}}]}])
    ensure_resident("http://lms/v1", [GEMMA["key"]], context_length=32768,
                    transport=t)
    assert t.calls == []


def test_probe_reports_the_loaded_window_and_the_ceiling_separately():
    """The two are 4× apart here and 30× apart in the wild — the gauge wants the
    window she's IN, not the one the model could have had."""
    entry = {**GEMMA, "max_context_length": 131072,
             "loaded_instances": [{"id": GEMMA["key"],
                                   "config": {"context_length": 32768}}]}
    found = probe_context("http://lms/v1", GEMMA["key"], transport=transport([entry]))
    assert found == {"loaded": 32768, "max": 131072}


def test_probe_of_an_unloaded_model_reports_no_window():
    found = probe_context("http://lms/v1", GEMMA["key"],
                          transport=transport([{**GEMMA, "max_context_length": 262144}]))
    assert found == {"loaded": 0, "max": 262144}


def test_probe_survives_a_dead_server():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    assert probe_context("http://lms/v1", GEMMA["key"],
                         transport=httpx.MockTransport(refuse)) == {"loaded": 0, "max": 0}


# ---- best-effort: she boots either way --------------------------------------

def test_unknown_model_is_skipped_not_fatal():
    t = transport([GEMMA])
    ensure_resident("http://lms/v1", ["never-downloaded", GEMMA["key"]], transport=t)
    assert t.calls == [("/api/v1/models/load", {"model": GEMMA["key"]})]


def test_unreachable_server_does_not_raise():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    ensure_resident("http://lms/v1", [GEMMA["key"]],
                    transport=httpx.MockTransport(refuse))   # no exception


def test_load_failure_does_not_raise():
    """Guardrails refusing a model that will not fit is a warning, not a crash."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [GEMMA]})
        return httpx.Response(400, json={"error": {"message": "insufficient memory"}})
    ensure_resident("http://lms/v1", [GEMMA["key"]],
                    transport=httpx.MockTransport(handler))  # no exception


# ---- which seams get asked for at all (app.main) -----------------------------

def asked_for(monkeypatch, cfg, **built):
    from yurios.app import main
    from yurios.app.providers import lmstudio

    seen: list[list[str]] = []
    monkeypatch.setattr(lmstudio, "ensure_resident",
                        lambda base, ids, **kw: seen.append(ids))
    main._preload_lmstudio(cfg, **built)
    return seen[0] if seen else []


def test_preload_covers_the_reply_voice_and_the_embedder(monkeypatch):
    from yurios.app.config import Config
    cfg = Config(chat_model="lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
                 utility_model="lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
                 embed_backend="lm_studio", embed_model=NOMIC["key"])
    # the lm_studio/ prefix is a route, not part of the id LM Studio knows
    assert asked_for(monkeypatch, cfg, chat=True, embed=True) == [
        NOMIC["key"],
        "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
        "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive"]


def test_injected_providers_never_touch_the_server(monkeypatch):
    """The test suite builds apps with fakes — nothing may reach localhost:1234."""
    from yurios.app.config import Config
    cfg = Config(chat_model="lm_studio/x", embed_backend="lm_studio")
    assert asked_for(monkeypatch, cfg, chat=False, embed=False) == []


def test_no_lmstudio_seam_asks_for_nothing(monkeypatch):
    from yurios.app.config import Config
    cfg = Config(chat_model="openrouter/anthropic/claude-sonnet-5",
                 utility_model="openrouter/anthropic/claude-sonnet-5",
                 embed_backend="sentence_tf")
    assert asked_for(monkeypatch, cfg, chat=True, embed=True) == []
