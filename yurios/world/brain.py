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
import re
from typing import AsyncIterator, Optional

from yurios.app.core import assemble as asm
from yurios.characters.setting import read_place
from yurios.desktop.brain import BrainAdapter
from yurios.desktop.config import Config
from yurios.mind.workspace import DESK_WRITE_TOOLS

from ..kernel import correlate
from .avatar.controller import VrmController
from .situation import render_situation
from .tools.client import ToolRunner, ToolSpec, build_directive
from .tools.guard import Guard, Turn
from .tools.timers import TimerBoard
from .tooltags import ToolCall, ToolTagParser

log = logging.getLogger("world.brain")


class _EchoSkipper:
    """Swallow a continuation pass's re-run of what she already said (§7.4).

    A continuation is the same turn: her partial reply is in the messages and the
    cue asks her to carry on from it. A 12B model reads that as an invitation to
    start again — every live tool turn came back with its lead-in said twice
    ("Let me see... Let me see... It says here that…"), which the user hears, the
    transcript keeps and TTS speaks.

    It matches characterwise against the previous pass, holding the candidate
    rather than swallowing it as it goes. Holding is what makes it safe: a
    continuation that merely *starts* the same way ("I'll check." → "I'll check
    the other one too.") is released whole at the point it diverges, so no
    sentence is ever left beginning mid-clause.

    Whitespace and `[emotion]` tags are skipped on both sides rather than counted
    as divergence — the model re-wraps freely and re-tags freely, and a repeat
    that opened `[tender]` where the first pass opened `[neutral]` used to defeat
    the match on its very first character. Only the words have to agree.

    Only the continuation passes are matched, never the first — the turn's first
    audio still leaves before any tool runs (§7.4). The held text is at most one
    lead-in long, and once she has diverged every later token passes straight
    through untouched.
    """

    #: Whitespace and complete `[tag]` spans: skippable on the `already` side.
    _SKIP = re.compile(r"(?:\s|\[[^\[\]]{0,24}\])+")
    #: How many times over she is allowed to have repeated herself. Live, the
    #: lead-in came back three times in one continuation; bounded so a pass that
    #: genuinely reprises a phrase cannot be swallowed indefinitely.
    _MAX_WRAPS = 3

    def __init__(self, already: str):
        self.already = already
        self.held = ""             # everything held, in order, for a divergence
        #: The tail of `held` since the last word that matched — tags and spaces
        #: that belong to whatever she says NEXT, not to the echo. Released when
        #: the echo completes, so a `[happy]` opening the new sentence still
        #: reaches the face instead of being dropped with the repeat.
        self.trail = ""
        self.i = 0
        self.open = False          # she has diverged: pass everything from here
        self.wraps = 0             # echoes absorbed so far (she can repeat twice)
        self._in_tag = False       # inside an incoming '[tag]'

    def _skip(self) -> None:
        """Advance past whitespace and emotion tags in what she already said."""
        m = self._SKIP.match(self.already, self.i)
        if m is not None:
            self.i = m.end()

    def push(self, text: str) -> str:
        if self.open:
            return text
        out: list[str] = []
        for ch in text:
            if self.open:
                out.append(ch)
                continue
            if self._in_tag:                   # an incoming tag: hold, don't match
                self.held += ch
                self.trail += ch
                if ch == "]":
                    self._in_tag = False
                continue
            if ch == "[":
                self.held += ch
                self.trail += ch
                self._in_tag = True
                continue
            if ch.isspace():
                self.held += ch                # re-wrapped, not diverged
                self.trail += ch
                continue
            self._skip()                       # …and the same on the other side
            if self.i >= len(self.already):    # an echo matched all the way
                if self.wraps < self._MAX_WRAPS:
                    # …and she sometimes says it a THIRD time. Commit this echo
                    # (drop its words, keep any pending tag) and start matching
                    # again from the top, so a repeat of the repeat also goes.
                    self.wraps += 1
                    self.held = self.trail
                    self.i = 0
                    self._skip()
                else:
                    out.append(self._release(self.trail) + ch)
                    self.open = True
                    continue
            self.held += ch
            if ch == self.already[self.i]:
                self.i += 1                    # still echoing
                self.trail = ""
                continue
            # A partial match only: she is saying something new that happens to
            # open the same way. Give every held character back.
            self.open = True
            out.append(self._release(self.held))
        return "".join(out)

    def _release(self, text: str) -> str:
        """`text`, minus a gap the previous pass already ended with — once an echo
        has actually been dropped, or the two passes join on a double space."""
        if self.wraps and self.already[-1:].isspace():
            text = text.lstrip(" \t")
        self.held = self.trail = ""
        return text

    def finish(self) -> str:
        """End of the pass. The held *words* matched what she already said all the
        way to here, so releasing them would be the repeat this exists to stop —
        but a trailing tag is hers to keep."""
        if self.open:
            self.held = self.trail = ""
            return ""
        return self._release(self.trail)


class ToolBrain(BrainAdapter):
    """BrainAdapter + the in-stream MCP tool loop (SPEC §7)."""

    def __init__(self, state, cfg: Config, *, guard: Guard,
                 timers: TimerBoard, controller: VrmController,
                 selfies=None, research=None):
        super().__init__(state, cfg)
        self.guard = guard
        self.timers = timers
        self.controller = controller
        self.selfies = selfies                 # SelfieLab | None (§7.6)
        self.research = research               # Researcher | None (§7.7)
        # mind/selfedit.py's door (§23), wired by the MindLoop the same way the
        # shelf and the desk are. None means the tool was never advertised.
        self.selfedit = None
        self.runner: Optional[ToolRunner] = None
        self.world = None                      # WorldModelStore, wired by the mind
        self._directive: str = ""
        # model-verbatim record per session (markers + results), for persist():
        # the corpus should see what the model actually did, not the cleaned speech
        self._raw: dict[str, str] = {}

    @classmethod
    def build(cls, cfg, *, guard: Guard, timers: TimerBoard,
              controller: VrmController, selfies=None, research=None,
              chat_model=None, utility_model=None, embedder=None) -> "ToolBrain":
        base = BrainAdapter.build(cfg, chat_model=chat_model,
                                  utility_model=utility_model, embedder=embedder)
        return cls(base.state, base.cfg, guard=guard, timers=timers,
                   controller=controller, selfies=selfies, research=research)

    def set_tools(self, runner: Optional[ToolRunner], specs: list[ToolSpec]) -> None:
        """Wire the discovered hands (SPEC §7.2). None/empty → she has no hands
        here — never an error, the directive simply isn't appended."""
        self.runner = runner
        self._directive = build_directive(
            specs, user_name=self.cfg.user_name,
            max_calls=self.cfg.tool_max_calls_per_turn) if runner and specs else ""

    def set_selfedit(self, selfedit) -> None:
        """Wire the §23 self-edit door, so `propose_edit` has somewhere to land.

        Late-bound like `set_world` and `set_knowledge`: `SelfEdit` belongs to
        the MindLoop. The tool is only advertised where the mind runs, so an
        unwired door means the branch below is unreachable rather than lossy.
        """
        self.selfedit = selfedit

    def set_world(self, world) -> None:
        """Wire the mind's WorldModelStore (SPEC §19.2). This is the seam swap
        Build #4 promised: the block's place in the prompt doesn't move — what
        fills it stops being a rendering and becomes the store's situation()."""
        self.world = world

    def turn_context(self, *, channel: str, client_id: str | None = None,
                     session_id: str | None = None,
                     proactive: bool = False):
        """Open the turn as a unit of work (world/correlate.py).

        Transport identity for tools started by this turn — which is what this
        used to carry alone — is now one part of an `Origin` that the tool audit
        and the prompt log stamp too, so a photo that arrives minutes later is
        still joinable to the sentence that asked for it.

        `proactive` is the caller saying she started this turn (a greeting, an
        ambient line): the voice route runs those through the same pump as a
        reply, and off-turn work needs to know which it inherited.
        """
        return correlate.scope(kind=correlate.CHAT_TURN, channel=channel,
                               client_id=client_id, session_id=session_id,
                               proactive=proactive)

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
                timers=self.timers, user_name=self.cfg.user_name,
                place=read_place(self.cfg.vault_dir))
        prompt.messages[0]["content"] += (
            "\n\n## THE SITUATION RIGHT NOW\n\n" + situation)
        return soul, prompt

    # -- the ReplyBrain seam, re-streamed through the tool loop ----------------
    async def stream_reply(self, session_id: str, text: str,
                           image: str | None = None) -> AsyncIterator[str]:
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
        if image:                                  # a picture you sent (§35)
            asm.mark_picture(prompt.messages)

        self.state.sessions.append_message(
            session_id, "user", asm.note_picture(text) if image else text)
        self._pending[session_id] = _Pending(prompt, turn_index, soul)

        raw: list[str] = []
        # The image part goes on the wire only (`with_image` copies): the
        # continuation passes below carry it too, so a tool she reaches for
        # halfway through does not cost her the picture she was looking at.
        messages = asm.with_image(prompt.messages, image) if image \
            else prompt.messages
        try:
            async for token in self._stream_with_tools(messages, raw):
                yield token
        finally:
            self._raw[session_id] = "".join(raw)

    def abandon(self, session_id: str) -> None:
        """B1's rollback, plus this subclass's own half-turn state: the verbatim
        record of a turn that didn't happen must not survive to be persisted
        against the next one (§7.4)."""
        self._raw.pop(session_id, None)
        super().abandon(session_id)

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
        # Never persisted anywhere else, by design — which is exactly why the
        # prompt log has to see it, or half of what she says in a day is a line
        # in the journal with no reasoning behind it (SPEC §24.2). The caller's
        # scope decides whether this reads as ambient, compose, or a greeting.
        origin = correlate.current()
        kind = origin.kind if origin and origin.kind != correlate.CHAT_TURN \
            else correlate.AMBIENT
        said: list[str] = []
        try:
            async for token in self.state.chat.stream(
                    prompt.messages, temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_reply_tokens):
                said.append(token)
                yield token
        finally:
            if self.prompt_log is not None:
                self.prompt_log.record(
                    kind=kind, messages=prompt.messages, completion="".join(said),
                    model=self.cfg.chat_model, cue=cue,
                    template_version=prompt.template_version)

    # -- the loop of passes (SPEC §7.4) -----------------------------------------
    async def _stream_with_tools(self, messages: list[dict],
                                 raw: list[str]) -> AsyncIterator[str]:
        messages = list(messages)
        calls_made = 0
        retried = False                # one re-emit per turn for a broken marker
        prev_spoken = ""               # the last pass's speech, for the echo skip
        cap = self.cfg.tool_max_calls_per_turn
        turn = self.guard.turn()      # one dedupe scope for this reply (§7.3)
        while True:
            parser = ToolTagParser()
            spoken_this_pass: list[str] = []
            # "Continue from where you left off" is an instruction the model takes
            # as "say it again, then continue": every live tool turn came out with
            # its lead-in doubled. The cue can't be trusted to prevent it, so the
            # echo is matched and dropped here (§7.4).
            echo = _EchoSkipper(prev_spoken) if prev_spoken else None
            armed = self.runner is not None and calls_made < cap
            call: ToolCall | None = None

            stream = self.state.chat.stream(
                messages, temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_reply_tokens)
            try:
                async for token in stream:
                    raw.append(token)
                    speak, closed = parser.push(token)
                    if speak and echo is not None:
                        speak = echo.push(speak)
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

            if call is None:                       # the pass ran to completion
                tail = parser.finish()
                if echo is not None:
                    tail = echo.push(tail) + echo.finish()
                if tail:
                    spoken_this_pass.append(tail)
                    yield tail
                if parser.salvaged and armed:
                    # She closed the object and ran out of brackets. The call is
                    # whole; only the marker wasn't (tooltags.finish).
                    call = parser.salvaged[0]
                elif parser.dropped and armed and not retried:
                    # She reached for a tool and the marker was junk. Silence here
                    # is how a lost `write_note` became a note she believed she
                    # had written: the broken marker stays in `raw`, so next turn
                    # she reads it back as evidence. Say it didn't land — in the
                    # verbatim record, in the audit log, and to her — and let her
                    # write it once more.
                    retried = True
                    prev_spoken = "".join(spoken_this_pass)
                    raw.append("\n[[the call above did not parse — nothing ran]]\n")
                    self.guard.audit("(unparsed marker)", {},
                                     "dropped: malformed marker", 0.0, "")
                    messages = messages + [
                        {"role": "assistant", "content": prev_spoken},
                        {"role": "user", "content":
                            "((That tool call didn't parse, so nothing ran — no "
                            "note was saved, no tool was called. Reply with ONLY "
                            "the corrected call and no other words: don't repeat "
                            "what you just said, don't explain, don't apologise. "
                            "Exactly as the TOOLS block shows — double brackets, "
                            "the tool's name, then the JSON on ONE line with "
                            "every line break inside a string written as \\n and "
                            "every quote inside it as \\\", ending in }]] with no "
                            "space between the brackets.))"},
                    ]
                    continue
                else:
                    return

            calls_made += 1
            prev_spoken = "".join(spoken_this_pass)
            result = await self._execute(call, turn)
            raw.append(f'\n[[{call.tool} → {result}]]\n')
            # the continuation: her partial reply + the result, back to the model
            # as the SAME turn (§7.4). The partial must be in the messages or she
            # restarts the sentence.
            messages = messages + [
                {"role": "assistant", "content": prev_spoken},
                {"role": "user", "content":
                    f"(({call.tool} returned: {result}. Continue the same spoken "
                    "reply from where you left off — weave the result in "
                    "naturally, never read data formats aloud"
                    + (", and your tool budget for this turn is now spent — "
                       "finish in words" if calls_made >= cap else "")
                    + ".))"},
            ]

    async def _execute(self, call: ToolCall, turn: Turn | None = None) -> str:
        """Guard → MCP → audit → host-side realisation. Never raises: a denied or
        failed call becomes a short result string the model can speak to (§7.3)."""
        t0 = self.guard.clock.now()
        ok, reason = self.guard.check(call.tool, call.args, turn=turn)
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
        # Host realization needs the complete machine-readable contract. A
        # detailed selfie `look` can push that JSON beyond the model-facing
        # result cap; truncating first makes it invalid JSON and silently skips
        # SelfieLab.start(). Only the continuation/audit copy is bounded.
        full_text = text
        text = self.guard.truncate(full_text, tool=call.tool)
        dt = (self.guard.clock.now() - t0) * 1000
        self.guard.audit(call.tool, call.args, "ok", dt, text)
        self._realise(call, full_text)
        return text

    def realise(self, tool: str, result: str, *, extra: dict | None = None) -> None:
        """`_realise` for a caller that has a tool name and a result string
        rather than a parsed marker — which is the mind (mind/loop.py).

        The same host-side effects, deliberately: a timer she set for herself at
        3am is scheduled by the same line as a timer you asked for, and a render
        she started for a goal reaches `SelfieLab.start` the same way. `extra`
        is merged into the contract before realisation, which is how the mind
        stamps `_deliver: "vault"` and `_goal_id` onto work it started (§18,
        principle 8; §22, principle 7).
        """
        self._realise(ToolCall(tool=tool, args={}), result, extra=extra)

    def _realise(self, call: ToolCall, result: str,
                 extra: dict | None = None) -> None:
        """Host-side effects (SPEC §7.5): the server returned the contract; the
        host owns the clock and the stage, so scheduling and sound happen here."""
        try:
            data = json.loads(result)
        except ValueError:
            return
        if extra:
            # Merged before every branch below, so a stamp cannot be forgotten
            # by whichever one happens to handle the tool.
            data = {**data, **extra}
        if call.tool in DESK_WRITE_TOOLS:
            # She wrote inside the Vault, from the other process (§34.2). Say so
            # here and the next tick commits it under a message that names what
            # happened; the effect is the whole point, so it runs before the
            # elif chain rather than inside it.
            if self._on_desk_write is not None:
                self._on_desk_write(call.tool, data)
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
        elif call.tool == "propose_edit" and data.get("status") == "proposed":
            # The §7.5 split at its sharpest: the server said what she is asking
            # for, and the queue, the approval UI, the journal line and the git
            # commit all live here. She has already been told it was queued —
            # which is true, because `classify` sends every soul surface to the
            # queue and this branch cannot reach the constitution (the server
            # refuses it, `MindVault` refuses it, and `propose` refuses it).
            if self.selfedit is not None:
                try:
                    self.selfedit.propose(data["surface"], data.get("content", ""),
                                          reason=data.get("reason", ""))
                except Exception:
                    log.exception("propose_edit: couldn't queue the proposal")
        elif call.tool == "read_page" and data.get("text"):
            # The page she just read, onto the shelf (§7.7). This is the branch
            # the whole read_page contract is shaped around: `data` here is the
            # UNtruncated result, so the store gets the page while the model got
            # 400 characters of it. Fire-and-forget — _realise is sync and the
            # turn is still streaming.
            if self.research is not None:
                self.research.shelve(data)
        elif call.tool == "research" and data.get("status") == "started":
            # start-don't-await (§7.6, again): the reading happens off-turn and
            # what she found arrives in the chat as a `message` when it's done.
            if self.research is not None:
                origin = correlate.current()
                data["_channel"] = origin.channel if origin else None
                data["_client_id"] = origin.client_id if origin else None
                data["_corr_id"] = origin.corr_id if origin else None
                self.research.start(data)
        elif (call.tool in ("take_selfie", "show_picture")
                and data.get("status") == "started"):
            # start-don't-await (§7.6): the render happens off-turn; the photo
            # arrives in the chat as a `message` event when it's done.
            if self.selfies is not None:
                origin = correlate.current()
                data["_channel"] = origin.channel if origin else None
                data["_client_id"] = origin.client_id if origin else None
                # travels into generations.jsonl, so the debug page can join a
                # rendered photo back to the turn that reached for the camera
                data["_corr_id"] = origin.corr_id if origin else None
                # …and whether the photo is an answer. She reaches for the
                # camera mid-turn, but the shot lands minutes later with no
                # turn around it, and the lab used to read that gap as her
                # speaking first — so every photo you asked for arrived marked
                # as one she volunteered (§15.5).
                data["_proactive"] = not correlate.answering()
                self.selfies.start(data)
