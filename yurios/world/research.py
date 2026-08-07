"""The reading desk (SPEC §7.7) — her web hands, realised host-side.

`research` is the second tool to learn §7.6's lesson, and it learns it harder
than the camera did. A render is one slow call; this is a search, then several
fetches, then an embedding pass per page — twenty seconds on a good day, and
`TOOL_TIMEOUT_S` is ten. So the MCP server answers `{"status": "started"}` and
the actual going-and-finding-out happens here, off-turn, exactly the way a
selfie renders (world/selfies.py — this file is deliberately its sibling, down
to the callables it's handed).

What makes this more than a slow `web_search` is where the text lands. Every
page read — by `research`, or by a plain `read_page` she made herself — is
ingested into the §20 KnowledgeStore: chunked, situated, embedded, and indexed
with a doc + span she can cite. So a tool result stops being 600 characters that
expire at the end of the turn and becomes something she still has next week,
retrievable through the same assembler slot as the books you drop on her shelf.
A page she read is a page she read; the fact that a tool call fetched it rather
than you copying it in is not a distinction worth keeping.

The store is reached through a **getter**, not a reference, because it belongs
to the MindLoop (mind/loop.py) — built after the tools are wired, and not built
at all with `MIND_ENABLED=false` or no model chosen. Mindless, she still
searches, still reads, still tells you what she found; the reading just isn't
kept, and the message says so rather than pretending.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Optional

from .clock import Clock

log = logging.getLogger("world.research")

#: How many pages are fetched at once. Small on purpose: this is somebody's
#: home connection and somebody else's web server, and a research run is never
#: the urgent thing happening on either.
FETCH_CONCURRENCY = 2

#: The announce cue (§8.3), spoken only if she's free — a drop is fine, because
#: the summary itself already landed in the chat.
ANNOUNCE_CUE = (
    "((You've finished reading up on {topic} — {count} page(s), and it's in "
    "the chat now. Say one short line about what you found, nothing else.))")


def _slug(text: str) -> str:
    """A filename that is still recognisable as the thing it came from — the
    shelf is a directory a human opens, so `web-tea-ceremony-1723.md` beats a
    hash even though a hash would collide less."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "page"


def as_document(page: dict, *, retrieved: str) -> str:
    """One fetched page, as a document for the shelf.

    The header is the whole point of writing it this way: `KnowledgeStore`
    citations are `doc (chars a-b)`, which tells you *which file* and *where in
    it* but nothing about where the file came from. A page whose first lines say
    so is a citation that survives the round trip back to a URL.
    """
    return (f"# {page.get('title') or page.get('url')}\n\n"
            f"Source: {page.get('url')}\n"
            f"Retrieved: {retrieved}\n\n"
            f"{page.get('text') or ''}\n")


class Researcher:
    """Owns the reading tasks. `start()` is the §7.5 host-side realisation."""

    def __init__(self, search, fetcher, *, clock: Clock,
                 post: Callable[..., dict],
                 speak: Callable[[str], Awaitable[bool]],
                 knowledge: Optional[Callable[[], object]] = None,
                 notify: Optional[Callable[[str, dict], None]] = None):
        self.search = search
        self.fetcher = fetcher
        self.clock = clock
        self.post = post                       # Runtime.post_message
        self.speak = speak                     # Runtime.speak_ambient (§8.4)
        self.knowledge = knowledge             # () -> KnowledgeStore | None
        self.notify = notify                   # EventHub.publish, when hosted
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- the shelf

    def _store(self):
        """The KnowledgeStore, or None. Never raises — a mindless runtime is an
        ordinary configuration, not a failure to report."""
        if self.knowledge is None:
            return None
        try:
            return self.knowledge()
        except Exception:
            log.exception("research: couldn't reach the knowledge shelf")
            return None

    async def _shelve(self, page: dict) -> str:
        """Ingest one page. Returns the doc name, or "" if it wasn't kept."""
        store = self._store()
        if store is None:
            return ""
        retrieved = self._stamp()
        name = f"web-{_slug(page.get('title') or page.get('url'))}-{int(self.clock.now())}.md"
        try:
            result = await store.ingest(name, as_document(page, retrieved=retrieved))
        except Exception:
            # No embedder backend, a mangled page, a full disk. §20.1's rule for
            # a doc that won't ingest: one loud warning, and the rest of the run
            # carries on — she still read it, she just didn't file it.
            log.warning("research: couldn't shelve %s", page.get("url"),
                        exc_info=True)
            return ""
        return result.doc

    def _stamp(self) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(
            self.clock.now()).isoformat(timespec="seconds")

    def shelve(self, page: dict) -> None:
        """Fire-and-forget ingestion for a page she read herself (`read_page`).

        Called from `ToolBrain._realise`, which is synchronous and is running
        inside a turn that is still streaming — so this must never be awaited
        there. The turn says what the page said; the filing happens behind it.
        """
        if not (page.get("text") or "").strip():
            return
        task = asyncio.create_task(self._shelve_one(page), name="shelve-page")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _shelve_one(self, page: dict) -> None:
        doc = await self._shelve(page)
        if doc:
            log.info("research: shelved %s as %s", page.get("url"), doc)

    # ------------------------------------------------- research, start-don't-await

    def start(self, contract: dict) -> None:
        """Spawn one research run from the tool's validated contract. Never
        blocks, never raises — the turn that asked is already moving on."""
        task = asyncio.create_task(self._job(dict(contract)),
                                   name=f"research-{contract.get('id', '?')}")
        self._tasks.add(task)
        self._status(contract, "started")
        task.add_done_callback(self._tasks.discard)

    def _status(self, contract: dict, state: str) -> None:
        if self.notify is None:
            return
        event = {"id": str(contract.get("id", "")), "state": state,
                 "topic": contract.get("topic", "")}
        if contract.get("_client_id"):
            event["client_id"] = contract["_client_id"]
        self.notify("research_status", event)

    async def _job(self, c: dict) -> None:
        topic = str(c.get("topic") or "").strip()
        depth = max(1, int(c.get("depth") or 3))
        try:
            hits = await self.search.search(topic, depth)
        except Exception as e:
            log.exception("research: the search failed")
            self._say(c, f"(couldn't look into {topic} — {type(e).__name__})")
            self._status(c, "error")
            return
        if not hits:
            self._say(c, f"(nothing came back about {topic})")
            self._status(c, "done")
            return

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(hit: dict) -> dict | None:
            async with sem:
                try:
                    page = await self.fetcher.fetch(hit["url"])
                except Exception as e:
                    # One page refusing to be read is not a failed run — it is
                    # a paywall, or a 404, or a PDF. Skip it and keep the rest.
                    log.info("research: skipped %s (%s)", hit["url"],
                             type(e).__name__)
                    return None
                if not (page.get("text") or "").strip():
                    return None
                page.setdefault("title", hit.get("title") or hit["url"])
                page["doc"] = await self._shelve(page)
                return page

        pages = [p for p in await asyncio.gather(*(one(h) for h in hits))
                 if p is not None]
        if not pages:
            self._say(c, f"(found some links about {topic}, but none of them "
                         "would open)")
            self._status(c, "done")
            return

        kept = sum(1 for p in pages if p.get("doc"))
        lines = [f"read up on {topic}:"]
        # A page with no <title> falls back to its own url (fetch.py), and
        # "· https://x — https://x" reads like a bug rather than a page.
        lines += [f"· {p['title']} — {p['url']}" if p["title"] != p["url"]
                  else f"· {p['url']}" for p in pages]
        if kept:
            lines.append(f"({kept} of {len(pages)} shelved — I can pull them "
                         "back up later)")
        else:
            lines.append("(not shelved this time — I'll have to read them "
                         "again if you need the detail)")
        self._say(c, "\n".join(lines))
        self._status(c, "done")

        try:
            await self.speak(ANNOUNCE_CUE.format(topic=topic, count=len(pages)))
        except Exception:
            log.exception("research announce failed")

    def _say(self, c: dict, text: str) -> None:
        """One message into the chat the run came from. Mirrors the selfie
        lab's posting rules: proactive, and routed back to the channel and
        client that asked, because the answer arrives long after the sentence."""
        kw: dict = {"proactive": True}
        if c.get("_channel"):
            kw["channel"] = c["_channel"]
        if c.get("_client_id"):
            kw["client_id"] = c["_client_id"]
        try:
            self.post("assistant", text, **kw)
        except Exception:
            log.exception("research: couldn't post the result")

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
