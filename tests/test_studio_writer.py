"""The SOUL writer's file surgery (characters/studio.py's write half).

`write_soul` edits markdown and YAML in place rather than regenerating it,
because a soul folder is a thing people hand-edit and comments are part of it.
That choice puts the correctness burden on the surgery itself, and these pin the
three ways it drew blood:

  * a manifest key whose value spans lines — `tags:` as a block sequence, a
    folded credit — was rewritten one line at a time, stranding the rest under a
    scalar. The result is not untidy, it is invalid YAML, and from then on
    `read_soul` refuses the character to every surface that reads her;
  * authored block headings (`## Alternate greeting — evening`) did not match the
    numbered pattern, so a save left them in place and appended a renumbered set
    beside them — dead prose that still shipped inside the card;
  * and a blank greeting was skipped when writing but still counted when the
    manifest references were synced, leaving a reference to a section that was
    never written.
"""
from __future__ import annotations

import pytest
import yaml

from yurios.characters.studio import Draft, _set_manifest, write_soul

MANIFEST = """\
# a header comment people write in
name: Yuri
creator: YuriOS Lab
character_version: 2.0.0
canon: canon-v2
tags:
  - companion
  - original

# a comment between the two halves
fields:
  description:
    - CONSTITUTION.md#Identity
  personality: PERSONA.md@personality
  scenario: SCENARIO.md#Scenario
  first_mes: "BOOTSTRAP.md#Cold open"
  alternate_greetings:
    - "SCENARIO.md#Alternate greeting — evening"
  mes_example: EXAMPLES.md
  system_prompt: "CONSTITUTION.md#Voice law"
  post_history_instructions: "CONSTITUTION.md#Hard limits"
  creator_notes: NOTES.md
  character_book: WORLD.md
runtime_only:
  - MEMORY.md
  - USER.md
"""

SCENARIO = """\
---
soul: scenario
---

# Scenario & Greetings

## Scenario

A small room, late.

## Alternate greeting — evening

You came back.

## Alternate greeting — morning

You are up early.
"""


@pytest.fixture
def soul(tmp_path):
    folder = tmp_path / "vault" / "soul"
    folder.mkdir(parents=True)
    (folder / "soul.yaml").write_text(MANIFEST, encoding="utf-8")
    (folder / "SCENARIO.md").write_text(SCENARIO, encoding="utf-8")
    return folder


def _draft(**over) -> Draft:
    base = dict(name="Yuri", identity="i", history="h", appearance="a", manner="m",
                personality="p", scenario="s", first_mes="cold",
                system_prompt="law", post_history_instructions="limits",
                creator_notes="notes")
    return Draft(**{**base, **over})


# ---- the manifest ----------------------------------------------------------

def test_a_block_sequence_value_is_replaced_whole(soul):
    """The corruption: rewriting only the `tags:` line left `  - companion`
    dangling under a scalar, and `soul.yaml` stopped parsing for good."""
    _set_manifest(soul / "soul.yaml", {"name": "Nyx", "tags": ["a", "b"]})
    manifest = yaml.safe_load((soul / "soul.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "Nyx"
    assert manifest["tags"] == ["a", "b"]
    assert manifest["fields"]["personality"] == "PERSONA.md@personality"
    assert manifest["runtime_only"] == ["MEMORY.md", "USER.md"]


def test_a_folded_scalar_value_is_replaced_whole(tmp_path):
    path = tmp_path / "soul.yaml"
    path.write_text("name: Yuri\ncreator: >-\n  a credit\n  over two lines\n"
                    "canon: canon-v2\nfields: {}\n", encoding="utf-8")
    _set_manifest(path, {"creator": "Short"})
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert manifest["creator"] == "Short"
    assert manifest["canon"] == "canon-v2"


def test_the_comments_people_write_in_survive(soul):
    _set_manifest(soul / "soul.yaml", {"canon": "original"})
    text = (soul / "soul.yaml").read_text(encoding="utf-8")

    assert "# a header comment people write in" in text
    assert "# a comment between the two halves" in text


def test_drives_round_trip_as_optional_manifest_metadata(soul):
    drives = ["Research before acting", "Protect the user's agency"]
    write_soul(soul, _draft(drives=drives))
    manifest = yaml.safe_load((soul / "soul.yaml").read_text(encoding="utf-8"))

    assert manifest["drives"] == drives
    assert "# a header comment people write in" in (soul / "soul.yaml").read_text()


def test_a_nested_key_of_the_same_name_is_not_the_one_rewritten(tmp_path):
    path = tmp_path / "soul.yaml"
    path.write_text("name: Top\nfields:\n  name: PERSONA.md@name\n", encoding="utf-8")
    _set_manifest(path, {"name": "New"})
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert manifest["name"] == "New"
    assert manifest["fields"]["name"] == "PERSONA.md@name"


# ---- the numbered blocks ---------------------------------------------------

def test_authored_greeting_headings_are_replaced_not_orphaned(soul):
    """`## Alternate greeting — evening` is the shape every authored soul in
    this repo uses. Missing it meant the old prose stayed in the file, unread by
    the manifest and still carried verbatim in the exported card."""
    write_soul(soul, _draft(alternate_greetings=["evening", "morning"]))
    text = (soul / "SCENARIO.md").read_text(encoding="utf-8")

    assert "— evening" not in text and "— morning" not in text
    assert text.count("## Alternate greeting") == 2
    assert "## Scenario" in text                    # the neighbouring section stands


def test_a_blank_greeting_is_dropped_from_both_sides(soul):
    """Skipped when writing but counted when syncing left a reference to a
    section that does not exist — which `_resolve_list` answers by returning no
    greetings at all, so one stray blank cost her every greeting she had."""
    write_soul(soul, _draft(alternate_greetings=["evening", "   ", "morning"]))
    manifest = yaml.safe_load((soul / "soul.yaml").read_text(encoding="utf-8"))
    headings = [line for line in (soul / "SCENARIO.md").read_text(encoding="utf-8")
                .splitlines() if line.startswith("## Alternate greeting")]

    assert manifest["fields"]["alternate_greetings"] == [
        "SCENARIO.md#Alternate greeting 1", "SCENARIO.md#Alternate greeting 2"]
    assert headings == ["## Alternate greeting 1", "## Alternate greeting 2"]


def test_every_manifest_greeting_reference_resolves(soul):
    from yurios.characters.soulfiles import SoulReader

    write_soul(soul, _draft(alternate_greetings=["evening", "", "morning"]))
    manifest = yaml.safe_load((soul / "soul.yaml").read_text(encoding="utf-8"))
    reader = SoulReader(soul)

    assert [reader.resolve(ref) for ref
            in manifest["fields"]["alternate_greetings"]] == ["evening", "morning"]


def test_saving_twice_does_not_accumulate(soul):
    write_soul(soul, _draft(alternate_greetings=["evening", "morning"]))
    first = (soul / "SCENARIO.md").read_text(encoding="utf-8")
    write_soul(soul, _draft(alternate_greetings=["evening", "morning"]))

    assert (soul / "SCENARIO.md").read_text(encoding="utf-8") == first


# ---- the section blocks ----------------------------------------------------

def test_a_replaced_section_keeps_its_blank_line_before_the_next_heading(soul):
    """The cut runs to the next `##`, blank separator included, so a block that
    ended in a single newline left `## Manner` flush against the last line of
    Appearance. Valid markdown by luck of the leading `#`, but the file people
    hand-edit came back from every studio save looking mangled."""
    (soul / "PERSONA.md").write_text(
        "---\nsoul: persona\n---\n\n# Yuri - Persona\n\n"
        "## Appearance\n\nold\n\n## Manner\n\nold\n", encoding="utf-8")
    write_soul(soul, _draft(appearance="tall and quiet", manner="warm"))

    assert (soul / "PERSONA.md").read_text(encoding="utf-8").endswith(
        "## Appearance\n\ntall and quiet\n\n## Manner\n\nwarm\n")


def test_a_section_written_into_a_fresh_persona_is_spaced_the_same(soul):
    """The importer's PERSONA.md is the common case: the studio's first save has
    to leave it looking exactly like the one the importer wrote."""
    write_soul(soul, _draft(appearance="tall", manner="warm"))
    text = (soul / "PERSONA.md").read_text(encoding="utf-8")

    assert "\n\n## Manner\n" in text
    assert "\n\n\n" not in text
