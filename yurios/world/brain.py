"""ToolBrain (SPEC §2.3, §7.4) — the Build #2 brain adapter, given hands.

This subclass is the whole story of "property 3a" in one file. It changes exactly
one behaviour — how a reply streams — and adds exactly one prompt block: the
situation (SPEC §2.5), so every prompt knows the time, her body, the room, and
the timers she has running. Everything else — prompt assembly, recall, the
greeting, the partner model, the corpus line, one-commit-per-turn — is the
`desktop.brain.BrainAdapter`, called, not copied.

The reply path becomes a loop of model passes:

    pass 1:  …ordinary tokens stream to the voice loop…  [[set_timer {…}]]
             └─ the lead-in sentence is already at TTS: first audio never
                waits on a tool (SPEC §7.4; the §1 budget holds)
    execute: guard → MCP call → audit line → host-side realisation (§7.5)
    pass 2:  messages + her partial reply + ((tool result…)) cue → she keeps
             talking, now knowing what her hands found — the same turn, the
             same OutEvent stream, the same barge-in cancel.

Build #1's provider seam (text tokens in/out, B1 §3.1) is untouched: the call
protocol is *in the stream*, the same discipline as B2 §6's emotion tags. That
is the price of keeping the brain byte-identical — and the seam where
native function-calling could later slot in behind the same ToolRunner.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

from yurios.desktop.brain import BrainAdapter
from yurios.desktop.config import Config

from .avatar.controller import VrmController
from .situation import render_situation
from .tools.client import ToolRunner, ToolSpec, build_directive
from .tools.guard import Guard
from .tools.timers import TimerBoard
from .tooltags import ToolCall, ToolTagParser

log = logging.getLogger("world.brain")


class ToolBrain(BrainAdapter):
    """BrainAdapter + the in-stream MCP tool loop (SPEC §7)."""

    def __init__(self, state, cfg: Config, *, guard: Guard,
                 timers: TimerBoard, controller: VrmController,
                 selfies=None):
        super().__init__(state, cfg)
        self.guard = guard
        self.timers = timers
        self.controller = controller
        self.selfies = selfies                 # SelfieLab | None (§7.6)
        self.runner: Optional[ToolRunner] = None
        self.world = None                      # WorldModelStore, wired by the mind
        self._directive: str = ""
        # model-verbatim record per session (markers + results), for persist():
        # the corpus should see what the model actually did, not the cleaned speech
        self._raw: dict[str, str] = {}

    @classmethod
    def build(cls, cfg, *, guard: Guard, timers: TimerBoard,
              controller: VrmController, selfies=None, chat_model=None,
              utility_model=None, embedder=None) -> "ToolBrain":
        base = BrainAdapter.build(cfg, chat_model=chat_model,
                                  utility_model=utility_model, embedder=embedder)
        return cls(base.state, base.cfg, guard=guard, timers=timers,
                   controller=controller, selfies=selfies)

    def set_tools(self, runner: Optional[ToolRunner], specs: list[ToolSpec]) -> None:
        """Wire the discovered hands (SPEC §7.2). None/empty → she has no hands
        here — never an error, the directive simply isn't appended."""
        self.runner = runner
        self._directive = build_directive(
            specs, user_name=self.cfg.user_name,
            max_calls=self.cfg.tool_max_calls_per_turn) if runner and specs else ""

    def set_world(self, world) -> None:
        """Wire the mind's WorldModelStore (SPEC §19.2). This is the seam swap
        Build #4 promised: the block's place in the prompt doesn't move — what
        fills it stops being a rendering and becomes the store's situation()."""
        self.world = world

    # -- prompt assembly: the blocks + the situation (SPEC §19.2) ------
    def _assemble(self, session_id: str, text: str, *, window: list[dict],
                  lore) -> object:
        """B2's assembly, then the present tense appended — so every prompt
        (reply, greeting, ambient self-talk) knows when and where she is. With
        the mind running, the block is the world model's live stage (presence,
        threads, expectations included); mindless, it degrades to Build #4's
        rendering of host state. The clock is the guard's injected one, never
        the wall clock."""
        soul, prompt = super()._assemble(session_id, text, window=window, lore=lore)
        if self.world is not None:
            situation = self.world.situation()
        else:
            situation = render_situation(
                self.guard.clock, controller=self.controller,
                timers=self.timers, user_name=self.cfg.user_name)
        prompt.messages[0]["content"] += (
            "\n\n## THE SITUATION RIGHT NOW\n\n" + situation)
        return soul, prompt

    # -- the ReplyBrain seam, re-streamed through the tool loop ----------------
    async def stream_reply(self, session_id: str, text: str) -> AsyncIterator[str]:
        # bookkeeping mirrors the BrainAdapter.stream_reply line for line
        # (B2 §2.2 — the base body streams directly, so the override restates it)
        from yurios.desktop.brain import _Pending
        turn_index = self.state.sessions.get(session_id)["turn_count"]
        soul, prompt = self._assemble(
            session_id, text,
            window=self.state.sessions.window(session_id, self.cfg.raw_window_turns),
            lore=self.state.soul_loader.load().lorebook_hits(text))
        if self._directive:                        # the tools directive (§7.4); the
                                                   # situation block rides _assemble (§2.5)
            prompt.messages[0]["content"] += f"\n\n## TOOLS\n\n{self._directive}"

        self.state.sessions.append_message(session_id, "user", text)
        self._pending[session_id] = _Pending(prompt, turn_index, soul)

        raw: list[str] = []
        try:
            async for token in self._stream_with_tools(prompt.messages, raw):
                yield token
        finally:
            self._raw[session_id] = "".join(raw)

    async def persist(self, session_id: str, user_text: str, reply: str) -> None:
        """B1's post-turn pipeline, but the corpus gets the model-verbatim record
        — markers and tool results included — so the training log reflects what
        actually happened in the turn, not just what was spoken (§7.4)."""
        raw = self._raw.pop(session_id, None)
        await super().persist(session_id, user_text, raw or reply)

    # -- ambient speech (SPEC §8.3): the greeting pattern, with any cue ---------
    async def stream_ambient(self, session_id: str, cue: str) -> AsyncIterator[str]:
        """Self-talk / timer announcements. Self-contained like stream_greeting
        (B2 §7): window=[], the cue never enters the transcript, never persisted."""
        _soul, prompt = self._assemble(session_id, cue, window=[], lore=[])
        async for token in self.state.chat.stream(
                prompt.messages, temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_reply_tokens):
            yield token

    # -- the loop of passes (SPEC §7.4) -----------------------------------------
    async def _stream_with_tools(self, messages: list[dict],
                                 raw: list[str]) -> AsyncIterator[str]:
        messages = list(messages)
        calls_made = 0
        cap = self.cfg.tool_max_calls_per_turn
        while True:
            parser = ToolTagParser()
            spoken_this_pass: list[str] = []
            armed = self.runner is not None and calls_made < cap
            call: ToolCall | None = None

            stream = self.state.chat.stream(
                messages, temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_reply_tokens)
            try:
                async for token in stream:
                    raw.append(token)
                    speak, closed = parser.push(token)
                    if speak:
                        spoken_this_pass.append(speak)
                        yield speak
                    if closed and armed:
                        call = closed[0]           # first closed marker ends the pass
                        break
                    for extra in closed:           # markers past the cap: denied, dropped
                        self.guard.audit(extra.tool, extra.args,
                                         "denied: per-turn cap", 0.0, "")
            finally:
                await stream.aclose()

            if call is None:                       # pass ran to completion — turn done
                tail = parser.finish()
                if tail:
                    yield tail
                return

            calls_made += 1
            result = await self._execute(call)
            raw.append(f'\n[[{call.tool} → {result}]]\n')
            # the continuation: her partial reply + the result, back to the model
            # as the SAME turn (§7.4). The partial must be in the messages or she
            # restarts the sentence.
            messages = messages + [
                {"role": "assistant", "content": "".join(spoken_this_pass)},
                {"role": "user", "content":
                    f"(({call.tool} returned: {result}. Continue the same spoken "
                    "reply from where you left off — weave the result in "
                    "naturally, never read data formats aloud"
                    + (", and your tool budget for this turn is now spent — "
                       "finish in words" if calls_made >= cap else "")
                    + ".))"},
            ]

    async def _execute(self, call: ToolCall) -> str:
        """Guard → MCP → audit → host-side realisation. Never raises: a denied or
        failed call becomes a short result string the model can speak to (§7.3)."""
        t0 = self.guard.clock.now()
        ok, reason = self.guard.check(call.tool)
        if not ok:
            self.guard.audit(call.tool, call.args, f"denied: {reason}", 0.0, "")
            return f"denied ({reason})"
        try:
            text = await asyncio.wait_for(
                self.runner.call(call.tool, call.args),
                timeout=self.cfg.tool_timeout_s)
        except Exception as e:                     # timeout, tool error, transport
            dt = (self.guard.clock.now() - t0) * 1000
            self.guard.audit(call.tool, call.args, "error", dt, str(e))
            return f"error ({e})"
        text = self.guard.truncate(text)
        dt = (self.guard.clock.now() - t0) * 1000
        self.guard.audit(call.tool, call.args, "ok", dt, text)
        self._realise(call, text)
        return text

    def _realise(self, call: ToolCall, result: str) -> None:
        """Host-side effects (SPEC §7.5): the server returned the contract; the
        host owns the clock and the stage, so scheduling and sound happen here."""
        try:
            data = json.loads(result)
        except ValueError:
            return
        if call.tool == "set_timer" and "seconds" in data:
            self.timers.add(id=data.get("id", "t"),
                            label=data.get("label", "your timer"),
                            seconds=float(data["seconds"]))
        elif call.tool == "play_music":
            if data.get("playing"):
                self.controller.music("play", track=data.get("track"),
                                      volume=data.get("volume"))
            else:
                self.controller.music("stop")
        elif call.tool == "take_selfie" and data.get("status") == "started":
            # start-don't-await (§7.6): the render happens off-turn; the photo
            # arrives in the chat as a `message` event when it's done.
            if self.selfies is not None:
                self.selfies.start(data)
