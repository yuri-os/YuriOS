"""Clone copies the whole tree; export+import is the identity-only duplicate."""
from __future__ import annotations

from yurios.characters import CharacterRegistry, clone_character
from yurios.characters.clone import CharacterCloneError

from tests.test_host import record


def test_clone_gets_a_new_id_and_keeps_the_source(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    (item.paths.root / "memory.txt").write_text("hers", encoding="utf-8")
    registry.add(item)

    cloned = clone_character(registry, "yuri", name="Yuri")

    assert cloned.id == "yuri_v2"
    assert cloned.display.name == "Yuri"
    assert cloned.lifecycle.review_required is False
    assert (tmp_path / "characters" / "yuri_v2" / "memory.txt").read_text(
        encoding="utf-8") == "hers"
    assert registry.get("yuri") is not None
    assert (tmp_path / "characters" / "yuri" / "memory.txt").is_file()


def test_archives_list_newest_stamp_first(tmp_path):
    from yurios.characters.archive import list_archives

    root = tmp_path / "archives"
    (root / "aaa-20260101-000000").mkdir(parents=True)
    (root / "zzz-20260801-000000").mkdir()
    (root / "mmm-20260903-120000").mkdir()
    names = [row["name"] for row in list_archives(tmp_path)]
    assert names[0] == "mmm-20260903-120000"
    assert names[-1] == "aaa-20260101-000000"


def test_clone_of_a_missing_character_is_a_readable_error(tmp_path):
    registry = CharacterRegistry(tmp_path)
    try:
        clone_character(registry, "ghost")
    except CharacterCloneError as exc:
        assert "ghost" in str(exc)
    else:
        raise AssertionError("expected CharacterCloneError")
