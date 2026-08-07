"""N servers behind one runner (SPEC §7.2) — the seam that lets her mount
somebody else's MCP server without the brain noticing.

The rules under test are both about failing in the direction of "she keeps
talking": a server that won't start costs her that server and nothing else, and
a name collision can never shadow one of her own hands.
"""
from __future__ import annotations

import json

import pytest

from yurios.world.tools.client import MultiToolRunner, ToolSpec, load_servers
from yurios.world.tools.fakes import FakeToolRunner


class Extra(FakeToolRunner):
    """Somebody else's server: two tools, one of which collides with hers."""

    def __init__(self, names=("scrape", "read_page")):
        super().__init__()
        self.names = names

    async def start(self):
        self.started = True
        return [ToolSpec(n, f"{n} from elsewhere", {}) for n in self.names]

    async def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        return json.dumps({"server": "extra", "tool": tool})


class DeadOnArrival(FakeToolRunner):
    async def start(self):
        raise RuntimeError("No module named 'mcp.server.fastmcp'")


async def test_the_tools_of_every_server_arrive_as_one_flat_list():
    mine, theirs = FakeToolRunner(), Extra()
    specs = await MultiToolRunner([("yurios", mine), ("extra", theirs)]).start()
    names = [s.name for s in specs]
    assert "set_timer" in names and "scrape" in names
    assert names.count("read_page") == 1        # the collision resolved, not duped


async def test_a_call_is_routed_to_the_server_that_offers_it():
    mine, theirs = FakeToolRunner(), Extra()
    m = MultiToolRunner([("yurios", mine), ("extra", theirs)])
    await m.start()

    assert json.loads(await m.call("scrape", {}))["server"] == "extra"
    await m.call("set_timer", {"minutes": 1})

    assert [t for t, _a in theirs.calls] == ["scrape"]
    assert [t for t, _a in mine.calls] == ["set_timer"]


async def test_her_own_hands_cannot_be_shadowed_by_a_later_server():
    """First mount wins, and hers is mounted first — so a third-party server
    advertising `read_page` gets ignored rather than intercepting hers."""
    mine, theirs = FakeToolRunner(), Extra()
    m = MultiToolRunner([("yurios", mine), ("extra", theirs)])
    await m.start()

    await m.call("read_page", {"url": "https://a/"})
    assert [t for t, _a in mine.calls] == ["read_page"]
    assert theirs.calls == []
    assert m.server_of("read_page") == "yurios"


async def test_a_server_that_will_not_start_costs_only_itself():
    mine = FakeToolRunner()
    m = MultiToolRunner([("yurios", mine), ("broken", DeadOnArrival())])
    specs = await m.start()

    assert "set_timer" in [s.name for s in specs]     # she still has her hands
    assert [n for n, _c in m.started] == ["yurios"]
    assert "fastmcp" in m.failures["broken"]          # …and the reason is kept


async def test_every_server_failing_leaves_her_hand_less_not_crashed():
    m = MultiToolRunner([("a", DeadOnArrival()), ("b", DeadOnArrival())])
    assert await m.start() == []
    assert set(m.failures) == {"a", "b"}


async def test_calling_a_tool_nobody_offers_is_a_turn_error_not_a_crash():
    m = MultiToolRunner([("yurios", FakeToolRunner())])
    await m.start()
    with pytest.raises(RuntimeError, match="no server offers"):
        await m.call("scrape", {})


async def test_close_closes_the_servers_that_actually_started():
    mine, theirs = FakeToolRunner(), Extra()
    m = MultiToolRunner([("yurios", mine), ("extra", theirs),
                         ("broken", DeadOnArrival())])
    await m.start()
    await m.close()
    assert mine.closed and theirs.closed


# ------------------------------------------------------------ the config file

def test_a_missing_file_is_the_default_configuration_not_an_error(tmp_path):
    assert load_servers(tmp_path / "nope.json") == []


def test_the_familiar_mcpservers_shape_loads(tmp_path):
    path = tmp_path / "mcp-servers.json"
    path.write_text(json.dumps({"mcpServers": {
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"],
                  "env": {"TOKEN": "x"}, "rate": 9},
        "plain": {"command": "node"},
    }}))
    servers = load_servers(path)
    assert [s["name"] for s in servers] == ["fetch", "plain"]
    assert servers[0]["command"] == ["uvx", "mcp-server-fetch"]
    assert servers[0]["env"] == {"TOKEN": "x"} and servers[0]["rate"] == 9
    assert servers[1]["command"] == ["node"] and servers[1]["rate"] is None


def test_a_file_that_is_there_but_wrong_is_loud(tmp_path):
    """Silently running with no third-party hands after you wrote some down is
    the worse failure — Runtime catches this and warns."""
    path = tmp_path / "mcp-servers.json"
    path.write_text(json.dumps({"mcpServers": {"x": {"args": ["y"]}}}))
    with pytest.raises(ValueError, match="no command"):
        load_servers(path)

    path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        load_servers(path)
