"""Small shared primitives for the mind: time conversion, JSONL, JSON files.

The build-wide `Clock` (world/clock.py) speaks float epoch seconds; the mind
often needs calendar arithmetic (day files, quiet hours, the DREAM window), so
`dt_of()` is the one sanctioned conversion — naive local time, the same
convention the situation renderer has used since Build #4, which is what makes
sim-time tests deterministic on any machine (a VirtualClock seeded from a naive
`datetime(...).timestamp()` round-trips to the same wall reading everywhere).
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from yurios.app.vaultgit import atomic_write  # the Vault write discipline

#: Read granularity for the reverse readers below. Large enough that a page of
#: tick traces is one syscall, small enough that paging a 32 MB prompt log does
#: not pull the whole thing into memory.
_BLOCK = 65536


def new_id(prefix: str = "") -> str:
    u = uuid.uuid4().hex[:12]
    return f"{prefix}-{u}" if prefix else u


def dt_of(ts: float) -> datetime.datetime:
    """Clock seconds → naive local datetime (the mind's wall reading)."""
    return datetime.datetime.fromtimestamp(ts)


def iso_of(ts: float) -> str:
    return dt_of(ts).isoformat(timespec="seconds")


def utc_iso_of(ts: float) -> str:
    """Clock seconds → aware UTC ISO — the memory index's convention
    (its recency math subtracts against `datetime.now(UTC)`)."""
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).isoformat(timespec="seconds")


def ts_of_iso(s: str) -> float:
    return datetime.datetime.fromisoformat(s).timestamp()


def day_of(ts: float) -> str:
    return dt_of(ts).strftime("%Y-%m-%d")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def jsonl_append(path: Path, obj: dict, *, max_bytes: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        # `default=str` because these are logs: a Path or a datetime that wandered
        # into a payload should land as its repr, never raise into the path being
        # observed. The tool audit has always written this way (world/tools/guard.py).
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if max_bytes is not None:
        try:
            if path.stat().st_size >= max_bytes:
                backup = path.with_name(path.name + ".1")
                if backup.exists():
                    backup.unlink()
                path.rename(backup)             # next append recreates path fresh
        except OSError:
            pass                                 # best-effort housekeeping, never fatal


def jsonl_read(path: Path) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn tail line after a crash is expected; skip it
    except FileNotFoundError:
        return


def jsonl_tail(path: Path, n: int) -> list[dict]:
    rows = list(jsonl_read(path))
    return rows[-n:]


def _reverse_lines(path: Path, size: int) -> Iterator[bytes]:
    """Yield the file's lines newest-last-first, reading backwards in blocks.

    `size` is snapshotted by the caller, so a concurrent append is simply
    unseen: a reader never gets a half-written record, and a page never shifts
    under a reader that is walking it.
    """
    with open(path, "rb") as f:
        pos = size
        tail = b""              # bytes that belong to a line continuing leftwards
        while pos > 0:
            step = min(_BLOCK, pos)
            pos -= step
            f.seek(pos)
            parts = (f.read(step) + tail).split(b"\n")
            tail = parts.pop(0)  # completed by the block before this one
            for part in reversed(parts):
                if part.strip():
                    yield part
        if tail.strip():
            yield tail


#: path → (st_size, st_mtime_ns, count). Counting means a full byte scan, and
#: these files only ever grow at the tail, so the stat pair is a sound cache key.
_counts: dict[str, tuple[int, int, int]] = {}


def jsonl_count(path: Path) -> int:
    """Number of complete records, by newline scan. A torn tail line after a
    crash has no newline and so is not counted — which matches what the readers
    below will hand back."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return 0
    hit = _counts.get(str(path))
    if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    count = 0
    with open(path, "rb") as f:
        while chunk := f.read(_BLOCK):
            count += chunk.count(b"\n")
    _counts[str(path)] = (st.st_size, st.st_mtime_ns, count)
    return count


def jsonl_page(path: Path, *, page: int = 0, limit: int = 50,
               match: Callable[[dict], bool] | None = None,
               shape: Callable[[dict], dict] | None = None,
               ) -> tuple[list[dict], bool, int | None]:
    """One newest-first page of a JSONL log, without reading the whole file.

    Returns `(items, has_more, total)`. `total` is None when `match` is given —
    counting a filtered file means a full pass, and a debug pager can say "20+"
    perfectly well. `shape` runs on each kept row before it is collected, so an
    index view can drop the expensive field (a whole assembled prompt) before it
    is ever held in a list.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return [], False, 0
    if size == 0:
        return [], False, 0

    page, limit = max(0, page), max(1, limit)
    skip = page * limit
    items: list[dict] = []
    has_more = False
    seen = 0
    for raw in _reverse_lines(path, size):
        try:
            row = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue            # a torn tail line, same tolerance as jsonl_read
        if not isinstance(row, dict):
            continue
        if match is not None and not match(row):
            continue
        seen += 1
        if seen <= skip:
            continue
        if len(items) == limit:
            has_more = True     # one qualifying row past the page is all we need
            break
        items.append(shape(row) if shape else row)
    return items, has_more, (None if match is not None else jsonl_count(path))


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 estimate — used for budgets, never billing."""
    return max(1, round(len(text) / 4)) if text else 0
