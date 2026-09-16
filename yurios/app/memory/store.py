"""MemoryStore — the ch. 19 contract, file-backed (SPEC §6).

Two homes, two jobs, never conflated:
  * `vault/soul/USER.md` — the partner model: durable, small, always injected whole.
    A living file: per-turn ops merge by topic, DREAM rewrites the narrative
    sections, and preferences about *her* become a gated PERSONA.md proposal.
  * `vault/memory/episodic/` — the journal: append-only prose events, embedded
    into the derived index for approximate recall.
Plus `memory/semantic/facts.md` (consolidated general facts; grows in DREAM
later) and `memory/semantic/forgotten.md` — the forget-ledger (§6.7).

The files are the source of truth; the index is a cache (§4). Nothing here is
rebuilt for Build #5 — the tick loop bolts onto this exact contract (§15).

Note on shape: the spec's §6.1 Protocol is the contract; `remember` and
`consolidate` are `async` here because they call the utility model — same
names, same semantics, awaited from the post-turn background task.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from yurios.app import vaultgit
from yurios.app.memory import partner
from yurios.app.memory.index import Chunk, ChunkIndex

log = logging.getLogger("mvw.memory")

MMR_LAMBDA = 0.5  # §6.4 — relevance vs. diversity trade-off


# --- contract types (§6.1) ----------------------------------------------------

@dataclass
class Record:
    """One exchange, handed to remember() after the reply streams."""
    session_id: str
    turn_index: int
    user_msg: str
    reply: str
    ts: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC))


@dataclass
class Memory:
    """One recalled/inspected memory, traceable to its source file (§6.1 inspect)."""
    text: str
    source: str          # vault-relative path (+ span) it came from
    kind: str            # 'turn' | 'summary' | 'user_md' | 'fact'
    created_at: str = ""
    similarity: float = 0.0
    salience: float = 1.0
    score: float = 0.0   # blended rank (§6.4)

    def age_days(self, now: datetime.datetime | None = None) -> float:
        if not self.created_at:
            return 0.0
        now = now or datetime.datetime.now(datetime.UTC)
        then = datetime.datetime.fromisoformat(self.created_at)
        return max((now - then).total_seconds() / 86400.0, 0.0)


@dataclass
class WriteResult:
    journal_path: str
    chunks_indexed: int
    user_md_ops: int
    quarantined: int


@dataclass
class ConsolidationReport:
    note: str
    merged: int = 0


class MemoryStore(Protocol):
    """The ch. 19 contract (§6.1). Build #5 wraps a tick loop around the same
    five verbs; a PgVectorMemoryStore is a legal drop-in (§6.6); a cloud memory
    service is not — it cannot answer inspect() ownably."""

    async def remember(self, record: Record) -> WriteResult: ...
    def recall(self, query: str, k: int) -> list[Memory]: ...
    async def consolidate(self) -> ConsolidationReport: ...
    def forget(self, selector: str) -> int: ...
    def inspect(self, selector: str = "") -> list[Memory]: ...


# --- the file backend (§6) ------------------------------------------------------

class FileMemoryStore:
    def __init__(self, vault: Path, embedder, utility=None, *,
                 char_name: str = "yuri", user_name: str = "you",
                 embed_dim: int = 384,
                 retrieval_min_sim: float = 0.25,
                 half_life_days: float = 30.0,
                 utility_log=None):
        self.vault = Path(vault)
        self.embedder = embedder
        self.utility = utility  # None ⇒ partner-model updates are skipped (tests)
        self.utility_log = utility_log  # None ⇒ no transparency sidecar (tests)
        self.char_name = char_name
        self.user_name = user_name
        # Whose names the slot matcher should discount. A host holds every
        # character on the node, so this cannot be a constant in partner.py —
        # it was "yuri", and every other character paid for the difference.
        self.names = (char_name, user_name)
        self.retrieval_min_sim = retrieval_min_sim
        self.half_life_days = half_life_days
        self.index = ChunkIndex(self.vault / "memory" / "index" / "chunks.db",
                                dim=embed_dim)
        self.quarantine = partner.Quarantine(
            self.vault / "state" / "quarantine.json", self.names)

    # -- paths --
    @property
    def user_md_path(self) -> Path:
        return self.vault / "soul" / "USER.md"

    @property
    def facts_path(self) -> Path:
        return self.vault / "memory" / "semantic" / "facts.md"

    @property
    def forgotten_path(self) -> Path:
        return self.vault / "memory" / "semantic" / "forgotten.md"

    def read_user_md(self) -> str:
        return self.user_md_path.read_text(encoding="utf-8") \
            if self.user_md_path.exists() else ""

    def read_summary(self) -> str:
        p = self.vault / "memory" / "summary.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def partner_evolution_pending(self) -> bool:
        """Whether USER.md contains learned state beyond a fresh card's seed."""
        current = self.read_user_md()
        if not current.strip():
            return False
        _, sections = partner.parse_user_md(current)
        # A fresh card's headings and early phase are scaffolding, not learned
        # state. Scheduling them would create maintenance work before the first
        # conversation. Keep this knowledge beside the partner parser so the
        # mind layer does not import characters through it and form a cycle.
        return bool(
            partner.all_bullets(sections)
            or partner.scrub(sections.get(partner.WHO, ""))
            or partner.phase_of(sections.get(partner.PHASE, "")) not in ("", "early")
        )

    # -- remember (§6.2) --------------------------------------------------------

    def _journal_append(self, record: Record) -> tuple[str, str]:
        """Step 1: append the exchange to memory/episodic/<today>.md as a dated
        prose event. Append-only. Returns (vault-relative path, line span)."""
        day = record.ts.strftime("%Y-%m-%d")
        rel = f"memory/episodic/{day}.md"
        path = self.vault / rel
        one = lambda s: " / ".join(s.strip().splitlines())
        entry = (f"### {record.ts.strftime('%H:%M')}  "
                 f"{self.user_name}: {one(record.user_msg)}  ⇄  "
                 f"{self.char_name}: {one(record.reply)}\n")
        before = path.read_text(encoding="utf-8").count("\n") if path.exists() else 0
        if not path.exists():
            vaultgit.atomic_write(path, f"# Journal — {day}\n\n")
            before = 2
        vaultgit.atomic_append(path, entry)
        return rel, f"{before + 1}-{before + 1}"

    async def remember(self, record: Record) -> WriteResult:
        """journal → index → partner model (§6.2). Tolerant by contract: a
        malformed utility reply is logged and dropped, never fatal to the turn."""
        rel, span = self._journal_append(record)

        # 2. embed + upsert one chunk row, traceable back to the journal line
        text = f"{self.user_name}: {record.user_msg}\n{self.char_name}: {record.reply}"
        embed = self.embedder.embed
        if getattr(self.embedder, "ready", True):
            vec = embed([text])[0]
        else:
            # Off the loop: a cold load is tens of seconds, and this host is
            # holding every other character's room open while it runs (§2.4).
            vec = (await asyncio.to_thread(embed, [text]))[0]
        self.index.upsert(
            id=f"turn-{record.session_id}-{record.turn_index}",
            kind="turn", source_path=rel, source_span=span, text=text,
            embedding=vec,
            created_at=record.ts.isoformat(), salience=1.0)

        # 3. partner model — this is where USER.md grows (§6.3)
        applied, held = 0, 0
        if self.utility is not None:
            try:
                raw, ops = await partner.extract_ops(
                    self.utility, self.read_user_md(), text)
                apply_now, quarantined = self.quarantine.triage(ops)
                if apply_now:
                    new_md = partner.apply_ops(
                        self.read_user_md(), apply_now, self.names)
                    vaultgit.atomic_write(self.user_md_path, new_md)
                    self._record_persona_delta(new_md, apply_now)
                applied, held = len(apply_now), len(quarantined)
                # transparency sidecar: what the model proposed and how triage
                # handled it — makes "why did this fact land?" answerable (§6.3)
                if self.utility_log is not None:
                    self.utility_log.log(
                        kind="extract", exchange=text, raw_reply=raw,
                        parsed=[asdict(o) for o in ops],
                        applied=[asdict(o) for o in apply_now],
                        quarantined=[asdict(o) for o in quarantined])
            except Exception:
                log.exception("partner-model update dropped (non-fatal, §6.2)")

        return WriteResult(journal_path=rel, chunks_indexed=1,
                           user_md_ops=applied, quarantined=held)

    def _record_persona_delta(self, user_md: str, ops: list[partner.Op]) -> None:
        """Preferences about *her* wait in state/persona_delta.json for the
        mind's gated PERSONA.md door. USER.md is ours to write; her identity
        is not (§23.3).

        Which ops those are is the extractor's `about_her` — the same model call
        that already read the turn, so the hot path stays one call (§6.2).
        """
        learned = {o.text: "learned" for o in ops
                   if o.about_her and o.op != "remove" and o.text}
        if not learned:
            return
        _, sections = partner.parse_user_md(user_md)
        delta = partner.persona_delta_from(sections, classified=learned,
                                           names=self.names)
        if delta is not None:
            partner.write_persona_delta(self.vault, delta, merge=True,
                                        names=self.names)

    @property
    def filing_path(self) -> Path:
        return self.vault / "state" / "partner_filing.json"

    async def _filing(self, bullets: list[str]) -> dict[str, str]:
        """`classify_bullets`, cached on the exact bullets it was asked about.

        The cache is keyed per bullet, so a night that added one line pays for
        one line rather than the whole file, and a bullet whose wording changed
        is simply a bullet the cache has never seen.

        It is also stamped with the rules it was judged under. The prompt *is*
        the implementation of this judgement, so tightening it is a behaviour
        change — and without the stamp every bullet already in the file would
        keep the verdict the old wording gave it, forever, while new bullets
        got the new one. A changed prompt is a cold cache.
        """
        cached: dict[str, str] = {}
        if self.filing_path.is_file():
            try:
                data = json.loads(self.filing_path.read_text(encoding="utf-8"))
                if data.get("rules") == partner.CLASSIFY_FINGERPRINT:
                    cached = {k: v for k, v in (data.get("labels") or {}).items()
                              if isinstance(k, str) and v in partner.LABELS}
            except (json.JSONDecodeError, AttributeError, OSError):
                cached = {}     # a corrupt cache is a cold cache, never a crash
        fresh = [b for b in bullets if b not in cached]
        if fresh:
            cached.update(await partner.classify_bullets(self.utility, fresh))
        keep = {b: cached[b] for b in bullets if b in cached}
        if fresh or keep != cached:   # also drops labels for bullets now gone
            vaultgit.atomic_write(self.filing_path, json.dumps(
                {"rules": partner.CLASSIFY_FINGERPRINT, "labels": keep}, indent=2))
        return keep

    async def evolve_partner(self, *, days_together: int = 0) -> bool:
        """DREAM rewrite of USER.md (§6.3, §21): one bullet per topic, phase
        from evidence, Who filled if it was still an empty slot. Returns True
        when the file changed.

        Async because the filing pass is a model call — one, off the hot path,
        which is what a DREAM is for. With no utility model the bullets stay in
        the sections they are in and the rest of the compact still runs.
        """
        current = self.read_user_md()
        if not current.strip():
            return False
        if days_together <= 0:
            days_together = sum(1 for _ in (self.vault / "memory" / "episodic").glob("*.md"))
        _, sections = partner.parse_user_md(current)
        bullets = partner.all_bullets(sections)
        # Every night is a DREAM, and most nights the bullets are yesterday's.
        # Re-asking the model where they go would buy the same answer for the
        # price of a call, so the filing is cached against the bullets it read
        # and only the rest of the compact (phase, Who, collapse) runs.
        classified = await self._filing(bullets)
        # The model call yields to edits and forget(). Merge against the file
        # as it stands now, and only reuse labels for unchanged bullets.
        current = self.read_user_md()
        if not current.strip():
            return False
        _, sections = partner.parse_user_md(current)
        classified = {b: classified[b] for b in partner.all_bullets(sections)
                      if b in classified}
        new_md, delta = partner.compact_user_md(
            current, days_together=days_together, classified=classified,
            names=self.names)
        changed = new_md != current
        if changed:
            vaultgit.atomic_write(self.user_md_path, new_md)
        if delta is not None:
            partner.write_persona_delta(self.vault, delta)
        return changed

    # -- recall (§6.4, the hot path) ---------------------------------------------

    def _recency(self, created_at: str, now: datetime.datetime) -> float:
        """exp(-age_days / HALF_LIFE_DAYS) — old memories fade, never vanish."""
        try:
            age = (now - datetime.datetime.fromisoformat(created_at)).total_seconds() / 86400
        except ValueError:
            return 1.0
        return math.exp(-max(age, 0.0) / self.half_life_days)

    @staticmethod
    def _mmr(chunks: list[Chunk], k: int, lam: float = MMR_LAMBDA) -> list[Chunk]:
        """Maximal Marginal Relevance — diversify so recall surfaces the small
        load-bearing detail, not k paraphrases of one memory (→ ch. 15)."""
        selected: list[Chunk] = []
        pool = list(chunks)
        while pool and len(selected) < k:
            def mmr_score(c: Chunk) -> float:
                redundancy = max(
                    (float(np.dot(c.embedding, s.embedding)
                           / ((np.linalg.norm(c.embedding) or 1)
                              * (np.linalg.norm(s.embedding) or 1)))
                     for s in selected), default=0.0)
                return lam * c.similarity - (1 - lam) * redundancy
            best = max(pool, key=mmr_score)
            pool.remove(best)
            selected.append(best)
        return selected

    def recall(self, query: str, k: int = 6) -> list[Memory]:
        if self.index.count() == 0:
            return []   # empty Vault ⇒ []; assembly proceeds on SOUL + USER.md alone
        if not getattr(self.embedder, "ready", True):
            # Weights still loading (SPEC §2.4): same as empty, not a freeze of
            # the event loop and not a failed turn.
            return []
        now = datetime.datetime.now(datetime.UTC)
        q = self.embedder.embed([query])[0]
        rows = self.index.search(q, limit=k * 4)
        rows = [r for r in rows if r.similarity >= self.retrieval_min_sim]
        # tombstoned memories are gone from every future prompt (§6.7)
        stones = [t.lower() for t in self.tombstones()]
        rows = [r for r in rows
                if not any(t in r.text.lower() for t in stones)]
        # blended rank, not raw similarity (§6.4)
        rows.sort(key=lambda r: r.similarity * r.salience
                  * self._recency(r.created_at, now), reverse=True)
        rows = self._mmr(rows, k)
        return [Memory(text=r.text, source=f"{r.source_path}:{r.source_span}",
                       kind=r.kind, created_at=r.created_at,
                       similarity=r.similarity, salience=r.salience,
                       score=r.similarity * r.salience
                       * self._recency(r.created_at, now))
                for r in rows]

    # -- consolidate (stub in Build #1) -------------------------------------------

    async def consolidate(self) -> ConsolidationReport:
        """The mind's DreamConsolidator owns the journal pass; this slot is the
        partner-model rewrite that used to be a stub, so a caller without a
        mind still gets a living USER.md."""
        changed = await self.evolve_partner()
        return ConsolidationReport(
            note="partner model rewritten" if changed else "partner model unchanged",
            merged=int(changed))

    # -- forget (§6.7, the covenant) -----------------------------------------------

    def tombstones(self) -> list[str]:
        """Texts in the forget-ledger. Never read into the system prompt; used
        only to suppress recall (§6.7)."""
        if not self.forgotten_path.exists():
            return []
        out = []
        for line in self.forgotten_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"###\s+\d{4}-\d{2}-\d{2}\s+forgot:\s*(.+)", line)
            if m:
                text = m.group(1)
                # strip the trailing "— <why/who asked>" if present
                text = text.rsplit("  —", 1)[0].strip()
                out.append(text)
        return out

    def forget(self, selector: str, why: str = "asked to forget") -> int:
        """Supersede-not-delete (→ ch. 15): remove matching lines from the
        working USER.md / facts.md, append a tombstone to forgotten.md, commit.
        The old value survives in `git log` (auditability) but is gone from
        every future prompt. Returns the count of memories superseded."""
        sel = selector.lower().strip()
        removed = 0
        for path in (self.user_md_path, self.facts_path):
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            if path == self.user_md_path:
                # USER.md now holds facts in prose too, including the name
                # copied into Who and the phase. Keep its YAML and headings.
                front, sections = partner.parse_user_md("\n".join(lines))
                count = 0
                for heading, body in sections.items():
                    body_lines = body.splitlines()
                    kept_body = [ln for ln in body_lines if sel not in ln.lower()]
                    count += len(body_lines) - len(kept_body)
                    sections[heading] = "\n".join(kept_body)
                if count:
                    removed += count
                    vaultgit.atomic_write(path, partner.render_user_md(front, sections))
                continue
            kept = [ln for ln in lines
                    if not (ln.lstrip().startswith("- ") and sel in ln.lower())]
            if len(kept) != len(lines):
                removed += len(lines) - len(kept)
                vaultgit.atomic_write(path, "\n".join(kept).rstrip() + "\n")
        # episodic chunks that mention it are suppressed at recall, count them too
        suppressed = sum(1 for c in self.index.all() if sel in c.text.lower())
        today = datetime.date.today().isoformat()
        vaultgit.atomic_append(
            self.forgotten_path,
            f"### {today}  forgot: {selector}  — {why}\n")
        # you asked for this one, by name: it goes into the diary as itself
        # rather than waiting to be swept under the next tick's message (§2.1)
        vaultgit.commit(self.vault, f"forget: {selector}", now=True)
        return removed + suppressed

    # -- inspect (§6.1 — what she knows, and why) -----------------------------------

    def inspect(self, selector: str = "") -> list[Memory]:
        """The audit surface: every matching memory with its source. The
        dashboard/debug view reads through this, never around it."""
        sel = selector.lower()
        out: list[Memory] = []
        if self.user_md_path.exists():
            for ln in self.user_md_path.read_text(encoding="utf-8").splitlines():
                if ln.lstrip().startswith("- ") and sel in ln.lower():
                    out.append(Memory(text=ln.lstrip("- ").strip(),
                                      source="soul/USER.md", kind="user_md"))
        if self.facts_path.exists():
            for ln in self.facts_path.read_text(encoding="utf-8").splitlines():
                if ln.lstrip().startswith("- ") and sel in ln.lower():
                    out.append(Memory(text=ln.lstrip("- ").strip(),
                                      source="memory/semantic/facts.md", kind="fact"))
        for c in self.index.all():
            if sel in c.text.lower():
                out.append(Memory(text=c.text,
                                  source=f"{c.source_path}:{c.source_span}",
                                  kind=c.kind, created_at=c.created_at,
                                  salience=c.salience))
        return out
