"""/ws/voice — the full-duplex voice loop (SPEC §4, §10).

The websocket is where barge-in has to *land*, so the handler does two things at
once: it reads inbound messages (audio frames, endpoint, barge-in) while it
streams outbound events (expression, audio, done) from an in-flight turn. That
concurrency is the whole reason barge-in works — a `{"type":"bargein"}` message
can arrive and call `controller.cancel()` mid-reply, tearing down TTS + the
brain's generation together (SPEC §4.3).

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
import base64
import logging

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..voice.latency import TurnTrace
from ..voice.speech_gate import SpeechGate
from ..voice.transcript import is_meaningful_transcript
from ..voice.turn import OutEvent, TurnController
from ..voice.ws_limits import (
    MAX_SESSION_ID_BYTES, MAX_TYPED_TEXT_BYTES, VoiceSocketClosed,
    VoiceSocketGuard, bounded_text, reject_capacity,
)

log = logging.getLogger("desktop.ws")
router = APIRouter()


def _encode(ev: OutEvent) -> dict:
    """OutEvent → a JSON-able dict; PCM is base64 float32 (§10)."""
    if ev.kind in ("filler", "audio") and ev.audio is not None:
        return {"type": ev.kind, "text": ev.text,
                "sr": ev.audio.sample_rate,
                "pcm": base64.b64encode(
                    ev.audio.audio.astype(np.float32).tobytes()).decode("ascii")}
    if ev.kind == "expression":
        return {"type": "expression", "expression": ev.expression}
    if ev.kind == "done":
        return {"type": "done", **(ev.detail or {})}
    if ev.kind == "error":
        return {"type": "error", **(ev.detail or {})}
    return {"type": ev.kind}


@router.websocket("/ws/voice")
async def voice(ws: WebSocket):
    rt = ws.app.state.rt
    limiter = rt.voice_ws_limiter
    if not limiter.try_acquire():
        await reject_capacity(ws)
        return
    try:
        await _connected(ws, rt)
    finally:
        limiter.release()


async def _connected(ws: WebSocket, rt) -> None:
    await ws.accept()
    guard = VoiceSocketGuard(ws, rt.cfg)
    brain = rt.brain

    async def safe_send(data: dict) -> bool:
        """Send unless the client is already gone. Returns False if the socket is
        closed/closing — the client can vanish mid-turn (a reload, a reconnect),
        and a send after close raises; we treat that as 'stop', not an error."""
        if ws.application_state != WebSocketState.CONNECTED:
            return False
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError):
            return False

    # resolve the session (reuse the client's id if it's still known)
    try:
        message = await guard.receive_initial()
    except VoiceSocketClosed:
        return                                    # client left before saying hello
    try:
        guard.accept_text_frame(message.get("text"))
    except OverflowError as exc:
        await guard.reject_limit(str(exc))
        return
    hello = _loads(message.get("text"))
    if message.get("type") != "websocket.receive" or hello.get("type") != "hello":
        await guard.reject_limit("voice hello required")
        return
    try:
        requested_session = bounded_text(
            hello.get("session_id"), maximum=MAX_SESSION_ID_BYTES,
            field="session_id", optional=True)
    except ValueError as exc:
        await guard.reject_limit(str(exc))
        return
    session_id = brain.resolve_session(requested_session)
    await safe_send({"type": "session", "session_id": session_id})

    # The voice stack may still be warming (Runtime loads it off-thread so the
    # page — her body — appears immediately; Kokoro alone is ~20 s cold). Wait
    # here, per connection: the socket stays open, her avatar is already up, and
    # the greeting fires the moment her voice is ready. Never uses a stand-in.
    await asyncio.to_thread(rt.voice_ready.wait)
    stt = rt.stt.create_session()
    # Server-side debounced VAD: as the client streams an utterance's frames, we
    # run them through the VAD + SpeechGate so an endpoint only becomes a turn if
    # real speech was actually heard — the last net under a naive edge gate (§3.4,
    # §4.2). `gate.confirmed` is read at endpoint; reset per turn.
    gate = SpeechGate(
        onset_frames=rt.cfg.vad_onset_frames,
        bargein_frames=rt.cfg.vad_bargein_frames,
        hangover_frames=max(1, rt.cfg.vad_min_silence_ms // max(1, rt.cfg.frame_ms)))
    controller = TurnController(
        brain=brain, tts=rt.tts, filler_bank=rt.filler_bank,
        mask_latency=rt.cfg.mask_latency,
        expression_default=rt.cfg.expression_default,
        trace_dir=rt.cfg.trace_dir)

    turn_task: asyncio.Task | None = None

    async def run(agen) -> None:
        """Pump one turn's OutEvents to the client until it ends or the client goes."""
        try:
            async for ev in agen:
                if not await safe_send(_encode(ev)):
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
                # a mic frame during the user's turn → feed STT (endpointing on the
                # client). Barge-in is a *control* message, not inferred here. We
                # also push the frame's VAD verdict into the gate so the server can
                # tell, at endpoint, whether the utterance held real speech (§3.4).
                payload = msg["bytes"]
                try:
                    guard.accept_audio(payload)
                except (ValueError, OverflowError) as exc:
                    await guard.reject_limit(str(exc))
                    return
                frame = np.frombuffer(payload, dtype=np.float32)
                stt.feed(frame, 16000)
                if rt.cfg.vad_confirm and rt.vad is not None:
                    gate.push(rt.vad.is_speech(frame, 16000))
                continue

            data = _loads(msg.get("text"))
            kind = data.get("type")

            if kind == "pong":
                continue

            if kind == "bargein":
                controller.cancel()                        # tears down TTS + generation
                continue

            if kind == "reset_audio":
                stt.reset()
                gate.reset()
                guard.reset_utterance()
                continue

            if kind == "endpoint" or kind == "text":
                # a new turn starts: make sure the previous one is torn down first
                if turn_task and not turn_task.done():
                    controller.cancel()
                    await asyncio.gather(turn_task, return_exceptions=True)
                if kind == "text":
                    try:
                        text = bounded_text(data.get("text"),
                                            maximum=MAX_TYPED_TEXT_BYTES,
                                            field="text")
                    except ValueError as exc:
                        await guard.reject_limit(str(exc))
                        return
                else:
                    # endpoint: transcribe the utterance — but only if the server's
                    # VAD confirmed real speech in it. All-noise (keyboard clatter
                    # that leaked past a naive edge gate) is dropped here (§4.2).
                    text = await asyncio.to_thread(stt.final) \
                        if (not rt.cfg.vad_confirm or gate.confirmed) else ""
                stt.reset()
                gate.reset()
                guard.reset_utterance()
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
        if turn_task and not turn_task.done():
            controller.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)


def _loads(text: str | None) -> dict:
    import json
    try:
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
