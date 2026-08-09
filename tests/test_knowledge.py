"""KnowledgeStore (SPEC §20) — drop-folder RAG, a sibling of memory.

The boundary is the load-bearing property: knowledge cites a document + span;
memory cites a conversation turn; and dropping a book on her shelf must never
pollute what she remembers about *you*.
"""
from __future__ import annotations

import pytest

from yurios.mind.knowledge import KnowledgeStore
from yurios.mind.vaultio import MindVault
from yurios.world.clock import VirtualClock

from .conftest import SIM_START, FakeEmbedder

DOC = """# Field Notes on Tea

Gyokuro is shaded for three weeks before harvest, which raises theanine
and gives the brew its savory depth.

Bancha is the everyday cut — later flushes, larger leaves, cheaper and
more forgiving of hot water.
"""


@pytest.fixture
def store(tmp_path):
    vault = MindVault(tmp_path / "vault")
    clock = VirtualClock(start=SIM_START.timestamp())
    ks = KnowledgeStore(vault, FakeEmbedder(), clock, chunk_chars=200)
    return ks


def test_shelf_exists_to_be_dropped_into(tmp_path):
    """The drop folder is an instruction to the user ("put a file here"), so
    constructing the store has to make the place real — including in a Vault
    seeded before §20 existed, which is every Vault upgraded rather than made."""
    store = KnowledgeStore(MindVault(tmp_path / "vault"), FakeEmbedder(),
                           VirtualClock(start=SIM_START.timestamp()))
    assert store.reference.is_dir()
    assert store.shelf() == []          # empty, but askable


def test_derived_index_ignores_itself(tmp_path):
    """chunks.jsonl is rewritten whole per ingest and holds an embedding per
    chunk. The ignore lives *in* the index dir rather than the Vault's
    `.gitignore`, which is written once at seed time and never refreshed."""
    store = KnowledgeStore(MindVault(tmp_path / "vault"), FakeEmbedder(),
                           VirtualClock(start=SIM_START.timestamp()))
    ignore = store.index_path.parent / ".gitignore"
    assert ignore.exists() and ignore.read_text().rstrip().endswith("*")


def test_shelf_creation_survives_a_read_only_vault(tmp_path, monkeypatch):
    """A Vault that can't be written is a strange configuration, not a reason
    to refuse to build the mind."""
    def no(*a, **kw):
        raise PermissionError("read-only")
    monkeypatch.setattr("pathlib.Path.mkdir", no)
    store = KnowledgeStore(MindVault(tmp_path / "vault"), FakeEmbedder(),
                           VirtualClock(start=SIM_START.timestamp()))
    assert store.shelf() == []
    assert store.pending_docs() == []


async def test_drop_scan_ingest_search_with_citation(store):
    ref = store.reference
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "tea.md").write_text(DOC)
    assert store.pending_docs() == ["tea.md"]

    results = await store.scan()
    assert [r.doc for r in results] == ["tea.md"]
    assert results[0].chunks >= 2
    assert store.pending_docs() == []              # seen; not re-chewed

    hits = store.search("gyokuro shaded theanine", k=2)
    assert hits, "the dropped doc is retrievable"
    top = hits[0]
    assert top.doc == "tea.md"
    assert top.span.startswith("chars ")           # the citation target
    assert "tea.md (chars" in top.citation
    assert "theanine" in top.text.lower()


async def test_reingest_replaces_not_duplicates(store):
    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    await store.scan()
    n1 = len(store.inspect())
    (store.reference / "tea.md").write_text(DOC + "\n\nMatcha is powdered.\n")
    assert store.pending_docs() == ["tea.md"]      # the change is noticed
    await store.scan()
    rows = store.inspect()
    assert len({r.id for r in rows}) == len(rows)
    assert len(rows) >= n1                          # replaced, not doubled
    assert all(r.doc == "tea.md" for r in rows)


async def test_forget_drops_doc_and_chunks(store):
    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    await store.scan()
    removed = store.forget("tea")
    assert removed >= 2
    assert store.inspect() == []
    assert store.shelf() == []
    assert store.search("gyokuro", k=2) == []


async def test_knowledge_never_pollutes_memory(store, tmp_path):
    """The D-019 boundary: the shelf and the relationship are separate stores
    with separate files — nothing ingested lands in memory/, and vice versa."""
    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    await store.scan()
    vault_root = store.vault.vault
    assert (vault_root / "knowledge" / "index" / "chunks.jsonl").exists()
    assert not (vault_root / "memory").exists()     # she read a book; you didn't change


async def test_ingest_given_content_lands_on_the_shelf(store):
    res = await store.ingest("notes from her research", text="Sencha is steamed.")
    assert res.doc.endswith(".md")
    assert res.doc in store.shelf()                 # the shelf is the durable home


async def test_the_index_is_not_reparsed_every_turn(store, monkeypatch):
    """`search()` used to run occasionally; §20.2 puts it on every turn, against
    a JSONL file carrying a full embedding per line. It caches on the file's own
    size+mtime — and a fresh ingest rewrites the file, so the next search sees
    it without anyone sending a signal."""
    from yurios.mind import knowledge as mod

    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    await store.scan()

    reads = []
    real = mod.jsonl_read
    monkeypatch.setattr(mod, "jsonl_read", lambda p: (reads.append(p), real(p))[1])

    assert store.search("gyokuro", k=1)
    assert store.search("gyokuro", k=1)
    assert store.search("bancha", k=1)
    assert len(reads) == 1, "three searches, one parse"

    await store.ingest("more.md", text="Matcha is powdered.\n")
    assert store.search("matcha powdered", k=1), "a new doc is visible at once"


# --------------------------------------------------- one reader at the shelf
#
# A `research` run shelves the page it fetched and ingests it; the loop's SENSE
# watches the same folder. The page is on the shelf before the ingest starts, so
# for the whole length of it — hundreds of utility calls for a long article —
# the doc looked pending to every tick, and one research call turned into three
# overlapping ingests of the same excerpts against the same local model.

async def test_a_doc_being_read_is_not_still_pending(store):
    """The claim happens before the slow part, not after it."""
    seen_midway = []
    real = store._contextualize

    async def watch(doc, chunk):
        seen_midway.append(store.pending_docs())
        return await real(doc, chunk)

    store._contextualize = watch
    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)

    await store.ingest("tea.md")
    assert seen_midway and all(p == [] for p in seen_midway), \
        "a doc someone is already reading must not look like work to do"


async def test_the_same_doc_is_not_read_twice_at_once(store):
    """The tick and the research run race for one page: it gets read once, and
    the loser is handed the same answer rather than repeating the work."""
    calls = []
    real = store._contextualize

    async def counted(doc, chunk):
        calls.append(doc)
        return await real(doc, chunk)

    store._contextualize = counted
    import asyncio
    a, b = await asyncio.gather(
        store.ingest("web-tea.md", text=DOC),
        store.ingest("web-tea.md", text=DOC))

    assert a.doc == b.doc
    assert a.chunks == b.chunks >= 2           # both callers get the real count
    assert len(calls) == a.chunks, "the second run re-read the document"
    assert len({r.id for r in store.inspect()}) == a.chunks


async def test_two_docs_at_once_do_not_erase_each_other(store):
    """`ingest` rewrites the index whole, so overlapping runs used to end with
    whichever finished last deleting the other's chunks."""
    import asyncio
    real = store._contextualize

    async def slow(doc, chunk):
        await asyncio.sleep(0)                 # a real await point: the two runs
        return await real(doc, chunk)          # interleave the way model calls do

    store._contextualize = slow
    await asyncio.gather(
        store.ingest("tea.md", text=DOC),
        store.ingest("coffee.md", text="Coffee is roasted.\n\nThen ground.\n"))

    docs = {r.doc for r in store.inspect()}
    assert docs == {"tea.md", "coffee.md"}, "both readings survived"


async def test_a_claimed_doc_that_never_finished_is_read_again(store):
    """Marked seen but no chunks in the index is a run that died halfway. It has
    to be redone — otherwise a cancelled ingest silently costs her the doc."""
    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    store._mark_seen("tea.md")                 # the claim, without the reading

    res = await store.ingest("tea.md")
    assert res.chunks >= 2
    assert {r.doc for r in store.inspect()} == {"tea.md"}


async def test_a_tick_steps_aside_while_something_is_being_read(store):
    """SENSE must not queue behind a long ingest: the tick that waits is a tick
    that isn't sensing, and the shelf keeps."""
    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    async with store._busy:
        assert await store.scan() == []
    assert store.pending_docs() == ["tea.md"], "still there for the next tick"


async def test_a_run_that_fails_gives_the_claim_back(store):
    """Claiming a doc is a promise to finish it. A run that can't — an embedder
    with nothing behind it — must not leave the doc marked read by nobody."""
    class Broken:
        def embed(self, texts):
            raise RuntimeError("no embedder backend")

    store.reference.mkdir(parents=True, exist_ok=True)
    (store.reference / "tea.md").write_text(DOC)
    store.embedder = Broken()

    with pytest.raises(RuntimeError):
        await store.ingest("tea.md")
    assert store.pending_docs() == ["tea.md"], "still work to do"

    store.embedder = FakeEmbedder()
    assert (await store.ingest("tea.md")).chunks >= 2


# ------------------------------------------------- a book, read for notes
#
# Word-for-word ingestion is two model calls per 1,200 characters. That is the
# right price for the page you dropped on her shelf and an impossible one for
# what `research` brings home — the encyclopaedia article that started this was
# 365,000 characters, i.e. 758 calls to the one model on this machine.

BOOK_PARA = ("Chronology in the Tang dynasty was kept by water clock, and the "
             "official Xuanming calendar of 822 fixed the year at 365.2446 "
             "days. Later commentators disputed the figure at length.\n\n")


def book(chars=60_000):
    return "# A Long Book About Time\n\n" + BOOK_PARA * (chars // len(BOOK_PARA))


@pytest.fixture
def summarising_store(store):
    """A store with a utility tier — which is what decides whether a long doc
    can be read for notes at all."""
    store.calls = []

    async def utility(messages):
        store.calls.append(messages)
        return "The Xuanming calendar of 822 fixed the year at 365.2446 days."

    store.utility = utility
    return store


async def test_a_short_doc_is_still_kept_word_for_word(summarising_store):
    """The threshold has to leave the ordinary case alone: a page she reads is
    a page she can quote."""
    store = summarising_store
    res = await store.ingest("tea.md", text=DOC)
    assert not res.digested
    rows = store.inspect()
    assert all(not r.summary for r in rows)
    assert "theanine" in " ".join(r.text for r in rows), "verbatim, not notes"


async def test_a_long_doc_is_summarised_before_it_is_indexed(summarising_store):
    """The whole point: a book costs a handful of calls, not hundreds."""
    store = summarising_store
    text = book(60_000)
    res = await store.ingest("book.md", text=text)

    assert res.digested
    verbatim = len(list(store._chunk(text)))
    assert verbatim > 40, "the fixture is long enough to matter"
    assert res.chunks < verbatim / 5, "notes, not a transcript"
    assert len(store.calls) == res.chunks, "one call per section, and no more"
    assert all(r.summary for r in store.inspect())


async def test_the_notes_still_cite_the_real_document(summarising_store):
    """A précis covers a stretch of the source, so the citation goes on
    pointing into the source — and says it is a précis, because the prompt
    renders the text and the citation and nothing else (§20.2)."""
    store = summarising_store
    text = book(60_000)
    await store.ingest("book.md", text=text)

    rows = sorted(store.inspect(), key=lambda c: int(c.span.split()[1].split("-")[0]))
    spans = [tuple(int(x) for x in r.span.removeprefix("chars ").split("-"))
             for r in rows]
    assert spans[0][0] == 0
    assert all(b > a for a, b in spans), "every span covers real ground"
    assert all(b <= len(text) + 2 for _, b in spans), "and stays inside the doc"
    assert sum(b - a for a, b in spans) > len(text) * 0.9, "the book was read"
    assert "summarised" in rows[0].citation and "book.md" in rows[0].citation

    assert (store.reference / "book.md").read_text() == text, \
        "the shelf keeps the document whole; only the index is notes"


async def test_a_long_doc_is_still_findable_by_name(summarising_store):
    store = summarising_store
    await store.ingest("book.md", text=book(60_000))
    hits = store.search("Xuanming calendar", k=2)
    assert hits and hits[0].doc == "book.md"


async def test_a_section_that_will_not_summarise_is_kept_verbatim(
        summarising_store):
    """A failed call must not leave a hole in the middle of the book: the
    section falls back to its own paragraphs, and the whole document is still
    covered — for the price of the embeddings alone."""
    store = summarising_store
    text = book(60_000)

    async def broken(messages):
        raise RuntimeError("utility model went away")

    store.utility = broken
    res = await store.ingest("book.md", text=text)

    assert res.chunks == len(list(store._chunk(text))), "nothing was skipped"
    rows = store.inspect()
    assert all(not r.summary for r in rows), "notes she never took"
    spans = sorted(tuple(int(x) for x in r.span.removeprefix("chars ").split("-"))
                   for r in rows)
    assert spans[0][0] == 0 and spans[-1][1] <= len(text) + 2
    assert store.search("Xuanming calendar", k=1)


async def test_without_a_utility_model_a_long_doc_is_read_the_old_way(store):
    """Nothing to summarise with is not a reason to refuse the document. The
    old path is cheap here anyway — `_contextualize` falls back to a string."""
    assert store.utility is None
    res = await store.ingest("book.md", text=book(60_000))
    assert not res.digested
    assert res.chunks > 40
    assert store.search("Xuanming calendar", k=1)


async def test_a_very_long_doc_costs_coarser_notes_not_more_of_them(
        summarising_store):
    """Section size grows with the document, so a novel and an article cost the
    same order of calls — the number that decides what an evening costs."""
    store = summarising_store
    await store.ingest("huge.md", text=book(600_000))
    assert len(store.calls) <= 64, f"{len(store.calls)} calls for one document"


# ------------------------------------------------ stop, resume, and watching
#
# Reading a book is the most expensive thing she does on her own initiative, it
# starts because a tool call decided it should, and until §24.3 it happened
# entirely out of sight. These are the properties the panel's three numbers and
# two buttons rest on.

async def test_the_cost_is_knowable_before_it_is_spent(summarising_store):
    store = summarising_store
    short, long_ = DOC, book(60_000)
    assert store.estimate(short)["digested"] is False
    assert store.estimate(long_)["digested"] is True
    # a call to summarise and a call to embed, per passage
    assert store.estimate(long_)["calls"] == store.estimate(long_)["passages"] * 2
    # and the estimate is the plan the ingest actually follows
    res = await store.ingest("book.md", text=long_)
    assert res.chunks == store.estimate(long_)["passages"]


async def test_she_reports_what_she_is_reading(summarising_store):
    """`progress()` is what the panel polls — it has to move, and it has to be
    None when there is nothing to watch."""
    store = summarising_store
    seen = []
    real = store._summarise

    async def watch(doc, section):
        seen.append(store.progress())
        return await real(doc, section)

    store._summarise = watch
    assert store.progress() is None
    await store.ingest("book.md", text=book(60_000))
    assert store.progress() is None, "nothing in flight once it's finished"

    assert seen[0]["doc"] == "book.md" and seen[0]["done"] == 0
    assert seen[-1]["done"] == len(seen) - 1, "the number moved, one per passage"
    assert all(p["passages"] == seen[0]["passages"] for p in seen)
    assert seen[0]["calls"] == seen[0]["passages"] * 2


async def test_progress_admits_a_stop_it_has_not_reached_yet(summarising_store):
    """A stop lands between passages, so the click and the stop are seconds and
    a model call apart. `progress()` has to carry the flag through that gap, or
    the panel's only honest option is a button that still says "stop"."""
    store = summarising_store
    real = store._summarise
    seen = []

    async def watch(doc, section):
        out = await real(doc, section)
        if len(store.calls) >= 2:
            store.stop()
        seen.append(store.progress())
        return out

    store._summarise = watch
    assert store.progress() is None
    res = await store.ingest("book.md", text=book(60_000))

    assert res.held
    assert seen[0]["stopping"] is False, "nobody has asked for anything yet"
    assert seen[-1]["stopping"] is True, "asked for, and not yet honoured"
    assert store.progress() is None, "and gone entirely once she has stopped"


async def test_stopping_keeps_what_she_read_and_parks_the_rest(summarising_store):
    """The button's whole contract: nothing is lost, and nothing is read again
    until you say so."""
    store = summarising_store
    real = store._summarise

    async def stop_after_three(doc, section):
        out = await real(doc, section)
        if len(store.calls) >= 3:
            store.stop()
        return out

    store._summarise = stop_after_three
    res = await store.ingest("book.md", text=book(60_000))

    assert res.held and res.chunks == 3
    assert len(store.inspect()) == 3, "three passages, kept and citable"
    assert store.pending_docs() == [], "and nothing pulls it back off the shelf"
    assert "book.md" in store.shelf(), "the document itself is still hers"

    (held,) = store.holds()
    assert held["doc"] == "book.md" and held["done"] == 3
    assert held["remaining_calls"] == (held["passages"] - 3) * 2


async def test_a_held_doc_is_not_read_by_the_tick(summarising_store):
    """`scan()` is what a heartbeat calls. Held means held."""
    store = summarising_store
    store._summarise = lambda d, s: _stop_now(store)
    await store.ingest("book.md", text=book(60_000))
    assert store.holds()

    store.calls.clear()
    assert await store.scan() == []
    assert store.calls == [], "not one model call for a doc you stopped"


async def _stop_now(store):
    store.stop()
    return "notes."


async def test_resuming_carries_on_rather_than_starting_again(summarising_store):
    """A resume that re-read the first twelve sections would make the button a
    lie about what it costs."""
    store = summarising_store
    real = store._summarise

    async def stop_after_two(doc, section):
        out = await real(doc, section)
        if len(store.calls) >= 2:
            store.stop()
        return out

    store._summarise = stop_after_two
    first = await store.ingest("book.md", text=book(60_000))
    assert first.held and first.chunks == 2

    store._summarise = real
    store.calls.clear()
    assert store.resume("book.md") is True
    assert store.pending_docs() == ["book.md"], "pending again, and only now"

    results = await store.scan()
    assert [r.doc for r in results] == ["book.md"]
    total = store.estimate(book(60_000))["passages"]
    assert len(store.calls) == total - 2, "it picked up where it stopped"
    assert len(store.inspect()) == total, "and the whole book is on the shelf"
    assert store.holds() == [] and store.pending_docs() == []


async def test_resuming_a_document_that_changed_starts_over(summarising_store):
    """Half of the old document is not a head start on the new one."""
    store = summarising_store
    store._summarise = lambda d, s: _stop_now(store)
    await store.ingest("book.md", text=book(60_000))

    (store.reference / "book.md").write_text(book(60_000) + "\n\nA new chapter.\n")
    store.resume("book.md")
    assert store._resume_point("book.md") == 0


async def test_a_parked_page_waits_on_the_shelf_unread(summarising_store):
    """What a stopped research run does with pages it already fetched: keep
    them, priced, and read none of them."""
    store = summarising_store
    doc = store.park("web-a-page.md", text=book(60_000))

    assert doc in store.shelf(), "the fetch wasn't wasted"
    assert store.inspect() == [], "and nothing was read"
    assert store.pending_docs() == []
    assert store.calls == []
    (held,) = store.holds()
    assert held["doc"] == doc and held["done"] == 0
    assert held["remaining_calls"] == held["passages"] * 2

    store.resume(doc)
    assert store.pending_docs() == [doc]
    assert (await store.scan())[0].chunks == held["passages"]
