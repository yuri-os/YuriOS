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


async def test_selfies_off_is_not_advertised():
    """SELFIE_BACKEND=off: the tool doesn't exist — no hand, not a dead one (§7.6)."""
    srv = build_server(weather=FakeWeather(), selfies=False)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        listed = await s.list_tools()
        assert sorted(t.name for t in listed.tools) == [
            "get_weather", "play_music", "set_timer"]


async def test_take_selfie_contract_and_validation():
    """The server is the contract point only (§7.5/§7.6): it validates against
    the template library and answers `started` — pixels happen on the host."""
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

        # wardrobe is a tier, not a gate (templates/selfie.yaml): every tier in
        # the library is nameable, and the contract carries the ask through
        r = await s.call_tool("take_selfie", {"wardrobe": "intimate"})
        assert not r.isError
        assert json.loads(result_text(r))["wardrobe"] == "intimate"

        assert (await s.call_tool("take_selfie", {"scene": "moon"})).isError
        assert (await s.call_tool("take_selfie", {"mood": "furious"})).isError
        assert (await s.call_tool("take_selfie", {"wardrobe": "armor"})).isError

        # the description the model reads is BUILT from the library (§7.6)
        listed = await s.list_tools()
        selfie = next(t for t in listed.tools if t.name == "take_selfie")
        assert "window" in selfie.description and "happy" in selfie.description
        assert "cozy" in selfie.description and "intimate" in selfie.description


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
