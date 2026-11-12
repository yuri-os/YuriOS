"""Her on the desktop, from the house that is already running (SPEC §6.5).

`python -m yurios.world --window` floats her by starting a whole house of its
own, and refuses when one is already on the port — which, with the daemon up, it
always is. This is the other direction: the running host spawns a window-only
process (`--attach <id>`, `world/window.py`) pointed back at itself, one per
character. The switchboard's Desktop button and the tray's character rows
(each one a switch for her window) are its two callers.

A process rather than a window in this one, because the host is an asyncio
server with no GUI main loop, and pywebview wants the main thread of whatever
process it runs in. A separate process also keeps a crashed web engine from
taking every character's room down with it.

The host keeps the handles so it can answer "is she already out?" — a second
click should not stack a second copy of her on the screen — and so the windows
go when the house does, rather than floating over a server that has stopped.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from collections.abc import Callable

log = logging.getLogger("world.desktop_windows")

#: How long a window gets to close itself before it is killed.
CLOSE_TIMEOUT_S = 3.0


class WindowUnavailable(RuntimeError):
    """This machine cannot open a window at all; the message says why."""


def unavailable() -> str | None:
    """Why a window cannot open here, or None.

    Asked before spawning because the spawned process is the only one that would
    otherwise find out, and it would say so in the daemon's log while the button
    that asked had already reported success. WSL draws its window on the Windows
    side and needs neither check (`desktop/window.py`).
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return None
    if importlib.util.find_spec("webview") is None:
        return ("the desktop window needs pywebview — install the extra: "
                "pip install -e \".[desktop]\"")
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "the house has no graphical session to open a window on"
    return None


class DesktopWindows:
    """One attached window process per character, at most."""

    def __init__(self, host: str, port: int, *,
                 spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
                 python: str = sys.executable,
                 check: Callable[[], str | None] = unavailable,
                 body_for: Callable[[str], str | None] = lambda _id: None):
        self.host = host
        # Which body each character floats (`CharacterHost.desktop_body`): hers
        # from the switchboard, else the house's. Asked at every open, so a body
        # changed in the board is the one the next window wears.
        self._body_for = body_for
        self._check = check
        self.port = port
        self._spawn = spawn
        self._python = python
        self._procs: dict[str, subprocess.Popen] = {}

    def is_open(self, character_id: str) -> bool:
        proc = self._procs.get(character_id)
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        # Closed by hand (or it failed to start): forget it, and reap it.
        del self._procs[character_id]
        return False

    def open(self, character_id: str, *, title: str | None = None) -> bool:
        """Float her. False when she is already out — nothing new was started.

        Raises `WindowUnavailable` when this machine cannot open one at all.
        """
        if self.is_open(character_id):
            return False
        reason = self._check()
        if reason:
            raise WindowUnavailable(reason)
        argv = [self._python, "-m", "yurios.world", "--attach", character_id]
        if title:
            argv += ["--title", title]
        body = self._body_for(character_id)
        if body:
            argv += ["--body", body]
        # The window reads the house `.env` like any other entry point, but it
        # must reach *this* server, which booted from whatever `.env` said then.
        env = {**os.environ, "HOST": self.host, "PORT": str(self.port)}
        # stdout/stderr are inherited on purpose: a window that cannot start
        # (no [desktop] extra, no display) says why in the daemon's own log.
        self._procs[character_id] = self._spawn(argv, env=env, stdin=subprocess.DEVNULL)
        log.info("desktop window for %s: pid %s", character_id,
                 self._procs[character_id].pid)
        return True

    def close(self, character_id: str) -> bool:
        """End her window. False when there was none of ours to end."""
        proc = self._procs.pop(character_id, None)
        if proc is None or proc.poll() is not None:
            return False
        proc.terminate()
        try:
            proc.wait(timeout=CLOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return True

    def close_all(self) -> None:
        for character_id in list(self._procs):
            self.close(character_id)
