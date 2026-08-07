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
