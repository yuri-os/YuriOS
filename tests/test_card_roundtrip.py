"""Export → import, on a vault that has actually been lived in.

The load-bearing test for SPEC §28. Three properties, and the feature is a
liability without all three:

  * what she grew travels — an approved self-edit is on the card;
  * what you are to her does not — nothing from `USER.md`, the memory tier, the
    corpus, the traces, her goals or her world model reaches the bytes;
  * and the far end reconstructs her exactly, starting the relationship at zero
    (`soul-src`, D-014).
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.support.cards import card_data, png_card, preferred, wrapper
from yurios.app.core.soul import SoulLoader
from yurios.characters import CharacterImporter, CharacterRegistry
from yurios.characters.exporter import ExportOptions, build_export

#: Distinct phrases, each planted in exactly one private surface. Every one is
#: long enough to be a canary and specific enough that a hit means a leak.
PRIVATE = {
    "vault/soul/USER.md": "He lives on the coast and takes his coffee black, always.",
    "vault/soul/MEMORY.md": "The night he finally talked about his father and the boat.",
    "memory/summary.md": "We have been circling his sister's wedding in November for weeks.",
    "memory/semantic/facts.md": "His sister is called Ada and she has moved abroad twice.",
    "memory/episodic": "He said the second interview went badly and he felt hollow after.",
    "goals.md": "Ask him how the conversation with his landlord actually went on Thursday.",
    "world/beliefs.jsonl": "He is quietly anxious about the mortgage decision this month.",
    "corpus": "I kept your spot by the window all afternoon, and I would do it again.",
    "traces": "He went very quiet after the call from the bank and stayed that way.",
}
GROWN = "She has grown quieter since the spring, and she waits longer before she speaks."


def live_in(record) -> None:
    """Fill every private surface, and let her edit her own persona once."""
    vault = record.paths.vault
    (vault / "soul" / "USER.md").write_text(
        f"---\nsoul: user\n---\n\n# User model\n\n## Who\n\n{PRIVATE['vault/soul/USER.md']}\n")
    (vault / "soul" / "MEMORY.md").write_text(
        f"# Relationship memory\n\n{PRIVATE['vault/soul/MEMORY.md']}\n")
    (vault / "memory" / "summary.md").write_text(
        f"# Conversation summary\n\n{PRIVATE['memory/summary.md']}\n")
    (vault / "memory" / "semantic" / "facts.md").write_text(
        f"# Facts\n\n- {PRIVATE['memory/semantic/facts.md']}\n")
    episodic = vault / "memory" / "episodic"
    episodic.mkdir(parents=True, exist_ok=True)
    (episodic / "2026-07-01.md").write_text(f"### 09:00\n\n{PRIVATE['memory/episodic']}\n")
    (vault / "goals.md").write_text(f"# Goals\n\n- {PRIVATE['goals.md']}\n")
    (vault / "world" / "beliefs.jsonl").write_text(
        json.dumps({"claim": PRIVATE["world/beliefs.jsonl"]}) + "\n")
    record.paths.corpus.mkdir(parents=True, exist_ok=True)
    (record.paths.corpus / "turns.jsonl").write_text(
        json.dumps({"completion": PRIVATE["corpus"], "collection_scope": "self"}) + "\n")
    record.paths.traces.mkdir(parents=True, exist_ok=True)
    (record.paths.traces / "context.jsonl").write_text(
        json.dumps({"note": PRIVATE["traces"]}) + "\n")

    persona = vault / "soul" / "PERSONA.md"
    persona.write_text(persona.read_text().rstrip() + f"\n\n{GROWN}\n")
    # and she has met someone, so her cold open is spent (§5.4)
    (vault / "soul" / "BOOTSTRAP.md").unlink()
    _commit(vault, "selfedit: apply persona note")


def _commit(vault, message: str) -> None:
    if shutil.which("git") is None:
        return
    subprocess.run(["git", "-C", str(vault), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(vault), "-c", "user.name=t",
                    "-c", "user.email=t@localhost", "commit", "-m", message],
                   capture_output=True)


@pytest.fixture
def grown(tmp_path):
    registry = CharacterRegistry(tmp_path / "source")
    record = CharacterImporter(registry, initialize_git=True).import_card(
        png_card(wrapper(card_data(), native=True)), character_id="subject")
    live_in(record)
    return record


def test_nothing_private_reaches_the_bytes(grown):
    result = build_export(grown, ExportOptions(acknowledged=True))

    blob = result.png.decode("latin-1")
    leaked = [surface for surface, phrase in PRIVATE.items() if phrase in blob]
    assert leaked == [], f"private content on the card from: {leaked}"


def test_what_she_grew_does_reach_the_bytes(grown):
    result = build_export(grown, ExportOptions(acknowledged=True))
    assert GROWN in json.dumps(result.card)


def test_the_soul_is_reconstructed_byte_for_byte(grown, tmp_path):
    result = build_export(grown, ExportOptions(acknowledged=True))
    registry = CharacterRegistry(tmp_path / "elsewhere")
    landed = CharacterImporter(registry, initialize_git=True).import_card(
        result.png, character_id="subject")

    before, after = grown.paths.vault / "soul", landed.paths.vault / "soul"
    exportable = [p.name for p in sorted(before.glob("*"))
                  if p.name not in ("USER.md", "MEMORY.md")]
    assert exportable, "the source vault has no exportable soul files"
    for name in exportable:
        assert (after / name).is_file(), f"{name} did not survive the trip"
        assert (after / name).read_text() == (before / name).read_text(), name


def test_the_relationship_starts_at_zero(grown, tmp_path):
    result = build_export(grown, ExportOptions(acknowledged=True))
    registry = CharacterRegistry(tmp_path / "elsewhere")
    landed = CharacterImporter(registry, initialize_git=True).import_card(
        result.png, character_id="subject")
    vault = landed.paths.vault

    assert "_(unknown)_" in (vault / "soul" / "USER.md").read_text()
    assert list((vault / "memory" / "episodic").glob("*")) == []
    assert (vault / "memory" / "semantic" / "facts.md").read_text().strip() == "# Facts"
    assert (vault / "memory" / "semantic" / "forgotten.md").read_text().strip() \
        == "# Forgotten facts"
    # her memory starts empty *in the memory tier*, where every reader looks —
    # not in a second copy under soul/ that nothing reads (seed_vault.py's rule,
    # which the import path used to disagree with)
    assert not (vault / "soul" / "MEMORY.md").exists()
    assert "No goals yet" in (vault / "goals.md").read_text()
    assert list(landed.paths.corpus.iterdir()) == []
    assert list(landed.paths.traces.iterdir()) == []
    if shutil.which("git"):
        log = subprocess.run(["git", "-C", str(vault), "log", "--oneline"],
                             capture_output=True, text=True).stdout.strip()
        assert log.count("\n") == 0, "her new life should begin at one commit"


def test_she_boots_on_the_other_machine(grown, tmp_path):
    """The point of the whole ladder: the runtime loads her, unchanged."""
    result = build_export(grown, ExportOptions(acknowledged=True))
    registry = CharacterRegistry(tmp_path / "elsewhere")
    landed = CharacterImporter(registry, initialize_git=False).import_card(
        result.png, character_id="subject")

    soul = SoulLoader(landed.paths.vault / "soul").load()

    assert soul.name == "Card Person"
    assert soul.personality == "dry, observant, kind"
    assert GROWN in soul.backbone
    assert [entry.name for entry in soul.lorebook] == ["Library"]
    # her cold open was spent before the card was cut, so the newcomer gets one
    assert soul.bootstrap


def test_generation_counts_the_hops(grown, tmp_path):
    first = build_export(grown, ExportOptions(acknowledged=True))
    assert first.card["data"]["extensions"]["yurios"]["lineage"]["generation"] == 0

    registry = CharacterRegistry(tmp_path / "elsewhere")
    landed = CharacterImporter(registry, initialize_git=True).import_card(
        first.png, character_id="subject")
    second = build_export(landed, ExportOptions(acknowledged=True))

    lineage = second.card["data"]["extensions"]["yurios"]["lineage"]
    assert lineage["generation"] == 1
    assert lineage["grown_from"].startswith("sha256:")


def test_the_card_survives_the_hop_unchanged(grown, tmp_path):
    """Export → import → export must not drift the persona."""
    options = ExportOptions(acknowledged=True, timestamps=False)
    first = build_export(grown, options)
    registry = CharacterRegistry(tmp_path / "elsewhere")
    landed = CharacterImporter(registry, initialize_git=True).import_card(
        first.png, character_id="subject")
    second = build_export(landed, options)

    def persona(card):
        return {key: value for key, value in card["data"].items()
                if key != "extensions"}

    assert persona(second.card) == persona(first.card)


def test_a_client_reads_the_card_the_way_a_client_would(grown):
    result = build_export(grown, ExportOptions(acknowledged=True))
    data = preferred(result.png)

    assert data["name"] == "Card Person"
    assert data["first_mes"]
    assert "{{user}}" in data["post_history_instructions"]


def test_a_card_without_a_soul_payload_still_imports(grown, tmp_path):
    """The lossy path stays working — that is what every non-YuriOS card uses."""
    result = build_export(grown, ExportOptions(acknowledged=True, include_soul=False))
    registry = CharacterRegistry(tmp_path / "elsewhere")
    landed = CharacterImporter(registry, initialize_git=False).import_card(
        result.png, character_id="subject")

    soul = SoulLoader(landed.paths.vault / "soul").load()
    assert soul.name == "Card Person"
    assert GROWN in soul.backbone          # via the flattened description
    assert "_(unknown)_" in (landed.paths.vault / "soul" / "USER.md").read_text()
