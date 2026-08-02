"""Per-character likeness (characters/appearance.py, forge/character.py).

The bug underneath every test here: one hardcoded `yuri.yaml` meant every
character in the house rendered with Yuri's face — cat ears, light-trace and
all — and the provenance sidecar recorded the photo as hers. A camera may take
a bad picture; it must never take a picture of the wrong person.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from yurios.characters.appearance import (
    APPEARANCE_SYSTEM, derive_identity, ensure_appearance, mechanical_identity,
    refine_appearance, render_yaml, visual_excerpt, write_appearance)
from yurios.characters.models import CharacterPaths, DisplayMetadata, CharacterRecord
from yurios.forge.character import Character

CARD_DESCRIPTION = """\
Description:
Character: Lumina "Lumi" Takahashi

Age: 19

Occupation: Night shift Nurse

Appearance: Long silver-white hair to her waist, dark red eyes with slit pupils,
very pale skin. Small fangs. Petite and slight.

Clothing:
At work: a nurse uniform, trying to appear innocent.
"""


class FakeUtility:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **params):
        self.calls.append(messages)
        return self.answer


class BrokenUtility:
    async def complete(self, messages, **params):
        raise RuntimeError("the model was busy")


def _record(root: Path, name: str = "Lumina") -> CharacterRecord:
    return CharacterRecord(id="lumina", display=DisplayMetadata(name=name),
                           paths=CharacterPaths.under(root))


# ---- the extraction ------------------------------------------------------

def test_visual_excerpt_keeps_the_body_and_drops_the_paperwork():
    excerpt = visual_excerpt(CARD_DESCRIPTION)
    assert "silver-white hair" in excerpt and "slit pupils" in excerpt
    # age, occupation and clothing render nothing — and an outfit baked into
    # identity would fight every shot that wanted a different one
    assert "19" not in excerpt and "Nurse" not in excerpt
    assert "nurse uniform" not in excerpt


def test_mechanical_identity_survives_a_card_with_no_appearance_section():
    """Cards are written by anyone, in any shape. Worse prose is fine; a
    character with no likeness of her own is not."""
    identity = mechanical_identity("Mara", "She is tall, with cropped black hair.")
    assert "cropped black hair" in identity


def test_mechanical_identity_never_returns_empty():
    assert mechanical_identity("Mara", "") .strip()


# ---- the model pass ------------------------------------------------------

async def test_derive_identity_parses_the_model_and_forwards_the_material():
    utility = FakeUtility('{"identity": "a petite young woman with silver-white '
                          'hair and dark red eyes", "negative": "no tan"}')
    identity, negative = await derive_identity(
        utility, name="Lumina", description=CARD_DESCRIPTION)
    assert identity == ("a petite young woman with silver-white hair and dark "
                        "red eyes")
    assert negative == "no tan"
    system = utility.calls[0][0]["content"]
    assert system == APPEARANCE_SYSTEM
    assert "silver-white hair" in utility.calls[0][1]["content"]


async def test_derive_identity_tolerates_a_fenced_answer():
    utility = FakeUtility('```json\n{"identity": "a tall woman", "negative": ""}\n```')
    identity, negative = await derive_identity(utility, name="X", description="tall")
    assert identity == "a tall woman" and negative == ""


async def test_a_failed_model_falls_back_to_the_card_rather_than_failing():
    identity, negative = await derive_identity(
        BrokenUtility(), name="Lumina", description=CARD_DESCRIPTION)
    assert "silver-white hair" in identity and negative == ""


# ---- the file ------------------------------------------------------------

def test_the_written_file_carries_only_what_is_hers():
    text = render_yaml("Lumina", "a petite young woman", "no tan")
    body = yaml.safe_load(text)
    assert body == {"name": "Lumina", "identity": "a petite young woman",
                    "character_negative_extra": "no tan"}
    # the register is inherited, never copied — so improving it later reaches
    # every character instead of none of them
    assert "quality_preamble" not in body and "base_negative" not in body


def test_a_derived_file_inherits_the_register_and_adds_to_its_guard(tmp_path):
    path = write_appearance(tmp_path / "appearance.yaml", "Lumina",
                            "a petite young woman with red eyes", "no tan")
    character = Character.load(path, defaults=Character.register())
    assert character.name == "Lumina"
    assert "red eyes" in character.identity
    assert "masterpiece" in character.quality_preamble      # inherited
    assert "no watermark" in character.base_negative        # inherited
    positive, negative = character.assemble("SCENE")
    assert "red eyes" in positive and "masterpiece" in positive
    assert "no tan" in negative                             # hers
    assert "no extra fingers" in negative                   # the house's, kept


# ---- what happens when she has no file yet -------------------------------

def test_ensure_derives_from_her_card(tmp_path):
    record = _record(tmp_path / "lumina")
    record.paths.card_json.parent.mkdir(parents=True, exist_ok=True)
    record.paths.card_json.write_text(json.dumps(
        {"data": {"name": "Lumina", "description": CARD_DESCRIPTION}}))
    path = ensure_appearance(record)
    assert path is not None and path.is_file()
    assert "silver-white hair" in yaml.safe_load(path.read_text())["identity"]


def test_ensure_leaves_an_existing_file_alone(tmp_path):
    record = _record(tmp_path / "lumina")
    write_appearance(record.paths.appearance, "Lumina", "hand written, mine")
    ensure_appearance(record)
    assert "hand written, mine" in record.paths.appearance.read_text()


def test_a_character_with_no_card_gets_the_shipped_house_face(tmp_path):
    """Only the repo's own character can reach this: import and the creator
    both write a card. Without it she would fall to the neutral stand-in and
    stop looking like herself, which is a regression, not a safety win."""
    record = _record(tmp_path / "yuri", name="Yuri")
    record.paths.root.mkdir(parents=True, exist_ok=True)
    path = ensure_appearance(record)
    assert path is not None
    assert "cat ears" in yaml.safe_load(path.read_text())["identity"]


def test_the_neutral_stand_in_is_nobody_in_particular():
    neutral = Character.neutral("Lumina")
    assert neutral.name == "Lumina"
    # it must not be another character's face — that is the whole point
    assert "cat ears" not in neutral.identity
    assert "unspecified" in neutral.identity
    assert "masterpiece" in neutral.quality_preamble      # still on-register


# ---- the refinement pass -------------------------------------------------

async def test_refine_rewrites_the_mechanical_file(tmp_path):
    record = _record(tmp_path / "lumina")
    record.paths.card_json.parent.mkdir(parents=True, exist_ok=True)
    record.paths.card_json.write_text(json.dumps(
        {"data": {"name": "Lumina", "description": CARD_DESCRIPTION}}))
    ensure_appearance(record)
    utility = FakeUtility('{"identity": "a petite young woman, silver-white '
                          'hair, dark red slit-pupil eyes", "negative": ""}')
    assert await refine_appearance(record, utility) is True
    body = yaml.safe_load(record.paths.appearance.read_text())
    assert body["identity"] == ("a petite young woman, silver-white hair, dark "
                                "red slit-pupil eyes")


async def test_refine_will_not_touch_a_file_you_wrote_yourself(tmp_path):
    """Once you have written her face by hand, no background pass gets to
    overwrite it."""
    record = _record(tmp_path / "lumina")
    record.paths.appearance.parent.mkdir(parents=True, exist_ok=True)
    record.paths.appearance.write_text("name: Lumina\nidentity: mine, by hand\n")
    utility = FakeUtility('{"identity": "the model\'s idea", "negative": ""}')
    assert await refine_appearance(record, utility) is False
    assert "mine, by hand" in record.paths.appearance.read_text()
