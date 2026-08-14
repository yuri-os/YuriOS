"""The reading desk (SPEC §7.7) — search → fetch → shelf → chat, over fakes.

The interesting assertions are about what survives: that a page she read is on
the shelf as a document with its URL in it, and that every way the run can go
wrong still ends in her saying something rather than in a traceback.
"""
from __future__ import annotations

import asyncio


from yurios.world.research import Researcher, as_document
from yurios.world.tools.fetch import FakeFetcher
from yurios.world.tools.search import FakeSearch

from .conftest import PostRecorder, SpeakRecorder


class FakeShelf:
    """KnowledgeStore's ingest contract, recorded."""

    def __init__(self, fail: bool = False):
        self.docs: dict[str, str] = {}
        self.fail = fail

    async def ingest(self, name, text=None):
        if self.fail:
            raise RuntimeError("no embedder backend")
        self.docs[name] = text

        class R:
            doc = name
        return R()


class Boom:
    async def search(self, query, k):
        raise RuntimeError("searx is down")


class Empty:
    async def search(self, query, k):
        return []


def make(clock, *, search=None, fetcher=None, shelf=None, notify=None):
    post, speak = PostRecorder(clock), SpeakRecorder(clock)
    r = Researcher(search or FakeSearch(), fetcher or FakeFetcher(),
                   clock=clock, post=post, speak=speak,
                   knowledge=(lambda: shelf) if shelf is not None else None,
                   notify=notify)
    return r, post, speak


CONTRACT = {"id": "r1", "topic": "tea ceremony", "depth": 2, "kind": "research",
            "status": "started"}


async def test_a_run_reads_the_pages_and_shelves_them(clock):
    shelf = FakeShelf()
    fetcher = FakeFetcher()
    r, post, speak = make(clock, fetcher=fetcher, shelf=shelf)

    await r._job(dict(CONTRACT))

    # depth bounded the run: two hits searched, two pages fetched, two shelved
    assert len(fetcher.fetched) == 2
    assert len(shelf.docs) == 2
    assert all(n.startswith("web-") and n.endswith(".md") for n in shelf.docs)
    # …and one message in the chat, naming what she read
    assert len(post.messages) == 1
    said = post.messages[0]["text"]
    assert "tea ceremony" in said and "https://example.invalid/overview" in said
    assert "2 of 2 shelved" in said
    assert post.messages[0]["proactive"] is True
    # the announce cue is offered, and dropping it is fine (nobody connected)
    assert len(speak.calls) == 1 and "tea ceremony" in speak.calls[0]["cue"]


async def test_a_shelved_document_carries_the_url_it_came_from(clock):
    """A citation is `doc (chars a-b)`, which names a file and not a source.
    The header is what makes the round trip back to a URL possible at all."""
    shelf = FakeShelf()
    r, _post, _speak = make(clock, shelf=shelf)
    await r._job(dict(CONTRACT))
    body = next(iter(shelf.docs.values()))
    assert "Source: https://example.invalid/" in body
    assert body.startswith("# ")
    assert "Retrieved: " in body


async def test_with_no_mind_she_still_reads_and_says_so(clock):
    """MIND_ENABLED=false, or no model chosen yet: the shelf simply isn't there,
    and that is an ordinary configuration rather than a failure."""
    r, post, _speak = make(clock, shelf=None)         # knowledge getter is None
    await r._job(dict(CONTRACT))
    said = post.messages[0]["text"]
    assert "not shelved this time" in said
    assert "https://example.invalid/overview" in said


async def test_a_shelf_that_will_not_ingest_does_not_sink_the_run(clock):
    r, post, _speak = make(clock, shelf=FakeShelf(fail=True))
    await r._job(dict(CONTRACT))
    assert "not shelved this time" in post.messages[0]["text"]


async def test_one_page_that_will_not_open_is_skipped_not_fatal(clock):
    class Half(FakeFetcher):
        async def fetch(self, url):
            if "current" in url:
                raise RuntimeError("403 paywall")
            return await super().fetch(url)

    shelf = FakeShelf()
    r, post, _speak = make(clock, fetcher=Half(), shelf=shelf)
    await r._job(dict(CONTRACT))
    assert len(shelf.docs) == 1
    assert "1 of 1 shelved" in post.messages[0]["text"]


async def test_no_page_opening_still_ends_in_words(clock):
    class Never(FakeFetcher):
        async def fetch(self, url):
            raise RuntimeError("nope")

    r, post, _speak = make(clock, fetcher=Never(), shelf=FakeShelf())
    await r._job(dict(CONTRACT))
    assert "none of them would open" in post.messages[0]["text"]


async def test_a_failed_search_is_a_quiet_line_not_a_crash(clock):
    r, post, _speak = make(clock, search=Boom(), shelf=FakeShelf())
    await r._job(dict(CONTRACT))
    assert "couldn't look into tea ceremony" in post.messages[0]["text"]


async def test_nothing_found_says_nothing_found(clock):
    r, post, _speak = make(clock, search=Empty(), shelf=FakeShelf())
    await r._job(dict(CONTRACT))
    assert "nothing came back" in post.messages[0]["text"]


async def test_the_run_is_routed_back_to_the_channel_that_asked(clock):
    """The answer arrives long after the sentence, so it has to carry its own
    way home — the selfie lab's rule (§7.6)."""
    r, post, _speak = make(clock, shelf=FakeShelf())
    await r._job({**CONTRACT, "_channel": "telegram", "_client_id": "c7"})
    assert post.messages[0]["channel"] == "telegram"
    assert post.messages[0]["client_id"] == "c7"


async def test_status_events_bracket_the_run(clock):
    events = []
    r, _post, _speak = make(clock, shelf=FakeShelf(),
                            notify=lambda kind, ev: events.append((kind, ev)))
    r.start(dict(CONTRACT))
    await _drain(r)
    assert [k for k, _ in events] == ["research_status", "research_status"]
    assert [e["state"] for _k, e in events] == ["started", "done"]


async def test_start_returns_immediately_and_works_behind_the_turn(clock):
    """start-don't-await (§7.6): the tool call is over before any of this runs."""
    shelf = FakeShelf()
    r, post, _speak = make(clock, shelf=shelf)
    r.start(dict(CONTRACT))
    assert post.messages == []                  # nothing has happened yet
    await _drain(r)
    assert len(post.messages) == 1 and len(shelf.docs) == 2


async def test_shelve_files_a_page_she_read_herself(clock):
    """read_page's half: no research run, just one page onto the shelf."""
    shelf = FakeShelf()
    r, _post, _speak = make(clock, shelf=shelf)
    r.shelve({"url": "https://example.com/a", "title": "A Page",
              "text": "the whole page, all of it"})
    await _drain(r)
    assert len(shelf.docs) == 1
    body = next(iter(shelf.docs.values()))
    assert "the whole page, all of it" in body
    assert "Source: https://example.com/a" in body


async def test_shelve_ignores_an_empty_page(clock):
    shelf = FakeShelf()
    r, _post, _speak = make(clock, shelf=shelf)
    r.shelve({"url": "https://example.com/a", "text": "   "})
    await _drain(r)
    assert shelf.docs == {}


def test_a_document_is_readable_markdown():
    body = as_document({"title": "On Tea", "url": "https://a/", "text": "words"},
                       retrieved="2026-08-07T10:00:00")
    assert body.splitlines()[0] == "# On Tea"
    assert "Source: https://a/" in body and "words" in body


async def _drain(r: Researcher) -> None:
    import asyncio
    while r._tasks:
        await asyncio.gather(*list(r._tasks), return_exceptions=True)
        await asyncio.sleep(0)


async def test_a_research_run_is_retrievable_afterwards(clock, tmp_path):
    """Against the **real** KnowledgeStore, not the recording fake: the point of
    shelving is that the same text comes back out of `search()` later. This is
    the half of §20.2 that lives on the research side; test_knowledge_slot.py
    carries it the rest of the way, from the index into the prompt."""
    from yurios.mind.knowledge import KnowledgeStore
    from yurios.mind.vaultio import MindVault

    from .conftest import FakeEmbedder

    store = KnowledgeStore(MindVault(tmp_path / "vault"), FakeEmbedder(), clock)
    # FakeFetcher's stock body is deliberately topic-free, so script the pages:
    # retrieval is being tested here, and it has to have words to retrieve on.
    fetcher = FakeFetcher({
        "https://example.invalid/overview":
            "The tea ceremony is called chanoyu. Matcha is whisked, not steeped.",
        "https://example.invalid/current":
            "A modern tea ceremony keeps the same four principles.",
    })
    r, post, _speak = make(clock, shelf=store, fetcher=fetcher)
    await r._job(dict(CONTRACT))

    shelved = store.shelf()
    assert shelved, "the run put documents on the shelf"
    assert all(name.startswith("web-") for name in shelved)

    hits = store.search("chanoyu matcha whisked", k=3)
    assert hits, "and they come back out of the index"
    assert hits[0].citation.startswith("web-")
    # the provenance header rode along, so a citation traces back to a URL
    doc = (store.reference / hits[0].doc).read_text()
    assert "Source: http" in doc


async def test_a_page_she_read_herself_is_retrievable(clock, tmp_path):
    """`read_page` shelves through the same door — fire-and-forget from a turn
    that is still streaming — so it must end up equally retrievable."""
    import asyncio

    from yurios.mind.knowledge import KnowledgeStore
    from yurios.mind.vaultio import MindVault

    from .conftest import FakeEmbedder

    store = KnowledgeStore(MindVault(tmp_path / "vault"), FakeEmbedder(), clock)
    r, _post, _speak = make(clock, shelf=store)
    r.shelve({"url": "https://example.invalid/sencha", "title": "Sencha",
              "text": "Sencha is steamed rather than pan-fired."})
    await asyncio.gather(*r._tasks)

    hits = store.search("sencha steamed", k=2)
    assert hits and "steamed" in hits[0].text


# ------------------------------------------------- watching it, stopping it
#
# A research call answers in milliseconds and then spends the next half hour of
# this machine reading documents nobody has seen. §24.3 puts that on a page with
# a button next to it; these are the properties the page rests on.

async def drain(r):
    """Wait out every task the researcher started, and anything they started."""
    while r._tasks:
        await asyncio.gather(*list(r._tasks), return_exceptions=True)


class StoreFake(FakeShelf):
    """The KnowledgeStore surface the Researcher actually touches."""

    def __init__(self):
        super().__init__()
        self.parked: dict[str, str] = {}
        self.stops = 0

    def estimate(self, text):
        passages = max(1, len(text) // 400)
        return {"passages": passages, "calls": passages * 2,
                "digested": len(text) > 40_000, "chars": len(text)}

    def park(self, name, text):
        self.parked[name] = text
        return name

    def holds(self):
        return [{"doc": d} for d in self.parked]

    def stop(self):
        self.stops += 1
        return True


async def test_a_run_says_what_it_is_doing_and_what_it_will_cost(clock):
    """The panel's row: stage, pages, and model calls priced per page as they
    arrive — a URL has no length until somebody fetches it."""
    shelf = StoreFake()
    r, post, speak = make(clock, shelf=shelf)
    r.start(CONTRACT)
    (run,) = r.runs()
    assert run["topic"] == "tea ceremony" and run["stage"] == "searching"

    await drain(r)
    (run,) = r.runs()
    assert run["stage"] == "done"
    assert run["found"] == 2 and run["read"] == len(run["pages"])
    assert all(p["calls"] > 0 and p["state"] == "read" for p in run["pages"])
    assert run["calls"] == sum(p["calls"] for p in run["pages"])


async def test_stopping_before_she_reads_costs_nothing(clock):
    """The flag goes up before the first fetch: nothing is fetched, nothing is
    shelved, and she says so rather than going quiet."""
    shelf = StoreFake()
    r, post, speak = make(clock, shelf=shelf)
    r.start(CONTRACT)
    assert r.stop("r1") is True
    await drain(r)

    assert shelf.docs == {} and shelf.parked == {}
    (run,) = r.runs()
    assert run["stage"] == "stopped"
    assert any("stopped reading" in m["text"] for m in post.messages)


async def test_a_page_already_fetched_is_kept_rather_than_dropped(clock):
    """Stopping between the fetch and the reading. Somebody's web server has
    already been paid for that page — it goes on the shelf held, so resuming is
    reading rather than fetching all over again."""
    shelf = StoreFake()
    r, post, speak = make(clock, shelf=shelf)

    class StopsOnFetch(FakeFetcher):
        async def fetch(self, url):
            page = await super().fetch(url)
            r.stop("r1")                       # you hit the button mid-run
            return page

    r.fetcher = StopsOnFetch()
    r.start(CONTRACT)
    await drain(r)

    assert shelf.parked, "the fetched page was kept"
    assert shelf.docs == {}, "and none of it was read"
    assert shelf.stops == 1, "the read in flight was asked to stop too"
    (run,) = r.runs()
    assert run["stage"] == "stopped"
    assert [p["state"] for p in run["pages"]] == ["held"]
    assert run["pages"][0]["calls"] > 0, "priced, so the panel can offer it"


async def test_stopping_a_run_that_already_finished_is_not_an_error(clock):
    r, _post, _speak = make(clock, shelf=StoreFake())
    r.start(CONTRACT)
    await drain(r)
    assert r.stop("r1") is False
    assert r.stop("nope") is False


async def test_finished_runs_are_kept_for_the_panel_but_not_forever(clock):
    r, _post, _speak = make(clock, shelf=StoreFake())
    for i in range(Researcher.KEEP_RUNS + 4):
        r.start({**CONTRACT, "id": f"r{i}"})
        await drain(r)
    assert len(r.runs()) == Researcher.KEEP_RUNS
