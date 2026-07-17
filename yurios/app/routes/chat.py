"""POST /api/chat — the §10.1 handler, SSE.

The hot path is recall + assembly + the model stream, nothing else (§2.2).
Journal, index, USER.md, summary, the git commit, and the corpus line all
happen after the reply streams — they never delay the first token.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from yurios.app.core import assemble as asm
from yurios.app.memory import summarise
from yurios.app.memory.store import Record
from yurios.app import vaultgit

log = logging.getLogger("mvw.chat")
router = APIRouter()


class ChatRequest(BaseModel):
    session_id: str
    message: str


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def post_turn(state, record: Record, session_id: str, turn_count: int) -> None:
    """§10.1 step 9 — the post-turn pipeline, off the critical path (§2.2):
    remember (journal + index + USER.md), maybe summarise, then ONE git commit."""
    async with state.vault_lock:
        try:
            await state.store.remember(record)
            if turn_count % state.cfg.summary_every_n == 0:
                window = state.sessions.window(
                    session_id, state.cfg.summary_every_n * 2)
                exchanges = "\n".join(
                    f"{state.cfg.user_name if m['role'] == 'user' else state.soul_name}: "
                    f"{m['content']}" for m in window)
                text = await summarise.update_summary(
                    state.utility,
                    prev_summary=state.store.read_summary(),
                    exchanges=exchanges,
                    char_name=state.soul_name, user_name=state.cfg.user_name,
                    budget_tokens=state.cfg.summary_budget_tokens)
                state.utility_log.log(kind="summarise", exchanges=exchanges,
                                      raw_reply=text)
                summarise.write_summary(state.cfg.vault_dir, text,
                                        state.store.index, state.embedder)
        except Exception:
            log.exception("post-turn pipeline error (turn already served)")
        finally:
            # every durable change is a commit — the diary of how she grew (§6.5)
            vaultgit.commit(state.cfg.vault_dir,
                            f"turn {session_id[:8]}:{record.turn_index}")


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    state = request.app.state.mvw
    session = state.sessions.get(req.session_id)
    if session is None:
        raise HTTPException(404, "unknown session")

    # ---- §10.1 steps 2–4: everything the prompt needs, before the stream ----
    turn_index = session["turn_count"]
    soul = state.soul_loader.load()                       # read every turn (§5)
    memories = state.store.recall(req.message, state.cfg.retrieval_k)
    prompt = asm.assemble(
        soul,
        user_md=state.store.read_user_md(),
        summary=state.store.read_summary(),
        memories=memories,
        lore=soul.lorebook_hits(req.message),
        window=state.sessions.window(req.session_id, state.cfg.raw_window_turns),
        user_msg=req.message,
        user_name=state.cfg.user_name,
        system_budget_tokens=state.cfg.system_budget_tokens,
        lorebook_budget_tokens=state.cfg.lorebook_budget_tokens)

    # step 5: the user message enters the transcript before the model speaks
    state.sessions.append_message(req.session_id, "user", req.message)

    async def stream():
        reply = ""
        try:
            async for token in state.chat.stream(
                    prompt.messages, temperature=state.cfg.temperature,
                    max_tokens=state.cfg.max_reply_tokens):
                reply += token
                yield sse({"token": token})
        except Exception as e:
            # §10.1: a mid-stream failure emits an error event and writes NO
            # corpus record and NO partial commit.
            log.exception("model stream failed")
            yield sse({"error": str(e)})
            return

        # ---- steps 7–8: transcript + corpus + bookkeeping ----
        turn_id = state.corpus.log_turn(
            session_id=req.session_id, turn_index=turn_index,
            messages=prompt.messages, completion=reply,
            model=state.cfg.chat_model,
            card_version=soul.card_version,
            companion=soul.name.lower(),
            template_version=prompt.template_version,
            gen_params={"temperature": state.cfg.temperature})
        state.sessions.append_message(req.session_id, "assistant", reply,
                                      turn_id=turn_id)
        state.sessions.bump_turn(req.session_id)

        # step 9: schedule the post-turn pipeline; never blocks the stream
        record = Record(session_id=req.session_id, turn_index=turn_index,
                        user_msg=req.message, reply=reply)
        task = asyncio.create_task(
            post_turn(state, record, req.session_id, turn_index + 1))
        state.pending_tasks.add(task)
        task.add_done_callback(state.pending_tasks.discard)

        yield sse({"done": True, "turn_id": turn_id})

    return StreamingResponse(stream(), media_type="text/event-stream")
