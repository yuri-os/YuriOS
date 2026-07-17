"""Rebuild `vault/memory/index/` from the .md files alone (SPEC §4.3).

The markdown is authoritative; the index is a derived cache. Rebuild it after
hand-editing journals, on a fresh clone of a Vault, or whenever the embedder that
built the vectors no longer matches the configured one (the startup freshness
check in app/main.py calls this automatically). `scripts/reindex.py` is the CLI.
"""
from __future__ import annotations

import re
from pathlib import Path

# one journal line = one embeddable event (§6.2 format)
EVENT_RE = re.compile(r"^### (\d{2}:\d{2})\s+(.*)$")


def reindex(vault: Path, embedder=None, embed_dim: int | None = None,
            *, embedder_id: str | None = None, index=None) -> int:
    """Wipe + rebuild. Returns the number of chunks indexed.

    `embedder`/`embed_dim` are injectable (tests, and the startup check passes the
    already-built ones); left None they come from the configured backend. `index`
    lets a caller reuse an open ChunkIndex (same SQLite connection) instead of
    opening a second one. `embedder_id` is stamped as the vectors' provenance so a
    later backend/model swap is detectable (§4.3)."""
    from yurios.app.memory.index import ChunkIndex

    vault = Path(vault)
    if embedder is None:
        from yurios.app.config import Config
        from yurios.app.main import _default_embedder
        cfg = Config()
        embedder, embed_dim = _default_embedder(cfg), cfg.embed_dim
        embedder_id = embedder_id or _embedder_id(cfg)

    own_index = index is None
    if own_index:
        index = ChunkIndex(vault / "memory" / "index" / "chunks.db",
                           dim=embed_dim or embedder.dim)
    index.wipe()
    n = 0

    for journal in sorted((vault / "memory" / "episodic").glob("*.md")):
        day = journal.stem  # YYYY-MM-DD
        for lineno, line in enumerate(
                journal.read_text(encoding="utf-8").splitlines(), start=1):
            m = EVENT_RE.match(line)
            if not m:
                continue
            hhmm, text = m.groups()
            text = text.replace("  ⇄  ", "\n")
            index.upsert(
                id=f"reindex-{day}-{lineno}",
                kind="turn",
                source_path=f"memory/episodic/{journal.name}",
                source_span=f"{lineno}-{lineno}",
                text=text,
                embedding=embedder.embed([text])[0],
                created_at=f"{day}T{hhmm}:00+00:00",
                salience=1.0)
            n += 1

    summary = vault / "memory" / "summary.md"
    if summary.exists() and summary.read_text(encoding="utf-8").strip():
        text = summary.read_text(encoding="utf-8").strip()
        index.upsert(id="reindex-summary", kind="summary",
                     source_path="memory/summary.md", source_span="1-",
                     text=text, embedding=embedder.embed([text])[0],
                     created_at=f"{day}T23:59:00+00:00" if n else
                     "1970-01-01T00:00:00+00:00",
                     salience=2.0)
        n += 1

    if embedder_id:
        index.set_embedder_id(embedder_id)
    if own_index:
        index.close()
    return n


def _embedder_id(cfg) -> str:
    """The provenance fingerprint for a config's embedder: backend:model:dim.
    A change in any part means the stored vectors were built differently (§4.3)."""
    return f"{cfg.embed_backend}:{cfg.embed_model}:{cfg.embed_dim}"
