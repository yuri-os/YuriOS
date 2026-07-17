"""The channel manager (SPEC §10.5) — builds the configured adapters and runs
their lifecycle beside the server's. A channel that fails to start is one
degraded medium, never a down host: she keeps talking everywhere else, and
/api/boot says what happened."""
from __future__ import annotations

import logging

from .base import Channel

log = logging.getLogger("world.channels")


class ChannelManager:
    def __init__(self, channels: list[Channel]):
        self.channels = channels
        self._running: list[Channel] = []

    @classmethod
    def from_config(cls, cfg) -> "ChannelManager":
        """The one place config becomes adapters. A channel is on when its
        credentials are set — no separate enable flag to forget."""
        channels: list[Channel] = []
        if cfg.telegram_bot_token:
            from .telegram import TelegramChannel
            channels.append(TelegramChannel(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                selfie_dir=cfg.selfie_dir))
        return cls(channels)

    @property
    def configured(self) -> bool:
        return bool(self.channels)

    async def start_all(self, rt) -> tuple[str, bool]:
        """Start every configured channel. Returns (boot detail, any_ok)."""
        details: list[str] = []
        ok = False
        for ch in self.channels:
            try:
                detail = await ch.start(rt)
                self._running.append(ch)
                details.append(f"{ch.name} · {detail}" if detail else ch.name)
                ok = True
                log.info("channel up: %s (%s)", ch.name, detail)
            except Exception as e:  # noqa: BLE001 — one dead medium, not a dead host
                log.exception("channel %s failed to start", ch.name)
                details.append(f"{ch.name} failed: {str(e)[:60]}")
        return " · ".join(details), ok

    async def stop_all(self) -> None:
        for ch in self._running:
            try:
                await ch.stop()
            except Exception:
                log.exception("channel %s failed to stop", ch.name)
        self._running.clear()
