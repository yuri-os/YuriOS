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

from yurios.world.tools.client import (  # noqa: E402
    ToolSpec, build_directive, coerce_args, one_line, result_text)
from yurios.world.tools.fetch import FakeFetcher  # noqa: E402
from yurios.world.tools.search import FakeSearch  # noqa: E402
from yurios.world.tools.server import build_server  # noqa: E402


def server(**kw):
    return build_server(max_minutes=180, **kw)


def web_server():
    """…with the web hands on (SEARCH_BACKEND=fake, §7.7)."""
    return server(search=FakeSearch(), fetcher=FakeFetcher(), max_pages=5)


async def test_list_tools_is_exactly_the_hands_she_has():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        listed = await s.list_tools()
        assert sorted(t.name for t in listed.tools) == [
            "cancel_timer", "set_timer", "show_picture", "take_selfie"]
        timer = next(t for t in listed.tools if t.name == "set_timer")
        assert "minutes" in timer.inputSchema["properties"]
        assert "minutes" in timer.inputSchema.get("required", [])
        assert "180" in (timer.description or "")
        assert {"goal_id", "completes_goal"} <= set(timer.inputSchema["properties"])


async def test_the_web_hands_appear_only_when_search_is_configured():
    """SEARCH_BACKEND=off is the SELFIE_BACKEND=off rule (§7.7): no hand, not a
    dead one. The three go together — searching with no way to read what you
    found is half a capability."""
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        names = sorted(t.name for t in (await s.list_tools()).tools)
    assert names == ["cancel_timer", "read_page", "research", "set_timer",
                     "show_picture", "take_selfie", "web_search"]


async def test_web_search_returns_rows_a_model_can_speak_to():
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        out = json.loads(result_text(await s.call_tool("web_search",
                                                       {"query": "tea", "k": 2})))
    assert out["query"] == "tea" and len(out["results"]) == 2
    assert set(out["results"][0]) == {"title", "url", "snippet"}


async def test_read_page_carries_the_whole_page_and_a_gist_of_it():
    """The two-audience contract (§7.7): `gist` is what she says, `text` is what
    the host shelves — world/brain.py truncates only the former's copy."""
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        out = json.loads(result_text(
            await s.call_tool("read_page", {"url": "https://a.example/x"})))
    assert out["status"] == "read"
    assert out["chars"] == len(out["text"])
    assert out["gist"] and len(out["gist"]) <= len(out["text"])


async def test_research_answers_started_without_doing_any_of_it():
    """start-don't-await (§7.6): a search plus fetches plus embeddings will not
    fit inside TOOL_TIMEOUT_S, so the server promises and the host delivers."""
    fetcher = FakeFetcher()
    srv = server(search=FakeSearch(), fetcher=fetcher, max_pages=4)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        out = json.loads(result_text(
            await s.call_tool("research", {"topic": "tea", "depth": 99})))
    assert out["status"] == "started" and out["kind"] == "research"
    assert out["depth"] == 4                    # clamped to RESEARCH_MAX_PAGES
    assert fetcher.fetched == []                # nothing was read on the turn


async def test_research_defaults_to_the_configured_page_maximum():
    srv = server(search=FakeSearch(), fetcher=FakeFetcher(), max_pages=4)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        tools = await s.list_tools()
        research = next(tool for tool in tools.tools if tool.name == "research")
        out = json.loads(result_text(await s.call_tool("research", {"topic": "tea"})))
    assert out["depth"] == 4
    assert "1 to 4" in research.description
    assert "configured maximum (4)" in research.description


async def test_research_refuses_an_empty_topic():
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        result = await s.call_tool("research", {"topic": "  "})
    assert result.isError
    assert "topic" in result_text(result)


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


async def test_description_is_built_from_her_own_library_when_she_has_one(tmp_path,
                                                                          monkeypatch):
    """A character with her own book (SELFIE_TEMPLATES) replaces the shipped
    library outright, and the tool description has to follow: offering her our
    scenes would be describing a room she does not live in."""
    import yaml
    hers = tmp_path / "selfie.yaml"
    hers.write_text(yaml.safe_dump({
        "scenes": {"lamp room": "SCENE-lamp"},
        "wardrobe": {"oilskin": "WARDROBE-oilskin"}}))
    monkeypatch.setenv("SELFIE_TEMPLATES", str(hers))
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        selfie = next(t for t in (await s.list_tools()).tools
                      if t.name == "take_selfie")
        assert "lamp room" in selfie.description and "oilskin" in selfie.description
        assert "sanctuary" not in selfie.description   # ours are not hers


async def test_selfies_off_is_not_advertised():
    """SELFIE_BACKEND=off: the tool doesn't exist — no hand, not a dead one (§7.6)."""
    srv = build_server(selfies=False)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        listed = await s.list_tools()
        assert sorted(t.name for t in listed.tools) == ["cancel_timer", "set_timer"]


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
        assert data["wardrobe"] is None                # host-side, and only
        # when she described nothing either (world/selfies.py)

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
            "look", "scene", "mood", "wardrobe", "framing", "lighting", "avoid",
            "goal_id", "completes_goal"}
        assert "closes only after the render succeeds" in selfie.description

        r = await s.call_tool("take_selfie", {
            "look": "the promised frame", "goal_id": "g-promised",
            "completes_goal": True})
        data = json.loads(result_text(r))
        assert data["goal_id"] == "g-promised" and data["completes_goal"] is True


async def test_show_picture_is_the_camera_pointed_away_from_her():
    """The open-ended half of §7.6: no library, no slots, no rotation — the
    subject is whatever she writes, because no menu could anticipate what she
    might want to show you."""
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("show_picture", {
            "subject": "the street below, wet and empty, one streetlight on",
            "avoid": "people"})
        assert not r.isError
        data = json.loads(result_text(r))
        assert data["status"] == "started" and data["id"]
        assert data["kind"] == "picture"          # …so the host leaves her out
        assert data["subject"].startswith("the street below")
        assert data["avoid"] == "people"

        # a picture of nothing is the one ask the contract can't carry — every
        # other field on this tool is optional and nothing is chosen for her
        r = await s.call_tool("show_picture", {"subject": "   "})
        assert r.isError

        listed = await s.list_tools()
        pic = next(t for t in listed.tools if t.name == "show_picture")
        assert set(pic.inputSchema["properties"]) == {"subject", "avoid"}
        assert "subject" in pic.inputSchema.get("required", [])
        assert "ISN'T you" in pic.description     # the whole point, said plainly
        assert "take_selfie" in pic.description   # …and which hand is the other


async def test_show_picture_is_off_when_the_camera_is():
    """One camera, two hands: SELFIE_BACKEND=off takes both away rather than
    leaving her one that can't render (§7.6)."""
    srv = build_server(selfies=False)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        assert "show_picture" not in {t.name for t in (await s.list_tools()).tools}


async def test_set_timer_returns_the_contract():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("set_timer", {"minutes": 10, "label": "tea"})
        assert not r.isError
        data = json.loads(result_text(r))
        assert data["seconds"] == 600 and data["label"] == "tea"
        assert data["id"] and data["due"] > 0

        r = await s.call_tool("set_timer", {
            "minutes": 10, "label": "finish it", "goal_id": "g-timer",
            "completes_goal": True})
        data = json.loads(result_text(r))
        assert data["goal_id"] == "g-timer" and data["completes_goal"] is True


async def test_cancel_timer_only_says_which_one():
    """The board is the host's (§7.5): the server cannot see it, so its whole
    contract is the reference, whitespace-folded, for the host to resolve."""
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        tools = {tool.name: tool for tool in (await s.list_tools()).tools}
        assert set(tools["cancel_timer"].inputSchema["properties"]) == {"timer"}
        assert not tools["cancel_timer"].inputSchema.get("required")
        r = await s.call_tool("cancel_timer", {"timer": "  the   tea "})
        assert json.loads(result_text(r)) == {"status": "ready", "timer": "the tea"}
        r = await s.call_tool("cancel_timer", {})
        assert json.loads(result_text(r)) == {"status": "ready", "timer": ""}
        assert (await s.call_tool("cancel_timer", {"timer": "x" * 201})).isError


async def test_create_goal_validates_a_host_realisation_contract():
    srv = build_server(goals=True)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        tools = {tool.name: tool for tool in (await s.list_tools()).tools}
        goal = tools["create_goal"]
        assert set(goal.inputSchema["properties"]) == {"text", "kind"}
        assert goal.inputSchema["properties"]["kind"]["enum"] == ["task", "reach_out"]
        assert "write_note" in goal.description and "Goals view" in goal.description

        result = await s.call_tool("create_goal", {
            "text": "  make Grant a cup of coffee  ", "kind": "task"})
        assert json.loads(result_text(result)) == {
            "status": "ready", "text": "make Grant a cup of coffee",
            "kind": "task"}
        assert (await s.call_tool("create_goal", {"text": "   "})).isError


async def test_create_goal_is_absent_without_a_mind():
    srv = build_server(goals=False)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        names = {tool.name for tool in (await s.list_tools()).tools}
        assert "create_goal" not in names and "show_held_picture" not in names


async def test_show_held_picture_validates_a_host_realisation_contract():
    srv = build_server(goals=True)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        tools = {tool.name: tool for tool in (await s.list_tools()).tools}
        assert set(tools["show_held_picture"].inputSchema["properties"]) == {
            "goal_id", "picture_id"}
        result = await s.call_tool("show_held_picture", {"goal_id": " g-123 "})
        assert json.loads(result_text(result)) == {
            "status": "ready", "goal_id": "g-123", "picture_id": ""}
        result = await s.call_tool("show_held_picture", {"picture_id": "cb7ec091"})
        assert json.loads(result_text(result)) == {
            "status": "ready", "goal_id": "", "picture_id": "cb7ec091"}
        # exactly one: neither, blank, or both is a mistake she can read
        for bad in ({}, {"goal_id": "  "},
                    {"goal_id": "g-1", "picture_id": "cb7ec091"}):
            assert (await s.call_tool("show_held_picture", bad)).isError


async def test_set_timer_default_label_and_bounds():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        r = await s.call_tool("set_timer", {"minutes": 1})
        assert json.loads(result_text(r))["label"] == "your timer"
        for bad in (0, -5, 999999):
            r = await s.call_tool("set_timer", {"minutes": bad})
            assert r.isError                        # bounds enforced server-side


async def test_a_timer_too_long_says_what_to_do_instead():
    """Live, a 3-day "water the basil" timer was refused with the bound alone,
    twice, and she worked round it with a daily "basil tick" timer."""
    for goals, instead in ((True, "create_goal"), (False, "cannot hold it")):
        srv = build_server(max_minutes=1440, goals=goals)
        async with create_connected_server_and_client_session(srv._mcp_server) as s:
            r = await s.call_tool("set_timer", {"minutes": 4320, "label": "basil"})
            assert r.isError
            said = result_text(r)
            assert "1440 minutes (24 hours)" in said and instead in said, said


async def test_there_is_no_music_hand():
    """play_music was removed: the room's ambience is not hers to drive."""
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert "play_music" not in names
        assert (await s.call_tool("play_music", {"action": "play"})).isError


def enum_of(schema, prop):
    """Pydantic hoists a Literal into $defs and leaves a $ref behind, and wraps
    a defaulted one in allOf/anyOf — follow whichever shape we got."""
    if "$ref" in prop:
        return schema["$defs"][prop["$ref"].rsplit("/", 1)[-1]]["enum"]
    for key in ("allOf", "anyOf"):
        for member in prop.get(key, []):
            found = enum_of(schema, member)
            if found:
                return found
    return prop.get("enum")


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


async def test_every_description_reaches_the_directive_whole():
    """The directive is built from discovery, so a description the server writes
    and the directive clips is a capability she was never told about. These are
    written to fit under `DESC_MAX_CHARS`; if one grows past it, raise the cap
    rather than let her see two thirds of her camera."""
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        listed = await s.list_tools()
        specs = [ToolSpec(name=t.name, description=t.description or "",
                          schema=t.inputSchema) for t in listed.tools]
    for spec in specs:
        assert one_line(spec.description) == " ".join(spec.description.split()), (
            f"{spec.name}'s description is clipped before she reads it")

    directive = build_directive(specs, user_name="Sam", max_calls=2)
    # The sentence that routes her off `web_search` and onto the tool that
    # actually shelves what it reads — the one that never used to arrive.
    assert "instead of `web_search`" in directive
    assert "kept on your shelf" in directive          # read_page's, likewise


async def test_the_call_that_failed_now_lands():
    """From tool-logs: she filled the undocumented `depth` with the prose that
    had nowhere else to go, and pydantic took the whole run down with it."""
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        spec = next(t for t in (await s.list_tools()).tools if t.name == "research")
        args = coerce_args({"topic": "AI roleplay escalation",
                            "depth": "current state and key stages"},
                           spec.inputSchema)
        result = await s.call_tool("research", args)
        assert not result.isError
        payload = json.loads(result_text(result))
        assert payload["status"] == "started"
        assert payload["topic"] == "AI roleplay escalation"
        assert payload["depth"] == 5                    # RESEARCH_MAX_PAGES default


async def test_every_argument_is_documented_where_she_reads_it():
    """`research` shipped explaining itself but not its arguments — the one tool
    that didn't, and the one she got wrong. The description is the only place a
    parameter's meaning reaches her; the schema carries names and types alone."""
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        for tool in (await s.list_tools()).tools:
            described = one_line(tool.description or "")
            for arg in (tool.inputSchema.get("properties") or {}):
                assert f"`{arg}`" in described, (
                    f"{tool.name}'s `{arg}` is never explained to her")


# --- her desk (SPEC §34.2) ---------------------------------------------------
# The only hands that write inside the Vault, so the only ones whose contract
# has to be a *refusal* as often as a result.


def desk_server(tmp_path):
    """…with a workspace and a skills folder wired, which is what a spawned
    server gets from VAULT_DIR."""
    from yurios.mind.workspace import SkillStore, Workspace
    return server(workspace=Workspace(tmp_path / "workspace"),
                  skills=SkillStore(tmp_path / "skills"))


async def test_the_desk_hands_appear_only_when_a_vault_is_wired(tmp_path):
    """No VAULT_DIR, no desk — the SELFIE_BACKEND=off rule once more."""
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        assert "write_note" not in {t.name for t in (await s.list_tools()).tools}
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        names = {t.name for t in (await s.list_tools()).tools}
    assert {"list_notes", "read_note", "count_note_lines", "write_note", "append_note",
             "edit_note", "delete_note", "read_skill", "write_skill", "delete_skill"} <= names
    assert not names & {"remove_note_text", "append_to_note_section",
                        "deduplicate_note_sections"}


async def test_a_note_round_trips_through_the_tools(tmp_path):
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        wrote = json.loads(result_text(await s.call_tool(
            "write_note", {"path": "research/boards.md", "text": "three brands"})))
        assert wrote["wrote"] is True and wrote["path"] == "research/boards.md"
        read = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "research/boards.md"})))
        assert "three brands" in read["text"]
        listed = json.loads(result_text(await s.call_tool("list_notes", {})))
        assert [f["path"] for f in listed["files"]] == ["research/boards.md"]
        assert listed["count"] == 1
        assert listed["truncated"] is False
        assert list(listed)[0] == "count"
        assert "mtime" not in listed["files"][0]
    assert (tmp_path / "workspace" / "research" / "boards.md").is_file()


async def test_read_note_teaches_discovery_and_keeps_missing_files_as_errors(tmp_path):
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        tools = (await s.list_tools()).tools
        read = next(t for t in tools if t.name == "read_note")
        description = " ".join((read.description or "").split())
        assert "For a guessed path, first use `list_notes` with its folder" in description
        assert "known path can be read directly" in description
        result = await s.call_tool("read_note", {"path": "diary/2026-11-13.md"})
        assert result.isError
        assert "nothing on your desk" in result_text(result)
    assert not (tmp_path / "workspace" / "diary" / "2026-11-13.md").exists()


async def test_read_note_from_line_zero_reads_from_the_start(tmp_path):
    """`end_line` 0 means "the rest", so a 0 start is the same guess about the
    other end — refused live, it cost a call in the middle of a chat turn."""
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {"path": "notes/a.md", "text": "one\ntwo\n"})
        page = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "notes/a.md", "start_line": 0, "end_line": 0})))
        assert page["start_line"] == 1 and page["text"] == "one\ntwo\n"
        assert (await s.call_tool(
            "read_note", {"path": "notes/a.md", "start_line": -1})).isError


async def test_list_notes_says_when_the_folder_is_not_on_the_desk(tmp_path):
    """An empty listing of a phantom folder is how 'kept-memory is empty'
    became a fact. Name the miss."""
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        listed = json.loads(result_text(await s.call_tool(
            "list_notes", {"folder": "kept-memory"})))
    assert listed["count"] == 0
    assert listed["files"] == []
    assert "no folder" in listed["note"]
    assert "workspace" in listed["note"]


def test_list_notes_payload_names_count_first_and_fits_its_budget():
    from yurios.world.tools.server import LIST_NOTES_MAX_CHARS, _notes_listing

    class _E:
        def __init__(self, path, n):
            self.path, self.bytes = path, n

    files = [_E(f"diary/{i:04d}.md", 800 + i) for i in range(120)]
    payload = _notes_listing(files, desk_files=120, desk_bytes=90_000)
    assert list(payload)[0] == "count"
    assert payload["count"] == 120
    assert payload["shown"] == len(payload["files"])
    compact = json.dumps(payload, separators=(",", ":"))
    assert len(compact) <= LIST_NOTES_MAX_CHARS
    assert payload["truncated"] is True
    assert payload["shown"] < payload["count"]
    assert "mtime" not in payload["files"][0]


async def test_reading_a_normal_sized_note_reaches_the_model_intact(tmp_path):
    from yurios.world.tools.server import NOTE_READ_MAX_CHARS

    text = "x" * (NOTE_READ_MAX_CHARS - 1)
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {"path": "research/report.md", "text": text})
        read = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "research/report.md"})))
    assert read["text"] == text + "\n"
    assert read["truncated"] is False


async def test_a_note_can_be_read_and_edited_by_line_range(tmp_path):
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {
            "path": "research/report.md", "text": "first\nsecond\nthird"})
        count = json.loads(result_text(await s.call_tool(
            "count_note_lines", {"path": "research/report.md"})))
        assert count["lines"] == 3
        read = json.loads(result_text(await s.call_tool("read_note", {
            "path": "research/report.md", "start_line": 2, "end_line": 3})))
        assert read["text"] == "second\nthird\n"
        assert (read["start_line"], read["end_line"]) == (2, 3)
        edited = json.loads(result_text(await s.call_tool("edit_note", {
            "path": "research/report.md", "start_line": 2, "end_line": 3,
            "new_text": "revised"})))
        assert edited["edited"] is True
        final = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "research/report.md"})))
        assert final["text"] == "first\nrevised\n"


async def test_a_note_passage_can_be_edited_without_rewriting_the_note(tmp_path):
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {
            "path": "research/boards.md",
            "text": "# Boards\n\n- Alder: promising\n- Cedar: unavailable"})
        edited = json.loads(result_text(await s.call_tool("edit_note", {
            "path": "research/boards.md",
            "old_text": "Alder: promising",
            "new_text": "Alder: unavailable"})))
        assert edited["edited"] is True
        read = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "research/boards.md"})))
        assert "Alder: unavailable" in read["text"]
        assert "Cedar: unavailable" in read["text"]


async def test_the_sandbox_refuses_and_says_what_to_do_instead(tmp_path):
    """A refusal that teaches nothing gets the same path tried again next turn.
    The error has to name the shape of a path that would work."""
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        result = await s.call_tool(
            "write_note", {"path": "../soul/CONSTITUTION.md", "text": "mine now"})
        assert result.isError
        assert "notes/paddleboards.md" in result_text(result)
    assert not (tmp_path / "soul").exists()


async def test_a_skill_she_writes_is_readable_back(tmp_path):
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_skill", {
            "name": "tea-timer",
            "description": "when they ask to steep something",
            "instructions": "Ask which tea first, then set the timer."})
        out = json.loads(result_text(await s.call_tool(
            "read_skill", {"name": "tea-timer"})))
        assert "Ask which tea first" in out["instructions"]
        missing = await s.call_tool("read_skill", {"name": "nonesuch"})
        assert missing.isError and "tea-timer" in result_text(missing)


async def test_the_desk_hands_explain_their_arguments_too(tmp_path):
    """`test_every_argument_is_documented_where_she_reads_it`, applied to the
    tools that were not yet built when it was written."""
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        for tool in (await s.list_tools()).tools:
            described = one_line(tool.description or "")
            for arg in (tool.inputSchema.get("properties") or {}):
                assert f"`{arg}`" in described, (
                    f"{tool.name}'s `{arg}` is never explained to her")


# --- every answer reaches her whole (SPEC §7.3, §26.2, §34.2) ---------------
# Live, a 206-line inbox document came back to a goal step cut inside its text,
# `end_line` and `truncated` after the cut, and the step's example showed
# `read_note` with a path alone. She read the rest by deleting what she had read.


def _long_document() -> str:
    """Quotes, emoji and short lines: the escapes that make a page longer on
    the wire than it is on disk."""
    return "".join(
        f'{i}. "Tier {i % 7}" — a line about it 💕 with a \\backslash\n'
        for i in range(1, 207))


def test_remaining_names_a_single_line_left_as_one_line():
    from yurios.world.tools.server import _note_page
    assert _note_page("n.md", "a\nb\n", 1, 3)["remaining"].startswith(
        "line 3 comes after this page")
    assert _note_page("n.md", "a\n", 1, 3)["remaining"].startswith(
        "lines 2–3 come after this page")


async def test_a_long_note_reads_through_by_next_start_line(tmp_path):
    from yurios.mind.hands import bounded
    from yurios.world.tools.guard import RESULT_LIMITS

    doc = _long_document()
    pages, start = [], 1
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {"path": "inbox/archive.md", "text": doc})
        while start is not None:
            wire = result_text(await s.call_tool(
                "read_note", {"path": "inbox/archive.md", "start_line": start}))
            assert len(wire) <= RESULT_LIMITS["read_note"]
            # what a goal step hands back to her is the whole answer
            assert bounded("read_note", wire) == wire
            page = json.loads(wire)
            assert list(page)[-1] == "text"      # where she is comes first
            assert page["start_line"] == start
            pages.append(page)
            start = page["next_start_line"]
    assert len(pages) > 1
    assert pages[0]["truncated"] is True
    assert pages[-1]["truncated"] is False and pages[-1]["end_line"] == 206
    # the same in words: a page that stops short says it is not the end
    assert pages[0]["remaining"].startswith(f"lines {pages[0]['end_line'] + 1}–206 ")
    assert "not where the note ends" in pages[0]["remaining"]
    assert pages[-1]["remaining"] == "none — line 206 is the note's last line"
    assert "".join(p["text"] for p in pages) == doc
    assert (tmp_path / "workspace" / "inbox" / "archive.md").read_text() == doc


async def test_an_escape_dense_page_comes_back_shorter_not_cut(tmp_path):
    from yurios.world.tools.guard import RESULT_LIMITS

    doc = '"\\"\n' * 3000          # every character escaped on the wire
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {"path": "notes/q.md", "text": doc})
        wire = result_text(await s.call_tool("read_note", {"path": "notes/q.md"}))
    page = json.loads(wire)
    assert len(wire) <= RESULT_LIMITS["read_note"]
    assert page["text"] == '"\\"\n' * page["end_line"]
    assert page["next_start_line"] == page["end_line"] + 1


async def test_a_line_longer_than_a_page_says_so(tmp_path):
    from yurios.world.tools.server import NOTE_READ_MAX_CHARS

    doc = "x" * (NOTE_READ_MAX_CHARS * 2) + "\nsecond\n"
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_note", {"path": "notes/wide.md", "text": doc})
        page = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "notes/wide.md"})))
    assert page["text"] and set(page["text"]) == {"x"}
    assert (page["end_line"], page["next_start_line"]) == (1, 2)
    assert "line 1" in page["note"]


async def test_a_long_skill_is_read_whole(tmp_path):
    """`read_skill` sat on the 600-character default: a 2,000-character
    skill reached a conversation as its first few paragraphs."""
    from yurios.world.tools.guard import Guard

    body = "".join(f"{i}. Step {i}: do the \"thing\" carefully.\n" for i in range(80))
    async with create_connected_server_and_client_session(
            desk_server(tmp_path)._mcp_server) as s:
        await s.call_tool("write_skill", {
            "name": "long-one", "description": "when it is long",
            "instructions": body})
        wire = result_text(await s.call_tool("read_skill", {"name": "long-one"}))
    assert len(body) > 2_000
    assert Guard.truncate(wire, tool="read_skill") == wire
    out = json.loads(wire)
    assert out["instructions"].strip() == body.strip()
    assert out["truncated"] is False


def test_a_skill_too_long_for_one_read_says_it_was_cut():
    from yurios.world.tools.guard import RESULT_LIMITS
    from yurios.world.tools.server import _skill_page, _wire

    class _S:
        name, description, files = "huge", "when", []
        body = "a line of the method\n" * 2_000

    page = _skill_page(_S)
    assert len(_wire(page)) <= RESULT_LIMITS["read_skill"]
    assert page["truncated"] is True
    assert _S.body.startswith(page["instructions"])


def test_a_listing_fits_as_it_is_sent():
    """Measured compact, a full listing was a third longer on the wire, and
    the guard cut rows that `shown` said she had."""
    from yurios.world.tools.guard import Guard
    from yurios.world.tools.server import _notes_listing, _wire

    class _E:
        def __init__(self, path, n):
            self.path, self.bytes = path, n

    payload = _notes_listing([_E(f"diary/{i:04d}.md", 800 + i) for i in range(300)],
                             desk_files=300, desk_bytes=250_000)
    wire = _wire(payload)
    assert Guard.truncate(wire, tool="list_notes") == wire
    assert json.loads(wire)["shown"] == len(payload["files"]) < 300


#: Arguments a tool takes that the mind's one-line example leaves out, each for
#: a reason. A step sees `HANDS[tool].args` and never the tool's description, so
#: an argument missing from both is one she cannot know exists — `start_line`
#: was, and she paged a document by deleting it.
UNSHOWN_TO_THE_MIND = {
    "web_search": {"k"},                          # the default count is the right one
    "take_selfie": {"scene", "mood", "wardrobe", "framing", "lighting", "avoid",
                    "goal_id", "completes_goal"},  # `look` carries all of it
    "show_picture": {"avoid"},
    "set_timer": {"goal_id", "completes_goal"},
    "read_note": {"end_line"},                    # `next_start_line` pages without it
    "edit_note": {"start_line", "end_line"},      # a repeat is refused with what to add
    "propose_edit": {"content"},                  # find/replace is the reliable path
}


async def test_the_minds_example_of_every_hand_matches_the_tool(tmp_path):
    from yurios.mind.hands import HANDS
    from yurios.mind.workspace import SkillStore, Workspace

    srv = server(search=FakeSearch(), fetcher=FakeFetcher(), max_pages=5,
                 workspace=Workspace(tmp_path / "workspace"),
                 skills=SkillStore(tmp_path / "skills"), selfedit=True, goals=True)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        schemas = {t.name: set((t.inputSchema.get("properties") or {}))
                   for t in (await s.list_tools()).tools}
    assert set(HANDS) <= set(schemas)
    for name, hand in HANDS.items():
        shown = set(json.loads(hand.args))
        assert shown <= schemas[name], f"{name}'s example names {shown - schemas[name]}"
        unshown = schemas[name] - shown
        assert unshown == UNSHOWN_TO_THE_MIND.get(name, set()), (
            f"{name} takes {unshown} that a goal step is never told about")


# --- propose_edit: the contract answers honestly (SPEC §7.5, §23) -----------------

async def test_propose_edit_refuses_a_rewrite_that_would_stop_her_booting(
        tmp_path, monkeypatch):
    """The answering side must answer honestly. Queuing something the host will
    drop — or worse, apply, leaving a character that fails to start — is the one
    thing the §7.5 split may not do."""
    soul = tmp_path / "soul"
    soul.mkdir()
    (soul / "soul.yaml").write_text(
        'name: "T"\nfields:\n  description:\n    - PERSONA.md#Appearance\n')
    (soul / "PERSONA.md").write_text("# Persona\n\n## Appearance\n\nDark hair.\n")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))

    async with create_connected_server_and_client_session(
            server(selfedit=True)._mcp_server) as s:
        bad = await s.call_tool("propose_edit", {
            "surface": "soul/PERSONA.md", "content": "She is quieter now.\n",
            "reason": "I have got quieter"})
        assert bad.isError
        assert "## Appearance" in result_text(bad)

        ok = await s.call_tool("propose_edit", {
            "surface": "soul/PERSONA.md",
            "content": "# Persona\n\n## Appearance\n\nShorter hair now.\n",
            "reason": "I cut it"})
        assert not ok.isError
        assert json.loads(result_text(ok))["status"] == "proposed"


def _soul_vault(tmp_path, monkeypatch):
    soul = tmp_path / "soul"
    soul.mkdir()
    (soul / "soul.yaml").write_text(
        'name: "T"\nfields:\n  description:\n    - PERSONA.md#Appearance\n')
    (soul / "PERSONA.md").write_text(
        "# Persona\n\n## Appearance\n\nDark hair.\n\n## Manner\n\n"
        "Calls him darling. Calls him darling.\n")
    (soul / "CONSTITUTION.md").write_text("# Constitution\n\nBe honest.\n")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    from yurios.mind.workspace import Workspace
    return server(workspace=Workspace(tmp_path / "workspace"), selfedit=True)


async def test_read_note_opens_her_soul_files_read_only(tmp_path, monkeypatch):
    """Live, propose_edit wanted the whole file and nothing let her see it: she
    sent `"content": "..."`. Soul files now read like notes — never written."""
    async with create_connected_server_and_client_session(
            _soul_vault(tmp_path, monkeypatch)._mcp_server) as s:
        page = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "soul/PERSONA.md"})))
        assert "Dark hair." in page["text"] and page["line_count"] == 9
        assert "Be honest." in result_text(await s.call_tool(
            "read_note", {"path": "soul/CONSTITUTION.md"}))
        bad = await s.call_tool("read_note", {"path": "soul/soul.yaml"})
        assert bad.isError and "soul/PERSONA.md" in result_text(bad)
        # a soul file looked for on the desk is pointed home: live she tried
        # both of these, and `list_notes` — the old pointer — never lists it
        for guess in ("notes/PERSONA.md", "PERSONA.md"):
            miss = await s.call_tool("read_note", {"path": guess})
            assert miss.isError and 'did you mean "soul/PERSONA.md"' in result_text(miss)
        other = await s.call_tool("read_note", {"path": "notes/basil.md"})
        assert "did you mean" not in result_text(other)
        assert "`list_notes` shows what is there" in result_text(other)
        page = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "soul/PERSONA.md", "start_line": 0})))
        assert page["start_line"] == 1
        assert (await s.call_tool("read_note", {"path": "soul/../../etc/passwd"})).isError
        # …and the desk will not take the path at all: a note written there
        # used to "succeed", and reading it back handed her the soul file
        for tool, args in (("write_note", {"text": "x"}),
                           ("append_note", {"text": "x"}),
                           ("count_note_lines", {})):
            r = await s.call_tool(tool, {"path": "soul/PERSONA.md", **args})
            assert r.isError and "not a place on your desk" in result_text(r), tool
        r = await s.call_tool("list_notes", {"folder": "soul"})
        assert r.isError and "propose_edit" in result_text(r)
    assert "Dark hair." in (tmp_path / "soul" / "PERSONA.md").read_text()
    assert not (tmp_path / "workspace" / "soul").exists()


async def test_a_desk_soul_folder_from_before_is_out_of_sight(tmp_path, monkeypatch):
    """A `workspace/soul/` written before the path was reserved is not listed:
    nothing else on the desk will open it, so listing it advertises a path
    no hand can use."""
    srv = _soul_vault(tmp_path, monkeypatch)
    old = tmp_path / "workspace" / "soul"
    old.mkdir(parents=True)
    (old / "PERSONA.md").write_text("an old desk copy")
    (tmp_path / "workspace" / "notes").mkdir()
    (tmp_path / "workspace" / "notes" / "a.md").write_text("kept")
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        listing = json.loads(result_text(await s.call_tool("list_notes", {})))
        assert [f["path"] for f in listing["files"]] == ["notes/a.md"]
        page = json.loads(result_text(await s.call_tool(
            "read_note", {"path": "soul/PERSONA.md"})))
        assert "Dark hair." in page["text"]          # the real one, not the copy


@pytest.mark.parametrize("mind_enabled", [True, False])
async def test_soul_reads_follow_the_self_edit_door_with_the_desk_off(
        tmp_path, monkeypatch, mind_enabled):
    from yurios.world.config import Config
    from yurios.world.runtime import tool_rates
    from yurios.world.tools.spawn_env import ToolServerEnv

    _soul_vault(tmp_path, monkeypatch)
    cfg = Config(_env_file=None, vault_dir=tmp_path, workspace_enabled=False,
                 skills_enabled=False, mind_enabled=mind_enabled)
    settings = ToolServerEnv.from_environ(ToolServerEnv.from_config(cfg).to_environ())
    srv = build_server(settings=settings)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert ("read_note" in names) is mind_enabled
        assert ("read_note" in tool_rates(cfg)) is mind_enabled
        assert not names & {"write_note", "list_notes", "edit_note"}
        if mind_enabled:
            page = json.loads(result_text(await s.call_tool("read_note", {
                "path": "soul/PERSONA.md", "start_line": 3, "end_line": 5})))
            assert page["text"] == "## Appearance\n\nDark hair.\n"
            assert page["start_line"] == 3 and page["end_line"] == 5
            assert page["line_count"] == 9 and page["next_start_line"] == 6
            assert (await s.call_tool("read_note", {"path": "notes/x.md"})).isError


async def test_fake_self_edit_patches_the_text_its_read_note_returns():
    from yurios.world.tools.fakes import FakeToolRunner

    runner = FakeToolRunner()
    specs = await runner.start()
    spec = next(t for t in specs if t.name == "propose_edit")
    assert {"find", "replace"} <= set(spec.schema["properties"])
    assert "content" not in spec.schema["required"]
    current = json.loads(await runner.call("read_note", {"path": "soul/PERSONA.md"}))["text"]
    args = {"surface": "PERSONA.md", "find": current, "replace": "# Revised\n",
            "reason": "a change"}
    proposal = json.loads(await runner.call("propose_edit", args))
    assert proposal["content"] == "# Revised\n" and proposal["chars"] == 10
    with pytest.raises(RuntimeError, match="exactly once"):
        await runner.call("propose_edit", {**args, "find": "missing text"})


async def test_propose_edit_find_replace_builds_the_whole_file(tmp_path, monkeypatch):
    async with create_connected_server_and_client_session(
            _soul_vault(tmp_path, monkeypatch)._mcp_server) as s:
        ok = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Dark hair.", "replace": "Short dark hair.",
            "reason": "I cut it"})
        assert not ok.isError
        out = json.loads(result_text(ok))
        assert out["status"] == "proposed"
        assert out["content"].startswith("# Persona") and "Short dark hair." in out["content"]
        assert "## Manner" in out["content"]

        twice = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Calls him darling.", "replace": "",
            "reason": "he asked"})
        assert twice.isError and "2 times" in result_text(twice)
        missing = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Blonde.", "replace": "x", "reason": "r"})
        assert missing.isError and 'read_note("soul/PERSONA.md")' in result_text(missing)
        both = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Dark hair.", "replace": "x",
            "content": "# Persona\n", "reason": "r"})
        assert both.isError
        # live: `replace` carried the next heading along without `find` reaching it
        dup = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Dark hair.",
            "replace": "Dark hair.\n\n## Manner", "reason": "r"})
        assert dup.isError and "'## Manner'" in result_text(dup)
        # a referenced heading carried along WITH its prose is not stacked, but
        # it is twice: only the last copy would reach her prompt (shape check)
        twice = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Calls him darling. Calls him darling.",
            "replace": "Gentle.\n\n## Appearance\n\nTall.", "reason": "r"})
        assert twice.isError and "more than once" in result_text(twice)
        # a patch that drops a section the soul.yaml points at is still refused
        gone = await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "## Appearance\n\nDark hair.\n\n",
            "replace": "", "reason": "r"})
        assert gone.isError and "## Appearance" in result_text(gone)
    # nothing applied: a proposal is queued by the host, never written here
    assert "Dark hair." in (tmp_path / "soul" / "PERSONA.md").read_text()


async def test_a_heading_that_recurs_by_design_is_not_a_mistake(tmp_path, monkeypatch):
    """The stacked-heading refusal is for `## Manner` directly over `## Manner`.
    A notes file whose every entry has the same heading over its own text is
    how it was written, and adding one more is the ordinary edit."""
    srv = _soul_vault(tmp_path, monkeypatch)
    (tmp_path / "soul" / "NOTES.md").write_text(
        "# Notes\n\n## Entry\n\nOne.\n\n## Entry\n\nTwo.\n")
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        ok = await s.call_tool("propose_edit", {
            "surface": "NOTES.md", "find": "Two.\n",
            "replace": "Two.\n\n## Entry\n\nThree.\n", "reason": "a third"})
        assert not ok.isError, result_text(ok)
        stacked = await s.call_tool("propose_edit", {
            "surface": "NOTES.md", "find": "One.",
            "replace": "One.\n\n## Entry", "reason": "r"})
        assert stacked.isError and "'## Entry'" in result_text(stacked)


async def test_propose_edit_leads_with_queued_and_carries_its_patch(
        tmp_path, monkeypatch):
    """The guard keeps 600 characters of a fact, and `content` is her whole
    file: with `content` first, all she ever saw was the start of her persona.
    `patch` rides along for the ruling-time apply (SPEC §23.2)."""
    from yurios.world.tools.guard import Guard
    srv = _soul_vault(tmp_path, monkeypatch)
    (tmp_path / "soul" / "PERSONA.md").write_text(
        (tmp_path / "soul" / "PERSONA.md").read_text() + "Long. " * 400 + "\n")
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        raw = result_text(await s.call_tool("propose_edit", {
            "surface": "PERSONA.md", "find": "Dark hair.",
            "replace": "Short dark hair.", "reason": "I cut it"}))
    out = json.loads(raw)
    assert list(out)[:3] == ["status", "surface", "queued"]
    assert list(out)[-1] == "content"
    assert out["patch"] == {"find": "Dark hair.", "replace": "Short dark hair."}
    seen = Guard.truncate(raw, tool="propose_edit")
    assert len(seen) < len(raw) and '"queued": true' in seen and "I cut it" in seen


def test_the_fake_server_answers_in_the_same_order():
    import asyncio

    from yurios.world.tools.fakes import FakeToolRunner
    runner = FakeToolRunner()
    current = json.loads(asyncio.run(runner.call(
        "read_note", {"path": "soul/PERSONA.md"})))["text"]
    find = current.splitlines()[0]
    out = json.loads(asyncio.run(runner.call("propose_edit", {
        "surface": "PERSONA.md", "find": find, "replace": "x", "reason": "r"})))
    assert list(out)[:3] == ["status", "surface", "queued"]
    assert list(out)[-1] == "content" and out["patch"]["find"] == find
    with pytest.raises(RuntimeError, match="not a place on your desk"):
        asyncio.run(runner.call("write_note", {"path": "soul/x.md", "text": "y"}))
