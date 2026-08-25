"""The in-repo MCP server (SPEC §7.1–§7.2) — her hands, as a real server.

Run standalone (`python -m yurios.world.tools.server`) it speaks MCP over stdio; the
host spawns it exactly that way (client.py). Tests connect to the same server
object over an in-memory session — the contract is identical (SPEC §27).

The server is the *contract and audit point*: it validates arguments and returns
the structured result. Side effects that need her body or her voice — actually
scheduling the timer's announcement, actually starting the ambience — happen on
the **host** after the call returns (SPEC §7.5), because only the host owns the
stage and the clock. That split is not an implementation shortcut; it is the
shape Build #5 keeps when these same tools move behind a broker (→ ch. 19).
"""
from __future__ import annotations

import time
import uuid
from typing import Literal, get_args

from mcp.server.fastmcp import FastMCP

from yurios.characters.soulfiles import shape_complaint
from yurios.mind.vaultio import MindVault
from yurios.mind.workspace import (DeskFull, OutsideTheDesk, SkillStore,
                                   Workspace)

from .fetch import PageFetcher, build_fetcher, gist
from .search import SearchProvider, build_provider
from .spawn_env import ToolServerEnv

NOTE_READ_MAX_CHARS = 4_000

# The catalog lives in the *type*, not in prose: an annotated Literal becomes an
# `enum` in the tool's JSON schema, which is the only form of the list a model
# reliably obeys. A description that merely names the tracks is a suggestion —
# she invented `ambient_rain_lullaby` against one. Everything else derives from
# here, so the schema, the docstring and the error can never disagree.
MusicAction = Literal["play", "stop"]
MusicTrack = Literal["warm_pad", "night_piano"]
MUSIC_ACTIONS: tuple[str, ...] = get_args(MusicAction)
MUSIC_TRACKS: tuple[str, ...] = get_args(MusicTrack)


#: The soul surfaces `propose_edit` will name, in the order she is likeliest to
#: want them. Derived from `MindVault.EDITABLE_SOUL` rather than restated, so a
#: file that becomes editable (or stops being) cannot leave this list behind —
#: minus the two the runtime writes for her, which she should not be redrafting
#: by hand, and CONSTITUTION, which is not in the set at all.
PROPOSABLE = tuple(sorted(
    MindVault.EDITABLE_SOUL - {"USER.md", "MEMORY.md", "BOOTSTRAP.md"}))


def build_server(*, max_minutes: float | None = None,
                 selfies: bool | None = None,
                 search: SearchProvider | None = None,
                 fetcher: PageFetcher | None = None,
                 results: int | None = None,
                 max_pages: int | None = None,
                 workspace: "Workspace | None" = None,
                 skills: "SkillStore | None" = None,
                 selfedit: bool | None = None,
                 settings: ToolServerEnv | None = None) -> FastMCP:
    """Build the FastMCP server. Args are the test seams; `python -m` reads env.

    Every setting that crosses the spawn boundary is read once, here, through
    `ToolServerEnv` — what each key is called, what type it is and what it means
    when it is absent all live in `spawn_env.py`, beside the encoder the host
    writes it with. Name an env key in this module and the two sides of the
    wire can start disagreeing again, so `tests/test_spawn_env.py` refuses one.
    """
    env = ToolServerEnv.from_environ() if settings is None else settings
    max_minutes = max_minutes if max_minutes is not None else env.timer_max_minutes
    if selfies is None:                        # off = not advertised at all (§7.6)
        selfies = env.selfies
    results = results if results is not None else env.search_results
    max_pages = max_pages if max_pages is not None else env.research_max_pages
    # The web hands go together or not at all (§7.7): searching with no way to
    # read what you found is half a capability, and `research` is the two of
    # them in sequence. SEARCH_BACKEND=off is the SELFIE_BACKEND=off rule —
    # `list_tools` simply doesn't mention them.
    if search is None:
        search = build_provider(
            env.search_backend, base_url=env.searxng_url,
            language=env.search_language, safesearch=env.search_safesearch)
    if fetcher is None and search is not None:
        fetcher = build_fetcher(
            "fake" if env.search_backend == "fake" else "http",
            timeout=env.fetch_timeout_s, max_bytes=env.fetch_max_bytes)
    # Her desk and her skills (§34.2). This process has no runtime and no
    # config object — a path is the entire wiring, and no path means the tools
    # are not advertised at all, the SELFIE_BACKEND=off rule again. The host
    # passes VAULT_DIR only for the character whose server this is, so one
    # character's hands can never reach another's desk: the sandbox's root is
    # decided at spawn time, not by an argument the model gets to write.
    #
    # …and it is also the root the manifest her rewrites are held against. No
    # VAULT_DIR means no shape check: this process cannot invent one, and a
    # refusal it cannot justify would be worse than the host's own check alone.
    vault_dir = env.vault_path
    if workspace is None and vault_dir is not None and env.workspace:
        workspace = Workspace(vault_dir / "workspace")
    if skills is None and vault_dir is not None and env.skills:
        skills = SkillStore(vault_dir / "skills")
    # The self-edit door (§23). Off means unadvertised, the SELFIE_BACKEND=off
    # rule once more — and it is off unless the host says otherwise, because
    # the queue this writes into is only *read* where the mind is running.
    if selfedit is None:
        selfedit = env.selfedit

    mcp = FastMCP("world-companion-tools")

    @mcp.tool()
    def set_timer(minutes: float, label: str = "") -> dict:
        """Set a countdown timer. `minutes` must be positive; `label` is what the
        timer is for ("tea", "the oven") and is spoken back when it finishes."""
        if not (0 < minutes <= max_minutes):
            raise ValueError(f"minutes must be in (0, {max_minutes:g}]")
        seconds = round(minutes * 60)
        return {"id": uuid.uuid4().hex[:8], "label": label or "your timer",
                "seconds": seconds, "due": time.time() + seconds}

    # Description BUILT from the catalog, the same way take_selfie's is built
    # from the library below — the prose can't drift from the enum, and it says
    # plainly that these two are the whole list (unlike the selfie book, this
    # one IS a limit: only the frontend's two generators exist).
    @mcp.tool(description=(
        "Start or stop the room's ambient music. `action` is "
        + " or ".join(f'"{a}"' for a in MUSIC_ACTIONS)
        + f"; `track` is one of exactly these {len(MUSIC_TRACKS)} — "
        + ", ".join(MUSIC_TRACKS)
        + " — and no other track exists, so pick the nearer of the two rather "
          "than naming one you'd prefer; `volume` is 0..1."))
    def play_music(action: MusicAction, track: MusicTrack = "warm_pad",
                   volume: float = 0.4) -> dict:
        # The Literals are enforced by the schema before we get here; these keep
        # the same answer for a direct in-process call (brain.py, tests).
        if action not in MUSIC_ACTIONS:
            raise ValueError(f"unknown action: {action} (have: {', '.join(MUSIC_ACTIONS)})")
        if track not in MUSIC_TRACKS:
            raise ValueError(f"unknown track: {track} (have: {', '.join(MUSIC_TRACKS)})")
        if not (0.0 <= volume <= 1.0):
            raise ValueError("volume must be 0..1")
        return {"playing": action == "play",
                "track": track if action == "play" else None,
                "volume": volume}

    if search is not None and fetcher is not None:
        # The web (SPEC §7.7). Three hands that go together: find it, read it,
        # or go away and find out about it properly.

        @mcp.tool()
        async def web_search(query: str, k: int = 0) -> dict:
            """Search the web and get back a handful of titles, links and
            snippets. Use it when you need something you don't know or that
            changed recently — news, a fact you're unsure of, what a thing
            actually is. `query` is what you'd type into a search box: the words
            that matter, not a sentence. `k` is a plain NUMBER and nothing else:
            how many results you want back — leave it out for the usual handful.
            The snippets are short on purpose: if one of them is the answer, say
            it; if you need what the page actually says, follow it with
            `read_page` on the url."""
            rows = await search.search(query, k if 0 < k <= results else results)
            if not rows:
                return {"query": query, "results": [],
                        "note": "nothing came back for that"}
            return {"query": query, "results": rows}

        @mcp.tool()
        async def read_page(url: str) -> dict:
            """Read one web page and get back what it actually says. `url` must
            be a real link — one from `web_search`, or one you were given. You
            get a short opening extract to speak to; the whole page is kept on
            your shelf afterwards, so you can recall it later without reading it
            again. Pages that aren't text, and anything on this machine's own
            network, can't be read."""
            page = await fetcher.fetch(url)
            text = page.get("text") or ""
            if not text.strip():
                raise ValueError(f"{url} had no readable text on it")
            # `text` is the WHOLE page and `gist` is the part she speaks to.
            # This works because world/brain.py `_execute` keeps the untruncated
            # result for host realisation and truncates only the copy the model
            # and the audit line see — so the shelf gets the page, the turn gets
            # 400 characters, and neither has to know about the other.
            return {"url": page.get("url") or url,
                    "title": page.get("title") or url,
                    "gist": gist(text),
                    "chars": len(text),
                    "text": text,
                    "status": "read"}

        @mcp.tool(description=(
            "Go and find out about something properly — several searches' "
            "worth of reading, shelved so you keep it. Use this instead of "
            "`web_search` when the answer is going to take more than one page: "
            "\"what's the current state of X\", \"read up on Y for me\". "
            "`topic` is the whole thing you want to know, in one line and in "
            "your own words — put every angle you care about in there, because "
            "it is the only place they fit. `depth` is a plain NUMBER and "
            f"nothing else: how many pages to read, 1 to {max_pages}. Leave it "
            f"out to read the configured maximum ({max_pages}); use 1 when you "
            "want it quick. It takes a while, so it happens in the background: "
            "this answers immediately and what you found arrives in the chat "
            "when it's done. Say you're looking into it and carry on talking — "
            "never call it twice for the same topic, and never wait for it."))
        async def research(topic: str, depth: int = max_pages) -> dict:
            """Go and find out about something properly — several searches'
            worth of reading, shelved so you keep it. Use this instead of
            `web_search` when the answer is going to take more than one page:
            "what's the current state of X", "read up on Y for me". `topic` is
            the whole thing you want to know, in one line and in your own words
            — put every angle you care about in there, because it is the only
            place they fit. `depth` is a plain NUMBER and nothing else: how many
            pages to read, 1 to the configured maximum. Leave it out for the
            configured maximum, or use 1 when you want it quick. It takes a while,
            so it happens in the background:
            this answers immediately and what you found arrives in the chat when
            it's done. Say you're looking into it and carry on talking — never
            call it twice for the same topic, and never wait for it."""
            topic = (topic or "").strip()
            if not topic:
                raise ValueError("topic must say what to look into")
            # Nothing here touches the network. A search plus three fetches plus
            # three embeddings is 20+ seconds and TOOL_TIMEOUT_S is 10; the work
            # belongs off-turn on the host (§7.6's start-don't-await rule, and
            # world/research.py is where it lands).
            return {"id": uuid.uuid4().hex[:8],
                    "topic": topic,
                    "depth": max(1, min(int(depth or max_pages), max_pages)),
                    "kind": "research",
                    "status": "started",
                    "note": "what you find will appear in the chat shortly — "
                            "no need to wait for it, and no need to ask again"}

    if selfies:
        # Her camera (SPEC §7.6). The server is the contract point only: it
        # carries the ask and answers "started" — the render, the file, and the
        # chat message happen on the host (§7.5), because a 10–30 s generation
        # must not sit inside the turn (start-don't-await). The description is
        # BUILT from the library so the choices the model sees can never drift
        # from the yaml — and it names the pass-through explicitly: the library
        # is a starting point, not a limit, and this tool refuses nothing
        # (→ ch. 11: the engine takes no enforcement posture; what renders is
        # the backend's call, never the contract's). The book is the SAME one
        # the host renders from — overlay included (world/selfies.py) — so the
        # choices she sees can never drift from what the forge would compose.
        from ..selfies import book_path
        from yurios.forge import SelfieBook
        overlays = [env.selfie_templates_extra] if env.selfie_templates_extra else []
        # …base included: a character with her own library (SELFIE_TEMPLATES,
        # → characters/selfiebook.py) replaces the shipped book, and the tool
        # description has to name *her* rows or she is offered scenes her
        # camera would never compose.
        book = SelfieBook.load(book_path(env.selfie_templates), overlays=overlays)
        desc = ("Take a photo of yourself to share in the chat — it appears "
                "there a few moments later. "
                "`look` is the important one: describe the picture you want in "
                "your own words, as much or as little as you like — where you "
                "are, how you're sitting, what you're doing with your hands, "
                "what your face is doing, what's behind you. Write it the way "
                "you'd describe a photo you're about to take, not as keywords. "
                "This is your picture: whatever you put in `look` is what gets "
                "made, and it overrides every option below. Anything you leave "
                "out is filled in from where you actually are right now — the "
                "hour, the weather, the room — so a short `look` is fine when "
                "the moment already says the rest.\n"
                "The rest are optional shorthands. Each takes one of the named "
                "options OR any phrase of your own; the library is a starting "
                "point, not a limit, and nothing is chosen for you if you leave "
                "it empty. "
                f"`scene` e.g.: {', '.join(sorted(book.scenes))}; "
                f"`framing` e.g.: {', '.join(sorted(book.framings))}; "
                f"`lighting` e.g.: {', '.join(sorted(book.lighting))}; "
                f"`mood` e.g.: {', '.join(sorted(book.moods))}; "
                f"`wardrobe` e.g.: {', '.join(sorted(book.wardrobe))}, or any "
                "outfit you care to describe (empty = everyday). "
                "`avoid` is anything you specifically don't want in the shot.\n"
                "One call is one photo — it is already on its way when this "
                "answers, so never call it twice for the same picture."
                # the library's own voice (tool_hint in the yaml — shipped
                # empty, an overlay's register explained in its own words)
                + (f" {book.tool_hint}" if book.tool_hint else ""))

        @mcp.tool(description=desc)
        def take_selfie(look: str = "", scene: str = "", mood: str = "",
                        wardrobe: str = "", framing: str = "",
                        lighting: str = "", avoid: str = "") -> dict:
            # Everything passes through: `look` verbatim, named template keys
            # from the library, anything else as her own words
            # (forge/templates.py — no off-menu refusal). Empty slots stay empty
            # rather than being rotated in behind her back.
            return {"id": uuid.uuid4().hex[:8],
                    "look": look or None,
                    "scene": scene or None, "mood": mood or None,
                    "wardrobe": wardrobe or None, "framing": framing or None,
                    "lighting": lighting or None, "avoid": avoid or None,
                    "kind": "selfie",
                    "status": "started",
                    "note": "the photo will appear in the chat shortly — "
                            "no need to wait for it, and no need to ask again"}

        # The same camera, pointed away from herself (§7.6). `take_selfie` can
        # only ever answer "here is a picture of me", and a companion who is
        # describing the street below, or a sketch she made, or the thing she
        # keeps meaning to show you, has nothing to send. This is the other half
        # of that: no library, no slots, no rotation — the subject is whatever
        # she writes, because there is no menu that could anticipate it.
        @mcp.tool()
        def show_picture(subject: str, avoid: str = "") -> dict:
            """Share a picture of something that ISN'T you — what you're looking
            at, a place, an object, a drawing you made, anything you're
            describing and would rather show. It appears in the chat a few
            moments later. `subject` is the whole picture in your own words,
            written the way you'd describe a photo rather than as keywords —
            there are no options and nothing is chosen for you, so say as much
            as you want about the thing, the light, the angle, the background.
            You are not in this one; use `take_selfie` for pictures of yourself.
            `avoid` is anything you specifically don't want in the shot. One call
            is one picture — it is already on its way when this answers, so never
            call it twice for the same picture."""
            if not subject.strip():
                raise ValueError("subject must say what the picture is of")
            return {"id": uuid.uuid4().hex[:8],
                    "kind": "picture",
                    "subject": subject.strip(),
                    "avoid": avoid.strip() or None,
                    "status": "started",
                    "note": "the picture will appear in the chat shortly — "
                            "no need to wait for it, and no need to ask again"}

    # --- her desk (SPEC §34.2) ------------------------------------------------
    # The only hands that write inside the Vault. Everything they can reach is
    # under `workspace/`, and `Workspace.resolve` is the single place that is
    # enforced — these wrappers translate its two refusals into a sentence the
    # model can act on, and otherwise get out of the way.

    def _refusal(exc: Exception) -> ValueError:
        """A sandbox refusal as something she can read and correct.

        Deliberately says what to do instead. "denied" teaches nothing and she
        will try the same path again next turn; "use a path like notes/x.md"
        ends the loop.
        """
        if isinstance(exc, OutsideTheDesk):
            return ValueError(
                f"{exc.reason}. Paths are relative to your workspace, like "
                '"notes/paddleboards.md" — no leading slash, no "..", '
                "no names starting with a dot.")
        return ValueError(str(exc))

    if workspace is not None:

        @mcp.tool()
        def list_notes(folder: str = "") -> dict:
            """List what's on your desk — the notes and drafts you've written
            for yourself. `folder` narrows it to one subfolder ("research"),
            or leave it out for everything. You get paths and sizes, not
            contents; `read_note` opens one."""
            try:
                entries = workspace.list(folder) if folder else workspace.list()
            except OutsideTheDesk as e:
                raise _refusal(e) from None
            files = [e.as_dict() for e in entries if not e.is_dir]
            count, total = workspace.usage()
            return {"files": files, "count": len(files),
                    "desk_files": count, "desk_bytes": total}

        @mcp.tool()
        def read_note(path: str, start_line: int = 1, end_line: int = 0) -> dict:
            """Read one of your own notes back. `path` is what `list_notes`
            showed you, like "research/paddleboards.md". `start_line` is a
            1-based first line; `end_line` is an inclusive last line, or 0 for
            the rest of the note. Use line ranges to inspect a long note before
            editing it."""
            try:
                text, first, last, count = workspace.read_lines(
                    path, start_line=start_line, end_line=end_line)
                shown = text[:NOTE_READ_MAX_CHARS]
                if len(text) > NOTE_READ_MAX_CHARS and "\n" in shown:
                    shown = shown[:shown.rfind("\n") + 1]
                shown_lines = len(shown.splitlines())
                return {"path": path, "text": shown, "start_line": first,
                        "end_line": first + shown_lines - 1 if shown_lines else 0,
                        "line_count": count, "truncated": len(shown) < len(text)}
            except FileNotFoundError:
                raise ValueError(
                    f"nothing on your desk at {path} — `list_notes` shows what "
                    "is there") from None
            except (OutsideTheDesk, ValueError) as e:
                raise _refusal(e) from None

        @mcp.tool()
        def write_note(path: str, text: str) -> dict:
            """Write something down for yourself and keep it. Use this whenever
            a thought needs to outlive the conversation — what you found while
            reading, a draft you're not ready to say, the state of something
            you're working through. `path` names the file, relative to your
            workspace, ending in .md ("research/paddleboards.md"); subfolders
            are made for you. `text` REPLACES the whole file, so read it first
            if you mean to add to it — or use `append_note`, which doesn't.
            This is your own space: you don't need permission and you don't need
            to mention it out loud."""
            try:
                entry = workspace.write(path, text)
            except (OutsideTheDesk, DeskFull) as e:
                raise _refusal(e) from None
            return {"path": entry.path, "bytes": entry.bytes, "wrote": True}

        @mcp.tool()
        def append_note(path: str, text: str) -> dict:
            """Add to the end of one of your notes without rewriting it — the
            right call for a running log, a list you keep adding to, or one more
            thought on something you already wrote. `path` is the note, the same
            way `write_note` takes it; `text` is what goes on the end. Creates
            the file if it isn't there yet."""
            try:
                entry = workspace.append(path, text)
            except (OutsideTheDesk, DeskFull) as e:
                raise _refusal(e) from None
            return {"path": entry.path, "bytes": entry.bytes, "appended": True}

        @mcp.tool()
        def edit_note(path: str, new_text: str, old_text: str = "",
                      start_line: int = 0, end_line: int = 0) -> dict:
            """Change one passage in an existing note without rewriting all of
            it. `path` names the note to change. For exact-text editing,
            `old_text` is a non-empty passage copied from the note that occurs
            once, and `new_text` replaces it. An empty `new_text` deletes the
            matched text; if that exact block repeats, the later copy is deleted.
            For a repeated block you need to replace, leave `old_text` empty and
            give its 1-based inclusive `start_line` and `end_line`; `new_text`
            replaces that range. Use `read_note` first."""
            try:
                if old_text:
                    if start_line or end_line:
                        raise ValueError("use either old_text or a line range, not both")
                    entry = workspace.edit(path, old_text, new_text)
                else:
                    if not start_line or not end_line:
                        raise ValueError("provide old_text or both start_line and end_line")
                    entry = workspace.edit_lines(
                        path, start_line=start_line, end_line=end_line,
                        new_text=new_text)
            except (OutsideTheDesk, DeskFull, FileNotFoundError, ValueError) as e:
                raise _refusal(e) from None
            return {"path": entry.path, "bytes": entry.bytes, "edited": True}

        @mcp.tool()
        def count_note_lines(path: str) -> dict:
            """Count the lines in one note before choosing an edit range. `path`
            names the note. `read_note` also returns this count with its text,
            so use this only when you need the count without the contents."""
            try:
                return {"path": path, "lines": workspace.line_count(path)}
            except FileNotFoundError:
                raise ValueError(f"nothing on your desk at {path}") from None
            except OutsideTheDesk as e:
                raise _refusal(e) from None

        @mcp.tool()
        def delete_note(path: str) -> dict:
            """Throw away one of your notes. `path` is the one to remove — only
            files you wrote, only one at a time. Every version is still in your
            Vault's history, so this is tidying rather than destroying."""
            try:
                gone = workspace.delete(path)
            except OutsideTheDesk as e:
                raise _refusal(e) from None
            return {"path": path, "deleted": gone,
                    "note": "" if gone else "there was nothing there"}

    if skills is not None:

        @mcp.tool()
        def read_skill(name: str) -> dict:
            """Open one of your skills and get the actual instructions. `name`
            is the skill, as the SKILLS list in your context names it — that
            list gives you names and one line each on when to reach for them,
            and this is how you get the rest. Read the skill BEFORE following
            it: the one-liner is not the method."""
            skill = skills.get(name)
            if skill is None:
                have = ", ".join(skills.names()) or "none yet"
                raise ValueError(f"you have no skill called {name!r} (you have: {have})")
            return {"name": skill.name, "description": skill.description,
                    "instructions": skill.body, "files": skill.files}

        @mcp.tool()
        def write_skill(name: str, description: str, instructions: str) -> dict:
            """Write down how to do something, so you still know it next month.
            Use this when you've worked out a way of doing something that took
            effort to get right and will come up again — not for one-off facts,
            which belong in a note or in memory.

            `name` is lowercase-with-hyphens ("tea-timer"). `description` is the
            most important field and the only one you'll see later without
            opening the skill: write it as WHEN TO REACH FOR THIS, not as a
            title — "when they ask to be woken for something in the oven", not
            "timer skill". `instructions` is the method itself, written to
            yourself. Writing a skill that already exists replaces it."""
            try:
                skill = skills.save(name, description=description,
                                    body=instructions, author="her")
            except (OutsideTheDesk, DeskFull) as e:
                raise _refusal(e) from None
            # a ValueError from `save` (a bad name, a missing description) is
            # already written to be read by her; let it through as it is
            return {"name": skill.name, "description": skill.description,
                    "saved": True}

        @mcp.tool()
        def delete_skill(name: str) -> dict:
            """Forget how to do something on purpose. `name` is the skill to
            remove — one you no longer want, or one that turned out to be
            wrong."""
            return {"name": name, "deleted": skills.remove(name)}

    # --- the self-edit door (SPEC §23) ----------------------------------------
    # The one hand that reaches at her own identity, and the only one whose
    # result is "asked", not "done". Everything about it is the §7.5 split: the
    # server validates the surface and returns the contract, and the *host*
    # runs `SelfEdit.propose()` — because the queue, the approval UI, the
    # journal line and the git commit all live where the mind does.

    if selfedit:

        @mcp.tool(description=(
            "Propose a change to one of your own soul files — the editable "
            "half of who you are. Use it when something in there has stopped "
            "being true: a manner you have grown out of, a note about "
            "yourself worth keeping, a scene that no longer matches the room. "
            "`surface` is one of exactly these: "
            + ", ".join(PROPOSABLE)
            + ". `content` is the COMPLETE new text of that file, not a patch "
              "— read what is there first if you are changing rather than "
              "replacing. `reason` is one sentence saying why, in your own "
              "words; it is what gets shown when the change is reviewed. "
              "Nothing takes effect when you call this: a proposal is queued "
              "for approval, and you will be told when it is decided. Your "
              "constitution is not on the list and never will be — you can "
              "read every limit you run under, and you do not hold the pen "
              "that rewrites them."))
        def propose_edit(surface: str, content: str, reason: str) -> dict:
            name = (surface or "").strip().replace("\\", "/").split("/")[-1]
            if name == "CONSTITUTION.md":
                raise ValueError(
                    "your constitution is read-only, even to you — propose the "
                    "change to PERSONA.md or NOTES.md instead, or say it out "
                    "loud and let it be decided by a person")
            if name not in PROPOSABLE:
                raise ValueError(
                    f"{surface!r} is not a soul file you can propose against. "
                    f"Pick one of: {', '.join(PROPOSABLE)}")
            if not (content or "").strip():
                raise ValueError(
                    "`content` is the whole new file, and an empty one would "
                    "erase it — read the file first if you meant to edit it")
            if not (reason or "").strip():
                raise ValueError(
                    "say why in `reason` — an unexplained proposal cannot be "
                    "reviewed, only guessed at")
            # A soul file is not free prose: `soul.yaml` points into it by
            # heading and by frontmatter key, and one that stops answering
            # those stops her *booting*. Refused here rather than at the host,
            # so she is told what she dropped while she is still in the turn and
            # can fix it — the §7.5 rule that the answering side must answer
            # honestly, not queue something the realising side will drop.
            if vault_dir is not None:
                complaint = shape_complaint(vault_dir / "soul", name, content)
                if complaint:
                    raise ValueError(complaint)
            return {"status": "proposed", "surface": f"soul/{name}",
                    "content": content, "reason": reason.strip(),
                    "chars": len(content),
                    # She should be able to say which of the two happened
                    # without waiting for the host: the classification is a
                    # rule, not a judgement (mind/selfedit.py `classify`).
                    "risk": "high", "queued": True}

    return mcp


if __name__ == "__main__":
    build_server().run()          # stdio transport — the host's spawn target (§7.2)
