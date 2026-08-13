"""The channel manager (SPEC §10.5) — builds the configured adapters and runs
their lifecycle beside the server's. A channel that fails to start is one
degraded medium, never a down host: she keeps talking everywhere else, and
/api/boot says what happened."""
from __future__ import annotations

import logging

from .base import Channel

log = logging.getLogger("world.channels")

# Who holds which outside account, process-wide (see Channel.claim). One host
# runs a runtime per character, so two of them on one Telegram bot would fight
# over its updates forever. Each character normally has her own credentials
# (world/host.py's telegram_for_character); this is the backstop for the pair
# they can still fall back to — and for any medium a future adapter can only
# hold once. Keyed by the channel's claim, valued with the character holding it,
# so a second character's boot panel can say whose it is.
_claims: dict[tuple[str, str], str] = {}


class ChannelManager:
    def __init__(self, channels: list[Channel]):
        self.channels = channels
        self._running: list[Channel] = []
        self._claimed: list[tuple[str, str]] = []

    @classmethod
    def from_config(cls, cfg) -> "ChannelManager":
        """The one place config becomes adapters. A channel is on when its
        credentials are set — no separate enable flag to forget. Under the host
        those credentials are already this character's own (world/host.py's
        `telegram_for_character`), so a second character with her own bot builds
        a second, uncontended adapter here."""
        channels: list[Channel] = []
        # The one channel with no credentials to be switched on by, so it carries
        # an explicit flag instead — and defaults off (SPEC §18.4). It contends
        # over nothing (no `claim`): every character may raise her own
        # notifications, because the thing being contended for is your attention,
        # and that is already rationed per character by Gate 2.
        if cfg.notify_enabled and cfg.notify_backend != "off":
            from .notify import NotifyChannel
            channels.append(NotifyChannel(
                backend=cfg.notify_backend,
                app_name=cfg.companion_name or "YuriOS",
                character_id=cfg.character_id))
        if cfg.telegram_bot_token:
            from .telegram import TelegramChannel
            channels.append(TelegramChannel(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                selfie_dir=cfg.selfie_dir,
                chat_id_env=cfg.telegram_chat_id_env,
                sending_enabled=cfg.telegram_send_non_telegram))
        return cls(channels)

    @property
    def configured(self) -> bool:
        return bool(self.channels)

    async def start_all(self, rt) -> tuple[str, bool]:
        """Start every configured channel. Returns (boot detail, any_ok).

        A channel whose account another character already holds is skipped, not
        failed: nothing is broken, the medium simply belongs to her."""
        details: list[str] = []
        ok = False
        who = getattr(getattr(rt, "cfg", None), "companion_name", "") or "another character"
        for ch in self.channels:
            claim = ch.claim
            if claim is not None:
                holder = _claims.get(claim)
                if holder is not None:
                    details.append(f"{ch.name} · held by {holder}")
                    log.info("channel %s: already held by %s — one account serves "
                             "one character; give this one its own credentials to "
                             "run both", ch.name, holder)
                    ok = True
                    continue
                _claims[claim] = who
                self._claimed.append(claim)
            try:
                detail = await ch.start(rt)
                self._running.append(ch)
                details.append(f"{ch.name} · {detail}" if detail else ch.name)
                ok = True
                log.info("channel up: %s (%s)", ch.name, detail)
            except Exception as e:  # noqa: BLE001 — one dead medium, not a dead host
                self._release(claim)        # a dead channel holds nothing
                log.exception("channel %s failed to start", ch.name)
                details.append(f"{ch.name} failed: {str(e)[:60]}")
        return " · ".join(details), ok

    def _release(self, claim: tuple[str, str] | None) -> None:
        if claim is not None and claim in self._claimed:
            self._claimed.remove(claim)
            _claims.pop(claim, None)

    async def stop_all(self) -> None:
        for ch in self._running:
            try:
                await ch.stop()
            except Exception:
                log.exception("channel %s failed to stop", ch.name)
        self._running.clear()
        # …and the accounts go back on the table: stopping a character (or
        # restarting it after a card edit) must leave its bot claimable again.
        for claim in list(self._claimed):
            self._release(claim)
