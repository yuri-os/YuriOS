"""Archive snapshots so an unarchive can restore the registry row (SPEC §29.6).

Archive used to drop the row with the rename, which made a faithful restore
impossible: models, loops, connection and lifecycle lived only in
`characters.json`. The snapshot is `archive.json` inside the archived folder.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CharacterPaths,
    CharacterRecord,
    DisplayMetadata,
    LifecycleFlags,
)

SNAPSHOT_NAME = "archive.json"
#: `<id>-YYYYMMDD-HHMMSS` — the stamp archive() writes. The id may itself
#: contain hyphens, so the stamp is the last two hyphen-separated numeric runs.
ARCHIVE_NAME_RE = re.compile(r"^(.+)-(\d{8}-\d{6})$")


def archive_name(character_id: str, stamp: str) -> str:
    return f"{character_id}-{stamp}"


def parse_archive_name(name: str) -> tuple[str, str] | None:
    """Return `(id, stamp)` when `name` looks like an archive folder."""
    match = ARCHIVE_NAME_RE.fullmatch(name)
    if match is None:
        return None
    return match.group(1), match.group(2)


def snapshot_payload(record: CharacterRecord, data_root: Path,
                     *, archived_at: str | None = None) -> dict[str, Any]:
    return {
        "record": record.to_dict(data_root=data_root),
        "archived_at": archived_at or datetime.now(timezone.utc).isoformat(),
    }


def write_snapshot(archive_root: Path, payload: Mapping[str, Any]) -> None:
    path = archive_root / SNAPSHOT_NAME
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_snapshot(archive_root: Path) -> dict[str, Any] | None:
    path = archive_root / SNAPSHOT_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def record_from_snapshot(
    payload: Mapping[str, Any],
    *,
    character_id: str,
    dest_root: Path,
    data_root: Path,
) -> CharacterRecord:
    raw = payload.get("record")
    if not isinstance(raw, Mapping):
        raise ValueError("archive snapshot is missing the character record")
    data = dict(raw)
    data["id"] = character_id
    data["paths"] = _paths_dict(dest_root, data_root)
    restored = CharacterRecord.from_dict(data, data_root=data_root)
    return CharacterRecord(
        id=character_id,
        display=restored.display,
        paths=CharacterPaths.under(dest_root),
        lifecycle=restored.lifecycle,
        loops=restored.loops,
        notify=restored.notify,
        connection=restored.connection,
        models=restored.models,
        voice=restored.voice,
        body=restored.body,
        created_at=restored.created_at,
    )


def _paths_dict(dest_root: Path, data_root: Path) -> dict[str, str]:
    paths = CharacterPaths.under(dest_root)
    root = data_root.resolve()
    out: dict[str, str] = {}
    for key in CharacterPaths.__dataclass_fields__:
        path = getattr(paths, key).resolve()
        out[key] = path.relative_to(root).as_posix()
    return out


def record_from_tree(archive_root: Path, character_id: str,
                     dest_root: Path) -> CharacterRecord:
    """Rebuild a row from the files alone — archives written before snapshots."""
    name = character_id
    creator = ""
    description = ""
    tags: list[str] = []
    card_path = archive_root / "card.json"
    if card_path.is_file():
        try:
            wrapper = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            wrapper = {}
        data = wrapper.get("data") if isinstance(wrapper, dict) else None
        if not isinstance(data, dict):
            data = wrapper if isinstance(wrapper, dict) else {}
        name = str(data.get("name") or name).strip() or character_id
        creator = str(data.get("creator") or "")
        description = str(data.get("description") or "")
        raw_tags = data.get("tags") or []
        if isinstance(raw_tags, list):
            tags = [str(tag) for tag in raw_tags if str(tag).strip()]
    return CharacterRecord(
        id=character_id,
        display=DisplayMetadata(name=name, creator=creator,
                                description=description, tags=tags),
        paths=CharacterPaths.under(dest_root),
        lifecycle=LifecycleFlags(enabled=False, autostart=False,
                                 review_required=False),
    )


def list_archives(data_root: Path) -> list[dict[str, Any]]:
    """Every folder under `archives/` that looks like one of ours, newest first."""
    root = Path(data_root) / "archives"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        parsed = parse_archive_name(path.name)
        if parsed is None:
            continue
        character_id, stamp = parsed
        snapshot = read_snapshot(path)
        archived_at = None
        if snapshot is not None:
            archived_at = snapshot.get("archived_at")
            raw = snapshot.get("record")
            if isinstance(raw, Mapping) and raw.get("id"):
                character_id = str(raw["id"])
        rows.append({
            "name": path.name,
            "id": character_id,
            "stamp": stamp,
            "archived_at": archived_at,
            "has_snapshot": snapshot is not None,
        })
    # Stamp is YYYYMMDD-HHMMSS, so reverse lexicographic order is newest first.
    # Sorting by folder name would put `testra-…` above `cliprobe-…` forever.
    rows.sort(key=lambda row: str(row["stamp"]), reverse=True)
    return rows
