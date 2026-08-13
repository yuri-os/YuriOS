"""The search seam (SPEC §7.7) — a real lookup behind a Protocol, with a fake.

SearXNG is the reference backend because it needs no account *and* no third
party. You run the instance, so the list of what she searched for is yours,
sitting on your own machine — which is the whole argument of a local-first
companion applied to the one capability that usually hands your curiosity to
somebody else.

The cost of that choice is a setup step, and one specific trap: SearXNG ships
with its JSON output format **disabled**. An instance that answers a browser
perfectly will answer this with 403 until `search.formats` in its `settings.yml`
lists `json`. That failure is named explicitly below, because "403" on its own
sends you looking at auth, which is not the problem.
"""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

log = logging.getLogger("world.search")

#: Snippets are trimmed to this before they reach the model. A tool result is a
#: fact for her to speak to, not a payload (guard.RESULT_MAX_CHARS = 600): five
#: untrimmed SearXNG `content` fields are ~2 KB, and the guard would cut that
#: mid-word, taking the last three URLs with it. Trimming here means she loses
#: the tail of one snippet instead of losing whole results.
SNIPPET_MAX_CHARS = 160


class SearchProvider(Protocol):
    async def search(self, query: str, k: int) -> list[dict]:
        """Return up to `k` [{"title", "url", "snippet"}]. Raises on failure."""
        ...


class SearxngProvider:
    """One GET against your own SearXNG instance. Keyless, and nobody else's."""

    def __init__(self, base_url: str, *, language: str = "en",
                 safesearch: int = 1, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 8.0):
        # `transport` is the test seam: httpx.MockTransport serves canned payloads
        # so the parser is pinned without the network (SPEC §13) — fetch.py's rule.
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.safesearch = safesearch
        self._transport = transport
        self._timeout = timeout

    async def search(self, query: str, k: int) -> list[dict]:
        query = (query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        async with httpx.AsyncClient(transport=self._transport,
                                     timeout=self._timeout) as client:
            resp = await client.get(f"{self.base_url}/search", params={
                "q": query, "format": "json", "language": self.language,
                "safesearch": self.safesearch})
        if resp.status_code == 403:
            # The one failure worth naming, because the status code lies about
            # the cause: this is almost never authentication.
            raise RuntimeError(
                f"{self.base_url} refused a JSON search — SearXNG disables the "
                "json format by default. Add it to `search.formats` in the "
                "instance's settings.yml and restart it.")
        resp.raise_for_status()
        payload = resp.json()
        out = []
        for row in payload.get("results") or []:
            url = (row.get("url") or "").strip()
            if not url:                        # filter BEFORE the slice, or a
                continue                       # junk first row costs a real one
            out.append({
                "title": (row.get("title") or url).strip(),
                "url": url,
                "snippet": _trim(row.get("content") or ""),
            })
            if len(out) >= k:
                break
        return out


class FakeSearch:
    """Deterministic, offline — the same three rows for any query, so a test
    asserts on the loop's behaviour and never on what the web said today."""

    async def search(self, query: str, k: int) -> list[dict]:
        query = (query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        rows = [
            {"title": f"{query} — an overview",
             "url": "https://example.invalid/overview",
             "snippet": f"A general introduction to {query}."},
            {"title": f"{query}: the current state",
             "url": "https://example.invalid/current",
             "snippet": f"Where {query} stands now, and what changed recently."},
            {"title": f"Notes on {query}",
             "url": "https://example.invalid/notes",
             "snippet": f"Assorted observations about {query}."},
        ]
        return rows[:k]


def _trim(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    return text[: SNIPPET_MAX_CHARS - 1] + "…"


def build_provider(backend: str, *, base_url: str, language: str = "en",
                   safesearch: int = 1) -> SearchProvider | None:
    """The backend named by config, or None when she has no search at all.

    `off` returning None is what keeps the tools unadvertised (server.py) —
    no hand, not a dead one, the SELFIE_BACKEND rule.
    """
    if backend == "off":
        return None
    if backend == "fake":
        return FakeSearch()
    return SearxngProvider(base_url, language=language, safesearch=safesearch)
