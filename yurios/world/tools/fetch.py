"""The page-fetch seam (SPEC §7.7) — read one URL, behind a Protocol, with a fake.

This is the first hand whose *argument* comes from a language model rather than
from a human, and that changes what the code has to be careful about. `city`
being wrong gets you the weather in the wrong place. A `url` being wrong — or
being right in a way nobody intended — gets you an HTTP client inside the
trust boundary, pointed at whatever else is listening on this machine: her own
API on 8768 (which hands out the settings panel's keys), the SearXNG instance
next door, a cloud metadata endpoint on a hosted box. So `_check_url` runs
before every request, on every redirect hop, and it is the load-bearing part of
this file — the extraction below it is just tidying.

Extraction is stdlib. `trafilatura` reads a news page better than 90 lines of
HTMLParser ever will, but it arrives with lxml behind it, and this project caps
its dependency list on purpose (pyproject.toml's comment on litellm). The seam
is the answer: a better extractor drops in behind `PageFetcher` without anything
above it noticing.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from html import unescape
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlparse, urljoin

import httpx

log = logging.getLogger("world.fetch")

#: Content types worth handing to a language model. A PDF or an image would be
#: read as mojibake and spend her whole result budget saying so.
READABLE_TYPES = ("text/html", "application/xhtml+xml", "text/plain",
                  "text/markdown")
#: Redirect hops followed by hand. Manual, because httpx's `follow_redirects`
#: would take hop 2 to 127.0.0.1 without asking us — the check has to run again
#: on every Location, and that means owning the loop.
MAX_REDIRECTS = 5
#: How much of the extracted text the model sees inline. The rest still reaches
#: the host for shelving (world/research.py); this is the part she can speak to
#: inside the turn, sized to survive guard truncation with the URL intact.
GIST_CHARS = 400

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas",
              "nav", "header", "footer", "aside", "form", "button", "iframe"}
_BLOCK_TAGS = {"p", "div", "section", "article", "br", "li", "tr", "blockquote",
               "pre", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####",
             "h6": "######"}


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> dict:
        """Return {"url", "title", "text"}. Raises on failure."""
        ...


class UnsafeURL(ValueError):
    """The URL points somewhere a model-supplied URL is not allowed to point."""


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address `host` answers to. The real DNS, off the event loop."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"couldn't resolve {host}") from e
    return [info[4][0] for info in infos]


async def check_url(url: str, resolve=system_resolver) -> str:
    """Reject anything that isn't a public http(s) address. Returns the url.

    Resolution happens here rather than being left to the client because the
    hostname is the interesting part: `http://localhost/`, `http://127.0.0.1/`,
    `http://[::1]/`, `http://192.168.1.1/` and `http://metadata.google.internal/`
    are all ordinary-looking strings that a model will happily produce from a
    page it just read.

    `resolve` is a seam, and a narrow one on purpose: it fakes DNS only, so a
    test still runs this function's actual policy rather than stepping around
    it. A security check with a test-mode bypass is a security check that gets
    tested in the mode nobody ships.

    A determined DNS-rebinding attack can still move the name between this
    check and the connection. Closing that gap means connecting to the resolved
    address with an explicit Host header, which is a bigger change than it
    sounds and is worth doing if this ever faces a hostile page rather than a
    confused model. The gap is named here so it is a known trade-off and not an
    oversight.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(
            f"only http and https can be read, not {parsed.scheme or 'that'}")
    host = parsed.hostname
    if not host:
        raise UnsafeURL("that url has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    for raw in await resolve(host, port):
        addr = ipaddress.ip_address(raw)
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise UnsafeURL(
                f"{host} is on this machine or this network — pages she reads "
                "have to be on the public internet")
    return url


class HttpFetcher:
    """One page, read carefully: checked, size-capped, and reduced to text."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 8.0, max_bytes: int = 2_000_000,
                 resolve=system_resolver):
        self._transport = transport
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._resolve = resolve

    async def fetch(self, url: str) -> dict:
        url = (url or "").strip()
        if not url:
            raise ValueError("url must not be empty")
        async with httpx.AsyncClient(transport=self._transport,
                                     timeout=self._timeout,
                                     follow_redirects=False,
                                     headers={"user-agent": "YuriOS/0.2 (+companion)"}
                                     ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await check_url(url, self._resolve)
                resp = await client.get(url)
                if resp.is_redirect and resp.headers.get("location"):
                    # …and round again, so the next hop is checked too. This is
                    # the whole reason follow_redirects is off: hop 1 to a
                    # public host and hop 2 to 169.254.169.254 is one Location
                    # header, and httpx would follow it without asking.
                    url = urljoin(url, resp.headers["location"])
                    continue
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                if ctype and not any(ctype.startswith(t) for t in READABLE_TYPES):
                    raise ValueError(f"{url} is {ctype}, which she can't read")
                raw = resp.content
                if len(raw) > self._max_bytes:
                    log.info("fetch: truncated %s at %d bytes", url, self._max_bytes)
                    raw = raw[: self._max_bytes]
                body = raw.decode(resp.encoding or "utf-8", errors="replace")
                if ctype.startswith("text/plain") or ctype.startswith("text/markdown"):
                    title, text = "", body.strip()
                else:
                    title, text = extract(body)
                return {"url": url, "title": title or url, "text": text}
        raise ValueError(f"too many redirects from {url}")


class FakeFetcher:
    """Deterministic, offline. Keyed by url so a test can script two pages."""

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = pages or {}
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> dict:
        self.fetched.append(url)
        if url in self.pages:
            body = self.pages[url]
        else:
            body = (f"A page at {url}. It says several plausible things about "
                    "the subject, at enough length to be worth shelving.")
        return {"url": url, "title": f"page: {url}", "text": body}


# --------------------------------------------------------------- extraction

class _Reader(HTMLParser):
    """HTML in, readable text out. Deliberately small (see the module docstring).

    Two rules do most of the work: drop the tags that are furniture rather than
    content (`_SKIP_TAGS`), and put a newline where the layout implied one, so
    paragraphs survive as paragraphs. The KnowledgeStore chunks on paragraph
    budget (mind/knowledge.py `_chunk`), so losing the blank lines would hand it
    one 40 KB blob and it would cite `chars 0-40000` — technically a citation.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._heading = ""

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title" and not self.title:
            # Only the FIRST one. Real pages carry a second <title> more often
            # than you'd think (a stray one in the body, a framework template
            # rendering twice), and appending them produced doc names and chat
            # lines with the same title in them twice.
            self._in_title = True
        elif tag in _HEADINGS:
            self.parts.append(f"\n\n{_HEADINGS[tag]} ")
            self._heading = tag
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _HEADINGS:
            self._heading = ""
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        if not data.strip():
            return
        # Whitespace INSIDE a text node is insignificant in HTML — a paragraph
        # wrapped at 80 columns in the source is still one paragraph. Collapse
        # it here so the only newlines left in `parts` are the ones the tags put
        # there, which is what `_tidy` reads as structure.
        lead = " " if data[:1].isspace() else ""
        trail = " " if data[-1:].isspace() else ""
        self.parts.append(lead + " ".join(data.split()) + trail)


def extract(html: str) -> tuple[str, str]:
    """(title, text) from a page. Never raises: a page that won't parse is
    still worth whatever text fell out of it before the parser gave up."""
    reader = _Reader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:                          # malformed markup, deep nesting
        log.debug("fetch: parser gave up early; keeping what it got")
    title = " ".join(unescape(reader.title).split())
    return title, _tidy("".join(reader.parts))


def _tidy(text: str) -> str:
    """Collapse the whitespace an HTML document is mostly made of, keeping the
    paragraph breaks that survived as structure."""
    out: list[str] = []
    for block in text.split("\n"):
        block = " ".join(block.split())
        if block:
            out.append(block)
        elif out and out[-1] != "":
            out.append("")
    return "\n\n".join(b for b in out if b).strip()


def gist(text: str, limit: int = GIST_CHARS) -> str:
    """The part she reads out loud. Cut on a word so it doesn't end mid-name."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip() + "…"


def build_fetcher(backend: str, *, timeout: float = 8.0,
                  max_bytes: int = 2_000_000) -> PageFetcher | None:
    """The fetcher named by config, or None when she has no web at all."""
    if backend == "off":
        return None
    if backend == "fake":
        return FakeFetcher()
    return HttpFetcher(timeout=timeout, max_bytes=max_bytes)
