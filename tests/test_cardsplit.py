"""The deterministic half of card repair: routing a foreign `description`.

The property that matters most here is not that the router is clever — it is
that it is *lossless*. Every test that moves text also asserts the words came
out the far side, because a section router that quietly eats a paragraph would
be far worse than one that gives up and files everything under Identity.
"""
from __future__ import annotations

import pytest

from yurios.characters.cardsplit import clean_version, split_description


def _words(text: str) -> set[str]:
    return {word for word in text.split() if len(word) > 3}


def test_an_unlabelled_card_comes_out_exactly_as_it_went_in():
    description = "She is tall and she is tired.\n\nThat is the whole card."
    result = split_description(description)

    assert result["identity"] == description
    assert not result["history"] and not result["appearance"] and not result["manner"]


@pytest.mark.parametrize("header", [
    "Appearance:",
    "**Appearance**",
    "## Appearance",
    "> 👀 **Appearance**",
    "// --- {{char}} - Appearance ---",
    "* Physical Description:",
    "[Appearance:",
])
def test_the_shapes_a_header_actually_wears(header):
    result = split_description(f"She is nineteen.\n{header}\nGreen eyes, always.")

    assert "Green eyes, always." in result["appearance"]
    assert "She is nineteen." in result["identity"]


def test_a_whole_section_on_one_line_is_still_a_header():
    """The `[Backstory: …two thousand characters… ]` shape, which is common and
    which a length check against the whole line would miss entirely."""
    body = "She grew up in the Ravenholds. " * 60
    result = split_description(f"Name: Virelle\n[Backstory: {body}]")

    assert "Ravenholds" in result["history"]
    assert "Ravenholds" not in result["identity"]
    assert "Virelle" in result["identity"]


def test_four_sections_are_routed_and_nothing_is_lost():
    description = (
        "Name: Virelle\nAge: 20\n\n"
        "Appearance: Long raven-black hair and violet highlights.\n\n"
        "Backstory: Born within the Ravenholds, a secluded territory.\n\n"
        "Personality: Sharp-tongued and quietly loyal once trusted.\n")
    result = split_description(description)

    assert "raven-black" in result["appearance"]
    assert "Ravenholds" in result["history"]
    assert "Sharp-tongued" in result["manner"]
    assert "Virelle" in result["identity"]
    # every meaningful word survives the routing, somewhere
    assert _words(description) <= set().union(*(_words(v) for v in result.values()))


def test_an_unknown_label_continues_the_open_section():
    """Cards are full of colons. Only a *known* label may move the router, or a
    sentence like "She said: run" would tear a paragraph in half."""
    result = split_description(
        "Appearance: tall.\nFavourite drink: coffee.\nShe said: run.")

    assert "coffee" in result["appearance"]
    assert "She said: run." in result["appearance"]
    assert not result["identity"]


def test_a_bare_word_on_its_own_line_is_not_a_header():
    result = split_description("She waits.\n\nAppearance\n\nis everything to her.")

    assert result["identity"].count("Appearance") == 1
    assert not result["appearance"]


def test_a_section_wrapper_split_in_two_does_not_leave_half_a_bracket():
    """Cards wrap a whole run of fields in one `[ … ]`. Routing the run into two
    sections leaves each holding one side of the pair, which reads as damage."""
    result = split_description(
        "[Character info:\nName: Virelle\nAppearance: she is pale.\n]")

    assert result["identity"] == "Character info:\nName: Virelle"
    assert result["appearance"] == "Appearance: she is pale."


def test_a_balanced_bracket_on_one_line_is_left_alone():
    result = split_description("[Backstory: she was left to rot.]")

    assert result["history"] == "[Backstory: she was left to rot.]"


def test_two_blocks_of_one_section_are_kept_apart():
    result = split_description(
        "Appearance: pale.\nBackstory: raised badly.\nAttire: a maid uniform.")

    assert "pale" in result["appearance"] and "maid uniform" in result["appearance"]
    assert "\n\n" in result["appearance"]


@pytest.mark.parametrize("value,expected", [("1.0.0", "1.0.0"), ("v2", "v2"),
                                            ("2024.11", "2024.11"), ("", "1.0.0")])
def test_a_version_that_is_one_is_kept(value, expected):
    version, misfiled = clean_version(value)

    assert version == expected
    assert not misfiled


def test_something_that_is_not_a_version_is_kept_anyway():
    """A digitless token is not a version, and is also not rubbish — whatever the
    author put there goes to the notes rather than over the side."""
    version, misfiled = clean_version("main")

    assert version == "1.0.0"
    assert misfiled == "main"


def test_a_url_in_the_version_field_is_moved_not_dropped():
    version, misfiled = clean_version(
        "https://janitorai.com/characters/71e4aed8\nChat Name: Virelle")

    assert version == "1.0.0"
    assert "janitorai.com" in misfiled and "Chat Name" in misfiled
