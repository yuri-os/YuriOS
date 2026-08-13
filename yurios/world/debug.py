"""Reading one character's mind off disk (SPEC §24.3).

The rule this module exists to keep: *everything reads through the mind's own
stores, so the dashboard can never disagree with the files.* Nothing here
derives a fact she did not write down. The activity timeline is her activity
log, the goals are parsed by her own `GoalStore`, the diary is `parse_day_entries`
— the same function the journal uses. Where a value genuinely only exists in a
running process (the live context meter), it is reported separately and marked
as such, never blended into the history.

The second rule is that none of it needs her to be running. `/api/mind` answers
503 when the loop is off, which is precisely when you most want to read what
happened. Every reader here takes a `CharacterRecord` and touches files, so a
stopped, crashed or archived character is still fully inspectable.

Paging is newest-first everywhere, over `mind/util.jsonl_page`, which reads
backwards from the end of the file — these logs are appended to forever and a
debug page must not pull one into memory to show you its last twenty rows.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from yurios.app import vaultgit
from yurios.mind.journal import canonical_day, is_canonical_day, parse_day_entries
from yurios.mind.util import jsonl_page, read_json

#: Per-view page ceilings. Generous, but bounded — a debug page asking for
#: 100000 rows is a bug, and answering it would be a worse one.
MAX_LIMIT = 200
DEFAULT_LIMIT = 25

#: The logs this page reads, and where they live under a character root. Also
#: what the overview's file manifest walks, so a new sink shows up there by
#: being added here and nowhere else.
SOURCES: dict[str, tuple[str, str]] = {
    "ticks": ("traces", "ticks.jsonl"),
    "activity": ("traces", "activity.jsonl"),
    "signals": ("traces", "signals.jsonl"),
    "context": ("traces", "context.jsonl"),
    "prompts": ("traces", "prompts.jsonl"),
    "calls": ("tool_logs", "calls.jsonl"),
    "turns": ("corpus", "turns.jsonl"),
    "utility": ("corpus", "utility.jsonl"),
    "ratings": ("corpus", "ratings.jsonl"),
    "generations": ("selfies", "generations.jsonl"),
}


def source(record, name: str) -> Path:
    directory, filename = SOURCES[name]
    return Path(getattr(record.paths, directory)) / filename


def clamp(page: int, limit: int) -> tuple[int, int]:
    return max(0, page), max(1, min(limit, MAX_LIMIT))


def paged(path: Path, *, page: int, limit: int,
          match: Callable[[dict], bool] | None = None,
          shape: Callable[[dict], dict] | None = None) -> dict:
    """The one response envelope. A missing file is an empty page, never a 404 —
    a character who has not run yet has nothing to say, which is not an error."""
    page, limit = clamp(page, limit)
    items, has_more, total = jsonl_page(path, page=page, limit=limit,
                                        match=match, shape=shape)
    return {"items": items, "page": page, "limit": limit,
            "has_more": has_more, "total": total}


def _matcher(**equals) -> Callable[[dict], bool] | None:
    """Filter on exact field values, ignoring the ones left unset."""
    wanted = {k: v for k, v in equals.items() if v}
    if not wanted:
        return None
    return lambda row: all(row.get(k) == v for k, v in wanted.items())


# --- overview -----------------------------------------------------------------

def manifest(record) -> list[dict]:
    """Every log, its size, and whether a rotated generation exists beside it.

    The page reads only the live file (a rolled `.1` is deliberately not merged
    in), so it has to be able to *say* that older records exist and are not
    being shown. Silently truncated history is worse than none."""
    out = []
    for name in SOURCES:
        path = source(record, name)
        exists = path.is_file()
        out.append({
            "name": name,
            "path": f"{SOURCES[name][0]}/{SOURCES[name][1]}",
            "bytes": path.stat().st_size if exists else 0,
            "mtime": path.stat().st_mtime if exists else None,
            "rotated": path.with_name(path.name + ".1").is_file(),
        })
    return out


def counts(record) -> dict:
    from yurios.mind.util import jsonl_count
    return {name: jsonl_count(source(record, name)) for name in SOURCES}


def overview(record, runtime=None) -> dict:
    vault = Path(record.paths.vault)
    state = vault / "state"
    return {
        "character": record.id,
        "activity": read_json(state / "activity.json", {}) or {},
        "budget": read_json(state / "budget.json", {}) or {},
        "engine": read_json(state / "engine.json", {}) or {},
        "vault": {"head": vaultgit.head(vault),
                  "commits": vaultgit.count_commits(vault)},
        "counts": counts(record),
        "files": manifest(record),
        # The one genuinely live value. Null when she is stopped, and kept in its
        # own field so nothing on the page confuses it for history.
        "live": ({"context": runtime.context.snapshot()} if runtime else None),
    }


# --- the ladder, the trace, the inbox -----------------------------------------

def activity(record, *, page: int = 0, limit: int = 100) -> dict:
    out = paged(source(record, "activity"), page=page, limit=limit)
    out["current"] = read_json(Path(record.paths.vault) / "state" / "activity.json",
                               {}) or {}
    return out


def ticks(record, *, page: int = 0, limit: int = DEFAULT_LIMIT,
          state: str | None = None, q: str | None = None) -> dict:
    needle = (q or "").lower()

    def match(row: dict) -> bool:
        if state and row.get("activity_state") != state:
            return False
        if needle and needle not in json.dumps(row, default=str).lower():
            return False
        return True

    return paged(source(record, "ticks"), page=page, limit=limit,
                 match=match if (state or needle) else None)


def tick_detail(record, tick_id: str) -> dict | None:
    """One tick, with everything it caused.

    This is the join the correlation id was added for: before it, lining a tool
    call up with the tick that decided on it meant comparing timestamps across
    two files that stamped time in different units."""
    found, _, _ = jsonl_page(source(record, "ticks"), limit=1,
                             match=lambda r: r.get("tick_id") == tick_id)
    if not found:
        return None
    tick = found[0]
    by_tick = lambda r: r.get("tick_id") == tick_id     # noqa: E731
    calls, _, _ = jsonl_page(source(record, "calls"), limit=MAX_LIMIT, match=by_tick)
    prompts, _, _ = jsonl_page(source(record, "prompts"), limit=MAX_LIMIT,
                               match=by_tick, shape=strip_messages)
    # SENSE records the signal ids it drained, so the inbox rows behind a tick
    # are a lookup rather than a time-window guess.
    sensed = {s.get("id") for s in tick.get("sensed", []) if isinstance(s, dict)}
    signals: list[dict] = []
    if sensed:
        signals, _, _ = jsonl_page(source(record, "signals"), limit=MAX_LIMIT,
                                   match=lambda r: r.get("id") in sensed)
    return {"tick": tick, "calls": calls, "prompts": prompts, "signals": signals}


def signals(record, *, page: int = 0, limit: int = 100,
            type: str | None = None) -> dict:
    return paged(source(record, "signals"), page=page, limit=limit,
                 match=_matcher(type=type))


# --- her hands ----------------------------------------------------------------

def calls(record, *, page: int = 0, limit: int = 50, tool: str | None = None,
          verdict: str | None = None, corr_id: str | None = None) -> dict:
    out = paged(source(record, "calls"), page=page, limit=limit,
                match=_matcher(tool=tool, verdict=verdict, corr_id=corr_id))
    photos = _generations_by_corr(record)
    for row in out["items"]:
        photo = photos.get(row.get("corr_id"))
        if photo:
            row["selfie"] = photo
    return out


def _generations_by_corr(record) -> dict[str, dict]:
    """The render ledger, keyed by the turn that asked for the photo.

    A render finishes long after the sentence that started it, so this is the
    join that puts the image next to the tool call instead of leaving them as
    two events that happened near each other."""
    rows, _, _ = jsonl_page(source(record, "generations"), limit=MAX_LIMIT)
    out = {}
    for row in rows:
        corr = row.get("corr_id")
        if corr and corr not in out:            # newest render for that turn wins
            out[corr] = _selfie(record, row)
    return out


def _selfie(record, row: dict) -> dict:
    return {"image": row.get("image"),
            "url": f"/api/characters/{record.id}/selfies/{row.get('image', '')}",
            "backend": row.get("backend"), "model": row.get("model"),
            "seed": row.get("seed"), "prompt": row.get("prompt"),
            "negative": row.get("negative"), "created_at": row.get("created_at"),
            "selfie_id": row.get("selfie_id"), "corr_id": row.get("corr_id")}


def selfies(record, *, page: int = 0, limit: int = 24) -> dict:
    return paged(source(record, "generations"), page=page, limit=limit,
                 shape=lambda row: _selfie(record, row))


# --- the context windows ------------------------------------------------------

def strip_messages(row: dict) -> dict:
    """An index row: everything except the thing that makes it expensive.

    Applied inside the pager, so a page of twenty-five prompts never holds
    twenty-five assembled contexts in memory at once."""
    messages = row.get("messages")
    preview = ""
    if isinstance(messages, list) and messages:
        preview = (messages[-1].get("content") or "")[:200]
    return {**{k: v for k, v in row.items() if k != "messages"},
            "has_messages": bool(messages) or bool(row.get("messages_ref")),
            "preview": preview or (row.get("cue") or "")[:200]}


def prompt_days(record, *, page: int = 0, limit: int = 20) -> dict:
    """The day index. Walks the whole log once — it is the one view that has to,
    and it is the entry point users open rarely and then page within."""
    page, limit = clamp(page, limit)
    days: dict[str, dict] = {}
    rows, _, _ = jsonl_page(source(record, "prompts"), limit=10 * MAX_LIMIT,
                            shape=lambda r: {"ts": r.get("ts"), "kind": r.get("kind")})
    for row in rows:
        day = (row.get("ts") or "")[:10]
        if not day:
            continue
        entry = days.setdefault(day, {"day": day, "count": 0, "kinds": {}})
        entry["count"] += 1
        entry["kinds"][row.get("kind") or "?"] = \
            entry["kinds"].get(row.get("kind") or "?", 0) + 1
    ordered = sorted(days.values(), key=lambda d: d["day"], reverse=True)
    start = page * limit
    return {"items": ordered[start:start + limit], "page": page, "limit": limit,
            "has_more": start + limit < len(ordered), "total": len(ordered)}


def prompts(record, *, day: str | None = None, kind: str | None = None,
            page: int = 0, limit: int = DEFAULT_LIMIT) -> dict:
    def match(row: dict) -> bool:
        if day and (row.get("ts") or "")[:10] != day:
            return False
        if kind and row.get("kind") != kind:
            return False
        return True

    return paged(source(record, "prompts"), page=page, limit=limit,
                 match=match if (day or kind) else None, shape=strip_messages)


def prompt_detail(record, prompt_id: str) -> dict | None:
    found, _, _ = jsonl_page(source(record, "prompts"), limit=1,
                             match=lambda r: r.get("id") == prompt_id)
    if not found:
        return None
    row = found[0]
    ref = row.get("messages_ref")
    if row.get("messages") is None and isinstance(ref, dict) and ref.get("id"):
        # A chat turn keeps its body in the corpus, where ratings.jsonl joins to
        # it. Resolve on demand: a single-record fetch, never a list operation.
        turn = _corpus_turn(record, ref["id"])
        if turn is not None:
            row["messages"] = turn.get("messages")
            row["completion"] = row.get("completion") or turn.get("completion")
            row["resolved_from"] = ref.get("file")
    return row


def _corpus_turn(record, turn_id: str) -> dict | None:
    found, _, _ = jsonl_page(source(record, "turns"), limit=1,
                             match=lambda r: r.get("id") == turn_id)
    return found[0] if found else None


# --- her own stores, read with her own parsers --------------------------------

def goals(record) -> dict:
    """Parsed by `GoalStore`, not by a second reader written for this page —
    a dashboard that parses `goals.md` its own way is a dashboard that will
    eventually disagree with her about what she meant to do (SPEC §24.3)."""
    from dataclasses import asdict

    from yurios.mind.goals import GoalStore
    from yurios.mind.vaultio import MindVault
    from yurios.world.clock import Clock
    try:
        store = GoalStore(MindVault(Path(record.paths.vault)), Clock())
        items = [asdict(g) for g in store.all()]
    except Exception:
        items = []
    return {"items": list(reversed(items)), "total": len(items)}


def self_edits(record) -> dict:
    vault = Path(record.paths.vault)
    pending = read_json(vault / "state" / "pending_edits.json", []) or []
    if isinstance(pending, dict):
        pending = pending.get("edits", []) or list(pending.values())
    return {"pending": pending,
            # her rulings are not a table anywhere: git is the record
            "history": vaultgit.log_records(vault, limit=25, path="soul")}


def journal_days(record) -> list[str]:
    episodic = Path(record.paths.vault) / "memory" / "episodic"
    if not episodic.is_dir():
        return []
    return sorted((p.stem for p in episodic.glob("*.md")
                   if is_canonical_day(p.stem)), reverse=True)


# --- the vault ----------------------------------------------------------------

def vault_commits(record, *, page: int = 0, limit: int = DEFAULT_LIMIT,
                  path: str | None = None) -> dict:
    page, limit = clamp(page, limit)
    vault = Path(record.paths.vault)
    items = vaultgit.log_records(vault, skip=page * limit, limit=limit, path=path)
    total = vaultgit.count_commits(vault, path=path)
    return {"items": items, "page": page, "limit": limit,
            "has_more": (page + 1) * limit < total, "total": total}


def memory(record) -> dict:
    vault = Path(record.paths.vault)
    read = lambda rel: (vault / rel).read_text(encoding="utf-8", errors="replace") \
        if (vault / rel).is_file() else ""                          # noqa: E731
    beliefs, _, _ = jsonl_page(vault / "world" / "beliefs.jsonl", limit=100)
    knowledge = vault / "knowledge" / "reference"
    return {
        "chunks": chunk_stats(record),
        "summary": read("memory/summary.md"),
        "facts": read("memory/semantic/facts.md"),
        "forgotten": read("memory/semantic/forgotten.md"),
        "situation": read("world/situation.md"),
        "state": read_json(vault / "world" / "state.json", {}) or {},
        "beliefs": beliefs,
        "knowledge": sorted(
            ({"name": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
             for p in knowledge.glob("*") if p.is_file()),
            key=lambda d: d["mtime"], reverse=True) if knowledge.is_dir() else [],
        "journal_days": journal_days(record),
    }


def journal_day(record, day: str) -> dict:
    day = canonical_day(day)
    path = Path(record.paths.vault) / "memory" / "episodic" / f"{day}.md"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    entries = parse_day_entries(text)
    entries.reverse()
    return {"day": day, "entries": entries}


# --- the recall index ---------------------------------------------------------

def _chunk_db(record) -> Path:
    return Path(record.paths.vault) / "memory" / "index" / "chunks.db"


def _read_only(path: Path) -> sqlite3.Connection:
    """Opened read-only, so a debug page can never damage the index it is
    inspecting while the mind is writing to it."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    return connection


def chunk_stats(record) -> dict:
    db = _chunk_db(record)
    if not db.is_file():
        # gitignored and rebuildable (scripts/reindex.py) — absent is normal
        return {"available": False, "count": 0, "kinds": {}}
    try:
        with _read_only(db) as c:
            count = c.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
            kinds = {r["kind"] or "?": r["n"] for r in c.execute(
                "SELECT kind, COUNT(*) AS n FROM chunk GROUP BY kind")}
            embedder = c.execute(
                "SELECT value FROM meta WHERE key='embedder_id'").fetchone()
        return {"available": True, "count": count, "kinds": kinds,
                "embedder_id": embedder[0] if embedder else None,
                "db_bytes": db.stat().st_size}
    except sqlite3.Error as e:
        return {"available": False, "count": 0, "kinds": {}, "error": str(e)}


def chunks(record, *, page: int = 0, limit: int = 50, kind: str | None = None,
           q: str | None = None) -> dict:
    """A page of the recall index.

    Deliberately not `ChunkIndex.all()`: that is `SELECT *` with no LIMIT and it
    materialises every embedding blob as a numpy array. On a mature index that
    is hundreds of megabytes to render fifty rows. The embedding is also never
    returned in a list — 768 floats per row would dwarf the text they describe.
    """
    page, limit = clamp(page, limit)
    db = _chunk_db(record)
    if not db.is_file():
        return {"items": [], "page": page, "limit": limit,
                "has_more": False, "total": 0, "available": False}
    where, params = [], {}
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    if q:
        where.append("text LIKE :like")
        params["like"] = f"%{q}%"
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        with _read_only(db) as c:
            total = c.execute(f"SELECT COUNT(*) FROM chunk{clause}",
                              params).fetchone()[0]
            rows = c.execute(
                "SELECT id, kind, source_path, source_span, text, created_at, "
                f"salience FROM chunk{clause} ORDER BY created_at DESC, id "
                "LIMIT :limit OFFSET :offset",
                {**params, "limit": limit, "offset": page * limit}).fetchall()
        items = [dict(r) for r in rows]
    except sqlite3.Error as e:
        return {"items": [], "page": page, "limit": limit, "has_more": False,
                "total": 0, "available": False, "error": str(e)}
    return {"items": items, "page": page, "limit": limit,
            "has_more": (page + 1) * limit < total, "total": total,
            "available": True}


def chunk(record, chunk_id: str) -> dict | None:
    """One chunk, with just enough of its vector to eyeball."""
    import numpy as np
    db = _chunk_db(record)
    if not db.is_file():
        return None
    try:
        with _read_only(db) as c:
            row = c.execute("SELECT * FROM chunk WHERE id = ?", (chunk_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    out = {k: row[k] for k in row.keys() if k != "embedding"}
    vector = np.frombuffer(row["embedding"], dtype=np.float32) \
        if row["embedding"] else np.array([], dtype=np.float32)
    out["dim"] = int(vector.size)
    out["embedding_preview"] = [float(x) for x in vector[:8]]
    out["norm"] = float(np.linalg.norm(vector)) if vector.size else 0.0
    return out


# --- what it all costs --------------------------------------------------------

def economics(record, *, points: int = 500) -> dict:
    history, _, _ = jsonl_page(source(record, "context"), limit=points)
    history.reverse()                               # a chart reads left to right
    utility_rows, _, _ = jsonl_page(source(record, "utility"), limit=MAX_LIMIT)
    applied = sum(1 for r in utility_rows if r.get("applied"))
    quarantined = sum(1 for r in utility_rows if r.get("quarantined"))
    by_kind: dict[str, dict] = {}
    for row in utility_rows:
        bucket = by_kind.setdefault(row.get("kind") or "?",
                                    {"total": 0, "applied": 0, "quarantined": 0})
        bucket["total"] += 1
        bucket["applied"] += 1 if row.get("applied") else 0
        bucket["quarantined"] += 1 if row.get("quarantined") else 0
    prompts_rows, _, _ = jsonl_page(source(record, "prompts"), limit=MAX_LIMIT,
                                    shape=lambda r: {"kind": r.get("kind"),
                                                     "tokens_in": r.get("tokens_in"),
                                                     "tokens_out": r.get("tokens_out")})
    spend: dict[str, dict] = {}
    for row in prompts_rows:
        bucket = spend.setdefault(row.get("kind") or "?",
                                  {"calls": 0, "tokens_in": 0, "tokens_out": 0})
        bucket["calls"] += 1
        bucket["tokens_in"] += row.get("tokens_in") or 0
        bucket["tokens_out"] += row.get("tokens_out") or 0
    return {
        "context": history,
        "budget": read_json(Path(record.paths.vault) / "state" / "budget.json", {}) or {},
        "utility": {"applied": applied, "quarantined": quarantined,
                    "total": len(utility_rows), "by_kind": by_kind},
        "by_kind": spend,
    }


def utility(record, *, page: int = 0, limit: int = DEFAULT_LIMIT,
            kind: str | None = None) -> dict:
    return paged(source(record, "utility"), page=page, limit=limit,
                 match=_matcher(kind=kind))


def context_history(record, runtime=None, *, limit: int = 500) -> dict[str, Any]:
    rows, _, _ = jsonl_page(source(record, "context"), limit=limit)
    rows.reverse()
    return {"context": runtime.context.snapshot() if runtime
            else {"used": 0, "limit": None}, "history": rows}
