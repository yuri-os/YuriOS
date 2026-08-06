"""The exporter: field mapping, the V3 shape, and what refuses to ship."""
from __future__ import annotations

import json

import pytest
from PIL import Image, PngImagePlugin

from tests.support.cards import card_data, png_card, preferred, st_reader, wrapper
from yurios.characters import CharacterImporter, CharacterRegistry
from yurios.characters.card import parse_png_card
from yurios.characters.exporter import (
    V3_FIELDS,
    ExportOptions,
    build_export,
    preview_export,
    read_card_chunks,
)
from yurios.characters.privacy import CardExportError


def make(tmp_path, data=None, *, native=True, character_id="subject", git=False):
    registry = CharacterRegistry(tmp_path / "data")
    source = png_card(wrapper(data or card_data(), native=native))
    return CharacterImporter(registry, initialize_git=git).import_card(
        source, character_id=character_id)


def test_card_carries_exactly_the_v3_field_set(tmp_path):
    result = build_export(make(tmp_path))
    data = result.card["data"]

    assert set(data) == V3_FIELDS
    assert data["name"] == "Card Person"
    assert data["scenario"] == "A rainlit library."
    assert data["group_only_greetings"] == []          # required by V3, may be empty
    assert data["alternate_greetings"] == ["Back again?", "Good morning."]
    assert data["mes_example"].startswith("<START>\n")
    assert data["character_book"]["entries"][0]["keys"] == ["book", "library"]
    assert "https://yurios.org" in data["source"]


def test_macros_survive_untouched(tmp_path):
    """`SoulLoader` expands {{user}} because it builds a prompt. An export must
    not, or the exporting user's name is baked into a stranger's card."""
    result = build_export(make(tmp_path))
    flat = json.dumps(result.card)

    assert "{{user}}" in flat
    assert "{{char}}" in result.card["data"]["mes_example"] or "{{user}}" in flat


def test_both_chunks_are_written_and_read_back(tmp_path):
    result = build_export(make(tmp_path))

    assert result.verified == {"chara": "Card Person", "ccv3": "Card Person"}
    # the strict parser and a client-shaped reader must agree
    assert parse_png_card(result.png).keyword == "ccv3"
    assert sorted(st_reader(result.png)) == ["ccv3", "chara"]
    assert preferred(result.png)["name"] == "Card Person"


def test_v2_chunk_drops_v3_only_keys_and_decorators(tmp_path):
    data = card_data(character_book={"entries": [
        {"name": "Lore", "keys": ["k"], "content": "@@depth 4\nthe real content"}]})
    result = build_export(make(tmp_path, data))
    chunks = st_reader(result.png)

    v2 = chunks["chara"]["data"]
    assert chunks["chara"]["spec"] == "chara_card_v2"
    for key in ("nickname", "assets", "source", "group_only_greetings",
                "creation_date", "modification_date"):
        assert key not in v2
    assert "@@depth" not in v2["character_book"]["entries"][0]["content"]
    assert "@@depth" in chunks["ccv3"]["data"]["character_book"]["entries"][0]["content"]


def test_spec_v2_writes_only_the_chara_chunk(tmp_path):
    result = build_export(make(tmp_path), ExportOptions(spec="v2"))
    assert sorted(st_reader(result.png)) == ["chara"]


def test_soul_payload_round_trips_with_digests(tmp_path):
    result = build_export(make(tmp_path))
    soul = result.card["data"]["extensions"]["yurios"]["soul"]

    assert "soul.yaml" in soul["files"]
    assert "USER.md" not in soul["files"]
    assert "MEMORY.md" not in soul["files"]
    assert "runtime_only" not in soul["manifest"]
    import hashlib
    for name, text in soul["files"].items():
        assert soul["sha256"][name] == hashlib.sha256(text.encode()).hexdigest()


def test_include_soul_false_omits_the_payload(tmp_path):
    result = build_export(make(tmp_path), ExportOptions(include_soul=False))
    block = result.card["data"]["extensions"]["yurios"]
    assert "soul" not in block


def test_growth_carries_counts_and_never_content(tmp_path):
    record = make(tmp_path, git=True)
    (record.paths.vault / "soul" / "USER.md").write_text(
        "# User\n\nHe is a person with a name and a life outside this room.\n")
    block = build_export(record).card["data"]["extensions"]["yurios"]

    growth = block["growth"]
    assert set(growth) <= {"vault_commits", "self_edits_applied",
                           "soul_files_changed", "days_lived"}
    assert isinstance(growth["vault_commits"], int)
    assert "a person with a name" not in json.dumps(growth)


def test_growth_names_the_soul_files_that_actually_moved(tmp_path):
    """The list is built from one batched `git log --name-only` rather than a
    subprocess per soul file — a dozen spawns on every export *and* every
    preview of one. Same answer, so pin the answer."""
    import subprocess

    from yurios.characters import vcs

    record = make(tmp_path, git=True)
    vault = record.paths.vault
    (vault / "soul" / "PERSONA.md").write_text(
        "---\nsoul: persona\npersonality: quieter now\n---\n\n"
        "## Appearance\n\nshe has changed\n\n## Manner\n\nslower\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(vault), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-m", "selfedit: persona"],
                   check=True, capture_output=True)

    growth = build_export(record).card["data"]["extensions"]["yurios"]["growth"]
    assert growth["soul_files_changed"] == ["PERSONA.md"]

    counts = vcs.commit_counts(vault, "soul")
    assert counts["soul/PERSONA.md"] == 2 and counts["soul/CONSTITUTION.md"] == 1


def test_commit_counts_answers_nothing_for_a_vault_with_no_git(tmp_path):
    from yurios.characters import vcs

    assert vcs.commit_counts(make(tmp_path).paths.vault, "soul") == {}


def test_consumed_bootstrap_falls_back_to_a_return_greeting(tmp_path):
    """Every character you have actually met has no BOOTSTRAP.md (§5.4), and
    soul.yaml points `first_mes` straight at it."""
    record = make(tmp_path)
    (record.paths.vault / "soul" / "BOOTSTRAP.md").unlink()

    result = build_export(record)

    assert result.card["data"]["first_mes"] == "Back again?"
    assert any(w.code == "bootstrap_consumed" for w in result.warnings)


def test_no_first_message_at_all_is_refused(tmp_path):
    record = make(tmp_path)
    (record.paths.vault / "soul" / "BOOTSTRAP.md").unlink()
    (record.paths.vault / "soul" / "SCENARIO.md").write_text(
        "---\nsoul: scenario\n---\n\n# S\n\n## Scenario\n\nA room.\n")
    (record.paths.vault / "soul" / "soul.yaml").write_text(
        (record.paths.vault / "soul" / "soul.yaml").read_text()
        .replace("    - SCENARIO.md#Alternate greeting 1\n", "")
        .replace("    - SCENARIO.md#Alternate greeting 2\n", ""))

    with pytest.raises(CardExportError) as caught:
        build_export(record)
    assert "first message" in str(caught.value)


def test_importer_placeholders_are_flagged_not_shipped_silently(tmp_path):
    result = build_export(make(tmp_path))
    codes = {(w.code, w.field) for w in result.warnings}
    # the seeded soul has no History section, so the importer wrote a placeholder
    assert ("placeholder", "description") in codes


def test_voice_law_warning_only_fires_when_her_own_law_says_so(tmp_path):
    loud = card_data(first_mes="You made it!", post_history_instructions="Be warm.")
    quiet = card_data(first_mes="You made it!",
                      post_history_instructions="Never use an exclamation mark.")

    without = build_export(make(tmp_path, loud, character_id="a"))
    with_law = build_export(make(tmp_path, quiet, character_id="b"))

    assert not any(w.code == "voice_law" for w in without.warnings)
    assert any(w.code == "voice_law" for w in with_law.warnings)


def test_timestamps_can_be_withheld(tmp_path):
    result = build_export(make(tmp_path), ExportOptions(timestamps=False))
    data = result.card["data"]

    assert data["creation_date"] == 0
    assert data["modification_date"] == 0
    assert "days_lived" not in data["extensions"]["yurios"]["growth"]


def test_attribution_never_touches_the_persona(tmp_path):
    on = build_export(make(tmp_path, character_id="a")).card["data"]
    off = build_export(make(tmp_path, character_id="b"),
                       ExportOptions(attribution=False)).card["data"]

    assert "YuriOS" in on["creator_notes"]
    assert "yurios" in on["tags"]
    for field in ("description", "personality", "scenario", "first_mes",
                  "system_prompt", "post_history_instructions"):
        assert on[field] == off[field]
    assert "YuriOS" not in off["creator_notes"]
    assert "yurios" not in off["tags"]


def test_third_party_extensions_pass_through_but_yurios_is_rebuilt(tmp_path):
    data = card_data(extensions={"depth_prompt": {"depth": 4, "prompt": "stay"},
                                 "yurios": {"schema_version": 1, "bogus": "dropped"}})
    result = build_export(make(tmp_path, data))
    extensions = result.card["data"]["extensions"]

    assert extensions["depth_prompt"] == {"depth": 4, "prompt": "stay"}
    assert "bogus" not in extensions["yurios"]
    assert extensions["yurios"]["runtime"] == "YuriOS"


def test_oversized_extension_is_dropped_with_a_warning(tmp_path):
    data = card_data(extensions={"yurios": {"schema_version": 1},
                                 "huge": {"blob": "x" * (80 * 1024)}})
    result = build_export(make(tmp_path, data))

    assert "huge" not in result.card["data"]["extensions"]
    assert any(w.code == "extension_dropped" and w.field == "huge"
               for w in result.warnings)


def test_one_oversized_soul_file_is_dropped_from_the_payload(tmp_path):
    """The narrowest degrade: lose that file, keep the card and everything else."""
    record = make(tmp_path)
    (record.paths.vault / "soul" / "WORLD.md").write_text(
        "---\nsoul: world\n---\n\n# World\n\n## Big\n\nkeys: big\n" + "x " * 200_000)

    result = build_export(record)
    soul = result.card["data"]["extensions"]["yurios"]["soul"]

    assert "WORLD.md" not in soul["files"]
    assert "PERSONA.md" in soul["files"]
    assert any(w.code == "soul_file_too_large" and w.field == "WORLD.md"
               for w in result.warnings)
    assert result.verified


def test_an_oversized_payload_is_dropped_whole(tmp_path):
    record = make(tmp_path)
    soul = record.paths.vault / "soul"
    for name in ("PERSONA.md", "SCENARIO.md", "EXAMPLES.md"):
        soul.joinpath(name).write_text(
            soul.joinpath(name).read_text() + "\n\n" + "padding " * 25_000)

    result = build_export(record)

    assert result.card["data"]["extensions"]["yurios"].get("soul_omitted") == "size"
    assert any(w.code == "soul_payload_dropped" for w in result.warnings)
    assert result.verified  # it still produces a readable card


def test_a_card_that_no_reader_would_accept_is_refused(tmp_path):
    record = make(tmp_path)
    (record.paths.vault / "soul" / "SCENARIO.md").write_text(
        "---\nsoul: scenario\n---\n\n# S\n\n## Scenario\n\n" + "x " * 1_800_000
        + "\n\n## Alternate greeting 1\n\nhi\n\n## Alternate greeting 2\n\nhey\n")

    with pytest.raises(CardExportError) as caught:
        build_export(record)
    assert "limit a reader will accept" in str(caught.value)


def test_image_metadata_is_stripped_and_credentials_kept(tmp_path):
    record = make(tmp_path)
    record.paths.selfies.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", "a generation prompt nobody else should read")
    info.add_text("content_credentials", '{"ai_generated": true}')
    Image.new("RGB", (300, 400), (80, 60, 90)).save(
        record.paths.selfies / "one.png", "PNG", pnginfo=info)

    result = build_export(record, ExportOptions(image="selfie:one.png"))

    assert result.privacy.image["stripped"] == ["prompt"]
    assert result.privacy.image["kept"] == ["content_credentials"]
    assert b"a generation prompt" not in result.png
    assert read_card_chunks(result.png)          # and it did not hang on that chunk


def test_selfie_selection_is_jailed_to_the_character(tmp_path):
    record = make(tmp_path)
    record.paths.selfies.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CardExportError):
        build_export(record, ExportOptions(image="selfie:../../../etc/hostname"))


def test_fit_modes_frame_to_the_card_canvas(tmp_path):
    record = make(tmp_path)
    for mode, expected in (("contain", (512, 768)), ("cover", (512, 768))):
        result = build_export(record, ExportOptions(fit=mode))
        assert tuple(result.privacy.image["size"]) == expected
    as_is = build_export(record, ExportOptions(fit="none"))
    assert tuple(as_is.privacy.image["size"]) == (5, 4)


def test_preview_produces_no_bytes_but_the_whole_report(tmp_path):
    result = preview_export(make(tmp_path))

    assert result.png == b""
    assert result.card["data"]["name"] == "Card Person"
    assert [row.field for row in result.report][:2] == ["description", "personality"]
    assert result.privacy.stays
    assert result.privacy.ran_on == ["json"]


def test_export_is_idempotent(tmp_path):
    record = make(tmp_path)
    first = build_export(record, ExportOptions(timestamps=False)).card
    second = build_export(record, ExportOptions(timestamps=False)).card
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_unknown_options_are_refused_early(tmp_path):
    with pytest.raises(CardExportError):
        ExportOptions(spec="v9")
    with pytest.raises(CardExportError):
        ExportOptions(fit="squish")
