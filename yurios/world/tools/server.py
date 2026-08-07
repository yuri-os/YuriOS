"""The in-repo MCP server (SPEC §7.1–§7.2) — her three hands, as a real server.

Run standalone (`python -m yurios.world.tools.server`) it speaks MCP over stdio; the
host spawns it exactly that way (client.py). Tests connect to the same server
object over an in-memory session — the contract is identical (SPEC §13).

The server is the *contract and audit point*: it validates arguments and returns
the structured result. Side effects that need her body or her voice — actually
scheduling the timer's announcement, actually starting the ambience — happen on
the **host** after the call returns (SPEC §7.5), because only the host owns the
stage and the clock. That split is not an implementation shortcut; it is the
shape Build #5 keeps when these same tools move behind a broker (→ ch. 19).
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Literal, get_args

from mcp.server.fastmcp import FastMCP

from .fetch import PageFetcher, build_fetcher, gist
from .search import SearchProvider, build_provider
from .weather import FakeWeather, OpenMeteoProvider, WeatherProvider

# The catalog lives in the *type*, not in prose: an annotated Literal becomes an
# `enum` in the tool's JSON schema, which is the only form of the list a model
# reliably obeys. A description that merely names the tracks is a suggestion —
# she invented `ambient_rain_lullaby` against one. Everything else derives from
# here, so the schema, the docstring and the error can never disagree.
MusicAction = Literal["play", "stop"]
MusicTrack = Literal["warm_pad", "night_piano"]
MUSIC_ACTIONS: tuple[str, ...] = get_args(MusicAction)
MUSIC_TRACKS: tuple[str, ...] = get_args(MusicTrack)


def build_server(*, weather: WeatherProvider | None = None,
                 max_minutes: float | None = None,
                 default_city: str | None = None,
                 selfies: bool | None = None,
                 search: SearchProvider | None = None,
                 fetcher: PageFetcher | None = None,
                 results: int | None = None,
                 max_pages: int | None = None) -> FastMCP:
    """Build the FastMCP server. Args are the test seams; `python -m` reads env."""
    max_minutes = max_minutes if max_minutes is not None else float(
        os.environ.get("TIMER_MAX_MINUTES", "180"))
    default_city = default_city or os.environ.get("WEATHER_CITY", "Tokyo")
    if weather is None:
        weather = (FakeWeather() if os.environ.get("WEATHER_BACKEND") == "fake"
                   else OpenMeteoProvider())
    if selfies is None:                        # off = not advertised at all (§7.6)
        selfies = os.environ.get("SELFIE_ENABLED", "1") != "0"
    results = results if results is not None else int(
        os.environ.get("SEARCH_RESULTS", "5"))
    max_pages = max_pages if max_pages is not None else int(
        os.environ.get("RESEARCH_MAX_PAGES", "5"))
    # The web hands go together or not at all (§7.7): searching with no way to
    # read what you found is half a capability, and `research` is the two of
    # them in sequence. SEARCH_BACKEND=off is the SELFIE_BACKEND=off rule —
    # `list_tools` simply doesn't mention them.
    backend = os.environ.get("SEARCH_BACKEND", "off")
    if search is None:
        search = build_provider(
            backend, base_url=os.environ.get("SEARXNG_URL", "http://localhost:8080"),
            language=os.environ.get("SEARCH_LANGUAGE", "en"),
            safesearch=int(os.environ.get("SEARCH_SAFESEARCH", "1")))
    if fetcher is None and search is not None:
        fetcher = build_fetcher(
            "fake" if backend == "fake" else "http",
            timeout=float(os.environ.get("FETCH_TIMEOUT_S", "8")),
            max_bytes=int(os.environ.get("FETCH_MAX_BYTES", "2000000")))

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

    @mcp.tool()
    async def get_weather(city: str = "") -> dict:
        """Look up the current weather. `city` defaults to the configured city."""
        return await weather.current(city or default_city)

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

        @mcp.tool()
        async def research(topic: str, depth: int = 3) -> dict:
            """Go and find out about something properly — several searches'
            worth of reading, shelved so you keep it. Use this instead of
            `web_search` when the answer is going to take more than one page:
            "what's the current state of X", "read up on Y for me". `topic` is
            the whole thing you want to know, in one line and in your own words
            — put every angle you care about in there, because it is the only
            place they fit. `depth` is a plain NUMBER and nothing else: how many
            pages to read, 1 to 5. Leave it out unless you want it quick (1) or
            thorough (5). It takes a while, so it happens in the background:
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
                    "depth": max(1, min(int(depth or 3), max_pages)),
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
        overlays = [os.environ["SELFIE_TEMPLATES_EXTRA"]] \
            if os.environ.get("SELFIE_TEMPLATES_EXTRA") else []
        # …base included: a character with her own library (SELFIE_TEMPLATES,
        # → characters/selfiebook.py) replaces the shipped book, and the tool
        # description has to name *her* rows or she is offered scenes her
        # camera would never compose.
        book = SelfieBook.load(book_path(os.environ.get("SELFIE_TEMPLATES")),
                               overlays=overlays)
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

    return mcp


if __name__ == "__main__":
    build_server().run()          # stdio transport — the host's spawn target (§7.2)
