from __future__ import annotations

import json

import pytest

from yurios.characters import (
    CharacterPaths,
    CharacterRecord,
    CharacterRegistry,
    DisplayMetadata,
    LifecycleFlags,
    LoopSwitches,
    ModelBinding,
)


def test_registry_round_trip_uses_stable_id_and_portable_paths(tmp_path):
    root = tmp_path / "portable-data"
    character_root = root / "characters" / "stable-01"
    record = CharacterRecord(
        id="stable-01",
        display=DisplayMetadata("A Name", creator="Creator", tags=["one"]),
        paths=CharacterPaths.under(character_root),
        lifecycle=LifecycleFlags(enabled=True, autostart=True, review_required=False),
        loops=LoopSwitches(mind=True, utility=False, dream=False),
        models=ModelBinding(chat="local/chat", utility="local/utility"),
    )

    registry = CharacterRegistry(root)
    registry.add(record)
    loaded = CharacterRegistry(root).require("stable-01")

    assert loaded.id == record.id
    assert loaded.display.name == "A Name"
    assert loaded.lifecycle.autostart
    assert not loaded.loops.utility
    assert loaded.models.chat == "local/chat"
    assert loaded.paths.vault == character_root.resolve() / "vault"
    raw = json.loads((root / "characters.json").read_text(encoding="utf-8"))
    assert raw["characters"][0]["paths"]["root"] == "characters/stable-01"
    assert not list(root.glob(".characters.json.*.tmp"))


def test_registry_rejects_duplicate_ids_and_escaping_paths(tmp_path):
    root = tmp_path / "data"
    record = CharacterRecord(
        id="same",
        display=DisplayMetadata("Same"),
        paths=CharacterPaths.under(root / "characters" / "same"),
    )
    registry = CharacterRegistry(root)
    registry.add(record)

    with pytest.raises(ValueError, match="already exists"):
        registry.add(record)

    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    raw["characters"][0]["paths"]["vault"] = "../outside"
    registry.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes data root"):
        CharacterRegistry(root)
