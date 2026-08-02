from __future__ import annotations

import base64
import io
import json
import shutil
import struct
import subprocess
import zlib

from PIL import Image

from yurios.app.core.soul import SoulLoader
from yurios.characters import CharacterImporter, CharacterRegistry, parse_png_card
from yurios.characters.importer import _initialize_git
from yurios.world.host import _update_soul


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _png_card(card: dict) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGBA", (5, 4), (20, 40, 60, 100))
    image.save(output, "PNG", pnginfo=None)
    png = output.getvalue()
    iend = png.rfind(b"\x00\x00\x00\x00IEND")
    payload = b"ccv3\x00" + base64.b64encode(json.dumps(card).encode("utf-8"))
    return png[:iend] + _chunk(b"tEXt", payload) + png[iend:]


def _card(*, native: bool = False) -> dict:
    data = {
        "name": "Card Person",
        "creator": "Offline Test",
        "character_version": "3.2.1",
        "description": "A complete identity from the card.",
        "personality": "dry, observant, kind",
        "scenario": "A rainlit library.",
        "first_mes": "You made it.",
        "alternate_greetings": ["Back again?", "Good morning."],
        "mes_example": "<START>\n{{user}}: Hello\n{{char}}: Hello yourself.",
        "system_prompt": "Speak plainly.",
        "post_history_instructions": "Never narrate for {{user}}.",
        "creator_notes": "Imported without flattening unknown fields.",
        "tags": ["original", "test"],
        "character_book": {
            "entries": [
                {"name": "Library", "keys": ["book", "library"], "content": "It never closes."}
            ]
        },
        "future_extension": {"preserve": [1, 2, 3]},
    }
    if native:
        data["extensions"] = {"yurios": {"schema_version": 1}}
    return {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}


def test_generic_import_is_disabled_reviewable_and_complete(tmp_path):
    registry = CharacterRegistry(tmp_path / "data")
    source = _png_card(_card())

    record = CharacterImporter(registry, initialize_git=True).import_card(
        source, character_id="card-person", enabled=True, autostart=True
    )

    assert not record.lifecycle.enabled
    assert not record.lifecycle.autostart
    assert record.lifecycle.review_required
    assert record.paths.source_card.read_bytes() == source
    stored = json.loads(record.paths.card_json.read_text(encoding="utf-8"))
    assert stored["data"]["future_extension"] == {"preserve": [1, 2, 3]}
    assert {path.name for path in (
        record.paths.corpus,
        record.paths.traces,
        record.paths.tool_logs,
        record.paths.selfies,
    ) if path.is_dir()} == {"corpus", "traces", "tool-logs", "selfies"}

    # Re-encoding removes card metadata while retaining bounded decoded pixels.
    assert Image.open(record.paths.portrait).size == (5, 4)
    try:
        parse_png_card(record.paths.portrait)
        assert False, "sanitized portrait unexpectedly retained character metadata"
    except ValueError as exc:
        assert "no ccv3 or chara" in str(exc)

    soul = SoulLoader(record.paths.vault / "soul").load()
    assert soul.name == "Card Person"
    assert "complete identity" in soul.backbone
    assert soul.personality == "dry, observant, kind"
    assert soul.bootstrap == "You made it."
    assert [entry.name for entry in soul.lorebook] == ["Library"]
    assert (record.paths.vault / ".git").is_dir() == bool(shutil.which("git"))
    if shutil.which("git"):
        result = subprocess.run(
            ["git", "-c", f"safe.directory={record.paths.vault.resolve()}",
             "-C", str(record.paths.vault), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "vault: import character card"
    assert CharacterRegistry(registry.data_root).require(record.id).paths.root == record.paths.root


def test_explicit_yurios_card_can_be_enabled_and_autostarted(tmp_path):
    registry = CharacterRegistry(tmp_path)
    record = CharacterImporter(registry, initialize_git=False).import_card(
        _png_card(_card(native=True)),
        character_id="native-card",
        enabled=True,
        autostart=True,
    )

    assert record.lifecycle.enabled
    assert record.lifecycle.autostart
    assert not record.lifecycle.review_required


def test_automatic_ids_use_name_and_increment_version(tmp_path):
    registry = CharacterRegistry(tmp_path)
    importer = CharacterImporter(registry, initialize_git=False)
    source = _png_card(_card(native=True))

    first = importer.import_card(source)
    second = importer.import_card(source)
    third = importer.import_card(source)

    assert [first.id, second.id, third.id] == [
        "card_person", "card_person_v2", "card_person_v3"
    ]
    assert first.paths.root.name == "card_person"


def test_description_edit_updates_runtime_soul(tmp_path):
    record = CharacterImporter(
        CharacterRegistry(tmp_path), initialize_git=False
    ).import_card(_png_card(_card(native=True)), enabled=False)

    _update_soul(record, {"description": "A newly edited identity."})

    assert "A newly edited identity." in SoulLoader(record.paths.vault / "soul").load().backbone


def test_git_initialization_marks_mounted_vault_safe(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        output = str(vault) if command[-2:] == ["rev-parse", "--show-toplevel"] else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("yurios.characters.importer.shutil.which", lambda name: "/usr/bin/git")
    monkeypatch.setattr("yurios.characters.importer.subprocess.run", run)

    assert _initialize_git(vault)
    assert commands[0] == ["/usr/bin/git", "init", "-q", str(vault)]
    assert commands[1][:4] == [
        "/usr/bin/git", "-c", f"safe.directory={vault.resolve()}", "-C"
    ]
    assert commands[1][5:] == ["rev-parse", "--show-toplevel"]
    assert commands[2][5:7] == ["config", "--local"]
    assert commands[-2][5:] == ["add", "-A"]
    assert commands[-1][5:] == ["commit", "-q", "-m", "vault: import character card"]


def _two_payload_card(live: dict, stale: dict) -> bytes:
    """One PNG carrying two different `chara` payloads, the way a card that was
    edited on a site and re-uploaded actually arrives."""
    output = io.BytesIO()
    Image.new("RGBA", (5, 4), (20, 40, 60, 100)).save(output, "PNG", pnginfo=None)
    png = output.getvalue()
    iend = png.rfind(b"\x00\x00\x00\x00IEND")
    chunks = b"".join(
        _chunk(b"tEXt", b"chara\x00" + base64.b64encode(
            json.dumps(payload).encode("utf-8")))
        for payload in (live, stale))
    return png[:iend] + chunks + png[iend:]


def test_a_card_with_two_payloads_imports_the_live_one_and_notes_it(tmp_path):
    live = _card()["data"] | {"first_mes": "The guild hall roars."}
    stale = {key: value for key, value in live.items() if key != "character_book"}
    stale["first_mes"] = "later"

    record = CharacterImporter(CharacterRegistry(tmp_path),
                               initialize_git=False).import_card(
        _two_payload_card({"spec": "chara_card_v2", "data": live},
                          {"spec": "chara_card_v2", "data": stale}))

    bootstrap = (record.paths.vault / "soul" / "BOOTSTRAP.md").read_text(encoding="utf-8")
    assert "The guild hall roars." in bootstrap
    assert "later" not in bootstrap
    # The choice the parser made is in the file the reviewer opens, not only in a
    # log line they will never see.
    notes = (record.paths.vault / "soul" / "NOTES.md").read_text(encoding="utf-8")
    assert "more than one chara payload" in notes


def test_a_native_card_with_two_payloads_is_not_trusted_to_start(tmp_path):
    """`extensions.yurios` normally means "this came from here, run it". A file
    the parser had to disambiguate does not get that, however native it claims to
    be — the payload that vouches for it is one of the two in question."""
    live = _card(native=True)["data"]
    stale = live | {"name": "Someone Else"}

    record = CharacterImporter(CharacterRegistry(tmp_path),
                               initialize_git=False).import_card(
        _two_payload_card({"spec": "chara_card_v3", "data": live},
                          {"spec": "chara_card_v3", "data": stale}),
        enabled=True, autostart=True)

    assert not record.lifecycle.enabled
    assert not record.lifecycle.autostart
    assert record.lifecycle.review_required
