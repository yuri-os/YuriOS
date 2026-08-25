"""The MCP client side (SPEC §7.2) — the brain's hands, discovered not hardcoded.

`McpToolRunner` spawns the in-repo server (`python -m yurios.world.tools.server`) over
stdio and speaks real MCP to it: `initialize`, `list_tools`, `call_tool`. The
tool *directive* the model reads (SPEC §7.4) is built from the discovered
schemas, not from constants — build the capability behind MCP once and the same
client talks to any server (→ ch. 17). Point `McpToolRunner` at a different
command line and she has different hands; the brain doesn't change.

`ToolRunner` is the seam the brain actually depends on, so tests drive the tool
loop with `fakes.FakeToolRunner` and pin the *real* server's contract separately
over an in-memory session (SPEC §27) — the same split as the voice seams (B2 §3).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Protocol

log = logging.getLogger("world.tools")


@dataclass
class ToolSpec:
    """One discovered tool: what the directive tells the model about it."""
    name: str
    description: str
    schema: dict          # JSON schema of the arguments


def _as_number(value, kind: str):
    """`value` as an int/number, or None if it plainly isn't one."""
    if isinstance(value, bool):                   # bools are ints in Python; a
        return None                               # model meaning True didn't mean 1
    if isinstance(value, (int, float)):
        return int(value) if kind == "integer" else float(value)
    if isinstance(value, str):
        try:
            return int(value.strip()) if kind == "integer" else float(value.strip())
        except ValueError:
            return None
    return None


def coerce_args(args: dict, schema: dict) -> dict:
    """Fit the model's arguments to the tool's declared schema (SPEC §7.4).

    A 12B model writing JSON mid-sentence gets a scalar wrong sometimes — a
    quoted number, or an *optional* argument filled with the prose it had no
    slot for. The schema is strict, so the server answers with a pydantic
    validation error and the entire call is lost, including the required
    arguments it got right. That trade is backwards: an optional argument
    exists precisely because the tool has a sensible answer without it.

    So: coerce what is coercible, drop an **optional** argument that cannot be
    made to fit, and leave a **required** one alone — dropping that would turn a
    precise "depth must be an integer" into a vaguer "field required", and the
    tool's own error is the better one to show. Unknown keys are left for the
    server to judge; it knows its schema better than this does.
    """
    props = (schema or {}).get("properties") or {}
    if not props:
        return args
    required = set((schema or {}).get("required") or [])
    out = {}
    for key, value in (args or {}).items():
        rule = props.get(key)
        kind = rule.get("type") if isinstance(rule, dict) else None
        # `anyOf`/`oneOf`/no declared type: not ours to second-guess.
        if not isinstance(kind, str) or value is None:
            out[key] = value
            continue
        fixed = value
        if kind in ("integer", "number"):
            fixed = _as_number(value, kind)
        elif kind == "boolean" and isinstance(value, str):
            low = value.strip().lower()
            fixed = True if low == "true" else False if low == "false" else None
        elif kind == "string" and isinstance(value, (int, float, bool)):
            fixed = str(value)
        if fixed is None and value is not None:
            if key in required:
                out[key] = value                  # let the tool say what's wrong
            else:
                log.info("dropped optional arg %s=%r — the schema wants type "
                         "%s; using the tool's default", key, value, kind)
            continue
        out[key] = fixed
    return out


def start_failure(err: BaseException) -> str:
    """The one-line "why" for a spawn that didn't come up, with the TaskGroups peeled off.

    A server that dies on startup — a bad import, a missing interpreter — reaches us
    through two nested anyio task groups, and `str(ExceptionGroup)` is "unhandled errors
    in a TaskGroup (1 sub-exception)": true, useless, and the only thing the boot log
    would otherwise carry. The leaves are the sentence worth printing. Keep every leaf
    (a group can hold more than one) and name its type, since the leaf message alone is
    often just "Connection closed" — the *server's* traceback goes to stderr above,
    which is the other half of the story."""
    def leaves(e: BaseException):
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                yield from leaves(sub)
        else:
            yield e
    return "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves(err))


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
        # server (TIMER_*, SEARCH_*) must be passed explicitly, merged on top
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
                spec = next((s for s in self._specs if s.name == tool), None)
                result = await session.call_tool(
                    tool, coerce_args(args, spec.schema) if spec else args)
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


class MultiToolRunner:
    """N servers behind the one `ToolRunner` seam (SPEC §7.2).

    The docstring above promised that pointing the client at a different command
    line gives her different hands without the brain changing. This is that
    promise with the count relaxed: her own in-repo server, plus whatever else
    is named in `mcp-servers.json`, discovered the same way and reachable
    through the same `call`. The brain still sees one runner and one flat list
    of tools, which is what keeps `_realise` and the §7.4 directive unchanged.

    Two rules, both chosen to fail in the direction of "she keeps talking":

    * **A server that won't start is skipped, not fatal.** Runtime.start_async
      already degrades the *whole* runner to hand-less rather than crashing the
      boot; doing anything less granular here would mean a typo in a
      third-party entry costs her the timers and the camera too.
    * **On a name collision the first server wins.** Names stay unprefixed
      because the model reads them, the audit log records them and
      `ToolBrain._realise` dispatches on them — a `fetch__read_page` would be
      correct and useless. The in-repo server is mounted first, so her own
      hands can never be shadowed by somebody else's.
    """

    def __init__(self, children: Sequence[tuple[str, object]]):
        #: [(server name, runner)] in mount order — hers first.
        self.children = list(children)
        self.started: list[tuple[str, object]] = []
        self.failures: dict[str, str] = {}
        self._owner: dict[str, object] = {}
        self._specs: list[ToolSpec] = []

    async def start(self) -> list[ToolSpec]:
        results = await asyncio.gather(
            *(child.start() for _name, child in self.children),
            return_exceptions=True)
        for (name, child), outcome in zip(self.children, results):
            if isinstance(outcome, BaseException):
                why = start_failure(outcome)
                log.warning("mcp server %r didn't start — skipping it: %s",
                            name, why)
                self.failures[name] = why
                try:
                    await child.close()        # unwind whatever did come up
                except Exception:
                    log.debug("closing failed server %r also failed", name,
                              exc_info=True)
                continue
            self.started.append((name, child))
            for spec in outcome:
                if spec.name in self._owner:
                    log.warning("mcp server %r also offers %r, which %r already "
                                "provides — keeping the first", name, spec.name,
                                self._name_of(self._owner[spec.name]))
                    continue
                self._owner[spec.name] = child
                self._specs.append(spec)
        return list(self._specs)

    def _name_of(self, child) -> str:
        for name, candidate in self.children:
            if candidate is child:
                return name
        return "?"

    def server_of(self, tool: str) -> str:
        """Which server advertises `tool` — for the audit trail and the docs."""
        child = self._owner.get(tool)
        return self._name_of(child) if child is not None else ""

    async def call(self, tool: str, args: dict) -> str:
        child = self._owner.get(tool)
        if child is None:
            # The guard's allowlist is built from discovery, so this is close to
            # unreachable — but a server that dies mid-session gets here, and
            # "no server offers it" is a better turn than an AttributeError.
            raise RuntimeError(f"no server offers {tool}")
        return await child.call(tool, args)

    async def close(self) -> None:
        await asyncio.gather(*(child.close() for _n, child in self.started),
                             return_exceptions=True)
        self.started.clear()
        self._owner.clear()


def load_servers(path: str | Path) -> list[dict]:
    """Read `mcp-servers.json` — the `{"mcpServers": {…}}` shape everything else
    uses, so a config you already have pastes straight in.

        {"mcpServers": {"fetch": {"command": "uvx",
                                  "args": ["mcp-server-fetch"],
                                  "env": {}, "rate": 4}}}

    `rate` is the one addition: calls per minute for every tool that server
    offers (§7.3's bucket). Absent, they get `TOOL_RATE_EXTERNAL`.

    A missing file is not an error — it is the default configuration. A file
    that *is* there and won't parse is, because silently running with no
    third-party hands after you wrote some down is the worse failure.
    """
    path = Path(path)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = []
    for name, entry in (data.get("mcpServers") or {}).items():
        command = entry.get("command")
        if not command:
            raise ValueError(f"mcp server {name!r} has no command")
        servers.append({
            "name": name,
            "command": [command, *(entry.get("args") or [])],
            "env": {str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            "rate": entry.get("rate"),
        })
    return servers


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


#: How much of one tool's description reaches the directive.
#:
#: This is a bound on a *stranger's* server (§7.2), not a budget for ours. Every
#: first-party description is hand-written to fit under it whole — `take_selfie`,
#: the longest at ~1.4k, is long because teaching her to write a photograph in
#: her own words takes that many words, and clipping it is how you get a camera
#: she uses timidly. Keep it above the longest tool in `tools/server.py`; a
#: mounted third-party server that ships three paragraphs of schema notes is the
#: only thing this should ever cut.
DESC_MAX_CHARS = 1500


def one_line(description: str, limit: int = DESC_MAX_CHARS) -> str:
    """A tool description as a single directive line.

    This unwraps rather than truncates, and the difference is the whole point. A
    docstring is *wrapped prose* — the newlines are typography, not structure —
    so taking the text up to the first one keeps a fragment that stops mid-clause
    and silently discards every sentence that says when to reach for the tool.
    A description whose second sentence is "use this instead of `web_search`
    when the answer will take more than one page" is a tool she never picks.
    """
    text = " ".join((description or "").split())
    if len(text) <= limit:
        return text
    # Cut at a word boundary and say that it was cut, so a clipped description
    # reads as clipped rather than as a sentence that happens to end oddly.
    return text[:limit].rsplit(" ", 1)[0] + " …"


def build_directive(specs: list[ToolSpec], *, user_name: str, max_calls: int) -> str:
    """The ## TOOLS system block (SPEC §7.4), built from discovery. Kept tiny and
    example-led, like B2 §6.1's expression directive: the model already has the
    persona; this only teaches the marker grammar and the lead-in rule."""
    lines = []
    for s in specs:
        props = (s.schema or {}).get("properties", {})
        required = set((s.schema or {}).get("required", []))
        args = ", ".join(n if n in required else f"{n}?" for n in props)
        lines.append(f"- {s.name}({args}) — {one_line(s.description)}")
    tools = "\n".join(lines)
    return (
        "You have hands — real tools. Use one only when "
        f"{user_name} asks for something a tool covers.\n"
        "How: say one short natural lead-in sentence first, then end your message "
        "with the call on its own — double brackets, the tool's own name, JSON "
        "args, nothing after it, exactly like the example at the end of this "
        "block. Close it with two brackets together and NO space between them: "
        "`}]]`, never `}] ]`. Keep the JSON on one line — a line break inside a "
        "string is `\\n` and a quote inside one is `\\\"`. You'll be prompted to "
        "continue once the result is back; weave it in naturally and never read "
        "JSON or mention the mechanics aloud.\n"
        f"Your tools (the only ones that exist — never invent one; at most "
        f"{max_calls} calls per reply):\n{tools}\n"
        'Example: "Mm, hold on — let me set that. [[set_timer {"minutes": 10, '
        '"label": "tea"}]]"'
    )
