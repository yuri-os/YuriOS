"""/ws/voice — the real-time voice loop (SPEC §2.2, §9, §10).

The turn spine — the audio wire, the SpeechGate, the greeting-once logic, the
barge-in path — pumps one turn's OutEvents at a time through a TurnController.
On top of that spine:

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
     rule: a turn that didn't happen leaves no trace (§4.4);
  5. expressions leave this wire: an expression OutEvent becomes a
     `controller.set_expression(…, reset_ms=0)` — one lane for the face, so
     both bodies and every open page see turn emotions on the hub (SPEC §10);
  6. the signal tee (SPEC §16): a user turn posts `user_message` (the mind's
     ENGAGED preempt) and a committed exchange posts `turn_committed` (the
     mind's REFLECT share: world model, promise extraction) onto the
     SignalBus. The reply itself still streams on this reactive path — the
     loop observes the conversation, it never sits in front of it (SPEC §15.3);
  7. the voice stack's lifetime (SPEC §9.9): this socket is the only thing in
     the process that wants her ears and voice, so it holds them. The first
     connection warms the stack (and says `warming` while it does), every later
     one joins it, and the last one out drops it — otherwise a host running six
     characters warms six of them at boot for nobody (world/voicestack.py).

Client → server messages (JSON, except audio which is binary frames):
    {"type":"hello", "session_id": "<optional prior id>"}
    (binary)                     one Float32 PCM mic frame @ 16 kHz (during speech)
    {"type":"endpoint"}          the user's turn is done → transcribe + reply
    {"type":"bargein"}           the user talked over her → cancel the current turn
    {"type":"text", "text":...}  typed input (the chat composer; skips STT)

Server → client messages (JSON; audio PCM is base64 in `pcm`):
    {"type":"session", "session_id":...}
    {"type":"warming", "message":...}   her voice is loading — first one in (§9.9)
    {"type":"ready"}                    …and it landed; the composer reopens
    {"type":"filler"|"audio", "text":..., "sr":..., "pcm": <base64 float32>}
    {"type":"done", "latency":..., "expression":...} | {"type":"cancelled"}
    {"type":"error", "message":...}
(expressions and chat text now ride /api/events — SPEC §10)
"""
from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import nullcontext

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

    # This socket IS "the user entered the room" (SPEC §9.9): her voice loads
    # here, for as long as somebody is holding one open, and is freed a beat
    # after the last one closes — which is what keeps a node full of characters
    # from warming a Kokoro nobody is listening to. Waiting is per connection:
    # the socket stays open, her avatar is already up, and the greeting fires the
    # moment her voice is ready. Never uses a stand-in.
    #
    # Announced FIRST, before even the session id: the notice is what closes the
    # client's composer, and the gap between "socket open" and "server actually
    # reading" is precisely when a typed line is lost. Nothing below this line
    # is read off the wire until `acquire` returns.
    warming = not rt.voice.loaded
    if warming:
        # Say so rather than looking hung: the client captions it and shuts the
        # composer, and a cold stack is ~20 s of silence (web/js/voice.js).
        await safe_send({"type": "warming", "message": "loading her voice…"})
    await safe_send({"type": "session", "session_id": session_id})
    # `acquire` counts this listener BEFORE it warms anything, so it belongs
    # inside the release scope, not in front of it: a warm that raises would
    # otherwise leave the count above zero for the life of the process, and
    # `unload` returns early forever — her weights pinned by a connection that
    # never actually got into the room. That is the exact leak the split below
    # exists to prevent, so the guard has to start here.
    try:
        await rt.voice.acquire()
        # Always send the all-clear. A demand-driven client closes this socket
        # while muted, and a later reconnect may find an already-warm stack
        # without ever receiving the `warming` frame.
        await safe_send({"type": "ready"})
        await _in_the_room(ws, rt, session_id, safe_send)
    finally:
        # every way out of the room — a clean close, a reload, a raise — puts the
        # stack down; the last one to leave takes her voice with them (§9.9)
        rt.voice.release()


async def _in_the_room(ws: WebSocket, rt, session_id: str, safe_send) -> None:
    """One connected client's turn machinery, for as long as they're here.

    Split from the handshake above only so the voice stack is acquired and
    released around exactly this — a listener that leaked because something in
    here raised would pin her weights in memory for the life of the process,
    which is the bug this whole seam exists to avoid.
    """
    brain = rt.brain
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

    async def run(agen, proactive: bool = False, user_text: str = "",
                  commit_text: str | None = None,
                  client_id: str | None = None) -> None:
        """Pump one turn's OutEvents to the client until it ends or the client goes.

        Spoken sentences accumulate into a `draft` on the hub and commit as a
        `message` on `done` (drop on `cancelled`); expression events reroute
        onto the puppet lane instead of this wire. `proactive` marks lines she
        spoke unprompted (greeting, ambient). A committed real turn is teed
        onto the SignalBus as a `turn_committed` signal — the mind's REFLECT
        share of the conversation (world model, promise extraction). A
        barged-in turn posts nothing.

        `commit_text` is the cold open's exemption (§5.4): what she *shows* is
        given, not accumulated from what she said. Everything the pipeline does
        on the way to TTS — the expression tags, the `*narration*` strip — is
        right for speech and wrong for an authored scene, and a scene committed
        from its audio arrives as the fragments that happened to be in quotes.
        So the text goes up once, whole, as the draft, the audio plays under it,
        and `done` commits the same text the card carries."""
        # `agen` hasn't run a line yet (async generators are lazy), so this is
        # still before the brain is touched: hold the turn while a selfie has
        # her VRAM, and hold it before `turn_started` — the park's quiet gate
        # waits on that counter (§7.6, world/vram.py).
        await safe_send({"type": "processing", "client_id": client_id})
        await rt.park_gate.wait()
        rt.turn_started(proactive=proactive)       # the mind (§15.3)
        spoken: list[str] = []                     # the draft
        if commit_text:
            rt.hub.publish("draft", {"text": commit_text})
        try:
            turn_context = getattr(brain, "turn_context", None)
            context = turn_context(channel="voice", client_id=client_id) \
                if turn_context else nullcontext()
            with context:
                async for ev in agen:
                    if ev.kind == "expression":        # one lane for the face (§10)
                        rt.controller.set_expression(ev.expression, 1.0, reset_ms=0)
                        continue
                    if ev.kind == "audio" and ev.text:  # the draft grows
                        spoken.append(ev.text)
                        if not commit_text:            # …unless the text is given
                            rt.hub.publish("draft", {"text": " ".join(spoken)})
                    elif ev.kind == "done" and (spoken or commit_text):  # commit
                        rt.post_message("assistant", commit_text or " ".join(spoken),
                                        proactive=proactive, channel="voice")
                        if user_text:                  # the SignalBus tee
                            rt.signals.post("turn_committed",
                                            {"text": user_text,
                                             "reply": " ".join(spoken)},
                                            source="voice")
                    elif ev.kind in ("cancelled", "error"):   # no trace
                        rt.hub.publish("draft_cancel", {})
                    payload = _encode(ev)
                    if ev.kind == "done":
                        payload["client_id"] = client_id
                        payload["active_selfies"] = (
                            rt.selfies.active_ids(client_id)
                            if rt.selfies and client_id else [])
                    if not await safe_send(payload):
                        controller.cancel()        # client vanished → tear the turn down
                        return
        except Exception:
            log.exception("turn stream failed")
            rt.hub.publish("draft_cancel", {})
            await safe_send({"type": "error", "message": "turn failed"})
        finally:
            rt.turn_ended()
            # "no trace" has to mean her memory too. `stream_reply`
            # writes the user's line into the session window before the first
            # token (the model must see it); `persist` writes her half. Every way
            # out of the pump that isn't a clean commit — barge-in, brain error,
            # a client that vanished mid-turn — leaves only the first half, and
            # the next prompt reads it as a question she still owes an answer to:
            # she replies to it a second time, folded into the new turn. This is
            # the rollback. A no-op after a clean turn (persist took the pending)
            # and for greeting/ambient lines (they never append one).
            brain.abandon(session_id)

    # the ambient injector (SPEC §15.5). The mind calls this to
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
            proactive=True))                       # she reached out
        return True

    rt.attach_ambient(session_id, inject)

    # she speaks first (B2 §7): greet from memory the moment the headset goes on —
    # but only once per session. Check-and-mark is atomic on the event loop.
    if session_id not in rt.greeted:
        rt.greeted.add(session_id)
        turn_task = asyncio.create_task(run(
            controller.run_turn(session_id, "", persist=False,
                                tokens=brain.stream_greeting(session_id)),
            proactive=True,                        # she speaks first
            commit_text=brain.cold_open()))        # …and on the first-ever
                                                   # arrival, from the card (§5.4)

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

            if kind == "reset_audio":
                stt.reset()
                gate.reset()
                continue

            if kind == "cancel":
                controller.cancel()
                selfie_ids = data.get("selfie_ids")
                cancel_client = data.get("client_id")
                if rt.selfies and isinstance(cancel_client, str) and cancel_client:
                    ids = [str(i) for i in selfie_ids[:16]] \
                        if isinstance(selfie_ids, list) else []
                    await rt.selfies.cancel(
                        ids, client_id=cancel_client)
                await safe_send({"type": "cancelled",
                                 "client_id": data.get("client_id")})
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
                    if kind == "text":
                        await safe_send({"type": "rejected",
                                         "client_id": data.get("client_id"),
                                         "message": "not a meaningful turn"})
                    continue
                # the user's turn joins the transcript — this is
                # what makes a *spoken* turn visible in the chat panel (§2.6)
                client_id = data.get("client_id")
                if not isinstance(client_id, str) or len(client_id) > 64:
                    client_id = None
                entry = rt.post_message("user", text, channel="voice",
                                        client_id=client_id)
                await safe_send({"type": "accepted", "message": entry,
                                 "client_id": client_id})
                # …and the SignalBus — the ENGAGED preempt rides it
                rt.signals.post("user_message", {"text": text}, source="voice")
                trace = TurnTrace()
                turn_task = asyncio.create_task(
                    run(controller.run_turn(session_id, text, trace=trace),
                        user_text=text, client_id=client_id))
    except WebSocketDisconnect:
        pass
    finally:
        rt.detach_ambient(session_id)
        if turn_task and not turn_task.done():
            controller.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)


def _loads(text: str | None) -> dict:
    import json
    try:
        return json.loads(text) if text else {}
    except (ValueError, TypeError):
        return {}
