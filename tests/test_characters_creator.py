from __future__ import annotations

from yurios.characters import CharacterRegistry, Draft, create_character


def test_created_character_seeds_the_document_editing_skill(tmp_path):
    record = create_character(
        CharacterRegistry(tmp_path), Draft(name="New Character"), initialize_git=False)

    skill = (record.paths.vault / "skills" / "document-editing" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "name: document-editing" in skill
    assert "Call `read_note` before changing" in skill
