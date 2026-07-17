"""The Telegram channel (SPEC §10.5) — she's in your pocket.

A thin adapter on the `base.Channel` contract, raw Bot API over httpx — no
bot-framework dependency for two HTTP calls:

  - **inbound**: one long-poll task on `getUpdates`. A text message from *the
    one configured chat* becomes an ordinary text turn (`rt.turns.run`), with
    a `typing…` chat action while she thinks. Telegram is an asynchronous
    inbox, so the channel never posts `user_present` — she is *reachable*
    there, not watched; the `user_message` signal each real message produces
    is what preempts the mind to ENGAGED.
  - **outbound**: one delivery task draining an EventHub subscription. Every
    committed assistant `message` — replies to any channel, greetings, and
    the mind's proactive lines — is sent to the chat, so a reach-out decided
    at 3pm lands on your phone even though no page is open. A selfie entry
    sends the PNG itself (the file is local; a chat client can't reach
    `/selfies/`).

Single-user discipline (the companion is one-on-one, SPEC §1): messages from
any chat but `TELEGRAM_CHAT_ID` are dropped. With the id unset the channel
starts in **pairing mode**: it replies to the first message with the chat id
to put in `.env` and processes nothing — binding her to a stranger's DM by
accident is the failure this refuses.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from .base import Channel

log = logging.getLogger("world.telegram")

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 50          # Telegram's long-poll window
RETRY_BACKOFF_S = 3.0        # after a network error; polling is idempotent
MAX_MESSAGE_CHARS = 4096     # Telegram's hard sendMessage cap


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str = "", *,
                 selfie_dir: Path | None = None,
                 api_base: str = API_BASE,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.chat_id = str(chat_id or "")
        self.selfie_dir = selfie_dir
        self.api_base = api_base
        self._transport = transport          # tests inject a MockTransport
        self.rt = None
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue | None = None
        # per-chat session ids, this process's lifetime: her *memory* is the
        # Vault; this only keeps the short conversation window stitched.
        self._sessions: dict[str, str | None] = {}

    # ---- lifecycle ----

    async def start(self, rt) -> str:
        self.rt = rt
        self._client = httpx.AsyncClient(
            base_url=f"{self.api_base}/bot{self.token}",
            timeout=httpx.Timeout(POLL_TIMEOUT_S + 10, connect=10),
            transport=self._transport)
        me = await self._api("getMe")        # validates the token, loudly, now
        self._queue = rt.hub.subscribe()
        self._tasks = [
            asyncio.create_task(self._poll(), name="telegram-poll"),
            asyncio.create_task(self._deliver(), name="telegram-deliver"),
        ]
        who = "@" + me.get("username", "?")
        return who if self.chat_id else f"{who} · pairing (TELEGRAM_CHAT_ID unset)"

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._queue is not None and self.rt is not None:
            self.rt.hub.unsubscribe(self._queue)
            self._queue = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- the Bot API ----

    async def _api(self, method: str, **params):
        resp = await self._client.post(f"/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: "
                               f"{data.get('description', resp.status_code)}")
        return data["result"]

    # ---- inbound: long poll → text turns ----

    async def _poll(self) -> None:
        offset = None
        while True:
            try:
                updates = await self._api(
                    "getUpdates", timeout=POLL_TIMEOUT_S,
                    allowed_updates=["message"],
                    **({"offset": offset} if offset is not None else {}))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the net flaps; polling resumes
                log.warning("telegram poll error (retrying): %s", e)
                await asyncio.sleep(RETRY_BACKOFF_S)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self._handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception:   # one bad message never stops the inbox
                    log.exception("telegram update failed")

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            return
        if not self.chat_id:                 # pairing mode: introduce, bind nothing
            log.warning("telegram: message from chat %s but TELEGRAM_CHAT_ID is "
                        "unset — set TELEGRAM_CHAT_ID=%s to pair", chat_id, chat_id)
            await self._api("sendMessage", chat_id=chat_id, text=(
                "This companion isn't paired yet. If this is your bot, set "
                f"TELEGRAM_CHAT_ID={chat_id} in its .env and restart."))
            return
        if chat_id != self.chat_id:          # one person, one chat (SPEC §1)
            log.warning("telegram: ignoring message from unconfigured chat %s",
                        chat_id)
            return
        text = msg.get("text")
        if not text:
            await self._api("sendMessage", chat_id=chat_id,
                            text="(I can only read text here, for now.)")
            return
        await self._api("sendChatAction", chat_id=chat_id, action="typing")
        result = await self.rt.turns.run(
            text, channel=self.name, session_id=self._sessions.get(chat_id))
        self._sessions[chat_id] = result["session_id"]
        # the reply itself arrives via _deliver — one outbound path, no echoes

    # ---- outbound: the hub → the chat ----

    async def _deliver(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._deliver_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a dropped line, not a dead channel
                log.warning("telegram delivery failed: %s", e)

    async def _deliver_event(self, event: dict) -> None:
        """Send one hub event to the chat, if it's hers to hear: committed
        assistant lines only (drafts and puppet traffic stay in the room)."""
        if not self.chat_id:
            return
        if event.get("type") != "message" or event.get("role") != "assistant":
            return
        text = event.get("text", "")
        image = self._selfie_path(event.get("image_url"))
        if image is not None:
            await self._send_photo(image, caption=text)
            return
        if not text:
            return
        for i in range(0, len(text), MAX_MESSAGE_CHARS):
            await self._api("sendMessage", chat_id=self.chat_id,
                            text=text[i:i + MAX_MESSAGE_CHARS])

    def _selfie_path(self, image_url: str | None) -> Path | None:
        """A `message` with an image carries a local `/selfies/<name>` URL;
        resolve it against the selfie dir (jailed, like the route is)."""
        if not image_url or self.selfie_dir is None:
            return None
        base = self.selfie_dir.resolve()
        path = (base / Path(image_url).name).resolve()
        return path if path.parent == base and path.is_file() else None

    async def _send_photo(self, path: Path, caption: str) -> None:
        resp = await self._client.post(
            "/sendPhoto",
            data={"chat_id": self.chat_id, "caption": caption[:1024]},
            files={"photo": (path.name, path.read_bytes(), "image/png")})
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram sendPhoto: {data.get('description')}")
