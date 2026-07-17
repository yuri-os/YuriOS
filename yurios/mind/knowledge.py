"""KnowledgeStore (SPEC §20) — drop-folder RAG, a sibling of memory, never
folded in.

The boundary rule is enforced by shape: **knowledge cites a document; memory
cites a conversation turn.** The book you drop in `knowledge/reference/` is
knowledge; "you told me you play bass" is memory — and each store answers
`inspect()` from its own files, so the two can never silently mix.

Drop a `.md`/`.txt` file in the folder and the loop's SENSE notices it
(`scan()`, run on the loop's cadence), chunks it, situates each chunk with a
short blurb, embeds it, and hybrid-indexes it — vector similarity blended with
a keyword idf score, because a name or an exact term should beat a vibe. Every
retrieved `Chunk` carries its source doc + character span: groundedness is
load-bearing, a citation she can actually show you.

The index is derived and rebuildable from the files alone; when they disagree,
the index is discarded (Build #1's rule, held).
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("mind.knowledge")

from yurios.world.clock import Clock

from .util import iso_of, jsonl_append, jsonl_read, new_id, read_json, write_json
from .vaultio import MindVault

UtilityCall = Callable[[list[dict]], Awaitable[str]]

_WORD_RE = re.compile(r"[a-z0-9']+")
SUFFIXES = (".md", ".txt")


@dataclass
class Chunk:
    id: str
    doc: str             # source document name in knowledge/reference/
    span: str            # "chars a-b" — the citation target
    text: str
    context: str         # the situating blurb
    score: float = 0.0

    @property
    def citation(self) -> str:
        return f"{self.doc} ({self.span})"


@dataclass
class IngestResult:
    doc: str
    chunks: int


class KnowledgeStore:
    def __init__(self, vault: MindVault, embedder, clock: Clock, *,
                 utility: UtilityCall | None = None,
                 chunk_chars: int = 1200, min_score: float = 0.05):
        self.vault = vault
        self.embedder = embedder
        self.clock = clock
        self.utility = utility
        self.chunk_chars = chunk_chars
        self.min_score = min_score
        self.reference = vault.vault / "knowledge" / "reference"
        self.index_path = vault.vault / "knowledge" / "index" / "chunks.jsonl"
        self.seen_path = vault.vault / "knowledge" / "index" / "ingested.json"

    # ------------------------------------------------------------------- scan

    def pending_docs(self) -> list[str]:
        """New or changed files on the shelf, by size+mtime — the cheap check
        SENSE runs every tick without touching the index."""
        if not self.reference.exists():
            return []
        seen = read_json(self.seen_path, {}) or {}
        out = []
        for p in sorted(self.reference.iterdir()):
            if not p.is_file() or p.suffix.lower() not in SUFFIXES:
                continue
            st = p.stat()
            sig = [st.st_size, int(st.st_mtime)]
            if seen.get(p.name) != sig:
                out.append(p.name)
        return out

    async def scan(self) -> list[IngestResult]:
        """Ingest everything pending. Tolerant by contract: a doc that fails
        (an embedder with no backend running, a mangled file) is marked seen
        with one loud WARNING and retried only when the file changes — a
        broken shelf item must never become a retry loop in the tick."""
        results = []
        for name in self.pending_docs():
            try:
                results.append(await self.ingest(name))
            except Exception as e:  # noqa: BLE001
                log.warning("ingest failed for %s: %s — leaving it on the "
                            "shelf; it retries when the file changes", name, e)
                self._mark_seen(name)
        return results

    def _mark_seen(self, doc: str) -> None:
        path = self.reference / doc
        if not path.exists():
            return
        seen = read_json(self.seen_path, {}) or {}
        st = path.stat()
        seen[doc] = [st.st_size, int(st.st_mtime)]
        write_json(self.seen_path, seen)

    # ----------------------------------------------------------------- ingest

    async def ingest(self, name: str, text: str | None = None) -> IngestResult:
        """Ingest one doc: a file already on the shelf (text=None), or given
        content — written to the shelf first, so the shelf is the durable home."""
        if text is not None:
            safe = re.sub(r"[^\w.-]+", "_", name)[:80]
            if not safe.endswith(SUFFIXES):
                safe += ".md"
            self.vault.write(f"knowledge/reference/{safe}", text)
            doc = safe
        else:
            doc = name
            path = self.reference / doc
            if not path.exists():
                raise FileNotFoundError(f"no such reference doc: {doc}")
            text = path.read_text(encoding="utf-8", errors="replace")

        # re-ingest replaces: drop the doc's old chunks first
        rows = [r for r in jsonl_read(self.index_path) if r["doc"] != doc]
        n = 0
        for start, chunk_text in self._chunk(text):
            context = await self._contextualize(doc, chunk_text)
            vec = self.embedder.embed([f"{context}\n{chunk_text}"])[0]
            rows.append({
                "id": new_id("k"), "doc": doc,
                "span": f"chars {start}-{start + len(chunk_text)}",
                "text": chunk_text, "context": context,
                "embedding": list(vec),
                "ingested_at": iso_of(self.clock.now())})
            n += 1
        self._rewrite_index(rows)
        self._mark_seen(doc)
        return IngestResult(doc=doc, chunks=n)

    def _rewrite_index(self, rows: list[dict]) -> None:
        self.index_path.unlink(missing_ok=True)
        for r in rows:
            jsonl_append(self.index_path, r)

    def _chunk(self, text: str):
        paras = re.split(r"\n\s*\n", text)
        buf, start, pos = [], 0, 0
        for p in paras:
            if buf and sum(len(b) for b in buf) + len(p) > self.chunk_chars:
                chunk = "\n\n".join(buf).strip()
                if chunk:
                    yield start, chunk
                buf, start = [], pos
            buf.append(p)
            pos += len(p) + 2
        chunk = "\n\n".join(buf).strip()
        if chunk:
            yield start, chunk

    async def _contextualize(self, doc: str, chunk: str) -> str:
        """A short situating blurb per chunk; offline fallback = the doc name
        plus the chunk's first line — enough to anchor retrieval."""
        if self.utility is None:
            first = chunk.strip().splitlines()[0][:80]
            return f"From {doc}: {first}"
        try:
            return (await self.utility([
                {"role": "system",
                 "content": "Write one sentence (<=25 words) situating this "
                            "excerpt within its document, for retrieval. "
                            "No preamble."},
                {"role": "user",
                 "content": f"Document: {doc}\n\nExcerpt:\n{chunk[:1500]}"},
            ])).strip()[:300]
        except Exception:  # noqa: BLE001 — the doc name is a fine fallback
            return f"From {doc}"

    # ----------------------------------------------------------------- search

    def search(self, query: str, k: int = 3) -> list[Chunk]:
        rows = list(jsonl_read(self.index_path))
        if not rows:
            return []
        qv = self.embedder.embed([query])[0]
        q_words = set(_WORD_RE.findall(query.lower()))

        df: Counter = Counter()                    # keyword idf over the shelf
        for r in rows:
            df.update(set(_WORD_RE.findall(r["text"].lower())) & q_words)
        n_docs = len(rows)

        out = []
        for r in rows:
            sim = _cosine(qv, r["embedding"])
            words = set(_WORD_RE.findall(r["text"].lower()))
            kw = sum(math.log(1 + n_docs / (1 + df[w])) for w in (q_words & words))
            kw_norm = kw / (1 + kw)
            score = 0.65 * sim + 0.35 * kw_norm
            if score < self.min_score:
                continue
            out.append(Chunk(id=r["id"], doc=r["doc"], span=r["span"],
                             text=r["text"], context=r.get("context", ""),
                             score=score))
        out.sort(key=lambda c: c.score, reverse=True)
        return out[:k]

    # ------------------------------------------------------- forget / inspect

    def forget(self, selector: str) -> int:
        """Drop a doc and its chunks — off the shelf and out of the index."""
        rows = list(jsonl_read(self.index_path))
        keep = [r for r in rows if selector not in r["doc"]]
        removed = len(rows) - len(keep)
        if removed:
            self._rewrite_index(keep)
            seen = read_json(self.seen_path, {}) or {}
            for p in list(self.reference.glob("*")):
                if selector in p.name:
                    p.unlink()
                    seen.pop(p.name, None)
                    self.vault.mark_dirty()
            write_json(self.seen_path, seen)
        return removed

    def inspect(self, selector: str = "") -> list[Chunk]:
        out = []
        for r in jsonl_read(self.index_path):
            if (selector and selector not in r["doc"]
                    and selector.lower() not in r["text"].lower()):
                continue
            out.append(Chunk(id=r["id"], doc=r["doc"], span=r["span"],
                             text=r["text"], context=r.get("context", "")))
        return out

    def shelf(self) -> list[str]:
        if not self.reference.exists():
            return []
        return sorted(p.name for p in self.reference.iterdir() if p.is_file())


def _cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return num / (na * nb)
