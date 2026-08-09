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
import hashlib
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


def _doc_name(page: dict) -> str:
    """A stable name for a page's slot on the shelf.

    The old scheme suffixed the wall-clock second (`web-x-{now()}.md`), so
    reading the same URL on two research runs — or once via `research` and
    once via a plain `read_page` — filed it twice under two different names.
    Keying on the URL instead means a re-read lands on the same doc name, and
    `KnowledgeStore.ingest`'s "re-ingest replaces" rule (mind/knowledge.py)
    does the deduplication: same doc, chunks refreshed, no pileup.
    """
    url = page.get("url") or ""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"web-{_slug(page.get('title') or url)}-{digest}.md"


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

    #: Finished runs kept for the panel, so "what did that cost?" survives the
    #: end of the run that answers it.
    KEEP_RUNS = 8

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
        #: id -> the record the inner-life panel reads (§24.3). Ordered, and
        #: trimmed to the last few finished runs.
        self._runs: dict[str, dict] = {}

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

    def _price(self, entry: dict, page: dict) -> None:
        """What reading this page is going to cost, in model calls, before any
        of them are made. Best-effort: no store, no estimate, no drama."""
        store = self._store()
        if store is None:
            return
        try:
            est = store.estimate(as_document(page, retrieved=self._stamp()))
            entry["calls"] = est["calls"]
            entry["passages"] = est["passages"]
            entry["digested"] = est["digested"]
        except Exception:  # noqa: BLE001 — a price tag is not worth a traceback
            log.debug("research: couldn't price %s", page.get("url"),
                      exc_info=True)

    def _park(self, page: dict) -> str:
        """Shelve a fetched page without reading it (KnowledgeStore.park)."""
        store = self._store()
        if store is None:
            return ""
        try:
            return store.park(_doc_name(page),
                              as_document(page, retrieved=self._stamp()))
        except Exception:  # noqa: BLE001
            log.warning("research: couldn't park %s", page.get("url"),
                        exc_info=True)
            return ""

    def _page_state(self, doc: str) -> str:
        """Did that page's reading finish, or did it end up held?"""
        store = self._store()
        if not doc or store is None:
            return "unshelved"
        try:
            if any(h["doc"] == doc for h in store.holds()):
                return "held"
        except Exception:  # noqa: BLE001
            pass
        return "read"

    async def _shelve(self, page: dict) -> str:
        """Ingest one page. Returns the doc name, or "" if it wasn't kept."""
        store = self._store()
        if store is None:
            return ""
        retrieved = self._stamp()
        name = _doc_name(page)
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
        run_id = str(contract.get("id") or "?")
        self._runs[run_id] = {
            "id": run_id, "topic": str(contract.get("topic") or ""),
            "depth": int(contract.get("depth") or 3),
            "stage": "searching", "started_at": self.clock.now(),
            "pages": [], "stopped": False, "corr_id": contract.get("_corr_id")}
        self._trim()
        task = asyncio.create_task(self._job(dict(contract)),
                                   name=f"research-{run_id}")
        self._tasks.add(task)
        self._status(contract, "started")
        task.add_done_callback(self._tasks.discard)

    def _trim(self) -> None:
        finished = [k for k, r in self._runs.items()
                    if r["stage"] in ("done", "error", "stopped")]
        for k in finished[: max(0, len(finished) - self.KEEP_RUNS)]:
            self._runs.pop(k, None)

    def _status(self, contract: dict, state: str) -> None:
        run = self._runs.get(str(contract.get("id", "")))
        if run is not None and state in ("done", "error", "stopped"):
            run["stage"] = state
            run["ended_at"] = self.clock.now()
            self._trim()
        if self.notify is None:
            return
        event = {"id": str(contract.get("id", "")), "state": state,
                 "topic": contract.get("topic", "")}
        if contract.get("_client_id"):
            event["client_id"] = contract["_client_id"]
        self.notify("research_status", event)

    # ------------------------------------------------ watching it, stopping it

    def runs(self) -> list[dict]:
        """Every run this process knows about, newest last — the panel's list.

        `calls` is what the reading is *estimated* to cost in model calls, page
        by page, and it only becomes knowable as pages arrive: a search result
        is a URL, and a URL's length is whatever the server sends back. So the
        number grows during the fetch phase and then counts down — which is the
        honest shape of it, and better than a spinner that implies nothing is
        being spent.
        """
        out = []
        for run in self._runs.values():
            pages = run["pages"]
            row = dict(run)
            row["pages"] = list(pages)
            row["calls"] = sum(p.get("calls", 0) for p in pages)
            row["read"] = sum(1 for p in pages if p.get("state") == "read")
            row["elapsed_s"] = round(
                (run.get("ended_at") or self.clock.now()) - run["started_at"], 1)
            out.append(row)
        return out

    def stop(self, run_id: str) -> bool:
        """Stop a run: no more fetching, no more reading, nothing lost.

        Not a task cancellation. A run is mostly spent *inside* one long
        `KnowledgeStore.ingest`, and killing that mid-call would throw away the
        passages already paid for and leave the doc claimed by a reader that no
        longer exists. So this raises a flag the run itself honours: the store
        stops after the section it's on and parks the rest (`hold`), pages
        fetched but not yet read are shelved held rather than dropped, and the
        run winds down through its ordinary ending.
        """
        run = self._runs.get(str(run_id))
        if run is None or run["stage"] in ("done", "error", "stopped"):
            return False
        run["stopped"] = True
        run["stage"] = "stopping"
        store = self._store()
        if store is not None:
            store.stop()          # the read in flight, if there is one
        return True

    async def _job(self, c: dict) -> None:
        topic = str(c.get("topic") or "").strip()
        depth = max(1, int(c.get("depth") or 3))
        run = self._runs.get(str(c.get("id", "")), {"pages": [], "stopped": False})
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
        run["found"] = len(hits)
        run["stage"] = "reading" if not run["stopped"] else run["stage"]

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(hit: dict) -> dict | None:
            async with sem:
                if run["stopped"]:
                    return None            # never fetched: nothing to keep
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
                entry = {"url": page["url"], "title": page["title"],
                         "chars": len(page["text"]), "state": "fetched",
                         "calls": 0, "doc": ""}
                run["pages"].append(entry)
                self._price(entry, page)
                if run["stopped"]:
                    # Fetched between the flag going up and this line. Somebody
                    # already paid a stranger's web server for it — put it on the
                    # shelf held rather than dropping it, so resuming is reading
                    # rather than fetching all over again.
                    entry["doc"] = self._park(page)
                    entry["state"] = "held" if entry["doc"] else "dropped"
                    return None
                entry["state"] = "reading"   # …for however long the read takes
                entry["doc"] = await self._shelve(page)
                entry["state"] = self._page_state(entry["doc"])
                page["doc"] = entry["doc"]
                return page

        pages = [p for p in await asyncio.gather(*(one(h) for h in hits))
                 if p is not None]
        if run["stopped"]:
            self._say(c, f"(stopped reading about {topic} — what she'd already "
                         "fetched is on her shelf, waiting for you)")
            self._status(c, "stopped")
            return
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
