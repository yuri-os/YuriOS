"""The real MCP server's contract (SPEC §7.1, §13) — over an in-memory session.

No subprocess: `create_connected_server_and_client_session` wires the same
`FastMCP` object `python -m yurios.world.tools.server` runs, through a genuine MCP
client session. What list_tools/call_tool return here is exactly what the
spawned stdio server returns in production.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from yurios.world.tools.client import result_text  # noqa: E402
from yurios.world.tools.server import build_server  # noqa: E402
from yurios.world.tools.weather import FakeWeather  # noqa: E402


def server():
    return build_server(weather=FakeWeather(), max_minutes=180,
                        default_city="Tokyo")


async def test_list_tools_is_exactly_the_four_hands():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        listed = await s.list_tools()
        assert sorted(t.name for t in listed.tools) == [
            "get_weather", "play_music", "set_timer", "take_selfie"]
        timer = next(t for t in listed.tools if t.name == "set_timer")
        assert "minutes" in timer.inputSchema["properties"]
        assert "minutes" in timer.inputSchema.get("required", [])


async def test_description_carries_the_overlay_and_its_hint(tmp_path, monkeypatch):
    """The tools server reads the SAME merged book the host renders from
    (SELFIE_TEMPLATES_EXTRA): an overlay's tiers appear in the description,
    and its `tool_hint` line is carried verbatim — an overlay's register is
    explained in its own words, never implied by the shipped file."""
    import yaml
    overlay = tmp_path / "extra.yaml"
    overlay.write_text(yaml.safe_dump({
        "tool_hint": "hint: name the tier that matches the ask.",
        "wardrobe": {"midnight": "WARDROBE-midnight"}}))
    monkeypatch.setenv("SELFIE_TEMPLATES_EXTRA", str(overlay))
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        selfie = next(t for t in (await s.list_tools()).tools
                      if t.name == "take_selfie")
        assert "midnight" in selfie.description
        assert "hint: name the tier that matches the ask." in selfie.description


async def test_selfies_off_is_not_advertised():
    """SELFIE_BACKEND=off: the tool doesn't exist — no hand, not a dead one (§7.6)."""
    srv = build_server(weather=FakeWeather(), selfies=False)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        listed = await s.list_tools()
        assert sorted(t.name for t in listed.tools) == [
            "get_weather", "play_music", "set_timer"]


async def test_take_selfie_contract_and_freeform_passthrough():
    """The server is the contract point only (§7.5/§7.6): it carries the ask —
    named template key or free-form — and answers `started`. Pixels happen on
    the host; refusal happens nowhere (the engine takes no enforcement
    posture — what renders is the backend's call, never the contract's)."""
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("take_selfie", {"scene": "window", "mood": "happy"})
        assert not r.isError
        data = json.loads(result_text(r))
        assert data["status"] == "started" and data["id"]
        assert data["scene"] == "window" and data["mood"] == "happy"

        r = await s.call_tool("take_selfie", {})       # empty = her choice
        data = json.loads(result_text(r))
        assert data["status"] == "started"
        assert data["scene"] is None and data["mood"] is None
        assert data["wardrobe"] is None                # everyday default, host-side

        # every tier in the shipped library is nameable, and the contract
        # carries the ask through
        r = await s.call_tool("take_selfie", {"wardrobe": "cozy"})
        assert not r.isError
        assert json.loads(result_text(r))["wardrobe"] == "cozy"

        # off-menu asks are NOT refused: free-form text passes through verbatim
        # (forge/templates.py — the library is a starting point, not a limit)
        r = await s.call_tool("take_selfie", {"scene": "on the moon",
                                              "mood": "mid-laugh, head thrown back",
                                              "wardrobe": "borrowed shirt, nothing else"})
        assert not r.isError
        data = json.loads(result_text(r))
        assert data["scene"] == "on the moon"
        assert data["mood"] == "mid-laugh, head thrown back"
        assert data["wardrobe"] == "borrowed shirt, nothing else"

        # the description the model reads is BUILT from the library (§7.6) and
        # names the free-form pass-through, so she knows the menu isn't a wall
        listed = await s.list_tools()
        selfie = next(t for t in listed.tools if t.name == "take_selfie")
        assert "window" in selfie.description and "happy" in selfie.description
        assert "cozy" in selfie.description and "dressy" in selfie.description
        assert "in your own words" in selfie.description
        # …and it leads with `look` — the field she describes a whole picture
        # in — because a menu of five slots is what made every selfie the same
        assert "`look`" in selfie.description
        assert set(selfie.inputSchema["properties"]) == {
            "look", "scene", "mood", "wardrobe", "framing", "lighting", "avoid"}


async def test_set_timer_returns_the_contract():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("set_timer", {"minutes": 10, "label": "tea"})
        assert not r.isError
        data = json.loads(result_text(r))
        assert data["seconds"] == 600 and data["label"] == "tea"
        assert data["id"] and data["due"] > 0


async def test_set_timer_default_label_and_bounds():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("set_timer", {"minutes": 1})
        assert json.loads(result_text(r))["label"] == "your timer"
        for bad in (0, -5, 999999):
            r = await s.call_tool("set_timer", {"minutes": bad})
            assert r.isError                        # bounds enforced server-side


async def test_play_music_validates_action_track_volume():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("play_music", {"action": "play", "track": "night_piano"})
        data = json.loads(result_text(r))
        assert data["playing"] is True and data["track"] == "night_piano"

        r = await s.call_tool("play_music", {"action": "stop"})
        assert json.loads(result_text(r))["playing"] is False

        assert (await s.call_tool("play_music", {"action": "blast"})).isError
        assert (await s.call_tool("play_music",
                                  {"action": "play", "track": "dubstep"})).isError
        assert (await s.call_tool("play_music",
                                  {"action": "play", "volume": 3.0})).isError


async def test_get_weather_uses_the_default_city():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("get_weather", {})
        data = json.loads(result_text(r))
        assert data["city"] == "Tokyo" and data["condition"] == "raining"


def test_a_server_that_never_came_up_is_reported_by_its_leaf():
    """The other half of a spawn failure: what the boot log gets to say about it.

    A server that dies at import — the shape of an SDK that renamed the module out
    from under it — surfaces through two nested anyio task groups, and the group's own
    message is "unhandled errors in a TaskGroup (1 sub-exception)": a real exception
    carrying no name of the real failure. Peel to the leaves, keep all of them."""
    from yurios.world.tools.client import start_failure

    dead = ExceptionGroup("unhandled errors in a TaskGroup",
                          [ExceptionGroup("unhandled errors in a TaskGroup",
                                          [RuntimeError("Connection closed")])])
    assert start_failure(dead) == "RuntimeError: Connection closed"
    # a plain exception is already its own leaf, and still gets its type named
    assert start_failure(FileNotFoundError("no python")) == "FileNotFoundError: no python"
    assert start_failure(ExceptionGroup("boom", [ValueError("a"), TypeError("b")])) == (
        "ValueError: a; TypeError: b")
