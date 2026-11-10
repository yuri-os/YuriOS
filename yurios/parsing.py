"""Bounded subprocess seam for untrusted image/PDF parsing (SPEC §11.6).

The parent never unpickles worker output. A private scratch directory carries
raw input and a size-bounded JSON result; stdout/stderr cannot fill host RAM.
Async callers use a worker thread, just as they do for the surrounding disk IO.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

MAX_INPUT_BYTES = 50_000_000
MAX_RESULT_BYTES = 64 * 1024 * 1024
WALL_TIMEOUT = 20
_slots = threading.BoundedSemaphore(2)


class ParseRejected(ValueError):
    """The parser refused the file or exceeded its resource budget."""


def _command(kind: str, source: Path, target: Path, options: dict) -> list[str]:
    return [sys.executable, "-I", "-m", "yurios.parse_worker", kind,
            str(source), str(target), json.dumps(options)]


def parse_file(kind: str, data: bytes, **options) -> dict:
    """Return a bounded result from a disposable parser; never write user state."""
    if kind not in {"image", "portrait", "pdf"}:
        raise ValueError("unknown parser")
    if len(data) > MAX_INPUT_BYTES:
        raise ParseRejected("file exceeds the parsing size limit")
    if not _slots.acquire(timeout=2):
        raise ParseRejected("file parsers are busy; retry shortly")
    try:
        with tempfile.TemporaryDirectory(prefix="yurios-parse-") as scratch:
            source, target = Path(scratch) / "input", Path(scratch) / "result"
            source.write_bytes(data)
            try:
                result = subprocess.run(
                    _command(kind, source, target, options),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=WALL_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                # subprocess.run kills AND waits for the child before raising.
                raise ParseRejected("file parsing exceeded the time limit") from exc
            if result.returncode or not target.is_file():
                raise ParseRejected("file parsing failed or exceeded its resource limits")
            if target.stat().st_size > MAX_RESULT_BYTES:
                raise ParseRejected("parsed file exceeds the output size limit")
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ParseRejected("file parser returned an invalid result") from exc
            if not isinstance(payload, dict):
                raise ParseRejected("file parser returned an invalid result")
            if payload.get("error"):
                raise ParseRejected(str(payload["error"])[:500])
            return payload
    finally:
        _slots.release()


def image_bytes(result: dict) -> bytes:
    """Decode only the worker's bounded JSON wire payload."""
    try:
        return base64.b64decode(result["image"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise ParseRejected("file parser returned invalid image data") from exc
