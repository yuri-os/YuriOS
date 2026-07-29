from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from yurios.characters import CharacterRegistry, default_portrait_bytes
from yurios.migrate import MigrationError, check_migration, migrate_legacy_data


def _config(tmp_path: Path, *, name: str | None = "Yuri") -> SimpleNamespace:
    roots = {
        key: tmp_path / value
        for key, value in {
            "vault_dir": "old-vault",
            "corpus_dir": "old-corpus",
            "trace_dir": "old-traces",
            "tool_log_dir": "old-tool-logs",
            "selfie_dir": "old-selfies",
        }.items()
    }
    soul = roots["vault_dir"] / "soul"
    soul.mkdir(parents=True)
    manifest = {"creator": "test"}
    if name is not None:
        manifest["name"] = name
    (soul / "soul.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (roots["vault_dir"] / "memory.bin").write_bytes(b"\x00legacy-vault\xff")
    for index, key in enumerate(
        ("corpus_dir", "trace_dir", "tool_log_dir", "selfie_dir"), start=1
    ):
        roots[key].mkdir()
        (roots[key] / "data.bin").write_bytes(bytes(range(index, index + 8)))
    return SimpleNamespace(
        **roots,
        companion_name="Fallback Name",
        chat_model="lm_studio/chat-model",
        utility_model="lm_studio/utility-model",
        lmstudio_base_url="http://localhost:1234/v1",
        context_length=32768,
        chat_thinking=False,
        utility_thinking=True,
        mind_enabled=True,
        tts_backend="kokoro",
        tts_register="af_heart",
        stt_backend="faster_whisper",
        desktop_body="vrm",
        avatar_model="hiyori",
    )


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_migrates_all_roots_atomically_and_is_idempotent(tmp_path):
    config = _config(tmp_path)
    data = tmp_path / "data"
    before = {
        name: _tree_digest(getattr(config, name))
        for name in (
            "vault_dir",
            "corpus_dir",
            "trace_dir",
            "tool_log_dir",
            "selfie_dir",
        )
    }

    result = migrate_legacy_data(config, data)

    assert result.status == "migrated"
    assert result.character_id == "yuri"
    record = CharacterRegistry(data).require("yuri")
    assert record.display.name == "Yuri"
    assert record.lifecycle.enabled and record.lifecycle.autostart
    assert not record.lifecycle.review_required
    assert record.models.chat == "lm_studio/chat-model"
    assert record.connection.endpoint == "http://localhost:1234/v1"
    assert record.voice.voice_id == "af_heart"
    assert record.body.backend == "vrm"
    for name, destination in {
        "corpus_dir": record.paths.corpus,
        "trace_dir": record.paths.traces,
        "tool_log_dir": record.paths.tool_logs,
        "selfie_dir": record.paths.selfies,
    }.items():
        assert _tree_digest(destination) == before[name]
    assert (record.paths.vault / "memory.bin").read_bytes() == b"\x00legacy-vault\xff"
    copied_soul = yaml.safe_load(
        (record.paths.vault / "soul" / "soul.yaml").read_text(encoding="utf-8")
    )
    assert str(copied_soul["vault_format"]) == "0.2"
    assert json.loads((data / "layout.json").read_text())["layout_version"] == "0.2"
    assert not list((data / "characters").glob(".*.migrate-*"))

    again = migrate_legacy_data(config, data)
    assert again.status == "already-migrated"
    assert again.character_id == "yuri"
    assert len(CharacterRegistry(data)) == 1
    for name, digest in before.items():
        assert _tree_digest(getattr(config, name)) == digest


def test_migration_gives_yuri_the_packaged_portrait(tmp_path):
    config = _config(tmp_path)
    data = tmp_path / "data"

    result = migrate_legacy_data(config, data)

    portrait = CharacterRegistry(data).require("yuri").paths.portrait
    assert portrait.read_bytes() == default_portrait_bytes()

    # hers once it is there: a replaced portrait survives every later start-up
    portrait.write_bytes(b"\x89PNG\r\n\x1a\nmine")
    assert migrate_legacy_data(config, data).status == "already-migrated"
    assert portrait.read_bytes() == b"\x89PNG\r\n\x1a\nmine"


def test_already_migrated_data_backfills_the_missing_portrait(tmp_path):
    config = _config(tmp_path)
    data = tmp_path / "data"
    result = migrate_legacy_data(config, data)
    portrait = CharacterRegistry(data).require("yuri").paths.portrait
    portrait.unlink()                       # migrated before the portrait shipped

    assert check_migration(config, data).status == "already-migrated"
    assert not portrait.exists()            # --check still writes nothing

    assert migrate_legacy_data(config, data).status == "already-migrated"
    assert portrait.read_bytes() == default_portrait_bytes()
    assert result.character_root == portrait.parent


def test_someone_elses_character_gets_no_default_portrait(tmp_path):
    config = _config(tmp_path, name="Mia")
    data = tmp_path / "data"

    result = migrate_legacy_data(config, data)

    assert result.display_name == "Mia"
    assert not CharacterRegistry(data).require("yuri").paths.portrait.exists()


def test_check_and_dry_run_validate_without_writing(tmp_path):
    config = _config(tmp_path, name=None)
    data = tmp_path / "data"

    checked = check_migration(config, data)
    dry = migrate_legacy_data(config, data, dry_run=True)

    assert checked.status == dry.status == "needed"
    assert checked.display_name == "Fallback Name"
    assert checked.character_id == "yuri"
    assert not data.exists()


def test_collision_gets_stable_suffix_without_overwrite(tmp_path):
    config = _config(tmp_path)
    data = tmp_path / "data"
    occupied = data / "characters" / "yuri"
    occupied.mkdir(parents=True)
    (occupied / "keep").write_text("mine", encoding="utf-8")

    result = migrate_legacy_data(config, data)

    assert result.character_id == "yuri-2"
    assert (occupied / "keep").read_text(encoding="utf-8") == "mine"


def test_passed_registry_supplies_data_dir(tmp_path, monkeypatch):
    config = _config(tmp_path)
    registry = CharacterRegistry(tmp_path / "chosen-data")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "wrong-data"))

    result = migrate_legacy_data(config, registry=registry)

    assert result.data_dir == registry.data_root
    assert registry.require("yuri").paths.root == result.character_root
    assert not (tmp_path / "wrong-data").exists()


def test_updates_explicit_legacy_vault_format_without_duplicate_key(tmp_path):
    config = _config(tmp_path)
    soul = config.vault_dir / "soul" / "soul.yaml"
    soul.write_text("name: Yuri\nvault_format: 0.1\n", encoding="utf-8")

    result = migrate_legacy_data(config, tmp_path / "data")

    copied = (result.character_root / "vault" / "soul" / "soul.yaml").read_text()
    assert copied.count("vault_format:") == 1
    assert yaml.safe_load(copied)["vault_format"] == "0.2"


def test_invalid_source_is_refused_without_target_changes(tmp_path):
    config = _config(tmp_path)
    (config.vault_dir / "soul" / "soul.yaml").write_text("name: [", encoding="utf-8")
    data = tmp_path / "data"

    with pytest.raises(MigrationError, match="invalid legacy soul"):
        migrate_legacy_data(config, data)

    assert not data.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_preserves_vault_git_and_commits_only_in_copy(tmp_path):
    config = _config(tmp_path)
    source = config.vault_dir
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-qm",
            "legacy",
        ],
        check=True,
    )
    source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = migrate_legacy_data(config, tmp_path / "data")

    copied = result.character_root / "vault"
    copied_head = subprocess.run(
        ["git", "-C", str(copied), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    current_source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subject = subprocess.run(
        ["git", "-C", str(copied), "log", "-1", "--pretty=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (copied / ".git").is_dir()
    assert current_source_head == source_head
    assert copied_head != source_head
    assert subject == "migrate: vault format 0.2"
