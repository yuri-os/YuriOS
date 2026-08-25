"""/ws/voice — the full-duplex voice loop (SPEC §4, §10).

The websocket is where barge-in has to *land*, so the handler does two things at
once: it reads inbound messages (audio frames, endpoint, barge-in) while it
streams outbound events (expression, audio, done) from an in-flight turn. That
concurrency is the whole reason barge-in works — a `{"type":"bargein"}` message
can arrive and call `controller.cancel()` mid-reply, tearing down TTS + the
brain's generation together (SPEC §4.3).

The wire underneath — the connection cap, the hello exchange, the frame
ceilings, the STT session — is `voice/ws_session.py`, shared with the browser's
route. What is here is what the native window alone does with it: no hub, no
SignalBus, no ambient injection; expressions ride this wire rather than a
puppet lane, and the whole voice stack is warmed once by the Runtime rather
than held up by whoever is listening.

Client → server messages (JSON, except audio which is binary frames):
    {"type":"hello", "session_id": "<optional prior id>"}
    (binary)                     one Float32 PCM mic frame @ 16 kHz (during speech)
    {"type":"endpoint"}          the user's turn is done → transcribe + reply
    {"type":"bargein"}           the user talked over her → cancel the current turn
    {"type":"text", "text":...}  typed input (a fallback path; skips STT)

Server → client messages (JSON; audio PCM is base64 in `pcm`):
    {"type":"session", "session_id":...}
    {"type":"filler"|"audio", "text":..., "sr":..., "pcm": <base64 float32>}
    {"type":"expression", "expression":...}
    {"type":"done", "latency":..., "expression":...} | {"type":"cancelled"}
    {"type":"error", "message":...}
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..voice.latency import TurnTrace
from ..voice.transcript import is_meaningful_transcript
from ..voice.ws_limits import (
    MAX_TYPED_TEXT_BYTES, VoiceSocketClosed, VoiceSocketGuard, bounded_text,
)
from ..voice.ws_session import (
    Utterance, encode_event, loads_frame, make_sender, open_session, serve,
    stop_turn, turn_controller,
)

log = logging.getLogger("desktop.ws")
router = APIRouter()


@router.websocket("/ws/voice")
async def voice(ws: WebSocket):
    await serve(ws, _connected)


async def _connected(ws: WebSocket, rt) -> None:
    await ws.accept()
    guard = VoiceSocketGuard(ws, rt.cfg)
    brain = rt.brain
    safe_send = make_sender(ws)

    session_id = await open_session(ws, guard, brain)
    if session_id is None:
        return
    await safe_send({"type": "session", "session_id": session_id})

    # The voice stack may still be warming (Runtime loads it off-thread so the
    # page — her body — appears immediately; Kokoro alone is ~20 s cold). Wait
    # here, per connection: the socket stays open, her avatar is already up, and
    # the greeting fires the moment her voice is ready. Never uses a stand-in.
    await asyncio.to_thread(rt.voice_ready.wait)
    heard = Utterance(rt, guard)
    controller = turn_controller(rt, brain)

    turn_task: asyncio.Task | None = None

    async def run(agen) -> None:
        """Pump one turn's OutEvents to the client until it ends or the client goes."""
        try:
            async for ev in agen:
                if not await safe_send(encode_event(ev)):
                    controller.cancel()        # client vanished → tear the turn down
                    return
        except Exception:
            log.exception("turn stream failed")
            await safe_send({"type": "error", "message": "turn failed"})
        finally:
            # §9.6: no trace includes her memory. stream_reply put the user's
            # line in the session window before the first token; only persist
            # writes her half. Every exit from this pump that isn't a clean
            # commit rolls the orphan back — otherwise the next prompt reads it
            # as unanswered and she answers it again. No-op after a clean turn,
            # and for the greeting (it appends nothing).
            brain.abandon(session_id)

    # she speaks first (§7): greet from memory the moment the headset goes on —
    # but only once per session. A reconnect (or a second connection that parked
    # in the voice-warm wait and released alongside this one) must not fire a
    # second greeting over the first. This check-and-mark is atomic on the event
    # loop — there is no await between them — so concurrent handlers can't both win.
    if session_id not in rt.greeted:
        rt.greeted.add(session_id)
        turn_task = asyncio.create_task(run(
            controller.run_turn(session_id, "", persist=False,
                                tokens=brain.stream_greeting(session_id))))

    try:
        while True:
            msg = await guard.receive(safe_send)
            if msg["type"] == "websocket.disconnect":
                break

            try:
                guard.accept_text_frame(msg.get("text"))
            except OverflowError as exc:
                await guard.reject_limit(str(exc))
                return

            if "bytes" in msg and msg["bytes"] is not None:
                # a mic frame during the user's turn → feed STT (endpointing on
                # the client). Barge-in is a *control* message, not inferred here.
                try:
                    heard.feed(msg["bytes"])
                except (ValueError, OverflowError) as exc:
                    await guard.reject_limit(str(exc))
                    return
                continue

            data = loads_frame(msg.get("text"))
            kind = data.get("type")

            if kind == "pong":
                continue

            if kind == "bargein":
                controller.cancel()                        # tears down TTS + generation
                continue

            if kind == "reset_audio":
                heard.reset()
                continue

            if kind == "endpoint" or kind == "text":
                # a new turn starts: make sure the previous one is torn down first
                await stop_turn(controller, turn_task)
                if kind == "text":
                    try:
                        text = bounded_text(data.get("text"),
                                            maximum=MAX_TYPED_TEXT_BYTES,
                                            field="text")
                    except ValueError as exc:
                        await guard.reject_limit(str(exc))
                        return
                else:
                    text = await heard.transcribe()
                heard.reset()
                # last net: a punctuation-only hallucination (". . . .") is not a
                # turn — never let it reach the brain or the Vault (§3.2).
                if not is_meaningful_transcript(text):
                    continue
                trace = TurnTrace()
                turn_task = asyncio.create_task(
                    run(controller.run_turn(session_id, text, trace=trace)))
    except (WebSocketDisconnect, VoiceSocketClosed):
        pass
    finally:
        await stop_turn(controller, turn_task)
