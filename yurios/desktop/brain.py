"""The brain seam (SPEC §2, §3.5) — Build #1, reused unchanged, wired for voice.

This is where "the brain is Build #1's loop" becomes literal. Nothing in the
`app` package is copied or edited: `create_app()` builds the *exact* AppState you
read in ch. 31 (SoulLoader, FileMemoryStore, the LiteLLM/Ollama providers, the
CorpusLogger, the Vault-git spine), and this adapter drives it.

Only two things differ from Build #1's HTTP chat route:
  1. The reply is streamed to the voice loop, not an SSE response.
  2. One extra system block asks the model for inline expression tags (§6) — the
     single prompt change the whole avatar layer rests on.

Everything else — assemble(), recall(), the honesty constraint, remember(),
summarise(), the one-commit-per-turn discipline, the corpus line — is called,
not reimplemented. `persist()` is literally Build #1's `post_turn`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

# The brain (yurios/app, originally Build #1) — the exact code book ch. 31 walks through.
from yurios.app.core import assemble as asm
from yurios.app.corpus import CorpusLogger  # noqa: F401 (documents the reused surface)
from yurios.app.main import AppState, create_app
from yurios.app.memory.store import Record
from yurios.app.routes.chat import post_turn

from .config import Config
from .voice.emotion import EXPRESSION_DIRECTIVE, SPOKEN_STYLE_DIRECTIVE

log = logging.getLogger("desktop.brain")

# She speaks first (SPEC §7) — the continuity opener, reusing Build #1's cue idea.
GREET_CUE = ("(({user} just opened the sanctuary and put their headset on — no "
             "words yet; you speak first. One short, warm spoken greeting in your "
             "own voice that surfaces something {user} told you before. Lead with "
             "an expression tag.))")


@dataclass
class _Pending:
    prompt: object
    turn_index: int
    soul: object


class BrainAdapter:
    """Implements the ReplyBrain Protocol over Build #1's AppState."""

    def __init__(self, state: AppState, cfg: Config):
        self.state = state
        self.cfg = cfg
        self._pending: dict[str, _Pending] = {}

    # -- construction: build the Build #1 brain from the sibling package --------
    @classmethod
    def build(cls, cfg: Config, *, chat_model=None, utility_model=None,
              embedder=None) -> "BrainAdapter":
        # create_app() runs the whole §14 wiring (incl. the "no Vault?" guard) and
        # leaves the AppState on app.state.mvw. We take the state and drop the app.
        # The injected-model params are Build #1's own test seam (§13.3) — the
        # integration test passes fakes so the whole path runs with no model.
        brain_app = create_app(cfg, chat_model=chat_model,
                               utility_model=utility_model, embedder=embedder)
        return cls(brain_app.state.mvw, cfg)

    def resolve_session(self, session_id: str | None) -> str:
        """Return a live session id: reuse the client's if valid + known, else mint
        one (Build #1's ids are server-issued 32-hex, → app/sessions.py)."""
        if session_id and self.state.sessions.get(session_id) is not None:
            return session_id
        return self.state.sessions.create()

    def _assemble(self, session_id: str, text: str, *, window: list[dict],
                  lore) -> object:
        """One assembled prompt (Build #1) + the Build #2 expression block (§6)."""
        soul = self.state.soul_loader.load()                  # read every turn (§5)
        prompt = asm.assemble(
            soul,
            user_md=self.state.store.read_user_md(),
            summary=self.state.store.read_summary(),
            memories=self.state.store.recall(text, self.cfg.retrieval_k),
            lore=lore,
            window=window,
            user_msg=text,
            user_name=self.cfg.user_name,
            system_budget_tokens=self.cfg.system_budget_tokens,
            lorebook_budget_tokens=self.cfg.lorebook_budget_tokens)
        # the two prompt changes Build #2 makes (§6): tell the model this is a
        # spoken (not written) exchange — no narration — and ask for inline
        # expression tags. Both are voice-only; Build #1's text chat keeps neither.
        prompt.messages[0]["content"] += (
            f"\n\n## VOICE\n\n{SPOKEN_STYLE_DIRECTIVE}"
            f"\n\n## EXPRESSION\n\n{EXPRESSION_DIRECTIVE}")
        return soul, prompt

    # -- the ReplyBrain seam ----------------------------------------------------
    async def stream_reply(self, session_id: str, text: str) -> AsyncIterator[str]:
        """Assemble one turn (Build #1) + the expression directive, then stream."""
        turn_index = self.state.sessions.get(session_id)["turn_count"]
        soul, prompt = self._assemble(
            session_id, text,
            window=self.state.sessions.window(session_id, self.cfg.raw_window_turns),
            lore=self.state.soul_loader.load().lorebook_hits(text))

        self.state.sessions.append_message(session_id, "user", text)
        self._pending[session_id] = _Pending(prompt, turn_index, soul)

        async for token in self.state.chat.stream(
                prompt.messages, temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_reply_tokens):
            yield token

    async def persist(self, session_id: str, user_text: str, reply: str) -> None:
        """Build #1's post-turn pipeline, verbatim: corpus line, then journal +
        index + USER.md + summary + exactly one git commit (SPEC §2, §4.4)."""
        pend = self._pending.pop(session_id, None)
        if pend is None:
            return
        turn_id = self.state.corpus.log_turn(
            session_id=session_id, turn_index=pend.turn_index,
            messages=pend.prompt.messages, completion=reply,
            model=self.cfg.chat_model, card_version=pend.soul.card_version,
            companion=pend.soul.name.lower(),
            template_version=pend.prompt.template_version,
            gen_params={"temperature": self.cfg.temperature},
            tags=["voice"])
        self.state.sessions.append_message(session_id, "assistant", reply,
                                           turn_id=turn_id)
        self.state.sessions.bump_turn(session_id)
        record = Record(session_id=session_id, turn_index=pend.turn_index,
                        user_msg=user_text, reply=reply)
        await post_turn(self.state, record, session_id, pend.turn_index + 1)

    # -- the greeting: she speaks first (SPEC §7) -------------------------------
    async def stream_greeting(self, session_id: str) -> AsyncIterator[str]:
        """Stream the continuity opener. Self-contained: window=[] and the cue is
        NOT appended to the transcript (an opener is not a turn the user took), so
        it never pollutes the next window and is never persisted (§7)."""
        cue = GREET_CUE.format(user=self.cfg.user_name)
        _soul, prompt = self._assemble(session_id, cue, window=[], lore=[])
        async for token in self.state.chat.stream(
                prompt.messages, temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_reply_tokens):
            yield token
