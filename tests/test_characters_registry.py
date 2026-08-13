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


def _with_retired_keys(registry, **extra):
    """Put an older build's fields back into the file on disk, verbatim."""
    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    raw["characters"][0].setdefault("connection", {}).update(extra)
    registry.path.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def test_a_retired_field_is_dropped_once_not_warned_about_forever(tmp_path, caplog):
    """The registry a real install carries has `connection.backend`/`endpoint`/
    `api_key_env` from before the connection became a named profile. Dropping
    them on read is right; doing it again on every start is the bug — a house of
    four characters printed twelve identical lines at every boot, forever,
    because nobody ever edits a character who is simply living there.
    """
    root = tmp_path / "data"
    registry = CharacterRegistry(root)
    registry.add(CharacterRecord(
        id="yuri", display=DisplayMetadata("Yuri"),
        paths=CharacterPaths.under(root / "characters" / "yuri")))
    _with_retired_keys(registry, backend="litellm", endpoint=None, api_key_env=None)

    with caplog.at_level("WARNING"):
        reloaded = CharacterRegistry(root)
    assert [r.id for r in reloaded.list()] == ["yuri"]
    assert reloaded.get("yuri").connection.profile == "default"
    first = [r for r in caplog.records if "ignoring unknown" in r.getMessage()]
    assert len(first) == 3, "the drop is still said out loud, once"

    # …and the file on disk no longer carries them, so the next start is quiet
    on_disk = json.loads(registry.path.read_text(encoding="utf-8"))
    assert set(on_disk["characters"][0]["connection"]) == {"profile", "options"}
    caplog.clear()
    with caplog.at_level("WARNING"):
        CharacterRegistry(root)
    assert not [r for r in caplog.records if "ignoring unknown" in r.getMessage()]


def test_the_rewrite_changes_nothing_but_the_dead_fields(tmp_path):
    """It is a rewrite of what was just parsed, not a merge — every live field
    has to survive it untouched, or a boot-noise fix becomes data loss."""
    root = tmp_path / "data"
    registry = CharacterRegistry(root)
    registry.add(CharacterRecord(
        id="iris", display=DisplayMetadata("Iris", creator="C", tags=["t"]),
        paths=CharacterPaths.under(root / "characters" / "iris"),
        lifecycle=LifecycleFlags(enabled=True, autostart=False, review_required=True),
        loops=LoopSwitches(mind=True, utility=False, dream=True),
        models=ModelBinding(chat="local/chat", utility="local/utility")))
    before = json.loads(registry.path.read_text(encoding="utf-8"))
    _with_retired_keys(registry, backend="litellm")

    reloaded = CharacterRegistry(root)
    after = json.loads(registry.path.read_text(encoding="utf-8"))
    assert after == before, "the rewrite moved something it should not have"
    kept = reloaded.require("iris")
    assert kept.display.tags == ["t"] and kept.models.chat == "local/chat"
    assert kept.lifecycle.review_required and kept.loops.dream


def test_a_clean_registry_is_never_rewritten(tmp_path):
    """No drops, no save: a load must not touch the file's mtime for nothing."""
    root = tmp_path / "data"
    registry = CharacterRegistry(root)
    registry.add(CharacterRecord(
        id="mika", display=DisplayMetadata("Mika"),
        paths=CharacterPaths.under(root / "characters" / "mika")))
    before = registry.path.stat().st_mtime_ns
    CharacterRegistry(root)
    assert registry.path.stat().st_mtime_ns == before


def test_a_read_only_data_dir_keeps_her_loading(tmp_path):
    """Best-effort: an unwritable registry is a reason to keep the warning, never
    a reason to refuse to load her."""
    root = tmp_path / "data"
    registry = CharacterRegistry(root)
    registry.add(CharacterRecord(
        id="adia", display=DisplayMetadata("Adia"),
        paths=CharacterPaths.under(root / "characters" / "adia")))
    _with_retired_keys(registry, backend="litellm")
    root.chmod(0o500)                                   # readable, not writable
    try:
        assert CharacterRegistry(root).require("adia").display.name == "Adia"
    finally:
        root.chmod(0o700)
