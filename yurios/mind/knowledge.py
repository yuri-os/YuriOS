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

That is the treatment a page gets. A *book* gets read for notes instead — past
`LONG_DOC_CHARS` the chunk-and-situate pass is replaced by one précis per
section (`_passages`), because word-for-word costs two model calls per 1,200
characters and `research` brings home documents measured in hundreds of
thousands. The file itself is kept whole either way; what changes is only what
lands in the index, and a summarised chunk says so in its citation.

The index is derived and rebuildable from the files alone; when they disagree,
the index is discarded (Build #1's rule, held).
"""
from __future__ import annotations

import asyncio
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

#: Past this many characters a document is *read for notes* rather than
#: transcribed (`_passages`). Roughly a long feature article: short enough that
#: an ordinary page is still indexed word-for-word, long enough that the
#: threshold is only crossed by things that really are books.
LONG_DOC_CHARS = 40_000
#: How much source one précis covers. Sized for a small local model's window —
#: ~2k tokens in, a short paragraph out — since the utility tier is whatever is
#: loaded on this machine, not a model with room to spare.
DIGEST_SECTION_CHARS = 8_000
#: …and the most it may grow to. A very long document is covered by *coarser*
#: notes rather than more of them, so an encyclopaedia article and a novel cost
#: the same order of calls; past this, growing the section further would stop
#: fitting the window that made the first number safe.
DIGEST_SECTION_MAX = 16_000
#: The section count a long document is aimed at, which the two bounds above
#: are free to overrule. This is the number that decides what an evening of
#: reading costs.
DIGEST_TARGET_SECTIONS = 48


@dataclass
class Chunk:
    id: str
    doc: str             # source document name in knowledge/reference/
    span: str            # "chars a-b" — the citation target
    text: str
    context: str         # the situating blurb
    score: float = 0.0
    summary: bool = False   # her notes on that span, not the document's words

    @property
    def citation(self) -> str:
        """What she can say out loud about where this came from.

        The prompt renders `text` above `citation` and nothing else (§20.2,
        app/core/assemble.py), so a précis has to say so *here* or it arrives
        looking like a quotation with a character range attached — the one way
        this feature could make her less honest rather than more widely read.
        """
        span = f"{self.span}, summarised" if self.summary else self.span
        return f"{self.doc} ({span})"


@dataclass
class _Passage:
    """One index row before it is one: what gets embedded, and the stretch of
    the document it stands for. The two come apart the moment a row is a
    summary — 90 words of notes are the citation for 8,000 characters of
    source — which is why `end` is carried rather than derived from `text`."""
    start: int
    end: int
    text: str
    context: str
    summary: bool = False


@dataclass
class IngestResult:
    doc: str
    chunks: int
    digested: bool = False    # read for notes rather than word-for-word
    held: bool = False        # stopped partway and parked (KnowledgeStore.hold)


class KnowledgeStore:
    def __init__(self, vault: MindVault, embedder, clock: Clock, *,
                 utility: UtilityCall | None = None,
                 chunk_chars: int = 1200, min_score: float = 0.05,
                 long_doc_chars: int = LONG_DOC_CHARS):
        self.vault = vault
        self.embedder = embedder
        self.clock = clock
        self.utility = utility
        self.chunk_chars = chunk_chars
        self.min_score = min_score
        self.long_doc_chars = long_doc_chars
        self.reference = vault.vault / "knowledge" / "reference"
        self.index_path = vault.vault / "knowledge" / "index" / "chunks.jsonl"
        self.seen_path = vault.vault / "knowledge" / "index" / "ingested.json"
        #: Docs parked by hand: doc -> {size, mtime, done, state, at, reason}.
        #: See `hold` — this is the file that makes "stop" mean stopped.
        self.holds_path = vault.vault / "knowledge" / "index" / "reading.json"
        #: (size, mtime_ns) -> parsed index rows; see `_rows`.
        self._cache: tuple[tuple[int, int], list[dict]] | None = None
        #: What she is reading this second, for the inner-life panel (§24.3) —
        #: None between documents. Live rather than derived, because the whole
        #: point is watching a number move while a long read is happening.
        self._reading: dict | None = None
        #: Set by `stop()`, cleared when the read notices. Cooperative: a stop
        #: lands after the section she's on, never in the middle of a call.
        self._stop = False
        #: One reader at the shelf at a time. Store-wide rather than per-doc,
        #: because the thing being mutated is one file: `ingest` reads the whole
        #: index, drops the doc's old rows, and rewrites it whole — so two
        #: ingests of *different* docs overlapping means the one that finishes
        #: last silently deletes the other's chunks. Serialising also stops the
        #: only machine here from being asked to embed two documents at once.
        self._busy = asyncio.Lock()
        self._ensure_shelf()

    #: The knowledge index is derived and rebuildable — the same class of file as
    #: `memory/index/`, which `VAULT_GITIGNORE` excludes. It is *not* in that list
    #: because this rule has to reach vaults that already exist: a Vault's
    #: `.gitignore` is written once at seed time and never refreshed, so a line
    #: added there today protects nobody's existing shelf. A `.gitignore` inside
    #: the directory it describes needs no migration to arrive.
    INDEX_GITIGNORE = (
        "# derived, rebuildable from knowledge/reference/ alone — never committed.\n"
        "# chunks.jsonl is rewritten whole on every ingest and carries a full\n"
        "# embedding per chunk, so committing it puts a few MB of floats in the\n"
        "# Vault's history each time she reads something.\n"
        "*\n")

    def _ensure_shelf(self) -> None:
        """Make the shelf a place that exists.

        The drop folder is a *user interface* — the docs, the doctor and §20 all
        say "put a file in `knowledge/reference/`" — and until something creates
        it, that instruction is addressed to a directory the user has to guess
        the name of. `pending_docs()` and `shelf()` both answer "nothing" for a
        missing folder, which is the right answer and an indistinguishable one.

        Best-effort: a Vault on a read-only mount is a strange configuration, not
        a reason to refuse to construct the mind.
        """
        try:
            self.reference.mkdir(parents=True, exist_ok=True)
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            ignore = self.index_path.parent / ".gitignore"
            if not ignore.exists():
                ignore.write_text(self.INDEX_GITIGNORE, encoding="utf-8")
        except OSError:
            log.warning("couldn't create the knowledge shelf under %s",
                        self.vault.vault / "knowledge", exc_info=True)

    # ------------------------------------------------------------------- scan

    def pending_docs(self) -> list[str]:
        """New or changed files on the shelf, by size+mtime — the cheap check
        SENSE runs every tick without touching the index.

        A doc you stopped is not pending. That is the whole of what "stop"
        buys: the file stays on the shelf, and nothing reads it again until
        `resume` says so."""
        if not self.reference.exists():
            return []
        seen = read_json(self.seen_path, {}) or {}
        holds = read_json(self.holds_path, {}) or {}
        out = []
        for p in sorted(self.reference.iterdir()):
            if not p.is_file() or p.suffix.lower() not in SUFFIXES:
                continue
            if (holds.get(p.name) or {}).get("state") == "held":
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
        broken shelf item must never become a retry loop in the tick.

        If something is already being read, this tick does nothing rather than
        queueing behind it. A long doc is minutes of model calls; the tick that
        waits for the lock is a tick that isn't sensing, and the shelf will
        still be there next time.
        """
        if self._busy.locked():
            return []
        results = []
        for name in self.pending_docs():
            try:
                results.append(await self.ingest(name))
            except Exception as e:  # noqa: BLE001
                log.warning("ingest failed for %s: %s — leaving it on the "
                            "shelf; it retries when the file changes", name, e)
                self._mark_seen(name)
        return results

    # ------------------------------------------- stop, resume, and watching it
    #
    # Reading a long document is the most expensive thing she does off her own
    # bat, it happens because a *tool call* decided to, and until now it happened
    # entirely out of sight: a `research` call returned in 12ms and then quietly
    # spent an hour of the machine. The three methods below are what put that on
    # a page with a button next to it (SPEC §24.3) — what she's reading, what it
    # will cost, and the ability to say no without losing the document.

    def progress(self) -> dict | None:
        """What she is reading this second, or None. Cheap enough to poll.

        Carries `stopping`: the flag `stop()` raised, still standing because the
        read hasn't reached the end of its section yet. That gap is seconds of
        model call, and the panel has to be able to say so — a stop button that
        still reads "stop" for those seconds looks like a click that missed.
        """
        if not self._reading:
            return None
        return {**self._reading, "stopping": self._stop}

    def stop(self) -> bool:
        """Ask the current read to stop after the section it's on.

        Cooperative on purpose: cancelling the task mid-call would abandon a
        request the model is already answering, and the honest place to stop is
        between passages, where the work done so far is a whole number of them.
        Returns whether there was anything to stop.
        """
        if self._reading is None:
            return False
        self._stop = True
        return True

    def holds(self) -> list[dict]:
        """Every doc parked by hand, with what finishing it would cost."""
        out = []
        for doc, rec in sorted((read_json(self.holds_path, {}) or {}).items()):
            if rec.get("state") != "held":
                continue
            row = {"doc": doc, "done": rec.get("done", 0),
                   "reason": rec.get("reason", ""), "at": rec.get("at", 0),
                   "passages": rec.get("passages", 0),
                   "digested": rec.get("digested", False)}
            row["remaining_calls"] = max(
                0, row["passages"] - row["done"]) * self._calls_each()
            out.append(row)
        return out

    def hold(self, doc: str, *, done: int = 0, passages: int = 0,
             digested: bool = False, reason: str = "you stopped it") -> None:
        """Park a doc: on the shelf, out of the tick's sight, its place kept."""
        path = self.reference / doc
        st = path.stat() if path.exists() else None
        recs = read_json(self.holds_path, {}) or {}
        recs[doc] = {"state": "held", "done": done, "passages": passages,
                     "digested": digested, "reason": reason,
                     "at": self.clock.now(),
                     "size": st.st_size if st else 0,
                     "mtime": int(st.st_mtime) if st else 0}
        write_json(self.holds_path, recs)
        log.info("knowledge: holding %s after %d/%d passages (%s)",
                 doc, done, passages, reason)

    def park(self, name: str, text: str) -> str:
        """Shelve a document without reading it: it waits for you.

        The other half of stopping a research run. Pages she already fetched
        shouldn't be thrown away because you stopped the reading — they cost a
        request to somebody's web server, and re-fetching them on resume would
        cost another. The file lands on the shelf held, so nothing indexes it
        until you say so, and `holds()` can price it for the panel.
        """
        doc = self._place(name, text)
        est = self.estimate(text)
        self.hold(doc, done=0, passages=est["passages"],
                  digested=est["digested"],
                  reason="you stopped it before she read it")
        return doc

    def resume(self, doc: str) -> bool:
        """Un-park a doc. It becomes pending again and the next tick picks it
        up — carrying on from where it stopped, not from the beginning, as long
        as the file hasn't changed underneath (`_resume_point`)."""
        recs = read_json(self.holds_path, {}) or {}
        rec = recs.get(doc)
        if not rec or rec.get("state") != "held":
            return False
        rec["state"] = "resuming"
        write_json(self.holds_path, recs)
        # …and forget we ever finished with it, so `pending_docs` offers it up.
        seen = read_json(self.seen_path, {}) or {}
        if seen.pop(doc, None) is not None:
            write_json(self.seen_path, seen)
        return True

    def _resume_point(self, doc: str) -> int:
        """How many passages of `doc` are already in the index and can be kept.

        Zero unless a stopped read left a record *and* the file is byte-for-byte
        the one it was reading — a document that changed is a different document,
        and half of the old one is not a head start on it.
        """
        rec = (read_json(self.holds_path, {}) or {}).get(doc)
        path = self.reference / doc
        if not rec or not path.exists():
            return 0
        st = path.stat()
        if [rec.get("size"), rec.get("mtime")] != [st.st_size, int(st.st_mtime)]:
            return 0
        return max(0, int(rec.get("done", 0)))

    def _clear_hold(self, doc: str) -> None:
        recs = read_json(self.holds_path, {}) or {}
        if recs.pop(doc, None) is not None:
            write_json(self.holds_path, recs)

    def _calls_each(self) -> int:
        """Model calls one passage costs: an embedding, plus the utility call
        that summarises or situates it when there's a utility tier at all."""
        return 2 if self.utility is not None else 1

    def estimate(self, text: str) -> dict:
        """What reading this would cost, without reading it. The number the
        panel shows before you decide whether to let it happen."""
        plan, digested = self._plan(text)
        return {"passages": len(plan), "digested": digested,
                "calls": len(plan) * self._calls_each(),
                "chars": len(text)}

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
        content — written to the shelf first, so the shelf is the durable home.

        Two callers reach this method and they don't know about each other: a
        `research` run shelving a page it just fetched (world/research.py), and
        the loop's SENSE noticing a file on the shelf. The page lands in
        `knowledge/reference/` the moment research has it, so for the whole
        length of the ingest — a chunk is a utility call plus an embedding, and
        a long page is hundreds of chunks — the same doc *also* looks pending to
        every tick. That is how one research call became three ingests of two
        documents, all reading the same excerpts to the same local model.

        Two rules close it. The doc is **claimed before the slow part**, not
        after it, so it stops looking pending the moment somebody starts; and a
        caller that arrives while it's claimed finds it already read and takes
        the shelved answer instead of doing the work again.
        """
        doc = self._place(name, text)
        async with self._busy:
            if self._already_read(doc):
                return IngestResult(doc=doc, chunks=self._shelved_chunks(doc))
            # The shelf is the source of truth, not the argument: by the time
            # the lock is ours, what's on it is what we agreed we'd read.
            body = (self.reference / doc).read_text(encoding="utf-8",
                                                    errors="replace")
            # The claim, and what it replaced — because a claim is a promise to
            # finish, and a run that doesn't (a dead embedder, a cancelled
            # research task) has to give it back or the doc is lost: marked read
            # by nobody, never pending again, never on the shelf she can search.
            previous = (read_json(self.seen_path, {}) or {}).get(doc)
            self._mark_seen(doc)

            # The plan before the price: which passages this document becomes is
            # string arithmetic, so the cost is knowable up front — and a number
            # you can see before it is spent is the difference between a panel
            # and a spinner.
            plan, digested = self._plan(body)
            done = self._resume_point(doc)
            self._reading = {
                "doc": doc, "done": done, "passages": len(plan),
                "digested": digested, "chars": len(body),
                "calls_each": self._calls_each(),
                "calls_done": done * self._calls_each(),
                "calls": len(plan) * self._calls_each(),
                "started_at": self.clock.now(), "resumed": bool(done)}
            self._stop = False
            held = False
            try:
                # re-ingest replaces: drop the doc's old chunks first — unless
                # this is a resumed read, whose earlier passages are exactly the
                # rows we are carrying on from.
                rows = list(jsonl_read(self.index_path))
                n = self._shelved_chunks(doc) if done else 0
                if not done:
                    rows = [r for r in rows if r["doc"] != doc]
                step, notes = done, False
                async for group in self._passages(doc, plan, digested, skip=done):
                    for p in group:
                        vec = self.embedder.embed([f"{p.context}\n{p.text}"])[0]
                        rows.append({
                            "id": new_id("k"), "doc": doc,
                            "span": f"chars {p.start}-{p.end}",
                            "text": p.text, "context": p.context,
                            "summary": p.summary,
                            "embedding": list(vec),
                            "ingested_at": iso_of(self.clock.now())})
                        n += 1
                        notes = notes or p.summary
                    step += 1
                    self._reading["done"] = step
                    self._reading["calls_done"] = step * self._calls_each()
                    self._reading["chunks"] = n
                    # After the group, never before it: the work for this step is
                    # already bought and paid for, and throwing it away would make
                    # "stop" cost her the section she was in the middle of.
                    if self._stop:
                        held = True
                        break
                self._rewrite_index(rows)
                # Stopped: keep the passages she did read (they are whole, and
                # citable), and park the rest. Finished: the doc is hers, and any
                # record of it having been interrupted is now history.
                if held:
                    self.hold(doc, done=step, passages=len(plan),
                              digested=digested)
                else:
                    self._clear_hold(doc)
            except BaseException:      # cancellation included — see above
                self._release(doc, previous)
                raise
            finally:
                self._reading = None
                self._stop = False
        return IngestResult(doc=doc, chunks=n, digested=notes, held=held)

    def _place(self, name: str, text: str | None) -> str:
        """Put the content on the shelf if we were handed some, and return the
        doc name. Runs outside the lock: it's a file write, and `MindVault.write`
        is change-detecting — re-shelving a page whose bytes haven't moved
        doesn't touch the file, which is what keeps the second caller's copy from
        looking like a new document to `_already_read`."""
        if text is None:
            if not (self.reference / name).exists():
                raise FileNotFoundError(f"no such reference doc: {name}")
            return name
        safe = re.sub(r"[^\w.-]+", "_", name)[:80]
        if not safe.endswith(SUFFIXES):
            safe += ".md"
        self.vault.write(f"knowledge/reference/{safe}", text)
        return safe

    def _release(self, doc: str, previous: list | None) -> None:
        """Undo a claim, leaving `ingested.json` as the failed run found it. The
        doc goes back to pending, and `scan()`'s own handler is what decides a
        doc that keeps failing has had its chances."""
        seen = read_json(self.seen_path, {}) or {}
        if previous is None:
            seen.pop(doc, None)
        else:
            seen[doc] = previous
        write_json(self.seen_path, seen)

    def _already_read(self, doc: str) -> bool:
        """Is this exact file already indexed? Both halves are load-bearing:
        seen-and-unchanged alone would swallow a doc whose claim was never
        released (a process killed outright runs no handler), and chunks-exist
        alone would refuse to re-read a file that changed underneath them."""
        path = self.reference / doc
        if not path.exists():
            return False
        st = path.stat()
        seen = read_json(self.seen_path, {}) or {}
        if seen.get(doc) != [st.st_size, int(st.st_mtime)]:
            return False
        return any(r["doc"] == doc for r in self._rows())

    def _shelved_chunks(self, doc: str) -> int:
        return sum(1 for r in self._rows() if r["doc"] == doc)

    def _rewrite_index(self, rows: list[dict]) -> None:
        self.index_path.unlink(missing_ok=True)
        for r in rows:
            jsonl_append(self.index_path, r)
        self._cache = None

    def _rows(self) -> list[dict]:
        """The index, parsed, cached against the file's own size+mtime.

        `search()` used to be called once in a while; since §20.2 wired it into
        the knowledge slot it runs on **every turn**, and the index is JSON with
        a full embedding on every line — megabytes to re-parse for a question
        nobody asked between one turn and the next. The signature is the same
        cheap check `pending_docs()` uses on the shelf, so a fresh ingest (which
        rewrites the file) is picked up on the next search without a signal.
        """
        try:
            st = self.index_path.stat()
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            self._cache = None
            return []
        if self._cache is not None and self._cache[0] == sig:
            return self._cache[1]
        rows = list(jsonl_read(self.index_path))
        self._cache = (sig, rows)
        return rows

    def _chunk(self, text: str, budget: int | None = None):
        budget = budget or self.chunk_chars
        paras = re.split(r"\n\s*\n", text)
        buf, start, pos = [], 0, 0
        for p in paras:
            if buf and sum(len(b) for b in buf) + len(p) > budget:
                chunk = "\n\n".join(buf).strip()
                if chunk:
                    yield start, chunk
                buf, start = [], pos
            buf.append(p)
            pos += len(p) + 2
        chunk = "\n\n".join(buf).strip()
        if chunk:
            yield start, chunk

    # ------------------------------------------------- long documents (§20.1)

    def _plan(self, text: str) -> tuple[list[tuple[int, str]], bool]:
        """How this document will be read, decided before a single call is made.

        Split out from `_passages` so the cost is knowable in advance: the plan
        is pure string arithmetic, and `len(plan)` is what the panel counts down
        (`estimate`, `progress`). It is also what makes stopping and resuming
        coherent — the same text always plans to the same passages, so "she got
        to 12 of 48" survives a stop, a restart, and a resume an hour later.
        """
        if len(text) <= self.long_doc_chars or self.utility is None:
            return list(self._chunk(text)), False
        section_chars = min(DIGEST_SECTION_MAX,
                            max(DIGEST_SECTION_CHARS,
                                len(text) // DIGEST_TARGET_SECTIONS + 1))
        return list(self._chunk(text, section_chars)), True

    async def _passages(self, doc: str, plan: list[tuple[int, str]],
                        digested: bool, *, skip: int = 0):
        """The rows a document becomes — and the one decision that makes a book
        affordable.

        Word-for-word ingestion costs a utility call *and* an embedding per
        1,200 characters. That is right for the page you dropped on her shelf
        and ruinous for what `research` brings home: one encyclopaedia article
        on time came back at 365,000 characters, which is 379 chunks, which is
        758 calls to the one model on this machine — for a single page of a
        single search (world/research.py).

        So past `long_doc_chars` she reads it the way a person reads something
        that long: in sections, taking notes. One précis per ~8,000 characters,
        no separate situating call (the notes *are* the situation), and the
        span still points at the stretch of the real document it covers, so a
        citation resolves the same way it always did. The document itself stays
        on the shelf whole — this changes what she indexes, never what she kept.

        The cost is retrieval on the exact word: `search()`'s keyword half
        matches against this text, and a paraphrase is where a name goes to
        die. Hence the summariser's first instruction — keep the nouns.

        With no utility model there is nothing to summarise *with*, so this
        degrades to the old path: the chunks are still cheap there, because
        `_contextualize`'s fallback is a string operation rather than a call.

        Yields one **group** per step of the plan, rather than one passage, so
        that a step is an indivisible unit of paid-for work: `skip` resumes on
        that grid, `progress` counts on it, and a section that has to fall back
        to verbatim can produce a handful of rows without the count that says
        "12 of 48" quietly meaning something else.
        """
        for start, source in plan[skip:]:
            if not digested:
                yield [_Passage(start, start + len(source), source,
                                await self._contextualize(doc, source))]
                continue
            note = await self._summarise(doc, source)
            if note:
                first = source.strip().splitlines()[0][:80]
                yield [_Passage(start, start + len(source), note,
                                f"Notes on {doc}: {first}", summary=True)]
            else:
                # The summariser is what makes this section cheap, and it
                # didn't answer. Fall back to keeping the section verbatim —
                # its own paragraphs, blurbed without asking the thing that
                # just failed a second time. Costs embeddings for this stretch
                # and nothing else, which beats a hole in the middle of a book.
                yield [_Passage(start + offset, start + offset + len(chunk),
                                chunk,
                                f"From {doc}: {chunk.strip().splitlines()[0][:80]}")
                       for offset, chunk in self._chunk(source)]

    async def _summarise(self, doc: str, section: str) -> str:
        """One section, in her own words. "" if it couldn't be done."""
        try:
            return (await self.utility([
                {"role": "system",
                 "content": "Summarise this section of a document so it can be "
                            "found and used later. Keep names, dates, numbers "
                            "and technical terms exactly as written; drop "
                            "examples, asides and repetition. At most 120 "
                            "words. No preamble."},
                {"role": "user",
                 "content": f"Document: {doc}\n\nSection:\n{section}"},
            ])).strip()
        except Exception:  # noqa: BLE001 — one section, not the whole reading
            log.warning("knowledge: couldn't summarise a section of %s", doc,
                        exc_info=True)
            return ""

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
        rows = self._rows()
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
                             score=score, summary=r.get("summary", False)))
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
                             text=r["text"], context=r.get("context", ""),
                             summary=r.get("summary", False)))
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
