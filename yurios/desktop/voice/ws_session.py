"""The part of /ws/voice that is the same in both bodies.

There are two voice routes — `desktop/routes/voice_ws.py` (the native window)
and `world/routes/voice_ws.py` (the browser, plus every fork the mind needs) —
and they are different on purpose: only one of them has a hub, a SignalBus, a
selfie lab or an ambient injector. What they are *not* allowed to differ on is
the wire underneath all of that, because that is where the safety lives:

  * the capacity limiter, acquired before the socket is even accepted, released
    by every exit (`serve`);
  * the hello exchange — the first frame must be a `hello`, it is size-capped
    before it is parsed, and the session id it carries is bounded and then
    *resolved by the brain* rather than trusted (`open_session`). A client does
    not get to name a session; it gets to ask for one;
  * the mic-frame path — every audio frame is checked against the per-frame and
    per-utterance ceilings before a single sample is copied (`Utterance.feed`);
  * a send that cannot raise on a client that has already gone (`make_sender`).

Each of those was written once, forked, and then had to be fixed twice. This
module is where they live now; what stays in each route is the part that is
genuinely that body's own.

Not here on purpose: the `session`/`warming`/`ready` frames. The two routes
send them in a different *order* — the browser announces a cold voice stack
ahead of everything, because that notice is what closes its composer — and an
ordering that matters is not a detail to hide behind a helper.
"""
from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .speech_gate import SpeechGate
from .turn import OutEvent, TurnController
from .ws_limits import (
    MAX_SESSION_ID_BYTES, VoiceSocketClosed, VoiceSocketGuard, bounded_text,
    reject_capacity,
)

Sender = Callable[[dict], Awaitable[bool]]


async def serve(ws: WebSocket, handler: Callable[[WebSocket, object], Awaitable[None]],
                ) -> None:
    """Hold one of the process's voice-connection slots for `handler`'s lifetime.

    The count is taken before the handshake and given back by every exit,
    including a raise: a slot leaked here is a slot nobody can ever have again.
    """
    rt = ws.app.state.rt
    limiter = rt.voice_ws_limiter
    if not limiter.try_acquire():
        await reject_capacity(ws)
        return
    try:
        await handler(ws, rt)
    finally:
        limiter.release()


def make_sender(ws: WebSocket) -> Sender:
    """A send that returns False instead of raising when the client is gone.

    The client can vanish mid-turn (a reload, a reconnect) and a send after
    close raises; every caller treats that as 'stop', not as an error.
    """
    async def safe_send(data: dict) -> bool:
        if ws.application_state != WebSocketState.CONNECTED:
            return False
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError):
            return False
    return safe_send


async def open_session(ws: WebSocket, guard: VoiceSocketGuard, brain) -> str | None:
    """The hello exchange. Returns the session id, or None if the socket is done.

    None means the socket has already been closed — a client that left before
    saying hello, or one the guard rejected — and the caller's only correct move
    is to return. Nothing is sent on success: the routes announce the session in
    their own order (see the module docstring).

    The session id is *asked for*, not given: whatever the client sends is size-
    bounded and then handed to `brain.resolve_session`, which returns a live id
    it already knows or mints a new one. That is what keeps this frame from
    being a way to read somebody else's conversation.
    """
    try:
        message = await guard.receive_initial()
    except VoiceSocketClosed:
        return None                               # client left before saying hello
    try:
        guard.accept_text_frame(message.get("text"))
    except OverflowError as exc:
        await guard.reject_limit(str(exc))
        return None
    hello = loads_frame(message.get("text"))
    if message.get("type") != "websocket.receive" or hello.get("type") != "hello":
        await guard.reject_limit("voice hello required")
        return None
    try:
        requested = bounded_text(hello.get("session_id"),
                                 maximum=MAX_SESSION_ID_BYTES,
                                 field="session_id", optional=True)
    except ValueError as exc:
        await guard.reject_limit(str(exc))
        return None
    return brain.resolve_session(requested)


class Utterance:
    """One connection's ears: the STT session, the VAD gate, and the byte caps.

    Held per connection and never shared — two sockets that fed one STT session
    transcribed each other's audio, which is why `create_session()` exists.
    """

    def __init__(self, rt, guard: VoiceSocketGuard):
        self._rt = rt
        self._guard = guard
        self.stt = rt.stt.create_session()
        # Server-side debounced VAD (B2 §3.4, §4.2): an endpoint only becomes a
        # turn if real speech was actually heard — the last net under a naive
        # edge gate on the client. `confirmed` is read at endpoint.
        self.gate = SpeechGate(
            onset_frames=rt.cfg.vad_onset_frames,
            bargein_frames=rt.cfg.vad_bargein_frames,
            hangover_frames=max(1, rt.cfg.vad_min_silence_ms // max(1, rt.cfg.frame_ms)))

    def feed(self, payload: bytes) -> None:
        """Take one mic frame. Raises ValueError/OverflowError past a ceiling.

        The caps are checked before `frombuffer`, so an oversized or misaligned
        frame is refused rather than copied — a socket cannot spend the
        process's memory ahead of the guard that is supposed to stop it.
        """
        self._guard.accept_audio(payload)
        frame = np.frombuffer(payload, dtype=np.float32)
        self.stt.feed(frame, 16000)
        if self._rt.cfg.vad_confirm and self._rt.vad is not None:
            self.gate.push(self._rt.vad.is_speech(frame, 16000))

    async def transcribe(self) -> str:
        """The utterance's text — empty when the VAD heard no real speech.

        All-noise (keyboard clatter that leaked past the client's edge gate) is
        dropped here rather than sent to a model (B2 §4.2).
        """
        if self._rt.cfg.vad_confirm and not self.gate.confirmed:
            return ""
        return await asyncio.to_thread(self.stt.final)

    def reset(self) -> None:
        """Between turns: the buffer, the gate, and the utterance's byte budget."""
        self.stt.reset()
        self.gate.reset()
        self._guard.reset_utterance()


def turn_controller(rt, brain) -> TurnController:
    """The per-connection TurnController, wired the same way in both bodies."""
    return TurnController(
        brain=brain, tts=rt.tts, filler_bank=rt.filler_bank,
        mask_latency=rt.cfg.mask_latency,
        expression_default=rt.cfg.expression_default,
        trace_dir=rt.cfg.trace_dir)


async def stop_turn(controller: TurnController, task: asyncio.Task | None) -> None:
    """Cancel a turn in flight and wait for its pump to actually let go.

    Awaited before anything that arms a fresh cancel token — `run_turn` and
    `speak` each replace the controller's token, so a turn left running across
    one of those would be holding a token nobody can set any more.
    """
    if task and not task.done():
        controller.cancel()
        await asyncio.gather(task, return_exceptions=True)


def encode_event(ev: OutEvent) -> dict:
    """OutEvent → a JSON-able dict; PCM is base64 float32 (B2 §10)."""
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


def loads_frame(text: str | None) -> dict:
    """A client text frame → a dict, never a raise. Junk reads as `{}`."""
    try:
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
