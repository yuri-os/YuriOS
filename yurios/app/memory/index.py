"""The derived retrieval index (SPEC §4.3) — a rebuildable cache, gitignored.

One SQLite database at `vault/memory/index/chunks.db`, one row per embedded
chunk, schema exactly as §4.3. Search is a flat numpy cosine scan over the
stored vectors — the spec's sanctioned FAISS/numpy-flat alternative, which at
one-user-at-human-cadence scale is instant and dependency-free. `sqlite-vec`
ANN is a drop-in *inside this class* if a Vault ever grows past that.

The markdown is authoritative; if the index and the files disagree, rebuild
the index (`scripts/reindex.py`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk(
  id          TEXT PRIMARY KEY,
  kind        TEXT,             -- 'turn' | 'summary'
  source_path TEXT,             -- which .md file it came from (traceability, §4.3)
  source_span TEXT,             -- line range within that file
  text        TEXT,
  embedding   BLOB,             -- vector(EMBED_DIM), float32
  created_at  TEXT,             -- ISO-8601 UTC
  salience    REAL
);
-- provenance: which embedder built these vectors, so a backend/model swap that
-- keeps the same dim (e.g. ollama→lm_studio nomic, both 768-d) is detectable and
-- can trigger an auto-rebuild instead of silently drifting recall (§4.3).
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


@dataclass
class Chunk:
    id: str
    kind: str
    source_path: str
    source_span: str
    text: str
    embedding: np.ndarray
    created_at: str
    salience: float
    similarity: float = 0.0  # filled by search()


class ChunkIndex:
    def __init__(self, db_path: Path, dim: int):
        self.db_path = Path(db_path)
        self.dim = dim
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()
        # snapshot at open, BEFORE any upsert re-stamps it — the startup freshness
        # check (app/main.py) compares this to the configured embedder (§4.3).
        self.stored_embedder_id = self.get_embedder_id()

    def get_embedder_id(self) -> str | None:
        """The embedder fingerprint that built these vectors, or None if unknown
        (a legacy index from before provenance was tracked)."""
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = 'embedder_id'").fetchone()
        return row[0] if row else None

    def set_embedder_id(self, value: str) -> None:
        """Stamp the fingerprint of the embedder these vectors were built with."""
        self._db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('embedder_id', ?)",
            (value,))
        self._db.commit()

    def upsert(self, *, id: str, kind: str, source_path: str, source_span: str,
               text: str, embedding: list[float], created_at: str,
               salience: float = 1.0) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        assert vec.shape == (self.dim,), \
            f"embedding is {vec.shape[0]}-d, index is {self.dim}-d (EMBED_DIM, §3.1)"
        self._db.execute(
            "INSERT OR REPLACE INTO chunk VALUES (?,?,?,?,?,?,?,?)",
            (id, kind, source_path, source_span, text, vec.tobytes(),
             created_at, salience))
        self._db.commit()

    def search(self, query_vec: list[float], limit: int) -> list[Chunk]:
        """Cosine similarity over every row (flat scan), top `limit`."""
        rows = self._db.execute("SELECT * FROM chunk").fetchall()
        if not rows:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        chunks: list[Chunk] = []
        for (cid, kind, spath, span, text, blob, created, salience) in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            v = v / (np.linalg.norm(v) or 1.0)
            chunks.append(Chunk(cid, kind, spath, span, text,
                                v, created, salience,
                                similarity=float(np.dot(q, v))))
        chunks.sort(key=lambda c: c.similarity, reverse=True)
        return chunks[:limit]

    def all(self) -> list[Chunk]:
        rows = self._db.execute("SELECT * FROM chunk").fetchall()
        return [Chunk(cid, kind, spath, span, text,
                      np.frombuffer(blob, dtype=np.float32), created, salience)
                for (cid, kind, spath, span, text, blob, created, salience) in rows]

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]

    def wipe(self) -> None:
        """Drop every row — reindex.py rebuilds from the .md files (§4.3)."""
        self._db.execute("DELETE FROM chunk")
        self._db.commit()

    def close(self) -> None:
        self._db.close()
