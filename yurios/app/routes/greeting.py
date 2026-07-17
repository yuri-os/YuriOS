"""GET /api/greeting — the continuity greeting (SPEC §9.3), SSE.

The DoD headline: close the tab, come back tomorrow, she opens with something
you told her before — unprompted. First-ever run (empty Vault) uses
BOOTSTRAP.md's cold open instead; once the relationship exists the bootstrap
is retired (`git mv` → soul/onboarded/, §5.4) and every opener is
memory-grounded, falling back to SCENARIO.md's return greetings on error.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from yurios.app.core import assemble as asm
from yurios.app import vaultgit

log = logging.getLogger("mvw.greeting")
router = APIRouter()


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _has_history(state) -> bool:
    """Has she met you yet? The journal knows (file-presence semantics, §5.4)."""
    episodic = state.cfg.vault_dir / "memory" / "episodic"
    return episodic.exists() and any(episodic.glob("*.md"))


def _retire_bootstrap(state) -> None:
    """Consumed once (§5.4): git mv soul/BOOTSTRAP.md soul/onboarded/… and
    commit. From now on, file-absence means 'she has met you'."""
    try:
        vaultgit.mv(state.cfg.vault_dir, "soul/BOOTSTRAP.md",
                    "soul/onboarded/BOOTSTRAP.done.md")
        vaultgit.commit(state.cfg.vault_dir, "first session complete")
    except Exception:
        log.exception("bootstrap retirement failed (will retry next greeting)")


GREET_CUE = ("(({user} just opened the sanctuary — no message yet; you speak "
             "first. One short greeting in your own voice that surfaces "
             "something {user} told you before, unprompted. If the memory "
             "blocks are empty, just welcome them back warmly.))")


@router.get("/api/greeting")
async def greeting(session_id: str, request: Request):
    state = request.app.state.mvw
    session = state.sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")

    soul = state.soul_loader.load()

    # first-ever meeting: the authored cold open, verbatim (§5.4). Not corpus-
    # logged — it is hand-authored SOUL text, not a model completion (§8 logs
    # what the model generates; the cold open already lives in the card).
    if soul.bootstrap is not None and not _has_history(state):
        async def stream_bootstrap():
            for chunk in soul.bootstrap.split(" "):
                yield sse({"token": chunk + " "})
            state.sessions.append_message(session_id, "assistant", soul.bootstrap)
            yield sse({"done": True})
        return StreamingResponse(stream_bootstrap(),
                                 media_type="text/event-stream")

    # she has met you: retire the bootstrap if it is still around (§5.4)…
    if soul.bootstrap is not None and _has_history(state):
        _retire_bootstrap(state)

    # …and open from memory (§9.3): persona + USER.md + summary + a top recall.
    user_md = state.store.read_user_md()
    summary = state.store.read_summary()
    probe = summary or user_md or "what matters lately"
    memories = state.store.recall(probe, state.cfg.retrieval_k)
    cue = GREET_CUE.format(user=state.cfg.user_name)
    prompt = asm.assemble(
        soul, user_md=user_md, summary=summary, memories=memories,
        lore=[], window=[], user_msg=cue,
        user_name=state.cfg.user_name,
        system_budget_tokens=state.cfg.system_budget_tokens,
        lorebook_budget_tokens=state.cfg.lorebook_budget_tokens)

    async def stream_greeting():
        reply = ""
        try:
            async for token in state.chat.stream(
                    prompt.messages, temperature=state.cfg.temperature,
                    max_tokens=state.cfg.max_reply_tokens):
                reply += token
                yield sse({"token": token})
        except Exception:
            # fall back to a static return greeting (§9.3) — she still greets
            log.exception("greeting model call failed; using return greeting")
            hour = datetime.datetime.now().hour
            fallback = (soul.return_greetings[0] if hour >= 15
                        else soul.return_greetings[-1]) if soul.return_greetings else ""
            for chunk in fallback.split(" "):
                yield sse({"token": chunk + " "})
            state.sessions.append_message(session_id, "assistant", fallback)
            yield sse({"done": True})
            return

        turn_id = state.corpus.log_turn(
            session_id=session_id, turn_index=session["turn_count"],
            messages=prompt.messages, completion=reply,
            model=state.cfg.chat_model, card_version=soul.card_version,
            companion=soul.name.lower(),
            template_version=prompt.template_version,
            tags=["greeting"])
        state.sessions.append_message(session_id, "assistant", reply,
                                      turn_id=turn_id)

        async def commit_later():
            async with state.vault_lock:
                vaultgit.commit(state.cfg.vault_dir,
                                f"greeting {session_id[:8]}")
        task = asyncio.create_task(commit_later())
        state.pending_tasks.add(task)
        task.add_done_callback(state.pending_tasks.discard)
        yield sse({"done": True, "turn_id": turn_id})

    return StreamingResponse(stream_greeting(), media_type="text/event-stream")
