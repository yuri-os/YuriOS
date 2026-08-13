"""The notification channel (SPEC §10.5, §18.4) — the last resort for a reach-out.

Every other channel is a place she can be *talked to*. This one is outbound only:
it has no inbound seam, posts no presence, claims no account, and carries exactly
one kind of traffic — the lines she decided to say on her own into a room with
nobody in it (`unheard`, stamped by `Runtime.post_message`).

**Why this is not "notifications".** The design rule is that the journal, not a
stream of pings, carries the value, and that rule is intact: nothing here decides
to interrupt you. Gate 2 already did that, in `MindLoop._act_reach_out`, against
a budget of two or three a day, before this channel ever sees the line. What was
missing was transport. A reach-out with no page open and no Telegram credentials
had precisely zero subscribers and evaporated — she paid for an interrupt that
was never delivered. This is the delivery, not a new reason to interrupt.

It is **off by default** (`NOTIFY_ENABLED`), because a program that starts
putting things on your desktop without being asked has earned every bit of the
suspicion it gets.

**One effector, two renderers.** The policy — is it on, is this line hers to
deliver — lives here, once. What varies is only how a notification is drawn:

  - `shell` — the Electron desktop shell (`desktop-shell/main.js`), which draws a
    real system notification you can click to open her room. It reads
    `GET /api/notifications`, a stream this channel owns.

    The shell **MUST NOT** read `/api/events` for this: attaching there posts a
    `user_present` signal (routes/events.py) and the shell is attached for as
    long as it is running. She would believe you were in the room permanently,
    Gate 2 would suppress every reach-out as an interruption of a conversation
    already in progress, and the feature would silence exactly what it exists to
    deliver. Hence a separate stream, with its own fan-out — deliberately *not*
    an `EventHub` subscription, which would also make the room look occupied to
    `_body_reflexes`.

  - `libnotify` — `notify-send`, the freedesktop notification the daemon can
    raise with no shell running at all. This is the case the whole feature is
    for: a headless always-on install where nothing is open.

`auto` (the default when enabled) prefers the shell when one is attached and
falls back to `notify-send`, so plugging in the desktop app upgrades the
notification and unplugging it degrades gracefully instead of going quiet.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

from .base import Channel

log = logging.getLogger("world.notify")

#: A notification is a doorbell, not the message. Anything longer is truncated
#: by the notification daemon anyway, usually mid-word and without an ellipsis.
MAX_BODY_CHARS = 180

BACKENDS = ("auto", "shell", "libnotify", "off")


class NotifyChannel(Channel):
    name = "notify"

    def __init__(self, *, backend: str = "auto", app_name: str = "YuriOS",
                 character_id: str = "", notifier=None):
        self.backend = backend if backend in BACKENDS else "auto"
        self.app_name = app_name
        self.character_id = character_id
        # tests inject an async (title, body, payload) -> None; production
        # resolves a real one per notification (the shell may come and go).
        self._notifier = notifier
        self.rt = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        # the shell fan-out: /api/notifications subscribes here. Not the
        # EventHub, on purpose — see the module docstring.
        self._listeners: list[asyncio.Queue] = []
        self._warned_missing = False

    # ---- lifecycle -----------------------------------------------------------

    async def start(self, rt) -> str:
        self.rt = rt
        self._queue = rt.hub.subscribe()
        self._task = asyncio.create_task(self._deliver(), name="notify-deliver")
        return f"{self.backend} · {self._available()}"

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._queue is not None and self.rt is not None:
            self.rt.hub.unsubscribe(self._queue)
            self._queue = None
        for q in list(self._listeners):
            q.put_nowait(None)              # end the open /api/notifications streams
        self._listeners.clear()

    # ---- the shell's stream --------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        """Attach a desktop shell. Posts no presence signal and takes no
        `EventHub` slot: a shell sitting in your system tray is not company."""
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    @property
    def shell_attached(self) -> bool:
        return bool(self._listeners)

    # ---- what gets through ---------------------------------------------------

    def _available(self) -> str:
        """Which renderer would draw the next notification, for the boot panel."""
        if self.backend == "off":
            return "off"
        if self.backend in ("auto", "shell") and self.shell_attached:
            return "shell"
        if self.backend in ("auto", "libnotify") and shutil.which("notify-send"):
            return "notify-send"
        return "shell (waiting)" if self.backend == "shell" else "nothing available"

    async def _deliver(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._deliver_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a missed doorbell, not a dead host
                log.warning("notification delivery failed", exc_info=True)

    async def _deliver_event(self, event: dict) -> None:
        """One hub event → at most one notification.

        The filter is `unheard`, not `proactive`. Proactive covers every line she
        starts — greetings, a murmur while you watch her, a selfie you asked for
        arriving late — and notifying you about a greeting you are currently
        being greeted by is the behaviour that gives notifications a bad name.
        `unheard` is stamped only where nobody may have been listening
        (`MindLoop._act_reach_out`, an unprompted selfie).
        """
        if self.backend == "off":
            return
        if event.get("type") != "message" or event.get("role") != "assistant":
            return
        if not event.get("unheard"):
            return
        title = getattr(getattr(self.rt, "cfg", None), "companion_name", "") or "YuriOS"
        text = (event.get("text") or "").strip()
        if event.get("image_url") and not text:
            text = "sent you a picture."
        if not text:
            return
        body = text if len(text) <= MAX_BODY_CHARS else text[:MAX_BODY_CHARS - 1] + "…"
        await self._notify(title, body, event)

    # ---- the renderers -------------------------------------------------------

    async def _notify(self, title: str, body: str, event: dict) -> None:
        if self._notifier is not None:
            await self._notifier(title, body, event)
            return
        payload = {"type": "notify", "character": self.character_id,
                   "title": title, "body": body,
                   "message_id": event.get("id"),
                   "kind": "selfie" if event.get("image_url") else "message"}
        if self.backend in ("auto", "shell") and self.shell_attached:
            for q in list(self._listeners):
                q.put_nowait(payload)
            return
        if self.backend == "shell":
            log.debug("notify: no shell attached; %s waits in her inbox", title)
            return
        await self._notify_send(title, body)

    async def _notify_send(self, title: str, body: str) -> None:
        """freedesktop, via the binary rather than a D-Bus dependency: two
        arguments do not justify pulling in `dbus-next`, and `notify-send` is
        present wherever a notification daemon is.

        Installed is not the same as *working*: `notify-send` exits non-zero when
        it cannot reach a notification daemon, which is the ordinary state of a
        headless box, an ssh session, and WSL without one running — and those are
        exactly the always-on installs this backend was written for. A silent
        failure there looks identical to a companion who never reached out, so
        the exit code is checked and said once.
        """
        exe = shutil.which("notify-send")
        if exe is None:
            self._warn_undelivered(
                "notify-send isn't installed. Install libnotify-bin, or run the "
                "desktop shell")
            return
        proc = await asyncio.create_subprocess_exec(
            exe, "--app-name", self.app_name, "--expire-time", "12000",
            title, body,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            self._warn_undelivered(
                f"notify-send failed ({proc.returncode}"
                f"{': ' + detail[:120] if detail else ''}). Usually no "
                f"notification daemon on this session (headless, ssh, or WSL)")

    def _warn_undelivered(self, why: str) -> None:
        """Say it once, and say what still worked. A reach-out that missed the
        screen is not a lost reach-out — the inbox holds it either way, and the
        switchboard is already marking her tile."""
        if self._warned_missing:
            return
        self._warned_missing = True
        log.warning("NOTIFY_ENABLED is on but %s. Her reach-outs are still filed "
                    "in her inbox and show when you next open her room.", why)
