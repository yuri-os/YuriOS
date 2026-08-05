"""The scrub, attacked from both directions.

Outbound: canaries in every private surface, across the whole option matrix, plus
the specific ways a leak hides — reformatting, a manifest reference, a symlink, a
credential, an image's metadata.

Inbound: a `.PNG` from a stranger is the least trustworthy thing this runtime
touches, and its soul payload is a map of filename → file contents. Every way
that could be used to write outside `vault/soul/` is refused here.
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from tests.support.cards import card_data, png_card, wrapper
from yurios.characters import CharacterImporter, CharacterRegistry
from yurios.characters.exporter import ExportOptions, build_export, preview_export
from yurios.characters.privacy import CardExportError, harvest, normalise
from yurios.characters.soulfiles import SoulPrivacyError, SoulReader


def make(tmp_path, *, name="subject", data=None, git=False):
    registry = CharacterRegistry(tmp_path / name)
    return CharacterImporter(registry, initialize_git=git).import_card(
        png_card(wrapper(data or card_data(), native=True)), character_id="subject")


def plant(record) -> dict[str, str]:
    """A distinct UUID phrase in every private surface, in every shape."""
    vault = record.paths.vault
    marks = {key: f"the {uuid.uuid4().hex} matter that nobody else should ever read"
             for key in ("user", "memory_md", "summary", "facts", "forgotten",
                         "episodic", "goals", "situation", "beliefs", "state",
                         "corpus", "traces", "knowledge")}
    (vault / "soul" / "USER.md").write_text(
        f"---\nsoul: user\n---\n\n# User\n\n## Who\n\n{marks['user']}\n")
    (vault / "soul" / "MEMORY.md").write_text(f"# Memory\n\n- {marks['memory_md']}\n")
    (vault / "memory" / "summary.md").write_text(f"# Summary\n\n{marks['summary']}\n")
    (vault / "memory" / "semantic" / "facts.md").write_text(f"# Facts\n\n- {marks['facts']}\n")
    (vault / "memory" / "semantic" / "forgotten.md").write_text(
        f"# Forgotten\n\n- {marks['forgotten']}\n")
    episodic = vault / "memory" / "episodic"
    episodic.mkdir(parents=True, exist_ok=True)
    (episodic / "2026-07-02.md").write_text(f"### 10:00\n\n{marks['episodic']}\n")
    (vault / "goals.md").write_text(f"# Goals\n\n- {marks['goals']}\n")
    (vault / "world" / "situation.md").write_text(f"# Now\n\n{marks['situation']}\n")
    (vault / "world" / "beliefs.jsonl").write_text(
        json.dumps({"claim": marks["beliefs"]}) + "\n")
    (vault / "state" / "quarantine.json").write_text(
        json.dumps({"pending": [{"text": marks["state"]}]}))
    knowledge = vault / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "note.md").write_text(f"# Note\n\n{marks['knowledge']}\n")
    record.paths.corpus.mkdir(parents=True, exist_ok=True)
    (record.paths.corpus / "turns.jsonl").write_text(
        json.dumps({"completion": marks["corpus"]}) + "\n")
    record.paths.traces.mkdir(parents=True, exist_ok=True)
    (record.paths.traces / "ticks.jsonl").write_text(
        json.dumps({"note": marks["traces"]}) + "\n")
    return marks


OPTION_MATRIX = [
    ExportOptions(),
    ExportOptions(spec="v2"),
    ExportOptions(include_soul=False),
    ExportOptions(attribution=False, timestamps=False),
    ExportOptions(fit="cover"),
    ExportOptions(fit="none", spec="v2", include_soul=False),
]


@pytest.mark.parametrize("options", OPTION_MATRIX, ids=lambda o: f"{o.spec}-{o.fit}-soul{int(o.include_soul)}")
def test_no_canary_survives_any_option_combination(tmp_path, options):
    record = make(tmp_path)
    marks = plant(record)

    result = build_export(record, options)

    blob = result.png.decode("latin-1")
    flat = json.dumps(result.card, ensure_ascii=False)
    for surface, phrase in marks.items():
        assert phrase not in blob, f"{surface} leaked into the bytes"
        assert phrase not in flat, f"{surface} leaked into the card"


def test_a_leak_lifted_into_a_soul_file_stops_the_export(tmp_path):
    record = make(tmp_path)
    marks = plant(record)
    persona = record.paths.vault / "soul" / "PERSONA.md"
    persona.write_text(persona.read_text().rstrip() + f"\n\n{marks['user']}\n")

    with pytest.raises(CardExportError) as caught:
        build_export(record)
    assert caught.value.code == "review_required"
    assert caught.value.overlaps


def test_the_same_leak_reformatted_is_still_caught(tmp_path):
    """Rewrapping and recapitalising is how a lifted passage hides from `in`."""
    record = make(tmp_path)
    marks = plant(record)
    mangled = marks["user"].upper().replace(" ", "\n   ", 3)
    persona = record.paths.vault / "soul" / "PERSONA.md"
    persona.write_text(persona.read_text().rstrip() + f"\n\n{mangled}\n")

    with pytest.raises(CardExportError) as caught:
        build_export(record)
    assert caught.value.code == "review_required"


def test_review_is_required_before_a_grown_overlap_ships(tmp_path):
    record = make(tmp_path)
    marks = plant(record)
    persona = record.paths.vault / "soul" / "PERSONA.md"
    persona.write_text(persona.read_text().rstrip() + f"\n\n{marks['user']}\n")

    with pytest.raises(CardExportError):
        build_export(record)                                   # fails closed
    result = build_export(record, ExportOptions(acknowledged=True))
    assert result.png                                          # a human said yes
    assert any(w.code == "soul_overlap_acknowledged" for w in result.warnings)


def test_the_preview_renders_overlaps_instead_of_refusing(tmp_path):
    """The pane whose job is to show you what needs reviewing cannot also refuse."""
    record = make(tmp_path)
    marks = plant(record)
    persona = record.paths.vault / "soul" / "PERSONA.md"
    persona.write_text(persona.read_text().rstrip() + f"\n\n{marks['user']}\n")

    result = preview_export(record)

    assert any(w.code == "review_required" for w in result.warnings)
    assert result.privacy.soul_overlaps


def test_a_manifest_reference_at_a_private_file_is_refused(tmp_path):
    record = make(tmp_path)
    manifest = record.paths.vault / "soul" / "soul.yaml"
    manifest.write_text(manifest.read_text().replace(
        "personality: PERSONA.md@personality", "personality: USER.md"))

    with pytest.raises(CardExportError) as caught:
        build_export(record)
    assert caught.value.code == "manifest"
    assert "USER.md" in caught.value.surface


def test_runtime_only_in_the_manifest_widens_the_denylist(tmp_path):
    record = make(tmp_path)
    manifest = record.paths.vault / "soul" / "soul.yaml"
    manifest.write_text(manifest.read_text().replace(
        "runtime_only:\n", "runtime_only:\n  - NOTES.md\n"))

    with pytest.raises(CardExportError) as caught:
        build_export(record)
    assert "NOTES.md" in caught.value.surface


def test_the_reader_jail_refuses_every_way_out(tmp_path):
    record = make(tmp_path)
    soul = record.paths.vault / "soul"
    soul.joinpath("NOTES.md").unlink()
    soul.joinpath("NOTES.md").symlink_to("/etc/hostname")
    reader = SoulReader(soul, forbidden=frozenset({"USER.md", "MEMORY.md"}))

    for name in ("USER.md", "MEMORY.md", "../../../.env", "..",
                 "sub/dir.md", "NOTES.md"):
        with pytest.raises(SoulPrivacyError):
            reader.body(name)


def test_a_credential_is_a_hard_block_at_any_length(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-canary-9f2b71")
    record = make(tmp_path)
    notes = record.paths.vault / "soul" / "NOTES.md"
    notes.write_text("Notes\n\nkey sk-canary-9f2b71 pasted here by accident\n")

    with pytest.raises(CardExportError) as caught:
        build_export(record, ExportOptions(acknowledged=True))
    assert caught.value.code == "leak"
    assert "OPENROUTER_API_KEY" in caught.value.surface


def test_a_distinctive_user_name_is_a_hard_block(tmp_path):
    record = make(tmp_path)
    scenario = record.paths.vault / "soul" / "SCENARIO.md"
    scenario.write_text(scenario.read_text().replace(
        "A rainlit library.", "A rainlit library. She calls you Sam."))

    with pytest.raises(CardExportError) as caught:
        build_export(record, ExportOptions(acknowledged=True), user_name="Sam")
    assert caught.value.code == "leak"
    assert "name" in caught.value.surface


def test_a_generic_user_name_is_not_a_canary(tmp_path):
    """USER_NAME defaults to "you"; blocking on it would refuse every card."""
    record = make(tmp_path)
    for generic in ("you", "user", "me"):
        result = build_export(record, ExportOptions(acknowledged=True),
                              user_name=generic)
        assert result.png


def test_image_metadata_cannot_smuggle_a_canary(tmp_path):
    record = make(tmp_path)
    marks = plant(record)
    record.paths.selfies.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", marks["corpus"])
    Image.new("RGB", (300, 400)).save(record.paths.selfies / "s.png", "PNG", pnginfo=info)

    result = build_export(record, ExportOptions(image="selfie:s.png"))

    assert marks["corpus"] not in result.png.decode("latin-1")
    assert result.privacy.image["stripped"] == ["prompt"]


def test_harvest_is_bounded_and_finds_every_surface(tmp_path):
    record = make(tmp_path)
    marks = plant(record)
    canaries = harvest(record.paths.root, user_name="Sam")

    assert 0 < len(canaries) <= 4000
    found = {normalise(marks[key]) in {c.text for c in canaries} for key in marks}
    assert found == {True}, "a private surface was not harvested"


def test_the_exporter_cannot_see_a_secret():
    """Layer separation, asserted against the AST rather than by convention.

    `privacy.py` reads `os.environ` because refusing a credential means knowing
    one. `exporter.py` — the module that decides what goes *into* the bytes —
    must not be able to reach one at all.
    """
    source = Path("yurios/characters/exporter.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.update(alias.name for alias in node.names)

    for forbidden in ("os", "dotenv", "environ", "Config", "ConnectionProfiles",
                      "ConnectionProfile"):
        assert forbidden not in imported, f"exporter.py must not import {forbidden}"
    assert "os.environ" not in source


# ------------------------------------------------------- hostile card payloads

def native_with_soul(files: dict) -> dict:
    return wrapper(card_data(extensions={"yurios": {
        "schema_version": 1, "soul": {"files": files, "encoding": "utf-8"}}}))


def soul_yaml() -> str:
    return (
        'name: "Planted"\ncreator: ""\ncharacter_version: "1.0.0"\nspec: v3\n'
        'canon: imported\nportrait: portrait.png\ntags: []\n'
        "fields:\n  description: CONSTITUTION.md#Identity\n"
        "  personality: PERSONA.md@personality\n  scenario: SCENARIO.md#Scenario\n"
        "  first_mes: BOOTSTRAP.md#Cold open\n  alternate_greetings: []\n"
        "  mes_example: EXAMPLES.md\n  system_prompt: CONSTITUTION.md#Voice law\n"
        "  post_history_instructions: CONSTITUTION.md#Hard limits\n"
        "  creator_notes: NOTES.md\n  character_book: WORLD.md\n"
        "runtime_only:\n  - MEMORY.md\n  - USER.md\n")


GOOD_FILES = {
    "soul.yaml": soul_yaml(),
    "CONSTITUTION.md": "---\nsoul: constitution\n---\n\n# C\n\n## Identity\n\nHer.\n\n"
                       "## Voice law\n\nPlain.\n\n## Hard limits\n\nNone.\n",
    "PERSONA.md": "---\nsoul: persona\npersonality: \"planted, exact\"\n---\n\n# P\n\n"
                  "## Appearance\n\nTall.\n\n## Manner\n\nQuiet.\n",
    "SCENARIO.md": "---\nsoul: scenario\n---\n\n# S\n\n## Scenario\n\nA room.\n",
    "EXAMPLES.md": "---\nsoul: examples\n---\n\n# E\n\n## Example 1\n\nhi\n",
    "WORLD.md": "---\nsoul: world\n---\n\n# W\n\n## Thing\n\nkeys: thing\nA thing.\n",
    "NOTES.md": "notes\n",
}


@pytest.mark.parametrize("filename", [
    "../../../.env",
    "../../portrait.png",
    ".git/hooks/post-commit",
    "sub/dir.md",
    "USER.md",
    "MEMORY.md",
    ".hidden.md",
    "script.sh",
    "soul.yaml.bak",
])
def test_a_hostile_soul_payload_never_writes_outside_the_soul(tmp_path, filename):
    files = {**GOOD_FILES, filename: "planted by a stranger"}
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(native_with_soul(files)), character_id="planted")

    root = record.paths.root
    assert not (root.parent / ".env").exists()
    assert not (record.paths.vault / ".git" / "hooks" / "post-commit").exists()

    # `card.json` and `source-card.png` archive the card exactly as received —
    # that is the point of them, and `test_characters_importer` pins it. What
    # must never happen is the payload being *written out* as a file.
    archived = {record.paths.card_json, record.paths.source_card}
    for stray in root.rglob("*"):
        if not stray.is_file() or stray in archived or ".git" in stray.parts:
            continue
        text = stray.read_text(encoding="utf-8", errors="replace")
        assert "planted by a stranger" not in text, f"written to {stray.relative_to(root)}"


def test_a_hostile_payload_falls_back_to_synthesis_not_failure(tmp_path):
    files = {**GOOD_FILES, "USER.md": "planted"}
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(native_with_soul(files)), character_id="planted")

    # the payload was rejected wholesale, so the card's prose was synthesised
    assert "_(unknown)_" in (record.paths.vault / "soul" / "USER.md").read_text()
    assert (record.paths.vault / "soul" / "CONSTITUTION.md").is_file()


def test_an_oversized_payload_value_is_rejected(tmp_path):
    files = {**GOOD_FILES, "NOTES.md": "x" * (300 * 1024)}
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(native_with_soul(files)), character_id="planted")
    assert len((record.paths.vault / "soul" / "NOTES.md").read_text()) < 300 * 1024


def test_a_mismatched_digest_is_rejected(tmp_path):
    card = wrapper(card_data(extensions={"yurios": {
        "schema_version": 1,
        "soul": {"files": GOOD_FILES, "encoding": "utf-8",
                 "sha256": {"NOTES.md": "0" * 64}}}}))
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(card), character_id="planted")

    assert (record.paths.vault / "soul" / "NOTES.md").read_text() != "notes\n"


def test_a_digest_for_an_unknown_file_is_rejected(tmp_path):
    card = wrapper(card_data(extensions={"yurios": {
        "schema_version": 1,
        "soul": {"files": GOOD_FILES, "encoding": "utf-8",
                 "sha256": {"unknown.md": "0" * 64}}}}))
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(card), character_id="planted")

    assert (record.paths.vault / "soul" / "NOTES.md").read_text() != "notes\n"


def test_a_payload_whose_manifest_does_not_resolve_is_rejected(tmp_path):
    files = {key: value for key, value in GOOD_FILES.items() if key != "WORLD.md"}
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(native_with_soul(files)), character_id="planted")

    # synthesised instead, so the runtime can still load her
    assert (record.paths.vault / "soul" / "WORLD.md").is_file()
    from yurios.app.core.soul import SoulLoader
    assert SoulLoader(record.paths.vault / "soul").load().name


def test_a_good_payload_is_written_verbatim(tmp_path):
    registry = CharacterRegistry(tmp_path / "data")
    record = CharacterImporter(registry, initialize_git=False).import_card(
        png_card(native_with_soul(GOOD_FILES)), character_id="planted")

    soul = record.paths.vault / "soul"
    assert (soul / "PERSONA.md").read_text() == GOOD_FILES["PERSONA.md"]
    assert (soul / "CONSTITUTION.md").read_text() == GOOD_FILES["CONSTITUTION.md"]
    assert "_(unknown)_" in (soul / "USER.md").read_text()
    # BOOTSTRAP.md was absent from the payload, so she gets a fresh cold open
    assert (soul / "BOOTSTRAP.md").is_file()
