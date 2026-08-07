"""The search seam (SPEC §7.7) — the parser pinned over httpx.MockTransport."""
from __future__ import annotations

import json

import httpx
import pytest

from yurios.world.tools.search import (SNIPPET_MAX_CHARS, FakeSearch,
                                       SearxngProvider, build_provider)

HITS = {"results": [
    {"title": "Tea", "url": "https://a.example/tea", "content": "About tea."},
    {"title": "More tea", "url": "https://b.example/tea", "content": "Also tea."},
]}


def transport(payload=HITS, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=json.dumps(payload))
    return httpx.MockTransport(handler)


async def test_results_map_to_title_url_snippet():
    out = await SearxngProvider("http://searx.local",
                                transport=transport()).search("tea", 5)
    assert out == [
        {"title": "Tea", "url": "https://a.example/tea", "snippet": "About tea."},
        {"title": "More tea", "url": "https://b.example/tea", "snippet": "Also tea."},
    ]


async def test_the_query_reaches_the_instance_as_json_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        seen["path"] = request.url.path
        return httpx.Response(200, text=json.dumps(HITS))

    await SearxngProvider("http://searx.local/",          # trailing slash trimmed
                          language="ja", safesearch=2,
                          transport=httpx.MockTransport(handler)).search("tea", 3)
    assert seen["path"] == "/search"
    assert seen["format"] == "json" and seen["q"] == "tea"
    assert seen["language"] == "ja" and seen["safesearch"] == "2"


async def test_a_403_names_the_disabled_json_format():
    """The one failure worth a sentence: SearXNG ships with json off, and the
    status code alone sends you looking at authentication instead."""
    provider = SearxngProvider("http://searx.local", transport=transport(status=403))
    with pytest.raises(RuntimeError, match="settings.yml"):
        await provider.search("tea", 3)


async def test_other_http_errors_still_raise():
    provider = SearxngProvider("http://searx.local", transport=transport(status=500))
    with pytest.raises(httpx.HTTPStatusError):
        await provider.search("tea", 3)


async def test_a_row_with_no_url_does_not_cost_a_real_result():
    payload = {"results": [{"title": "junk", "url": "", "content": "x"},
                           *HITS["results"]]}
    out = await SearxngProvider("http://searx.local",
                                transport=transport(payload)).search("tea", 1)
    assert [r["url"] for r in out] == ["https://a.example/tea"]


async def test_long_snippets_are_trimmed_before_the_model_sees_them():
    payload = {"results": [{"title": "T", "url": "https://a.example",
                            "content": "x" * 900}]}
    out = await SearxngProvider("http://searx.local",
                                transport=transport(payload)).search("tea", 1)
    assert len(out[0]["snippet"]) <= SNIPPET_MAX_CHARS
    assert out[0]["snippet"].endswith("…")


async def test_an_empty_query_is_refused_before_the_network():
    def explode(request):                       # never reached
        raise AssertionError("asked the instance about nothing")
    provider = SearxngProvider("http://searx.local",
                               transport=httpx.MockTransport(explode))
    with pytest.raises(ValueError, match="empty"):
        await provider.search("   ", 3)


async def test_fake_search_is_deterministic_and_respects_k():
    a = await FakeSearch().search("tea", 2)
    b = await FakeSearch().search("tea", 2)
    assert a == b and len(a) == 2
    assert all(r["url"].startswith("https://example.invalid/") for r in a)


def test_build_provider_off_means_no_provider_at_all():
    """`off` returning None is what keeps the tools unadvertised (§7.7)."""
    assert build_provider("off", base_url="http://x") is None
    assert isinstance(build_provider("fake", base_url="http://x"), FakeSearch)
    assert isinstance(build_provider("searxng", base_url="http://x"),
                      SearxngProvider)
