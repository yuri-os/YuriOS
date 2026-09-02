"""Copy a character root as a new registry entry (SPEC §29, §36).

Export + import duplicates identity. This duplicates the whole companion —
Vault, memory, journal, dreams, selfies, traces — under a new id. The copy is
transactional: a staging directory on the same filesystem, then one rename,
then the registry row. A failure leaves nothing behind.
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
from pathlib import Path

from .models import (
    CharacterPaths,
    CharacterRecord,
    DisplayMetadata,
    new_character_id,
)
from .registry import CharacterRegistry


class CharacterCloneError(ValueError):
    """The clone could not be produced. Always safe to show a user."""


def clone_character(
    registry: CharacterRegistry,
    source_id: str,
    *,
    name: str | None = None,
    character_id: str | None = None,
) -> CharacterRecord:
    """Duplicate ``source_id`` into a new id. Transactional: all or nothing."""
    source = registry.get(source_id)
    if source is None:
        raise CharacterCloneError(f"no such character: {source_id}")
    if not source.paths.root.is_dir():
        raise CharacterCloneError(f"character root is missing: {source.paths.root}")

    new_name = (name or "").strip() or f"{source.display.name} (copy)"
    characters_dir = registry.data_root / "characters"
    characters_dir.mkdir(parents=True, exist_ok=True)
    if character_id is None:
        unavailable = {record.id for record in registry}
        unavailable.update(path.name for path in characters_dir.iterdir())
        character_id = new_character_id(new_name, unavailable)
    if registry.get(character_id) is not None:
        raise CharacterCloneError(f"character already exists: {character_id}")
    final_root = characters_dir / character_id
    if final_root.exists():
        raise CharacterCloneError(f"character root already exists: {final_root}")

    record = CharacterRecord(
        id=character_id,
        display=DisplayMetadata(
            name=new_name,
            creator=source.display.creator,
            description=source.display.description,
            tags=list(source.display.tags),
        ),
        paths=CharacterPaths.under(final_root),
        lifecycle=copy.deepcopy(source.lifecycle),
        loops=copy.deepcopy(source.loops),
        notify=copy.deepcopy(source.notify),
        connection=copy.deepcopy(source.connection),
        models=copy.deepcopy(source.models),
        voice=copy.deepcopy(source.voice),
        body=copy.deepcopy(source.body),
    )

    temporary = Path(tempfile.mkdtemp(prefix=f".{character_id}.", dir=characters_dir))
    moved = False
    try:
        temporary.rmdir()
        shutil.copytree(source.paths.root, temporary, symlinks=False)
        os.replace(temporary, final_root)
        moved = True
        registry.add(record)
        return record
    except CharacterCloneError:
        shutil.rmtree(final_root if moved else temporary, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(final_root if moved else temporary, ignore_errors=True)
        raise CharacterCloneError(f"could not clone {source_id}: {exc}") from exc
