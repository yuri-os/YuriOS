"""The page-fetch seam (SPEC §7.7).

Two halves, and the first one matters more: `read_page` takes a URL a language
model wrote, so the tests that count are the ones proving it won't follow that
URL into the machine it is running on.
"""
from __future__ import annotations

import httpx
import pytest

from yurios.world.tools.fetch import (FakeFetcher, HttpFetcher, UnsafeURL,
                                      build_fetcher, check_url, extract, gist)

PAGE = """<html><head><title> Tea &amp; Cake </title></head><body>
<nav>menu menu</nav><script>var tracking = 1;</script><style>p{color:red}</style>
<h1>On Tea</h1>
<p>Tea is a   drink.
It is warm.</p>
<div><p>Second paragraph.</p></div>
<footer>copyright</footer></body></html>"""


def public(*addrs):
    """A resolver that answers with public addresses — the DNS seam only."""
    async def resolve(host, port):
        return list(addrs) or ["93.184.216.34"]
    return resolve


def pointing_at(addr):
    async def resolve(host, port):
        return [addr]
    return resolve


# ---------------------------------------------------------------- the guard

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "javascript:alert(1)",
    "data:text/html,<b>hi</b>",
])
async def test_only_http_and_https_can_be_read(url):
    with pytest.raises(UnsafeURL, match="http"):
        await check_url(url, public())


@pytest.mark.parametrize("addr", [
    "127.0.0.1",        # her own API is on 8768
    "::1",
    "10.0.0.5",         # the LAN
    "192.168.1.1",      # the router's admin page
    "169.254.169.254",  # cloud metadata, the classic
    "0.0.0.0",
])
async def test_private_and_local_addresses_are_refused(addr):
    with pytest.raises(UnsafeURL, match="this machine or this network"):
        await check_url("http://somewhere.example/page", pointing_at(addr))


async def test_a_public_address_passes():
    assert await check_url("https://example.com/x",
                           pointing_at("93.184.216.34")) == "https://example.com/x"


async def test_a_name_that_will_not_resolve_is_refused_not_attempted():
    async def nxdomain(host, port):
        raise UnsafeURL(f"couldn't resolve {host}")
    with pytest.raises(UnsafeURL, match="resolve"):
        await check_url("http://nope.invalid/", nxdomain)


async def test_a_redirect_into_the_private_range_is_caught_on_the_second_hop():
    """The reason follow_redirects is off: hop one is a perfectly good public
    host, and hop two is the metadata endpoint."""
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={
            "location": "http://169.254.169.254/latest/meta-data/"})

    async def resolve(host, port):
        return ["93.184.216.34"] if host == "good.example" else ["169.254.169.254"]

    fetcher = HttpFetcher(transport=httpx.MockTransport(handler), resolve=resolve)
    with pytest.raises(UnsafeURL):
        await fetcher.fetch("http://good.example/start")
    assert hops == ["http://good.example/start"]      # the second was never sent


# ---------------------------------------------------------------- the fetch

def html_response(body=PAGE, ctype="text/html; charset=utf-8", status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": ctype})
    return httpx.MockTransport(handler)


async def test_a_page_comes_back_as_title_and_readable_text():
    out = await HttpFetcher(transport=html_response(),
                            resolve=public()).fetch("https://example.com/tea")
    assert out["title"] == "Tea & Cake"
    assert "# On Tea" in out["text"]
    assert "Tea is a drink. It is warm." in out["text"]
    assert "Second paragraph." in out["text"]
    # furniture and scripts are gone, not merely deprioritised
    assert "tracking" not in out["text"] and "menu" not in out["text"]
    assert "copyright" not in out["text"] and "color:red" not in out["text"]


async def test_something_that_is_not_text_is_refused():
    fetcher = HttpFetcher(transport=html_response(ctype="application/pdf"),
                          resolve=public())
    with pytest.raises(ValueError, match="can't read"):
        await fetcher.fetch("https://example.com/paper.pdf")


async def test_plain_text_comes_through_without_the_html_parser():
    out = await HttpFetcher(transport=html_response("just words\n\nmore words",
                                                    ctype="text/plain"),
                            resolve=public()).fetch("https://example.com/a.txt")
    assert out["text"] == "just words\n\nmore words"
    assert out["title"] == "https://example.com/a.txt"      # nothing better to use


async def test_a_huge_page_stops_at_the_byte_cap():
    body = "<p>" + ("word " * 100_000) + "</p>"
    out = await HttpFetcher(transport=html_response(body), resolve=public(),
                            max_bytes=5_000).fetch("https://example.com/big")
    assert len(out["text"]) < 5_000


async def test_an_error_status_raises():
    fetcher = HttpFetcher(transport=html_response(status=404), resolve=public())
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher.fetch("https://example.com/gone")


async def test_endless_redirects_give_up_rather_than_spin():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/loop"})
    fetcher = HttpFetcher(transport=httpx.MockTransport(handler), resolve=public())
    with pytest.raises(ValueError, match="too many redirects"):
        await fetcher.fetch("https://example.com/loop")


# ------------------------------------------------------------- extraction

def test_paragraph_breaks_survive_because_the_shelf_chunks_on_them():
    _title, text = extract(PAGE)
    assert "\n\n" in text                       # KnowledgeStore._chunk needs these
    assert "   " not in text                    # …and nothing else does


def test_extraction_never_raises_on_broken_markup():
    title, text = extract("<p>half a <b>page")
    assert "half a" in text and isinstance(title, str)


def test_gist_cuts_on_a_word_not_mid_name():
    out = gist("Kagoshima is a prefecture in southern Kyushu", 20)
    assert out.endswith("…") and "Kagoshim…" not in out


def test_gist_leaves_short_text_alone():
    assert gist("short", 100) == "short"


async def test_fake_fetcher_is_scriptable_and_records():
    f = FakeFetcher({"https://a/": "the real body"})
    assert (await f.fetch("https://a/"))["text"] == "the real body"
    assert "https://b/" in (await f.fetch("https://b/"))["text"]
    assert f.fetched == ["https://a/", "https://b/"]


def test_build_fetcher_off_means_none():
    assert build_fetcher("off") is None
    assert isinstance(build_fetcher("fake"), FakeFetcher)
    assert isinstance(build_fetcher("http"), HttpFetcher)


def test_only_the_first_title_counts():
    """Real pages carry a stray second <title> more often than you'd think, and
    appending them put the same title in the doc name twice."""
    title, _text = extract("<html><head><title>Real</title></head>"
                           "<body><title>Again</title><p>x</p></body></html>")
    assert title == "Real"
