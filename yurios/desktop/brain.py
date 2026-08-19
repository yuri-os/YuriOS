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
from dataclasses import dataclass
from typing import AsyncIterator

# The brain (yurios/app, originally Build #1) — the exact code book ch. 31 walks through.
from yurios.app.core import assemble as asm
from yurios.app.corpus import CorpusLogger  # noqa: F401 (documents the reused surface)
from yurios.app.main import AppState, create_app
from yurios.app.memory.store import Record
from yurios.app.routes.chat import post_turn
from yurios.app import vaultgit
from yurios.mind.util import estimate_tokens
from yurios.world import correlate

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
        # mind/promptlog.py, wired by the world runtime. None on Build #1's own
        # path, which keeps no such record — the same nullable seam as set_world.
        self.prompt_log = None
        # mind/knowledge.py's store, wired by MindLoop. None on Build #1's path
        # and with MIND_ENABLED=false: no shelf, no block, and every other slot
        # assembles exactly as it did.
        self.knowledge = None
        # mind/workspace.py's two stores (§34), wired the same way and just as
        # nullable — her desk and her skills.
        self.workspace = None
        self.skills = None
        self._on_desk_write = None

    def set_prompt_log(self, prompt_log) -> None:
        """Wire the sink that records what she was actually asked (SPEC §24.2)."""
        self.prompt_log = prompt_log

    def set_knowledge(self, store) -> None:
        """Wire the §20 shelf into the §7.1 assembly (the §20.2 knowledge slot).

        Late-bound for the same reason `set_world` is: the KnowledgeStore belongs
        to the MindLoop, which is built after the brain and not at all when the
        mind is off.
        """
        self.knowledge = store

    def set_workspace(self, workspace, skills, on_write=None) -> None:
        """Wire her desk and her skills into the prompt (SPEC §34.3).

        Late-bound like the two above, and for the same reason. Either may be
        None; the corresponding block simply isn't appended.

        `on_write` is called after a desk tool changes a file. The tool server
        is a separate process and writes straight to disk, so nothing in *this*
        one would otherwise know the Vault had changed — and the tick loop only
        commits a tick it believes is dirty. Without this, a note she wrote at
        noon lands in whatever commit happens to fire next, labelled as
        something else entirely.
        """
        self.workspace = workspace
        self.skills = skills
        self._on_desk_write = on_write

    def _desk_block(self) -> str:
        """The two §34 blocks, or "" — built fresh each turn, off the files.

        Both are deliberately *indexes*, not contents. The skills catalog is one
        line per skill (name + when to reach for it) and the desk listing is one
        line per file; the bodies are behind `read_skill` and `read_note`, which
        she calls only once she has decided she wants them. That is what keeps a
        store of twenty skills and a desk of fifty notes affordable on every
        single turn.
        """
        parts = []
        try:
            catalog = self.skills.catalog() if self.skills is not None else ""
        except Exception:       # noqa: BLE001 — a mangled SKILL.md is not a lost turn
            log.warning("skill catalog failed; assembling without it", exc_info=True)
            catalog = ""
        if catalog:
            parts.append(
                "## SKILLS\n\nThings you know how to do. These are names and "
                "when-to-use lines only — call `read_skill` with the name to "
                "get the actual instructions before you follow one.\n\n"
                + catalog)
        try:
            desk = (self.workspace.digest(
                limit=getattr(self.cfg, "workspace_digest_files", 20))
                if self.workspace is not None else "")
        except Exception:       # noqa: BLE001 — same rule as the shelf
            log.warning("workspace digest failed; assembling without it",
                        exc_info=True)
            desk = ""
        if desk:
            parts.append(
                "## YOUR DESK\n\nFiles you have written for yourself, newest "
                "first. Before changing one, call `read_note` first. Use "
                "`write_note` only to replace the entire note, `append_note` "
                "only when text belongs at the end, and `edit_note` to replace "
                "one unique passage. Set `new_text` to an empty string to remove "
                "a duplicate passage. This is "
                "your scratch space — use it when a thought needs somewhere to "
                "live between now and later.\n"
                # Asked "where is the document?", the live model answered "I've "
                # "tucked it away into my own mind" — with the path sitting right
                # here in its context. The block said what the files are and how
                # to open them, and never that they are real things with real
                # names worth saying out loud.
                "These are real files with real paths, not a feeling about "
                "having remembered something. If you are asked where something "
                "is, or whether you wrote it down, answer with the path from "
                "this list — and if it isn't here, say that instead of implying "
                "it is.\n\n" + desk)
        return "\n\n".join(parts)

    def _recall_knowledge(self, text: str) -> list:
        """The shelf, searched for this turn. Never raises: a broken index is a
        turn without the block, not a turn that doesn't happen."""
        if self.knowledge is None or self.cfg.knowledge_k <= 0:
            return []
        try:
            return self.knowledge.search(text, self.cfg.knowledge_k)
        except Exception:       # noqa: BLE001 — no embedder, a half-written index
            log.warning("knowledge recall failed; assembling without the shelf",
                        exc_info=True)
            return []

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
            knowledge=self._recall_knowledge(text),
            lore=lore,
            window=window,
            user_msg=text,
            user_name=self.cfg.user_name,
            system_budget_tokens=self.cfg.system_budget_tokens,
            lorebook_budget_tokens=self.cfg.lorebook_budget_tokens,
            knowledge_budget_tokens=self.cfg.knowledge_budget_tokens)
        # the two prompt changes Build #2 makes (§6): tell the model this is a
        # spoken (not written) exchange — no narration — and ask for inline
        # expression tags. Both are voice-only; Build #1's text chat keeps neither.
        prompt.messages[0]["content"] += (
            f"\n\n## VOICE\n\n{SPOKEN_STYLE_DIRECTIVE}"
            f"\n\n## EXPRESSION\n\n{EXPRESSION_DIRECTIVE}")
        desk = self._desk_block()
        if desk:
            prompt.messages[0]["content"] += f"\n\n{desk}"
        return soul, prompt

    # -- the ReplyBrain seam ----------------------------------------------------
    async def stream_reply(self, session_id: str, text: str,
                           image: str | None = None) -> AsyncIterator[str]:
        """Assemble one turn (Build #1) + the expression directive, then stream.

        `image` is a picture the user sent with this line, already checked and
        shrunk (world/uploads.py), as the base64 data url the model reads. It
        rides *this* prompt only: what stays behind in the window, and in the
        corpus, is the note (§35), because a photo re-sent with every later turn
        would eat the window it was small enough to fit in the first place."""
        turn_index = self.state.sessions.get(session_id)["turn_count"]
        soul, prompt = self._assemble(
            session_id, text,
            window=self.state.sessions.window(session_id, self.cfg.raw_window_turns),
            lore=self.state.soul_loader.load().lorebook_hits(text))
        if image:
            asm.mark_picture(prompt.messages)

        self.state.sessions.append_message(
            session_id, "user", asm.note_picture(text) if image else text)
        self._pending[session_id] = _Pending(prompt, turn_index, soul)

        messages = asm.with_image(prompt.messages, image) if image \
            else prompt.messages
        async for token in self.state.chat.stream(
                messages, temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_reply_tokens):
            yield token

    def abandon(self, session_id: str) -> None:
        """The other close-out: this turn never happened (barge-in, brain error).

        `stream_reply` appends the user's line to the session transcript before
        the first token, because the model must see it — but `persist` is what
        writes her half. A turn torn down in between must undo that half, or the
        next prompt carries an unanswered question and she answers it a second
        time alongside the new one. Idempotent, and a no-op unless *this*
        session had a reply in flight (a greeting or an ambient line appends
        nothing, so cancelling one must never touch the transcript)."""
        if self._pending.pop(session_id, None) is None:
            return
        self.state.sessions.drop_last(session_id, "user")

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
        if self.prompt_log is not None:
            # A pointer, not a copy: the corpus already holds this prompt, and it
            # is the record ratings.jsonl joins against. What the prompt log adds
            # is that a chat turn appears in the same timeline as everything else
            # she asked a model, in the same shape, joinable by the same id.
            self.prompt_log.record(
                kind=correlate.CHAT_TURN, model=self.cfg.chat_model,
                template_version=pend.prompt.template_version,
                messages_ref={"file": "corpus/turns.jsonl", "id": turn_id},
                n_messages=len(pend.prompt.messages),
                tokens_in=sum(estimate_tokens(m.get("content") or "")
                              for m in pend.prompt.messages),
                tokens_out=estimate_tokens(reply))
        self.state.sessions.append_message(session_id, "assistant", reply,
                                           turn_id=turn_id)
        self.state.sessions.bump_turn(session_id)
        record = Record(session_id=session_id, turn_index=pend.turn_index,
                        user_msg=user_text, reply=reply)
        await post_turn(self.state, record, session_id, pend.turn_index + 1)

    # -- the greeting: she speaks first (SPEC §7) -------------------------------
    def _has_history(self) -> bool:
        """Has she met you yet? The journal knows (file-presence semantics, §5.4)."""
        episodic = self.state.cfg.vault_dir / "memory" / "episodic"
        return episodic.exists() and any(episodic.glob("*.md"))

    async def _retire_bootstrap(self) -> None:
        """Consumed once (§5.4): `git mv soul/BOOTSTRAP.md soul/onboarded/…` and
        commit, so file-absence now means "she has met you" and `git log` keeps
        the script that bootstrapped her inspectable. Under the Vault lock and
        off the event loop: it is git, and a turn may be committing beside it."""
        async with self.state.vault_lock:
            def retire() -> None:
                vaultgit.mv(self.state.cfg.vault_dir, "soul/BOOTSTRAP.md",
                            "soul/onboarded/BOOTSTRAP.done.md", force=True)
                vaultgit.commit(self.state.cfg.vault_dir, "first session complete")
            try:
                await asyncio.to_thread(retire)
            except Exception:
                log.exception("bootstrap retirement failed (retried next greeting)")

    def cold_open(self) -> str | None:
        """The authored first message, while she has not met you (§5.4) — or None
        once she has, which is every arrival after the first.

        This is the text to **show**: a cold open is a *scene* (the card's
        `first_mes`, stage directions and all), not an utterance. The voice
        pipeline strips `*narration*` on its way to TTS, which is right for
        speech and ruinous for this — a scene-shaped cold open reaches the
        transcript as the handful of noises that happened to be in quotes. So
        callers commit what this returns and let the pipeline speak whatever it
        can of it. Pure: the retirement is `stream_greeting`'s to do."""
        soul = self.state.soul_loader.load()
        if soul.bootstrap is None or self._has_history():
            return None
        return soul.bootstrap

    async def stream_greeting(self, session_id: str) -> AsyncIterator[str]:
        """Stream the continuity opener. Self-contained: window=[] and the cue is
        NOT appended to the transcript (an opener is not a turn the user took), so
        it never pollutes the next window and is never persisted (§7).

        Except on the first-ever arrival, where there is no continuity to open
        from: `BOOTSTRAP.md` is present, so she speaks its authored cold open
        verbatim instead of a completion (§5.4). No model call, and no corpus
        line — the text is hand-written SOUL, not something she generated. That
        one *does* join the session window, unlike every other opener: it is the
        scene her first reply has to follow from, and a model that never saw it
        answers into an empty room. Once the journal shows she has met you, the
        bootstrap is retired and every greeting from then on is memory-grounded."""
        cold = self.cold_open()
        if cold is not None:
            if self.state.sessions.get(session_id) is not None:
                self.state.sessions.append_message(session_id, "assistant", cold)
            for word in cold.split(" "):
                yield word + " "
            return
        if self.state.soul_loader.load().bootstrap is not None:
            await self._retire_bootstrap()

        cue = GREET_CUE.format(user=self.cfg.user_name)
        _soul, prompt = self._assemble(session_id, cue, window=[], lore=[])
        # A greeting is never persisted — no corpus line, no transcript entry — so
        # without this the first thing she says every session leaves no record of
        # what it was grounded in.
        with correlate.scope(kind=correlate.GREETING, session_id=session_id):
            said: list[str] = []
            try:
                async for token in self.state.chat.stream(
                        prompt.messages, temperature=self.cfg.temperature,
                        max_tokens=self.cfg.max_reply_tokens):
                    said.append(token)
                    yield token
            finally:
                # in `finally` so a barged-in greeting still records what it was
                # asked and how far it got
                if self.prompt_log is not None:
                    self.prompt_log.record(
                        kind=correlate.GREETING, messages=prompt.messages,
                        completion="".join(said), model=self.cfg.chat_model,
                        template_version=prompt.template_version)
