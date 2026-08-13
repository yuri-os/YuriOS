"""Bounded resources and liveness checks for the shared voice WebSocket wire."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect


CAPACITY_CLOSE = 4429
LIMIT_CLOSE = 4409
TIMEOUT_CLOSE = 4408
MAX_TYPED_TEXT_BYTES = 16_384
MAX_SESSION_ID_BYTES = 128


class VoiceSocketClosed(Exception):
    """The guard closed a socket after a timeout or resource violation."""


class VoiceConnectionLimiter:
    """A non-blocking process-local connection counter."""

    def __init__(self, maximum: int):
        self.maximum = max(1, int(maximum))
        self.active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self.active = max(0, self.active - 1)


class VoiceSocketGuard:
    """Per-socket timing, frame, and cumulative utterance accounting."""

    def __init__(self, ws: WebSocket, cfg):
        self.ws = ws
        self.initial_timeout = max(0.01, float(cfg.voice_ws_initial_timeout_s))
        self.idle_timeout = max(0.01, float(cfg.voice_ws_idle_timeout_s))
        self.heartbeat = max(0.0, float(cfg.voice_ws_heartbeat_s))
        self.max_frame_bytes = max(4, int(cfg.voice_ws_max_frame_bytes))
        self.max_message_bytes = max(256, int(cfg.voice_ws_max_message_bytes))
        self.max_utterance_samples = max(
            1, int(16000 * float(cfg.voice_ws_max_utterance_s)))
        self.utterance_samples = 0

    async def receive_initial(self) -> dict:
        try:
            return await asyncio.wait_for(self.ws.receive(), self.initial_timeout)
        except (TimeoutError, WebSocketDisconnect) as exc:
            await self._close(TIMEOUT_CLOSE, "voice hello timed out")
            raise VoiceSocketClosed from exc

    async def receive(self, send: Callable[[dict], Awaitable[bool]]) -> dict:
        """Receive with heartbeats while bounding time since the last client frame."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.idle_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._close(TIMEOUT_CLOSE, "voice connection idle")
                raise VoiceSocketClosed
            wait_for = remaining if self.heartbeat <= 0 else min(remaining, self.heartbeat)
            try:
                return await asyncio.wait_for(self.ws.receive(), wait_for)
            except TimeoutError:
                if loop.time() >= deadline:
                    await self._close(TIMEOUT_CLOSE, "voice connection idle")
                    raise VoiceSocketClosed
                if not await send({"type": "ping"}):
                    raise VoiceSocketClosed
            except WebSocketDisconnect as exc:
                raise VoiceSocketClosed from exc

    def accept_audio(self, payload: bytes) -> None:
        size = len(payload)
        if not size or size % 4 or size > self.max_frame_bytes:
            raise ValueError("invalid float32 audio frame")
        self.utterance_samples += size // 4
        if self.utterance_samples > self.max_utterance_samples:
            raise OverflowError("voice utterance is too long")

    def accept_text_frame(self, payload: str | None) -> None:
        if payload is not None and len(payload.encode("utf-8")) > self.max_message_bytes:
            raise OverflowError("voice text frame is too large")

    def reset_utterance(self) -> None:
        self.utterance_samples = 0

    async def reject_limit(self, message: str) -> None:
        try:
            await self.ws.send_json({"type": "error", "message": message})
        except (WebSocketDisconnect, RuntimeError):
            pass
        await self._close(LIMIT_CLOSE, message)

    async def _close(self, code: int, reason: str) -> None:
        try:
            await self.ws.close(code=code, reason=reason)
        except (WebSocketDisconnect, RuntimeError):
            pass


async def reject_capacity(ws: WebSocket) -> None:
    """Complete the handshake so browser clients receive a useful close code."""
    await ws.accept()
    try:
        await ws.send_json({"type": "error", "message": "voice connection limit reached"})
        await ws.close(code=CAPACITY_CLOSE, reason="voice connection limit reached")
    except (WebSocketDisconnect, RuntimeError):
        pass


def uvicorn_ws_options(cfg) -> dict:
    """Transport-level ceilings used by every first-party voice launcher."""
    heartbeat = float(cfg.voice_ws_heartbeat_s)
    return {
        "ws_max_size": max(1024, int(cfg.voice_ws_max_message_bytes)),
        "ws_max_queue": max(1, int(cfg.voice_ws_max_queue)),
        "ws_ping_interval": heartbeat if heartbeat > 0 else None,
        "ws_ping_timeout": max(0.1, float(cfg.voice_ws_idle_timeout_s)),
        "ws_per_message_deflate": False,
    }


def bounded_text(value: object, *, maximum: int, field: str,
                 optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} is too long or invalid")
    return value
