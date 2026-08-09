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
from yurios.world.tools.server import MUSIC_TRACKS, build_server  # noqa: E402


def server(**kw):
    return build_server(max_minutes=180, **kw)


def web_server():
    """…with the web hands on (SEARCH_BACKEND=fake, §7.7)."""
    return server(search=FakeSearch(), fetcher=FakeFetcher(), max_pages=5)


async def test_list_tools_is_exactly_the_hands_she_has():
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        listed = await s.list_tools()
        assert sorted(t.name for t in listed.tools) == [
            "play_music", "set_timer", "show_picture", "take_selfie"]
        timer = next(t for t in listed.tools if t.name == "set_timer")
        assert "minutes" in timer.inputSchema["properties"]
        assert "minutes" in timer.inputSchema.get("required", [])


async def test_the_web_hands_appear_only_when_search_is_configured():
    """SEARCH_BACKEND=off is the SELFIE_BACKEND=off rule (§7.7): no hand, not a
    dead one. The three go together — searching with no way to read what you
    found is half a capability."""
    async with create_connected_server_and_client_session(
            web_server()._mcp_server) as s:
        names = sorted(t.name for t in (await s.list_tools()).tools)
    assert names == ["play_music", "read_page", "research", "set_timer",
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
        assert sorted(t.name for t in listed.tools) == ["play_music", "set_timer"]


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


async def test_play_music_puts_the_track_list_in_the_schema():
    """A description that merely names the tracks is a suggestion — she invented
    `ambient_rain_lullaby` against one. The catalog has to reach the model as an
    `enum` on the parameter, and the prose has to be built from the same tuple
    so the two can never drift."""
    async with create_connected_server_and_client_session(server()._mcp_server) as s:
        music = next(t for t in (await s.list_tools()).tools if t.name == "play_music")
        props = music.inputSchema["properties"]
        assert enum_of(music.inputSchema, props["track"]) == list(MUSIC_TRACKS)
        assert enum_of(music.inputSchema, props["action"]) == ["play", "stop"]
        for name in MUSIC_TRACKS:
            assert name in music.description


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


def test_the_frontend_and_the_tool_agree_on_the_catalog():
    """Three copies of the track list exist — the tool's, the controller's, and
    web/js/music.js's TRACKS. The first two are importable, so pin them; a
    silent split there is a tool that validates a track her room can't play."""
    from yurios.world.avatar.controller import MUSIC_TRACKS as BODY_TRACKS
    assert tuple(BODY_TRACKS) == tuple(MUSIC_TRACKS)


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
        assert payload["depth"] == 3                    # the tool's own default


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
    assert {"list_notes", "read_note", "write_note", "append_note",
            "delete_note", "read_skill", "write_skill", "delete_skill"} <= names


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
    assert (tmp_path / "workspace" / "research" / "boards.md").is_file()


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
