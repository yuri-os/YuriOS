"""The `.env` table and the two surfaces over it (SPEC §11).

`yurios/envfile.py` is the single list of knobs the settings panel renders and
`yurios settings` prints. What these tests hold in place is the property that
makes it worth having: the list is not hand-maintained. Every field of the
running `Config` is in it, typed from its annotation and described by walking
`.env.example`, so a knob added to the config and documented in the example file
turns up on both surfaces with nobody editing a schema.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from yurios import envfile
from yurios.cli import main as cli_main
from yurios.world.config import Config


def test_every_config_knob_is_in_the_table_and_typed_from_its_annotation():
    cfg = Config(_env_file=None)
    table = envfile.fields_by_key(cfg)

    declared = {name.upper() for name in Config.model_fields
                if name not in envfile.HIDDEN}
    assert declared <= set(table), sorted(declared - set(table))[:5]

    assert table["USER_NAME"]["type"] == "text"
    assert any(g["group"] == "You" and any(f["key"] == "USER_NAME" for f in g["fields"])
               for g in envfile.groups_for(cfg))
    assert table["MIND_ENABLED"]["type"] == "bool"
    assert table["MIND_SEED"]["type"] == "number"
    assert table["MIND_ACT_THRESHOLD"]["step"] == "any"        # a float, not an int
    assert table["SEARXNG_URL"]["type"] == "text"
    assert table["VAULT_DIR"]["type"] == "text"                # a Path is a path string
    # anything named like a credential is write-only wherever it came from
    assert table["OPENROUTER_API_KEY"]["type"] == "password"
    assert table["TELEGRAM_BOT_TOKEN"]["type"] == "password"


def test_the_allowlist_is_a_vocabulary_rather_than_a_text_box():
    """MIND_TOOL_ALLOWLIST is a `str` in the config, so the derivation would
    type it as free text — and a text box is the one control you cannot fill in
    here, because the names it wants exist nowhere but in the source."""
    from yurios.mind.hands import HANDS

    cfg = Config(_env_file=None, search_backend="off")
    field = envfile.fields_by_key(cfg)["MIND_TOOL_ALLOWLIST"]

    assert field["type"] == "multi"
    assert field["options"] == list(HANDS)
    assert field["help"], "the example file gives this one no trailing comment"
    assert field["option_detail"]["write_note"] == {
        "group": "cheap", "help": HANDS["write_note"].does}
    # the class is a heading over its run of names, not a word on every row
    assert set(field["option_groups"]) == {"cheap", "expensive"}
    # the reason a ticked hand might still never fire, said where it is ticked
    assert field["option_detail"]["research"]["note"] == \
        "needs SEARCH_BACKEND, which is off"
    assert "note" not in field["option_detail"]["write_note"]


def test_a_name_that_is_not_a_hand_is_refused_at_the_form():
    cfg = Config(_env_file=None)
    field = envfile.fields_by_key(cfg)["MIND_TOOL_ALLOWLIST"]

    assert envfile.format_value(field, " read_note ,write_note, read_note") == \
        "read_note,write_note"
    assert envfile.format_value(field, ["read_note", "web_search"]) == \
        "read_note,web_search"
    assert envfile.format_value(field, "") == ""
    with pytest.raises(ValueError) as refused:
        envfile.format_value(field, "write_note,write_notes")
    assert "write_notes" in str(refused.value)
    assert "read_skill" in str(refused.value), "and it says what the names are"


def test_the_knobs_the_host_writes_are_not_offered():
    cfg = Config(_env_file=None)
    table = envfile.fields_by_key(cfg)

    for key in ("CHARACTER_ID", "TELEGRAM_BOT_TOKEN_ENV", "CONNECTION_API_KEY"):
        assert key not in table


def test_groups_and_help_come_from_the_example_file(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text(
        "# --- the mind: the tick loop ---------\n"
        "MIND_ENABLED=true                 # off = no ambient life\n"
        "MIND_SEED=0\n", encoding="utf-8")

    index = envfile.example_index(example)
    assert index["MIND_ENABLED"] == ("the mind: the tick loop", "off = no ambient life")
    assert index["MIND_SEED"] == ("the mind: the tick loop", "")

    # …and the shipped file really does describe most of them, which is what
    # keeps the derived half from being a wall of unlabelled keys
    shipped = envfile.example_index()
    described = [key for key, (_, help_text) in shipped.items() if help_text]
    assert len(described) > 60


def test_the_curated_groups_come_first_and_are_not_repeated_below():
    groups = envfile.groups_for(Config(_env_file=None))
    names = [group["group"] for group in groups]
    assert names[:5] == ["You", "Brain", "Embeddings", "Storage", "Server"]
    assert names[-1] == "Everything else"

    seen: set[str] = set()
    for group in groups:
        for field in group["fields"]:
            assert field["key"] not in seen, f"{field['key']} listed twice"
            seen.add(field["key"])


def test_voice_fields_are_grouped_and_relevant_to_the_selected_backend():
    groups = envfile.groups_for(Config(_env_file=None))
    voice = next(group for group in groups if group["group"] == "Text-to-speech")
    fields = {field["key"]: field for field in voice["fields"]}

    assert {"QWEN_MODEL", "QWEN_MODE", "QWEN_INSTRUCT", "QWEN_ATTN"} <= fields.keys()
    assert fields["QWEN_MODE"]["relevant_if"] == {
        "TTS_BACKEND": ["qwen3_tts"]}
    assert fields["QWEN_REF_AUDIO"]["relevant_if"]["QWEN_MODE"] == ["clone"]
    assert fields["QWEN_INSTRUCT"]["relevant_if"]["QWEN_MODE"] == ["design"]
    assert fields["TTS_REGISTER"]["relevant_if"] == {
        "TTS_BACKEND": ["kokoro"]}
    assert all(not group["advanced"] for group in groups[:len(envfile.CURATED)])
    assert all(group["advanced"] for group in groups[len(envfile.CURATED):])


def test_a_build_without_a_knob_never_shows_it():
    """The desktop app has no mind and no channels; the table is what THIS build
    has, not what some build might."""
    bare = SimpleNamespace(host="127.0.0.1", owner_token="", port=8768)
    keys = {f["key"] for g in envfile.groups_for(bare) for f in g["fields"]}
    assert keys == {"HOST", "PORT", "OWNER_TOKEN"}


def test_a_save_rewrites_only_what_changed_and_leaves_the_prose(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# the model she speaks with\nCHAT_MODEL=old\n"
                   "\n# not set yet\n# MIND_SEED=0\n", encoding="utf-8")
    cfg = Config(_env_file=None)

    written, ignored = envfile.apply(
        cfg, {"CHAT_MODEL": "lm_studio/new", "MIND_SEED": 7,
              "MIND_ENABLED": False, "NOT_A_KNOB": "x"}, path=env)

    assert sorted(written) == ["CHAT_MODEL", "MIND_ENABLED", "MIND_SEED"]
    assert ignored == ["NOT_A_KNOB"]
    text = env.read_text()
    assert "# the model she speaks with" in text        # the comment survived
    assert "CHAT_MODEL=lm_studio/new" in text
    assert "MIND_SEED=7" in text and "# MIND_SEED" not in text   # uncommented in place
    assert "MIND_ENABLED=false" in text                 # appended, spelled as .env spells it


def test_a_value_that_would_be_misread_is_quoted(tmp_path):
    env = tmp_path / ".env"
    cfg = Config(_env_file=None)
    envfile.apply(cfg, {"USER_NAME": "Sam # not a comment"}, path=env)
    assert 'USER_NAME="Sam # not a comment"' in env.read_text()


def test_a_save_that_would_stop_her_booting_is_refused(tmp_path):
    env = tmp_path / ".env"
    cfg = Config(_env_file=None, host="127.0.0.1", owner_token="")

    with pytest.raises(ValueError, match="at least 32 characters"):
        envfile.apply(cfg, {"OWNER_TOKEN": "short"}, path=env)
    with pytest.raises(ValueError, match="not loopback"):
        envfile.apply(cfg, {"HOST": "0.0.0.0"}, path=env)
    assert not env.exists()                     # nothing was written on the way

    # …and the pair together is fine, because the result boots
    written, _ = envfile.apply(
        cfg, {"HOST": "0.0.0.0", "OWNER_TOKEN": "k" * 43}, path=env)
    assert sorted(written) == ["HOST", "OWNER_TOKEN"]


# --- the terminal surface ----------------------------------------------------

@pytest.fixture
def stay_put():
    """`cli.main` enters the installation before dispatching (it has to: a bare
    `Config()` reads the `.env` beside the working directory). Put the suite
    back where it was afterwards, or every later test that opens a repo file by
    a relative path is reading somebody's temporary directory."""
    origin = Path.cwd()
    yield
    os.chdir(origin)


def _installation(tmp_path, monkeypatch, body: str = "CHAT_MODEL=old\n"):
    monkeypatch.setenv("YURIOS_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text(body, encoding="utf-8")
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / ".env")
    return tmp_path / ".env"


def test_yurios_settings_lists_the_common_and_the_changed(tmp_path, monkeypatch, capsys, stay_put):
    _installation(tmp_path, monkeypatch, "CHAT_MODEL=old\nMIND_SEED=11\n")

    assert cli_main(["settings"]) == 0
    out = capsys.readouterr().out
    assert "CHAT_MODEL          old" in out
    assert "MIND_SEED" in out                    # not curated, but changed here
    assert "MIND_DREAM_END_HOUR" not in out      # untouched default, so not listed
    assert cli_main(["settings", "--all"]) == 0
    assert "MIND_DREAM_END_HOUR" in capsys.readouterr().out


def test_yurios_settings_reads_and_writes_one_knob(tmp_path, monkeypatch, capsys, stay_put):
    env = _installation(tmp_path, monkeypatch)

    assert cli_main(["settings", "CHAT_MODEL"]) == 0
    assert capsys.readouterr().out.startswith("CHAT_MODEL=old")

    assert cli_main(["settings", "MIND_ENABLED=false", "PORT=9000"]) == 0
    assert "Restart before she reads it" in capsys.readouterr().out
    text = env.read_text()
    assert "MIND_ENABLED=false" in text and "PORT=9000" in text

    assert cli_main(["settings", "--unset", "CHAT_MODEL"]) == 0
    capsys.readouterr()
    assert "CHAT_MODEL=\n" in env.read_text()


def test_yurios_settings_never_prints_a_secret_and_never_blanks_one(
        tmp_path, monkeypatch, capsys, stay_put):
    env = _installation(tmp_path, monkeypatch, "OPENROUTER_API_KEY=sk-real-secret\n")

    assert cli_main(["settings", "OPENROUTER_API_KEY"]) == 0
    out = capsys.readouterr().out
    assert "sk-real-secret" not in out and "configured" in out

    assert cli_main(["settings", "OPENROUTER_API_KEY="]) == 1
    assert "--unset" in capsys.readouterr().err
    assert "OPENROUTER_API_KEY=sk-real-secret" in env.read_text()


def test_yurios_settings_shows_a_named_group_whole(tmp_path, monkeypatch, capsys, stay_put):
    """Naming a group is asking to see it. The listing hides untouched defaults
    to keep the terminal short — but a section where every knob is still default
    is exactly the section you have come here to configure."""
    _installation(tmp_path, monkeypatch)

    assert cli_main(["settings", "--group", "unasked"]) == 0
    out = capsys.readouterr().out
    assert "MIND_TOOLS_ENABLED" in out and "MIND_TOOL_ALLOWLIST" in out
    assert "the house switch" in out, "with its help, since you asked for it"


def test_yurios_settings_prints_the_vocabulary_of_a_closed_knob(
        tmp_path, monkeypatch, capsys, stay_put):
    """Reading MIND_TOOL_ALLOWLIST and being told only that it is empty is the
    whole of the problem: the value is a list of names nobody has been shown."""
    env = _installation(tmp_path, monkeypatch)

    assert cli_main(["settings", "MIND_TOOL_ALLOWLIST"]) == 0
    out = capsys.readouterr().out
    assert "write_note" in out and "take_selfie" in out
    assert "read several pages on a topic" in out, "what a name means, beside it"
    assert "cheap" in out and "expensive" in out, "and what ticking it commits to"

    assert cli_main(["settings", "MIND_TOOL_ALLOWLIST=write_note,read_note"]) == 0
    capsys.readouterr()
    assert "MIND_TOOL_ALLOWLIST=write_note,read_note" in env.read_text()

    assert cli_main(["settings", "MIND_TOOL_ALLOWLIST=write_notes"]) == 1
    assert "no such name" in capsys.readouterr().err
    assert "write_notes" not in env.read_text(), "and nothing was written"


def test_yurios_settings_refuses_a_knob_this_build_has_never_heard_of(
        tmp_path, monkeypatch, capsys, stay_put):
    env = _installation(tmp_path, monkeypatch)

    assert cli_main(["settings", "MADE_UP_KNOB=1"]) == 1
    assert "no such setting" in capsys.readouterr().err
    assert "MADE_UP_KNOB" not in env.read_text()
