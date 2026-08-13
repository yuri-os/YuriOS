"""Atomic JSON storage for the character registry."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import CharacterRecord

log = logging.getLogger("characters.registry")


REGISTRY_SCHEMA_VERSION = 1


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Durably replace ``path`` without exposing a partially written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)


class CharacterRegistry:
    """A small in-process registry rooted at a caller-selected data directory."""

    def __init__(self, data_root: str | Path, filename: str = "characters.json"):
        self.data_root = Path(data_root).resolve()
        if Path(filename).name != filename:
            raise ValueError("registry filename must be a plain filename")
        self.path = self.data_root / filename
        self._records: dict[str, CharacterRecord] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._records = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid character registry: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise ValueError("unsupported character registry schema")
        entries = raw.get("characters")
        if not isinstance(entries, list):
            raise ValueError("character registry 'characters' must be a list")
        records: dict[str, CharacterRecord] = {}
        dropped: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("character registry entries must be objects")
            record = CharacterRecord.from_dict(entry, data_root=self.data_root,
                                               dropped=dropped)
            if record.id in records:
                raise ValueError(f"duplicate character id: {record.id}")
            records[record.id] = record
        self._records = records
        if dropped:
            # Retired keys were dropped on the way in (`models._binding_data`).
            # Write the file back now, or the same keys are read and warned about
            # again on every start for as long as nobody edits that character —
            # which for a character who is simply *living here* is forever.
            #
            # Safe because it is a rewrite of what was just parsed, not a merge:
            # `save()` serialises the records this load produced, so the only
            # difference from the file on disk is the fields nobody can use.
            # Best-effort — a read-only data dir is a reason to keep the warning,
            # never a reason to refuse to load her.
            log.info("registry: dropping %d retired field(s) (%s) — rewriting %s",
                     len(dropped), ", ".join(sorted(set(dropped))), self.path.name)
            try:
                self.save()
            except OSError:
                log.warning("couldn't rewrite %s; the retired fields stay put",
                            self.path, exc_info=True)

    def save(self) -> None:
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "characters": [
                self._records[key].to_dict(data_root=self.data_root)
                for key in sorted(self._records)
            ],
        }
        atomic_write_json(self.path, payload)

    def list(self) -> list[CharacterRecord]:
        return [self._records[key] for key in sorted(self._records)]

    def get(self, character_id: str) -> CharacterRecord | None:
        return self._records.get(character_id)

    def require(self, character_id: str) -> CharacterRecord:
        record = self.get(character_id)
        if record is None:
            raise KeyError(character_id)
        return record

    def add(self, record: CharacterRecord) -> None:
        if record.id in self._records:
            raise ValueError(f"character already exists: {record.id}")
        self._records[record.id] = record
        try:
            self.save()
        except Exception:
            del self._records[record.id]
            raise

    def upsert(self, record: CharacterRecord) -> None:
        previous = self._records.get(record.id)
        self._records[record.id] = record
        try:
            self.save()
        except Exception:
            if previous is None:
                del self._records[record.id]
            else:
                self._records[record.id] = previous
            raise

    def remove(self, character_id: str) -> CharacterRecord:
        record = self.require(character_id)
        del self._records[character_id]
        try:
            self.save()
        except Exception:
            self._records[character_id] = record
            raise
        return record

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self):
        return iter(self.list())
