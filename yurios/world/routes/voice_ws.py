"""/ws/voice — the Build #2 voice loop, forked (SPEC §2.2, §9, §10).

FORK(B2 §10): this file is a copy of the `desktop/routes/voice_ws.py`
with a small set of additions, each marked `FORK(B2 §10)` below:

  1. engagement notifications — `rt.turn_started()` / `rt.turn_ended()` around
     each turn's pump, so the mind knows when she's talking (the ENGAGED
     preempt, SPEC §15.3);
  2. the ambient injector — this connection registers a coroutine that lets the
     mind speak a cue *through this connection's TurnController*, so her
     self-initiated speech gets the same OutEvent stream, the same barge-in
     cancel, and the same latency masking as a real turn (SPEC §15.5);
  3. its unregistration on disconnect;
  4. the transcript tee (SPEC §2.6): the user's turn (typed or transcribed) and
     her committed reply are posted to the EventHub as `message` events, with
     an accumulating `draft` while she speaks — the chat panel's feed. A
     barged-in turn drops its draft and commits nothing, mirroring the corpus
     rule: a turn that didn't happen leaves no trace (B2 §4.4);
  5. expressions leave this wire: an expression OutEvent becomes a
     `controller.set_expression(…, reset_ms=0)` — one lane for the face, so
     both bodies and every open page see turn emotions on the hub (SPEC §10);
  6. FORK(B5 §16) — the signal tee: a user turn posts `user_message` (the
     mind's ENGAGED preempt) and a committed exchange posts `turn_committed`
     (the mind's REFLECT share: world model, promise extraction) onto the
     SignalBus. The reply itself still streams on this reactive path — the
     loop observes the conversation, it never sits in front of it (SPEC §15.3).

Everything else — the audio wire, the SpeechGate, the greeting-once logic, the
barge-in path — is B2 §10 in behaviour. If Build #2's route changes, re-diff
this file against it.

Client → server messages (JSON, except audio which is binary frames):
    {"type":"hello", "session_id": "<optional prior id>"}
    (binary)                     one Float32 PCM mic frame @ 16 kHz (during speech)
    {"type":"endpoint"}          the user's turn is done → transcribe + reply
    {"type":"bargein"}           the user talked over her → cancel the current turn
    {"type":"text", "text":...}  typed input (the chat composer; skips STT)

Server → client messages (JSON; audio PCM is base64 in `pcm`):
    {"type":"session", "session_id":...}
    {"type":"filler"|"audio", "text":..., "sr":..., "pcm": <base64 float32>}
    {"type":"done", "latency":..., "expression":...} | {"type":"cancelled"}
    {"type":"error", "message":...}
(expressions and chat text now ride /api/events — SPEC §10)
"""
from __future__ import annotations

import asyncio
import base64
import logging

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from yurios.desktop.voice.latency import TurnTrace
from yurios.desktop.voice.speech_gate import SpeechGate
from yurios.desktop.voice.transcript import is_meaningful_transcript
from yurios.desktop.voice.turn import OutEvent, TurnController

log = logging.getLogger("world.ws")
router = APIRouter()


def _encode(ev: OutEvent) -> dict:
    """OutEvent → a JSON-able dict; PCM is base64 float32 (B2 §10).
    (`expression` events never reach here — the pump reroutes them, fork #5.)"""
    if ev.kind in ("filler", "audio") and ev.audio is not None:
        return {"type": ev.kind, "text": ev.text,
                "sr": ev.audio.sample_rate,
                "pcm": base64.b64encode(
                    ev.audio.audio.astype(np.float32).tobytes()).decode("ascii")}
    if ev.kind == "done":
        return {"type": "done", **(ev.detail or {})}
    if ev.kind == "error":
        return {"type": "error", **(ev.detail or {})}
    return {"type": ev.kind}


@router.websocket("/ws/voice")
async def voice(ws: WebSocket):
    await ws.accept()
    rt = ws.app.state.rt
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
        hello = await ws.receive_json()
    except WebSocketDisconnect:
        return                                    # client left before saying hello
    session_id = brain.resolve_session(hello.get("session_id"))
    await safe_send({"type": "session", "session_id": session_id})

    # The voice stack may still be warming (Runtime loads it off-thread so the
    # page — her body — appears immediately). Wait here, per connection: the
    # socket stays open, her avatar is already up, and the greeting fires the
    # moment her voice is ready. Never uses a stand-in.
    await asyncio.to_thread(rt.voice_ready.wait)
    stt = rt.stt
    # Server-side debounced VAD (B2 §3.4, §4.2): an endpoint only becomes a turn
    # if real speech was actually heard. `gate.confirmed` read at endpoint.
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

    async def run(agen, proactive: bool = False, user_text: str = "") -> None:
        """Pump one turn's OutEvents to the client until it ends or the client goes.

        FORK(B2 §10) #4/#5 live here: spoken sentences accumulate into a `draft`
        on the hub and commit as a `message` on `done` (drop on `cancelled`);
        expression events reroute onto the puppet lane instead of this wire.
        `proactive` marks lines she spoke unprompted (greeting, ambient).
        FORK(B5 §16): a committed real turn is teed onto the SignalBus as a
        `turn_committed` signal — the mind's REFLECT share of the conversation
        (world model, promise extraction). A barged-in turn posts nothing."""
        rt.turn_started()                          # FORK(B2 §10): the mind (§15.3)
        spoken: list[str] = []                     # FORK(B2 §10): the draft
        try:
            async for ev in agen:
                if ev.kind == "expression":        # FORK(B2 §10): one lane (§10)
                    rt.controller.set_expression(ev.expression, 1.0, reset_ms=0)
                    continue
                if ev.kind == "audio" and ev.text:  # FORK(B2 §10): the draft grows
                    spoken.append(ev.text)
                    rt.hub.publish("draft", {"text": " ".join(spoken)})
                elif ev.kind == "done" and spoken:  # FORK(B2 §10): commit
                    rt.post_message("assistant", " ".join(spoken),
                                    proactive=proactive)
                    if user_text:                  # FORK(B5 §16): the tee
                        rt.signals.post("turn_committed",
                                        {"text": user_text,
                                         "reply": " ".join(spoken)},
                                        source="voice")
                elif ev.kind in ("cancelled", "error"):   # FORK(B2 §10): no trace
                    rt.hub.publish("draft_cancel", {})
                if not await safe_send(_encode(ev)):
                    controller.cancel()        # client vanished → tear the turn down
                    return
        except Exception:
            log.exception("turn stream failed")
            rt.hub.publish("draft_cancel", {})     # FORK(B2 §10)
            await safe_send({"type": "error", "message": "turn failed"})
        finally:
            rt.turn_ended()                        # FORK(B2 §10)

    # FORK(B2 §10): the ambient injector (SPEC §15.5). The mind calls this to
    # speak a self-initiated line — a murmur, a timer announcement, a reach-out
    # — THROUGH this connection: same TurnController, so a barge-in cancels her
    # own initiative the same way it cancels a reply. Returns False when a turn
    # is already in flight — the mind treats that as "she's busy".
    async def inject(cue: str) -> bool:
        nonlocal turn_task
        if turn_task and not turn_task.done():
            return False
        turn_task = asyncio.create_task(run(
            controller.run_turn(session_id, "", persist=False,
                                tokens=brain.stream_ambient(session_id, cue)),
            proactive=True))                       # FORK(B2 §10): she reached out
        return True

    rt.attach_ambient(session_id, inject)

    # she speaks first (B2 §7): greet from memory the moment the headset goes on —
    # but only once per session. Check-and-mark is atomic on the event loop.
    if session_id not in rt.greeted:
        rt.greeted.add(session_id)
        turn_task = asyncio.create_task(run(
            controller.run_turn(session_id, "", persist=False,
                                tokens=brain.stream_greeting(session_id)),
            proactive=True))                       # FORK(B2 §10): she speaks first

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"] is not None:
                # a mic frame during the user's turn → feed STT (endpointing on
                # the client); the VAD verdict feeds the gate (B2 §3.4).
                frame = np.frombuffer(msg["bytes"], dtype=np.float32)
                stt.feed(frame, 16000)
                if rt.cfg.vad_confirm and rt.vad is not None:
                    gate.push(rt.vad.is_speech(frame, 16000))
                continue

            data = _loads(msg.get("text"))
            kind = data.get("type")

            if kind == "bargein":
                controller.cancel()                # tears down TTS + generation
                continue

            if kind == "endpoint" or kind == "text":
                # a new turn starts: make sure the previous one is torn down first
                if turn_task and not turn_task.done():
                    controller.cancel()
                    await asyncio.gather(turn_task, return_exceptions=True)
                if kind == "text":
                    text = data.get("text")        # typed input skips STT + VAD
                else:
                    # endpoint: transcribe — only if the server's VAD confirmed
                    # real speech in the utterance (B2 §4.2).
                    text = stt.final() if (not rt.cfg.vad_confirm or gate.confirmed) else ""
                stt.reset()
                gate.reset()
                # last net: a punctuation-only hallucination is not a turn (B2 §3.2)
                if not is_meaningful_transcript(text):
                    continue
                # FORK(B2 §10): the user's turn joins the transcript — this is
                # what makes a *spoken* turn visible in the chat panel (§2.6)
                rt.post_message("user", text)
                # FORK(B5 §16): and the SignalBus — the ENGAGED preempt rides it
                rt.signals.post("user_message", {"text": text}, source="voice")
                trace = TurnTrace()
                turn_task = asyncio.create_task(
                    run(controller.run_turn(session_id, text, trace=trace),
                        user_text=text))
    except WebSocketDisconnect:
        pass
    finally:
        rt.detach_ambient(session_id)              # FORK(B2 §10)
        if turn_task and not turn_task.done():
            controller.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)


def _loads(text: str | None) -> dict:
    import json
    try:
        return json.loads(text) if text else {}
    except (ValueError, TypeError):
        return {}
