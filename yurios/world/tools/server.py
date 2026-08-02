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

from mcp.server.fastmcp import FastMCP

from .weather import FakeWeather, OpenMeteoProvider, WeatherProvider

MUSIC_TRACKS = ("warm_pad", "night_piano")


def build_server(*, weather: WeatherProvider | None = None,
                 max_minutes: float | None = None,
                 default_city: str | None = None,
                 selfies: bool | None = None) -> FastMCP:
    """Build the FastMCP server. Args are the test seams; `python -m` reads env."""
    max_minutes = max_minutes if max_minutes is not None else float(
        os.environ.get("TIMER_MAX_MINUTES", "180"))
    default_city = default_city or os.environ.get("WEATHER_CITY", "Tokyo")
    if weather is None:
        weather = (FakeWeather() if os.environ.get("WEATHER_BACKEND") == "fake"
                   else OpenMeteoProvider())
    if selfies is None:                        # off = not advertised at all (§7.6)
        selfies = os.environ.get("SELFIE_ENABLED", "1") != "0"

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

    @mcp.tool()
    def play_music(action: str, track: str = "warm_pad", volume: float = 0.4) -> dict:
        """Start or stop the room's ambient music. `action` is "play" or "stop";
        `track` is one of: warm_pad, night_piano; `volume` is 0..1."""
        if action not in ("play", "stop"):
            raise ValueError('action must be "play" or "stop"')
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
        from ..selfies import FORGE_DIR
        from yurios.forge import SelfieBook
        overlays = [os.environ["SELFIE_TEMPLATES_EXTRA"]] \
            if os.environ.get("SELFIE_TEMPLATES_EXTRA") else []
        book = SelfieBook.load(FORGE_DIR / "templates" / "selfie.yaml",
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
                    "status": "started",
                    "note": "the photo will appear in the chat shortly — "
                            "no need to wait for it, and no need to ask again"}

    return mcp


if __name__ == "__main__":
    build_server().run()          # stdio transport — the host's spawn target (§7.2)
