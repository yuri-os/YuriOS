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
from typing import Any, Iterator

from yurios.app.vaultgit import atomic_write  # the Vault write discipline


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


def jsonl_append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


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


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 estimate — used for budgets, never billing."""
    return max(1, round(len(text) / 4)) if text else 0
