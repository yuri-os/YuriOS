"""A character's own model settings are visible, and can be given back.

The bug behind these tests: `.env` said `gguf/…`, one character's record still
said `lm_studio/…` from before the house moved, and the record wins (SPEC §31.2).
Startup dialled LM Studio, `.env` was innocent, and nothing on screen connected
the two.
"""
from __future__ import annotations

from argparse import Namespace

from yurios import cli
from yurios.characters import (
    CharacterPaths,
    CharacterRecord,
    CharacterRegistry,
    ConnectionBinding,
    ConnectionProfile,
    ConnectionProfiles,
    DisplayMetadata,
    LifecycleFlags,
    LoopSwitches,
    ModelBinding,
    VoiceBinding,
    overrides,
)
from yurios.world.config import Config


HOUSE = "gguf/HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced"
HERS = "lm_studio/gemma4-12b-qat-uncensored-hauhaucs-balanced"


def _house_config(**update) -> Config:
    cfg = Config(_env_file=None)
    return cfg.model_copy(update={"chat_model": HOUSE, "utility_model": HOUSE,
                                  **update})


def _record(character_id: str, root, *, models: ModelBinding | None = None,
            connection: ConnectionBinding | None = None) -> CharacterRecord:
    return CharacterRecord(
        id=character_id,
        display=DisplayMetadata(character_id.title()),
        paths=CharacterPaths.under(root / "characters" / character_id),
        lifecycle=LifecycleFlags(enabled=True, autostart=True, review_required=False),
        loops=LoopSwitches(mind=True, utility=True, dream=True),
        models=models or ModelBinding(),
        connection=connection or ConnectionBinding(),
        voice=VoiceBinding(tts_backend="piper", voice_id="her-voice"),
    )


def _house(tmp_path, *, hers: bool = True) -> tuple[Config, CharacterRegistry,
                                                    ConnectionProfiles]:
    """Two characters: one inheriting the house model, one on her own."""
    root = tmp_path / "data"
    registry = CharacterRegistry(root)
    registry.add(_record("adia", root))
    registry.add(_record(
        "yuri", root,
        models=(ModelBinding(chat=HERS, utility=HERS,
                             options={"chat_thinking": False})
                if hers else ModelBinding()),
        connection=ConnectionBinding(
            profile="default",
            endpoint="http://localhost:1234/v1" if hers else None)))
    profiles = ConnectionProfiles(root)
    profiles.upsert(ConnectionProfile(name="default", endpoint="http://localhost:1234/v1"))
    return _house_config(data_dir=root), registry, profiles


def test_describe_separates_the_house_model_from_a_characters_own(tmp_path):
    cfg, registry, profiles = _house(tmp_path)

    rows = {row.id: row for row in overrides.describe(cfg, registry.list(), profiles)}

    assert rows["adia"].chat_model == HOUSE
    assert rows["adia"].overrides == ()
    assert not rows["adia"].differs
    # The house profile names LM Studio, but her models are loaded in-process,
    # so there is no server for that url to be the url of.
    assert rows["adia"].endpoint == ""

    assert rows["yuri"].chat_model == HERS
    assert rows["yuri"].utility_model == HERS
    assert rows["yuri"].endpoint == "http://localhost:1234/v1"
    assert rows["yuri"].differs
    held = {item.key: item for item in rows["yuri"].overrides}
    assert held["chat_model"].value == HERS and held["chat_model"].house == HOUSE
    assert held["chat_model"].differs
    assert held["chat_thinking"].value is False
    assert held["endpoint"].value == "http://localhost:1234/v1"


def test_describe_reports_an_inheriting_house_without_overrides(tmp_path):
    cfg, registry, profiles = _house(tmp_path, hers=False)

    rows = overrides.describe(cfg, registry.list(), profiles)

    assert [row.chat_model for row in rows] == [HOUSE, HOUSE]
    assert not any(row.differs for row in rows)


def test_clearing_takes_the_model_settings_and_nothing_else(tmp_path):
    cfg, registry, profiles = _house(tmp_path)

    cleared = overrides.clear(registry, ["yuri"])

    assert set(cleared["yuri"]) == {"chat_model", "utility_model", "endpoint",
                                    "chat_thinking"}
    saved = CharacterRegistry(registry.data_root).require("yuri")
    assert saved.models.chat == "" and saved.models.utility == ""
    assert saved.models.options == {}
    assert saved.connection.endpoint is None
    # her voice, her loops and her profile are not her connection
    assert saved.voice.tts_backend == "piper" and saved.voice.voice_id == "her-voice"
    assert saved.loops.mind and saved.lifecycle.autostart
    assert saved.connection.profile == "default"
    rows = {row.id: row for row in overrides.describe(cfg, CharacterRegistry(
        registry.data_root).list(), profiles)}
    assert rows["yuri"].chat_model == HOUSE and not rows["yuri"].differs


def test_endpoint_is_dropped_when_no_model_needs_a_server():
    field, url = overrides.resolve_endpoint(
        HOUSE, HOUSE, record_endpoint="http://localhost:1234/v1",
        profile_endpoint="", lmstudio_url="http://localhost:1234/v1",
        ollama_url="http://localhost:11434")

    assert (field, url) == (None, "")

    field, url = overrides.resolve_endpoint(
        HERS, HERS, record_endpoint="http://box:1234/v1", profile_endpoint="",
        lmstudio_url="http://localhost:1234/v1", ollama_url="http://localhost:11434")

    assert (field, url) == ("lmstudio_base_url", "http://box:1234/v1")


def test_start_names_the_character_that_connects_somewhere_else(tmp_path, monkeypatch,
                                                               capsys):
    cfg, registry, profiles = _house(tmp_path)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_configured_cfg", lambda root: cfg)
    monkeypatch.setattr(cli, "_start_search_instance", lambda root: None)

    class Process:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(cli, "_wait_for_ready", lambda cfg, *, proc=None: None)

    assert cli.command_start(Namespace(foreground=False)) == 0

    out = capsys.readouterr().out
    assert "Characters and the model each one connects with:" in out
    assert "Yuri [yuri]" in out and HERS in out
    assert "http://localhost:1234/v1" in out
    assert "Adia [adia]" in out and HOUSE in out
    assert "her own settings, not the house's:" in out
    assert "`yurios configure` can clear them" in out


def test_start_says_nothing_when_every_character_uses_the_house_model(
        tmp_path, monkeypatch, capsys):
    cfg, registry, profiles = _house(tmp_path, hers=False)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_configured_cfg", lambda root: cfg)
    monkeypatch.setattr(cli, "_start_search_instance", lambda root: None)
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {"pid": 1, "poll": lambda self: None})())
    monkeypatch.setattr(cli, "_wait_for_ready", lambda cfg, *, proc=None: None)

    assert cli.command_start(Namespace(foreground=False)) == 0

    out = capsys.readouterr().out
    assert "Characters and the model each one connects with:" in out
    assert "her own settings" not in out
    assert "can clear them" not in out


def _configure_args(**update) -> Namespace:
    return Namespace(**{"model": None, "provider": None, "base_url": None,
                        "api_key": None, "clear_character_models": False, **update})


def test_configure_offers_to_put_every_character_on_the_chosen_model(
        tmp_path, monkeypatch, capsys):
    cfg, registry, profiles = _house(tmp_path)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_configured_cfg", lambda root: cfg)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "gguf_connection_defaults", lambda: {"GGUF_CONTEXT_LENGTH": "32768", "GGUF_N_GPU_LAYERS": "20",
                             "GGUF_FLASH_ATTN": "true"})
    monkeypatch.setattr(cli, "download_gguf", lambda cfg, model: tmp_path / "model.gguf")
    answers = []
    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "y")

    assert cli.command_configure(_configure_args(model=HOUSE)) == 0

    out = capsys.readouterr().out
    assert any("Clear these character settings" in prompt for prompt in answers)
    assert "Cleared" in out and "yuri" in out
    saved = CharacterRegistry(registry.data_root).require("yuri")
    assert saved.models.chat == "" and saved.connection.endpoint is None


def test_configure_leaves_the_character_alone_when_the_answer_is_no(
        tmp_path, monkeypatch, capsys):
    cfg, registry, profiles = _house(tmp_path)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_configured_cfg", lambda root: cfg)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "gguf_connection_defaults", lambda: {"GGUF_CONTEXT_LENGTH": "32768", "GGUF_N_GPU_LAYERS": "20",
                             "GGUF_FLASH_ATTN": "true"})
    monkeypatch.setattr(cli, "download_gguf", lambda cfg, model: tmp_path / "model.gguf")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert cli.command_configure(_configure_args(model=HOUSE)) == 0

    assert "Left the character settings alone." in capsys.readouterr().out
    assert CharacterRegistry(registry.data_root).require("yuri").models.chat == HERS


def test_configure_clear_flag_needs_no_terminal_and_no_new_model(
        tmp_path, monkeypatch, capsys):
    cfg, registry, profiles = _house(tmp_path)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_configured_cfg", lambda root: cfg)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.command_configure(_configure_args(clear_character_models=True)) == 0

    out = capsys.readouterr().out
    assert "Cleared" in out and "Restart YuriOS" in out
    assert CharacterRegistry(registry.data_root).require("yuri").models.chat == ""
    # …and the .env it was not asked to touch is still untouched
    assert not (tmp_path / ".env").exists()


def test_configure_without_a_terminal_says_how_to_clear_them(tmp_path, monkeypatch,
                                                             capsys):
    cfg, registry, profiles = _house(tmp_path)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_configured_cfg", lambda root: cfg)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "gguf_connection_defaults", lambda: {"GGUF_CONTEXT_LENGTH": "32768", "GGUF_N_GPU_LAYERS": "20",
                             "GGUF_FLASH_ATTN": "true"})
    monkeypatch.setattr(cli, "download_gguf", lambda cfg, model: tmp_path / "model.gguf")

    assert cli.command_configure(_configure_args(model=HOUSE)) == 0

    out = capsys.readouterr().out
    assert "yurios configure --clear-character-models" in out
    assert CharacterRegistry(registry.data_root).require("yuri").models.chat == HERS
