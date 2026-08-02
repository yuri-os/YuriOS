"""A deterministic ToolRunner (SPEC §13) — the tool loop's offline stand-in.

Same role as the voice fakes (B2 §3): the loop's behaviour — parsing, guarding,
continuation, cancellation — is what the tests pin; no server, no subprocess.
The real server's *contract* is pinned separately over an in-memory MCP session
(tests/test_mcp_contract.py).
"""
from __future__ import annotations

import json

from .client import ToolSpec

SPECS = [
    ToolSpec("set_timer", "Set a countdown timer.",
             {"properties": {"minutes": {"type": "number"},
                             "label": {"type": "string"}},
              "required": ["minutes"]}),
    ToolSpec("play_music", "Start or stop the room's ambient music.",
             {"properties": {"action": {"type": "string"},
                             "track": {"type": "string"},
                             "volume": {"type": "number"}},
              "required": ["action"]}),
    ToolSpec("get_weather", "Look up the current weather.",
             {"properties": {"city": {"type": "string"}}, "required": []}),
    ToolSpec("take_selfie", "Take a photo of yourself to share in the chat. "
             "`look` describes the picture in your own words; the rest are "
             "optional shorthands.",
             {"properties": {"look": {"type": "string"},
                             "scene": {"type": "string"},
                             "mood": {"type": "string"},
                             "wardrobe": {"type": "string"},
                             "framing": {"type": "string"},
                             "lighting": {"type": "string"},
                             "avoid": {"type": "string"}},
              "required": []}),
    ToolSpec("show_picture", "Share a picture of something that isn't you — "
             "what you're looking at, a place, a thing you're describing.",
             {"properties": {"subject": {"type": "string"},
                             "avoid": {"type": "string"}},
              "required": ["subject"]}),
]


class FakeToolRunner:
    """Scripted results + a call log the tests read."""

    def __init__(self, results: dict[str, object] | None = None,
                 errors: dict[str, str] | None = None):
        self.results = results or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dict]] = []
        self.started = False
        self.closed = False

    async def start(self) -> list[ToolSpec]:
        self.started = True
        return list(SPECS)

    async def call(self, tool: str, args: dict) -> str:
        self.calls.append((tool, dict(args)))
        if tool in self.errors:
            raise RuntimeError(self.errors[tool])
        if tool in self.results:
            r = self.results[tool]
            return r if isinstance(r, str) else json.dumps(r)
        if tool == "set_timer":
            return json.dumps({"id": f"fake{len(self.calls)}",
                               "label": args.get("label") or "your timer",
                               "seconds": round(float(args.get("minutes", 1)) * 60),
                               "due": 0})
        if tool == "play_music":
            return json.dumps({"playing": args.get("action") == "play",
                               "track": args.get("track", "warm_pad"),
                               "volume": args.get("volume", 0.4)})
        if tool == "get_weather":
            return json.dumps({"city": args.get("city") or "Tokyo",
                               "temp_c": 17.0, "condition": "raining",
                               "wind_kmh": 9.0})
        if tool == "take_selfie":
            return json.dumps({"id": f"fake{len(self.calls)}",
                               "look": args.get("look") or None,
                               "scene": args.get("scene") or None,
                               "mood": args.get("mood") or None,
                               "wardrobe": args.get("wardrobe") or None,
                               "framing": args.get("framing") or None,
                               "lighting": args.get("lighting") or None,
                               "avoid": args.get("avoid") or None,
                               "kind": "selfie", "status": "started",
                               "note": "the photo will appear in the chat shortly"})
        if tool == "show_picture":
            return json.dumps({"id": f"fake{len(self.calls)}",
                               "subject": (args.get("subject") or "").strip(),
                               "avoid": args.get("avoid") or None,
                               "kind": "picture", "status": "started",
                               "note": "the picture will appear in the chat shortly"})
        raise RuntimeError(f"unknown tool: {tool}")

    async def close(self) -> None:
        self.closed = True
