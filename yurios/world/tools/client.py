"""The MCP client side (SPEC §7.2) — the brain's hands, discovered not hardcoded.

`McpToolRunner` spawns the in-repo server (`python -m yurios.world.tools.server`) over
stdio and speaks real MCP to it: `initialize`, `list_tools`, `call_tool`. The
tool *directive* the model reads (SPEC §7.4) is built from the discovered
schemas, not from constants — build the capability behind MCP once and the same
client talks to any server (→ ch. 17). Point `McpToolRunner` at a different
command line and she has different hands; the brain doesn't change.

`ToolRunner` is the seam the brain actually depends on, so tests drive the tool
loop with `fakes.FakeToolRunner` and pin the *real* server's contract separately
over an in-memory session (SPEC §13) — the same split as the voice seams (B2 §3).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("world.tools")


@dataclass
class ToolSpec:
    """One discovered tool: what the directive tells the model about it."""
    name: str
    description: str
    schema: dict          # JSON schema of the arguments


class ToolRunner(Protocol):
    async def start(self) -> list[ToolSpec]:
        """Connect/spawn and return the discovered tools."""
        ...

    async def call(self, tool: str, args: dict) -> str:
        """Execute one call; return the result as text. Raises on tool error."""
        ...

    async def close(self) -> None:
        ...


class McpToolRunner:
    """A genuine MCP client over stdio (SPEC §7.2).

    The `stdio_client` and `ClientSession` contexts open anyio cancel scopes
    that MUST be entered and exited in the *same* asyncio task — spread the
    setup and teardown across two tasks (as a FastAPI startup/shutdown pair can,
    especially when a double Ctrl+C cancels the lifespan mid-`yield` and the loop
    finalizes the dangling generator on its own) and anyio raises
    "Attempted to exit cancel scope in a different task than it was entered in",
    which then jams the rest of shutdown. So the whole client lifetime lives in
    one owned task (`_serve`): it enters both contexts, discovers, then serves
    `call` requests off a queue until `close()` (or task cancellation) unwinds
    the `async with` — always in the task that opened it. Cancellation is the
    safety net: if the loop tears down without a clean `close()`, the contexts
    still exit inside `_serve`."""

    def __init__(self, command: list[str] | None = None, env: dict | None = None):
        # default: spawn the in-repo server with THIS interpreter, so the venv
        # that runs her also runs her hands
        self.command = command or [sys.executable, "-m", "yurios.world.tools.server"]
        self.env = env
        self._task: asyncio.Task | None = None
        self._requests: asyncio.Queue | None = None
        self._ready: asyncio.Event | None = None
        self._specs: list[ToolSpec] = []
        self._start_error: Exception | None = None

    async def start(self) -> list[ToolSpec]:
        self._requests = asyncio.Queue()
        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._serve(), name="mcp-client")
        await self._ready.wait()
        if self._start_error is not None:
            # surface the failure to Runtime.start_async, which degrades to
            # hand-less rather than crashing the boot
            await self.close()
            raise self._start_error
        return self._specs

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        # the SDK spawns with a sanitized default env — config vars for the
        # server (WEATHER_*, TIMER_*) must be passed explicitly, merged on top
        env = {**get_default_environment(), **(self.env or {})}
        params = StdioServerParameters(command=self.command[0],
                                       args=self.command[1:], env=env)
        try:
            async with stdio_client(params) as (read, write), \
                    ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                self._specs = [ToolSpec(t.name, t.description or "", t.inputSchema or {})
                               for t in listed.tools]
                self._ready.set()               # start() unblocks with the specs
                await self._pump(session)        # serve until close()/cancel
        except asyncio.CancelledError:
            raise                                 # unwinds the contexts, same task
        except Exception as e:                    # spawn/handshake failed
            self._start_error = e
        finally:
            self._ready.set()                     # never leave start() hanging

    async def _pump(self, session) -> None:
        """Run tool calls in the session's own task until the stop sentinel."""
        while True:
            req = await self._requests.get()
            if req is None:                       # close() sentinel
                return
            fut, tool, args = req
            if fut.cancelled():
                continue
            try:
                result = await session.call_tool(tool, args)
                text = result_text(result)
                if getattr(result, "isError", False):
                    raise RuntimeError(text or f"{tool} failed")
                fut.set_result(text)
            except Exception as e:                # a bad call fails one turn, not
                if not fut.done():                # the whole runner; CancelledError
                    fut.set_exception(e)          # (not Exception) unwinds _serve

    async def call(self, tool: str, args: dict) -> str:
        if self._task is None or self._task.done() or self._requests is None:
            raise RuntimeError("tool runner is not running")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._requests.put((fut, tool, args))
        return await fut

    async def close(self) -> None:
        if self._task is None:
            return
        if self._requests is not None:
            try:
                self._requests.put_nowait(None)   # ask _pump to unwind cleanly
            except Exception:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()                   # teardown still runs in _serve
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            log.exception("mcp client task ended badly")
        self._task = self._requests = self._ready = None


def result_text(result) -> str:
    """Flatten an MCP CallToolResult to the text the model gets back (§7.4).
    Prefers the structured payload (compact JSON) over prose content blocks."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return json.dumps(structured, separators=(",", ":"), default=str)
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def build_directive(specs: list[ToolSpec], *, user_name: str, max_calls: int) -> str:
    """The ## TOOLS system block (SPEC §7.4), built from discovery. Kept tiny and
    example-led, like B2 §6.1's expression directive: the model already has the
    persona; this only teaches the marker grammar and the lead-in rule."""
    lines = []
    for s in specs:
        props = (s.schema or {}).get("properties", {})
        required = set((s.schema or {}).get("required", []))
        args = ", ".join(n if n in required else f"{n}?" for n in props)
        desc = (s.description or "").strip().split("\n")[0]
        lines.append(f"- {s.name}({args}) — {desc}")
    tools = "\n".join(lines)
    return (
        "You have hands — real tools. Use one only when "
        f"{user_name} asks for something a tool covers.\n"
        "How: say one short natural lead-in sentence first, then end your message "
        'with the call on its own: [[tool_name {"arg": value}]] — double brackets, '
        "JSON args, nothing after it. You'll be prompted to continue once the "
        "result is back; weave it in naturally and never read JSON or mention the "
        "mechanics aloud.\n"
        f"Your tools (the only ones that exist — never invent one; at most "
        f"{max_calls} calls per reply):\n{tools}\n"
        'Example: "Mm, hold on — let me set that. [[set_timer {"minutes": 10, '
        '"label": "tea"}]]"'
    )
