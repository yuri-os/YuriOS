"""Where she is (characters/setting.py, world/situation.py).

The bug underneath every test here: one hardcoded place sentence in the
embodiment truth meant every imported character was told, every prompt, that she
lives in a small room above the Sprawl — a city from the shipped companion's
card and nobody else's — while `vault/world/situation.md`, the file that is
supposed to be her picture of now, was seeded `_(Unknown.)_` for all of them.
The card in hand said perfectly plainly where she was.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.cards import card_data, png_card, wrapper
from yurios.characters import CharacterImporter, CharacterRegistry
from yurios.characters.models import CharacterPaths, CharacterRecord, DisplayMetadata
from yurios.characters.setting import (
    DERIVED_MARK, SETTING_SYSTEM, derive_place, ensure_setting, mechanical_place,
    opening_situation, place_excerpt, place_of, read_place, refine_setting,
    write_authored, write_setting)
from yurios.kernel.clock import VirtualClock
from yurios.world.situation import (
    DESKTOP, EMBODIMENT, HOUSE_PLACE, embodiment, render_situation)
from yurios.world.avatar.controller import VrmController
from yurios.world.tools.timers import TimerBoard

CARD_DESCRIPTION = """\
Character: Halden

Age: 34

Occupation: Lighthouse keeper

Setting: the keeper's cottage at the foot of the Ardnoch light, two rooms and a
stove, the sea on three sides of it.

Personality: quiet, exact, slow to warm.
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


def _record(root: Path, name: str = "Halden") -> CharacterRecord:
    return CharacterRecord(id="halden", display=DisplayMetadata(name=name),
                           paths=CharacterPaths.under(root))


def _card(root: Path, **fields) -> CharacterRecord:
    record = _record(root)
    record.paths.card_json.parent.mkdir(parents=True, exist_ok=True)
    record.paths.card_json.write_text(json.dumps(
        {"data": {"name": "Halden", **fields}}), encoding="utf-8")
    return record


# ---- reading a place out of a card ---------------------------------------

def test_a_setting_section_is_found_in_the_description():
    excerpt = place_excerpt(CARD_DESCRIPTION)
    assert "keeper's cottage" in excerpt
    assert "Ardnoch" in excerpt
    # …and the sections that are not a place stay out of it
    assert "Lighthouse keeper" not in excerpt
    assert "slow to warm" not in excerpt


def test_the_scenario_is_preferred_over_the_description():
    """A card's `scenario` field is *defined* to be the present situation, so it
    outranks a heading match anywhere else."""
    place = mechanical_place("Halden", scenario="You keep the Ardnoch light.",
                             description=CARD_DESCRIPTION)
    assert place == "You keep the Ardnoch light."


def test_a_card_with_no_scenario_falls_back_to_its_setting_section():
    place = mechanical_place("Halden", description=CARD_DESCRIPTION)
    assert "keeper's cottage" in place


def test_macros_are_resolved_the_way_the_block_can_carry_them():
    """`{{char}}` is answered here, where the name is known. `{{user}}` becomes
    the block's own `{user}`, which the renderer fills at prompt time."""
    place = mechanical_place("Halden", scenario="{{char}}'s cottage, where {{user}} visits.")
    assert place == "Halden's cottage, where {user} visits."


def test_a_card_that_says_nothing_about_a_place_gets_no_place():
    """Silence beats invention: with nothing to go on the house place stays,
    which at least admits to being a room."""
    assert mechanical_place("Halden", scenario="", description="") == ""


def test_the_place_is_capped():
    place = mechanical_place("Halden", scenario="a room. " * 400)
    assert len(place) <= 600


# ---- the file ------------------------------------------------------------

def test_the_written_file_round_trips_through_place_of(tmp_path):
    path = write_setting(tmp_path / "setting.md", "Halden", "You keep the light.")
    assert place_of(path.read_text()) == "You keep the light."
    # …and it carries the marker that says it is still the machine's work
    assert DERIVED_MARK in path.read_text()


def test_an_authored_file_carries_no_derived_marker(tmp_path):
    path = write_authored(tmp_path / "setting.md", "You keep the light.")
    assert place_of(path.read_text()) == "You keep the light."
    assert DERIVED_MARK not in path.read_text()


def test_place_of_survives_a_person_with_an_editor(tmp_path):
    """Heading deleted, comment deleted, both — all still a setting."""
    assert place_of("You keep the light.") == "You keep the light."
    assert place_of("# Where she is\n\nYou keep the light.\n") == "You keep the light."
    assert place_of("<!-- anything -->\n\nYou keep the light.") == "You keep the light."
    assert place_of("# Where she is\n\n_(Nothing written yet.)_") == ""
    assert place_of("") == ""


# ---- the block it lands in -----------------------------------------------

def test_the_house_place_is_still_the_default_verbatim():
    """The embodiment truth is law, not paraphrase (§2.5). A character with no
    setting of her own gets exactly the sentence she always got."""
    assert HOUSE_PLACE in EMBODIMENT
    assert embodiment("Sam") == EMBODIMENT.replace("{user}", "Sam")


def test_her_own_place_replaces_the_house_one_rather_than_joining_it():
    text = embodiment("Sam", "You keep the Ardnoch light, the sea on three sides")
    assert "You keep the Ardnoch light, the sea on three sides." in text
    assert "above the Sprawl" not in text
    # the desktop is true wherever she lives, so it survives the swap
    assert DESKTOP.replace("{user}", "Sam") in text
    # …and so does the law the whole block exists for
    assert "Never say you have no body" in text


def test_the_situation_block_puts_her_in_her_own_room():
    clock = VirtualClock()
    text = render_situation(clock, controller=VrmController(),
                            timers=TimerBoard(clock), user_name="Sam",
                            place="You are in the keeper's cottage.")
    assert "You are in the keeper's cottage." in text
    assert "above the Sprawl" not in text


def test_read_place_reads_the_vault(tmp_path):
    assert read_place(tmp_path) == ""
    write_setting(tmp_path / "world" / "setting.md", "Halden", "You keep the light.")
    assert read_place(tmp_path) == "You keep the light."


# ---- the model pass ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_model_is_asked_for_the_place_and_its_json_is_read():
    utility = FakeUtility('{"place": "You are in the keeper\'s cottage at the '
                          'foot of the light, the sea on three sides."}')
    place = await derive_place(utility, name="Halden",
                               scenario="A lighthouse.",
                               description=CARD_DESCRIPTION)
    assert place.startswith("You are in the keeper's cottage")
    assert utility.calls[0][0]["content"] == SETTING_SYSTEM
    assert "A lighthouse." in utility.calls[0][1]["content"]


@pytest.mark.asyncio
async def test_a_broken_model_falls_back_to_the_cards_own_words():
    place = await derive_place(BrokenUtility(), name="Halden",
                               scenario="You keep the Ardnoch light.")
    assert place == "You keep the Ardnoch light."


@pytest.mark.asyncio
async def test_a_hand_edited_setting_is_never_refined_away(tmp_path):
    record = _card(tmp_path, scenario="A lighthouse.")
    write_authored(record.paths.setting, "My own words about her room.")
    assert await refine_setting(record, FakeUtility('{"place": "somewhere else"}')) is False
    assert place_of(record.paths.setting.read_text()) == "My own words about her room."
    # …unless you ask for it outright
    assert await refine_setting(record, FakeUtility('{"place": "somewhere else"}'),
                                force=True) is True


@pytest.mark.asyncio
async def test_refine_rewrites_a_derived_setting(tmp_path):
    record = _card(tmp_path, scenario="A lighthouse.")
    ensure_setting(record)
    assert place_of(record.paths.setting.read_text()) == "A lighthouse."
    assert await refine_setting(record, FakeUtility(
        '{"place": "You are in the keeper\'s cottage."}')) is True
    assert place_of(record.paths.setting.read_text()) == "You are in the keeper's cottage."


# ---- ensure, for everyone who arrived before this existed ------------------

def test_ensure_derives_from_the_card_and_leaves_an_existing_file_alone(tmp_path):
    record = _card(tmp_path, scenario="A lighthouse.", description=CARD_DESCRIPTION)
    path = ensure_setting(record)
    assert path is not None and place_of(path.read_text()) == "A lighthouse."
    write_authored(path, "Mine now.")
    ensure_setting(record)
    assert place_of(path.read_text()) == "Mine now."


def test_ensure_falls_back_to_the_soul_for_a_character_with_no_card(tmp_path):
    """Every install promoted from the pre-registry layout: the migration moves
    the Vault and never writes a card, but SCENARIO.md is right where it was."""
    record = _record(tmp_path)
    soul = record.paths.vault / "soul"
    soul.mkdir(parents=True)
    (soul / "SCENARIO.md").write_text(
        "---\nsoul: scenario\n---\n\n# Scenario\n\n## Scenario\n\n"
        "You keep the Ardnoch light.\n", encoding="utf-8")
    path = ensure_setting(record)
    assert path is not None
    assert place_of(path.read_text()) == "You keep the Ardnoch light."


def test_ensure_writes_nothing_when_the_card_says_nothing(tmp_path):
    assert ensure_setting(_card(tmp_path, scenario="", description="")) is None


# ---- the import seam ------------------------------------------------------

def _import(tmp_path, **fields):
    registry = CharacterRegistry(tmp_path / "data")
    data = card_data(**fields)
    return CharacterImporter(registry, initialize_git=False).import_card(
        png_card(wrapper(data, native=False)), character_id="subject")


def test_an_import_seeds_her_room_and_her_opening_situation(tmp_path):
    record = _import(tmp_path, scenario="A rainlit library, three floors of it.")
    assert place_of(record.paths.setting.read_text()) == "A rainlit library, three floors of it."
    situation = (record.paths.vault / "world" / "situation.md").read_text()
    # the placeholder this replaced said `_(Unknown.)_` for every character
    assert "Unknown" not in situation
    assert "A rainlit library, three floors of it." in situation
    assert "Nothing has happened here yet" in situation


def test_an_import_with_no_place_in_the_card_still_gets_a_situation_file(tmp_path):
    record = _import(tmp_path, scenario="", description="")
    assert not record.paths.setting.exists()
    situation = (record.paths.vault / "world" / "situation.md").read_text()
    assert "Nothing has happened here yet" in situation


def test_the_opening_situation_does_not_leave_a_raw_macro_in_the_file():
    assert "{user}" not in opening_situation("You wait for {user} by the window.")


# ---- what the mind writes on top of it ------------------------------------

def test_the_world_model_puts_her_own_room_in_the_snapshot(tmp_path):
    """`situation()` rewrites `situation.md` every time it changes, so her room
    has to live in a file the renderer *reads* — not in the one it overwrites."""
    from yurios.mind.vaultio import MindVault
    from yurios.mind.world import WorldModelStore

    vault = MindVault(tmp_path / "vault")
    write_setting(vault.vault / "world" / "setting.md", "Halden",
                  "You are in the keeper's cottage, the sea on three sides.")
    clock = VirtualClock()
    store = WorldModelStore(vault, clock, controller=VrmController(),
                            timers=TimerBoard(clock), user_name="Sam")
    text = store.situation()
    assert "You are in the keeper's cottage, the sea on three sides." in text
    assert "above the Sprawl" not in text
    assert (vault.vault / "world" / "situation.md").read_text() == text


# ---- the studio's surface --------------------------------------------------

@pytest.fixture
def node(tmp_path):
    from starlette.testclient import TestClient
    from yurios.world.config import Config
    from yurios.world.host import create_host_app

    cfg = Config(_env_file=None, data_dir=tmp_path / "data",
                 telegram_bot_token="", telegram_chat_id="")
    registry = CharacterRegistry(tmp_path / "data")
    CharacterImporter(registry, initialize_git=True).import_card(
        png_card(wrapper(card_data(scenario="A rainlit library."), native=True)),
        character_id="subject")
    with TestClient(create_host_app(cfg, registry)) as client:
        yield client, registry


def test_the_studio_page_carries_her_room_beside_the_draft(node):
    client, _ = node
    payload = client.get("/api/characters/subject/studio").json()
    assert payload["setting"] == {"setting": "A rainlit library.",
                                  "derived": True, "exists": True}


def test_saving_the_room_writes_it_and_marks_it_yours(node):
    client, registry = node
    payload = client.put("/api/characters/subject/setting",
                         json={"setting": "You are in the reading room, three "
                                          "floors of shelves above you."}).json()
    assert payload["derived"] is False
    record = registry.require("subject")
    assert read_place(record.paths.vault).startswith("You are in the reading room")
    # …and the next prompt has it, with no restart: the store reads the file
    assert client.get("/api/characters/subject/setting").json()["setting"] \
        .startswith("You are in the reading room")


def test_emptying_the_room_removes_the_file(node):
    client, registry = node
    client.put("/api/characters/subject/setting", json={"setting": ""})
    assert not registry.require("subject").paths.setting.exists()


def test_the_room_is_refused_for_a_character_who_does_not_exist(node):
    client, _ = node
    assert client.get("/api/characters/nobody/setting").status_code == 404
    assert client.put("/api/characters/nobody/setting",
                      json={"setting": "x"}).status_code == 404


# ---- and the scrub it must not trip ---------------------------------------

def test_a_freshly_imported_character_still_exports_without_an_acknowledgement(tmp_path):
    """Her setting is card prose, not private prose. Harvesting it out of
    `situation.md` would hand every imported character her own scenario back as
    an overlap with her own world model, and refuse the export until a human
    cleared it — a refusal with nothing behind it."""
    from yurios.characters.exporter import ExportOptions, build_export

    record = _import(tmp_path, scenario="A rainlit library, three floors of it.")
    # …the file exactly as the importer seeded it, place and all
    assert "A rainlit library, three floors of it." in (
        record.paths.vault / "world" / "situation.md").read_text()
    result = build_export(record, ExportOptions(), user_name="Sam")
    assert result.png

    # …and the same once the mind has rewritten it, where the place lands
    # mid-paragraph inside the embodiment truth
    (record.paths.vault / "world" / "situation.md").write_text(
        embodiment("Sam", read_place(record.paths.vault)) + "\n", encoding="utf-8")
    assert build_export(record, ExportOptions(), user_name="Sam").png
