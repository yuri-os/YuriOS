"""How long her replies run, and where what she knows about you is kept.

SPEC §38 — the reply-length ladder: a last-read note fused after the history,
a per-character override that moves live, and a sticky `reply_length` event the
rooms' shorter/longer buttons render. SPEC §2.1 block 5 — WHO YOU ARE TO HER
says it is kept for her, so a fact about the user is not filed by hand in a
desk note no prompt reads. SPEC §7.4 — a tool continuation is the same reply.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from yurios.app.config import REPLY_LENGTHS
from yurios.app.config import Config as AppConfig
from yurios.app.core import assemble as asm
from yurios.app.core.soul import Soul
from yurios.characters import CharacterRegistry
from yurios.characters.overrides import OVERRIDE_KEYS
from yurios.desktop.voice.backends.fakes import FakeBrain
from yurios.world import rewire
from yurios.world.brain import CONTINUATION_CUE
from yurios.world.config import Config
from yurios.world.host import create_host_app
from yurios.world.main import create_app
from yurios.world.tools.fakes import FakeToolRunner

from .conftest import ScriptedChat, collect, make_toolbrain
from .test_host import fake_character_app, record


def soul(**kw) -> Soul:
    base = dict(name="Yuri", voice_law="Speak softly.", backbone="She is Yuri.",
                personality="warm", scenario="A room.", card_version="test",
                return_greetings=[], hard_limits="", examples="")
    base.update(kw)
    return Soul(**base)


def final_user(**kw) -> str:
    out = asm.assemble(soul(), user_md=kw.pop("user_md", ""), summary="",
                       memories=[], lore=[], window=[], user_msg="hey",
                       user_name="Grant", **kw)
    return out.messages[-1]["content"]


# --- the note (§38.2) ---------------------------------------------------------

def test_every_rung_has_a_note_and_the_ladder_runs_shortest_first():
    assert tuple(asm.REPLY_LENGTH_NOTES) == REPLY_LENGTHS
    assert REPLY_LENGTHS.index("natural") == 2      # the default sits mid-ladder


def test_the_default_note_is_fused_after_the_history_and_says_no_walls():
    last = final_user()
    assert last.startswith("hey\n\n[system note — reply length, read last:")
    assert "Never a wall of text" in last
    assert "{{user}}" not in last and "Grant just said" in last


def test_the_rung_chooses_the_note():
    assert "one or two sentences" in final_user(reply_length="terse")
    assert "several paragraphs" in final_user(reply_length="expansive")


def test_an_unknown_rung_is_the_natural_one():
    assert final_user(reply_length="enormous") == final_user(reply_length="natural")


def test_the_hard_limits_stay_the_last_word():
    out = asm.assemble(soul(hard_limits="- She stays herself."), user_md="",
                       summary="", memories=[], lore=[], window=[],
                       user_msg="hey", user_name="Grant", reply_length="brief")
    last = out.messages[-1]["content"]
    assert last.index("reply length") < last.index("hard limits")
    assert last.rstrip().endswith("]") and "She stays herself" in last.rsplit("[", 1)[1]


def test_the_note_never_reaches_the_system_block():
    out = asm.assemble(soul(), user_md="", summary="", memories=[], lore=[],
                       window=[], user_msg="hey", user_name="Grant")
    assert "reply length, read last" not in out.system


# --- WHO YOU ARE TO HER says it is kept (§2.1, block 5) -----------------------

USER_MD = "## What helps, and what doesn't\n\n- Hates being asked if they are okay.\n"


def test_the_user_block_says_it_is_kept_for_her():
    out = asm.assemble(soul(), user_md=USER_MD, summary="", memories=[], lore=[],
                       window=[], user_msg="hey", user_name="Grant")
    block = out.system.split("## WHO YOU ARE TO HER", 1)[1]
    assert block.strip().startswith("What you know about Grant. It is kept for you")
    assert "You don't need to write a note" in block
    assert "Hates being asked" in block


def test_the_lead_is_there_before_anything_is_known():
    out = asm.assemble(soul(), user_md="", summary="", memories=[], lore=[],
                       window=[], user_msg="hey", user_name="Grant")
    block = out.system.split("## WHO YOU ARE TO HER", 1)[1]
    assert "It is kept for you" in block and "(nothing yet)" in block


def test_the_minds_preamble_carries_the_same_lead():
    text = asm.soul_preamble(soul(), user_md=USER_MD, user_name="Grant")
    assert "## WHO YOU ARE TO HER\n\nWhat you know about Grant. It is kept for you" in text


# --- the setting (§38.1) -------------------------------------------------------

def test_a_typo_in_env_is_the_default_not_a_dead_boot(monkeypatch):
    monkeypatch.setenv("REPLY_LENGTH", "Shortish")
    assert AppConfig(_env_file=None).reply_length == "natural"
    monkeypatch.setenv("REPLY_LENGTH", " Brief ")
    assert AppConfig(_env_file=None).reply_length == "brief"


def test_the_api_refuses_a_rung_the_ladder_does_not_have(cfg):
    assert rewire.coerce(cfg, "reply_length", "FULL") == "full"
    with pytest.raises(ValueError, match="terse, brief, natural, full, expansive"):
        rewire.coerce(cfg, "reply_length", "enormous")


def test_it_is_a_live_override_like_the_other_per_call_knobs(cfg):
    assert ("reply_length", "options") in OVERRIDE_KEYS
    assert "reply_length" in rewire.BRAIN_FIELDS
    spec = next(s for s in rewire.OVERRIDE_SCHEMA if s["key"] == "reply_length")
    assert spec["options"] == ["", *REPLY_LENGTHS]       # blank = inherit
    applied = rewire.apply(object(), cfg,
                           rewire.differences(cfg, {"reply_length": "terse"}))
    assert applied == ["reply_length"] and cfg.reply_length == "terse"


def test_the_room_nudge_saves_her_own_override(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    base = Config(data_dir=tmp_path, _env_file=None)

    with TestClient(create_host_app(base, registry)) as client:
        rt = client.app.state.host.runtime("yuri")
        saved = client.patch("/api/characters/yuri/brain",
                             json={"reply_length": "brief"})
        refused = client.patch("/api/characters/yuri/brain",
                               json={"reply_length": "enormous"})

    assert saved.status_code == 200 and saved.json()["applied"] == ["reply_length"]
    assert rt.cfg.reply_length == "brief"                   # live, no restart
    assert registry.require("yuri").models.options["reply_length"] == "brief"
    assert refused.status_code == 400
    assert registry.require("yuri").models.options["reply_length"] == "brief"


# --- the event (§38.3) ---------------------------------------------------------

def events_of(app) -> list[dict]:
    with TestClient(app) as client:
        rt = app.state.rt
        rt.loop.call_soon_threadsafe(rt.stopping.set)
        return [json.loads(line[len("data: "):])
                for line in client.get("/api/events").text.splitlines()
                if line.startswith("data: ")]


def test_the_rung_rides_the_bus_as_sticky_state(cfg):
    app = create_app(cfg.model_copy(update={"tools_backend": "off",
                                            "mind_enabled": False,
                                            "reply_length": "full"}),
                     brain=FakeBrain())
    sent = [e for e in events_of(app) if e["type"] == "reply_length"]
    assert sent == [{"type": "reply_length", "level": "full",
                     "levels": list(REPLY_LENGTHS)}]


async def test_a_retune_tells_every_open_room(cfg):
    app = create_app(cfg.model_copy(update={"tools_backend": "off",
                                            "mind_enabled": False,
                                            "chat_model": "ollama/first"}),
                     brain=FakeBrain())
    rt = app.state.rt
    published: list[tuple] = []
    real = rt.hub.publish
    rt.hub.publish = lambda kind, data, **kw: (published.append((kind, data)),
                                               real(kind, data, **kw))[1]
    await rt.retune({"reply_length": "terse"})
    await rt.retune({"temperature": 0.2})                  # not a length change
    lengths = [data for kind, data in published if kind == "reply_length"]
    assert lengths == [{"level": "terse", "levels": list(REPLY_LENGTHS)}]


# --- a tool continuation is the same reply (§7.4) ------------------------------

async def test_the_continuation_says_it_is_one_reply(cfg, guard, timers, controller):
    chat = ScriptedChat([["one sec [[set_timer {\"minutes\": 1}]]"], ["done."]])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    await collect(tb._stream_with_tools([], []))
    cue = chat.calls[1][-1]["content"]
    assert CONTINUATION_CUE in cue
    assert "not a new one" in cue and "re-greet" in cue


# --- the desk says it is not where the user is remembered (§34.2) -------------

async def test_the_note_hands_say_facts_about_the_user_are_kept_elsewhere(tmp_path):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from yurios.mind.workspace import SkillStore, Workspace
    from yurios.world.tools.server import build_server

    srv = build_server(max_minutes=180, workspace=Workspace(tmp_path / "workspace"),
                       skills=SkillStore(tmp_path / "skills"))
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        tools = {t.name: t.description or "" for t in (await s.list_tools()).tools}
    assert "WHO YOU ARE TO HER" in tools["write_note"]
    assert "Not for facts about the user" in tools["append_note"]
