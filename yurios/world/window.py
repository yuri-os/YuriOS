"""`python -m yurios.world --window` — her on the desktop, the room set aside (SPEC §6.5).

Build #2 proved the frame (the desktop/window.py, B2 §9): run the
FastAPI server in a background thread and point a native, frameless,
transparent, always-on-top pywebview window at it with `?desktop=1`. This file
is that launcher re-aimed at Build #4's app. The two subtle helpers — the
readiness probe and the Qt-vs-GTK engine pick (WebKitGTK caps rAF at ~30fps,
which blurred B2's sway and would blur her idle sway worse) — are *imported
from the module*, not copied; what B2's run() learned the hard way is
restated here because it binds to world's Config/create_app: build the app
INSIDE the server thread (sqlite3 objects refuse cross-thread use), the
WebKitGTK DMA-BUF smear quirk, and private_mode=False (WebKitGTK's private
session has no localStorage at all — B2's "no body, no voice" incident).

What `?desktop=1` means to THIS page is decided in web/js/main.js (SPEC §6.5):
the sanctuary is never built, the renderer clears to alpha 0, and the camera
frames the full body — the desktop is the room. Window knobs (`WINDOW_*`) are
inherited from the config; 360×640 portrait suits a standing VRM body
as well as it suited the Live2D bust. pywebview is imported lazily inside
run() so this module — and the test suite — never needs a GUI backend.

`attach()` is the other way in (`--attach <id>`): no server of its own, just the
window, aimed at one character on the house that is *already* running — the
daemon. It is what the switchboard's and the tray's Desktop buttons spawn
(`world/desktop_windows.py`), since `run()` refuses the port the daemon holds.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

# Build #2 launcher internals, called not copied (§2.2's discipline):
from yurios.desktop.window import (
    _is_wsl, _pick_gui, _require_webview, _run_wsl_window, _wait_for_server,
    _wsl_bind_host, _wsl_window_url,
)
from yurios.characters import CharacterRegistry
from yurios.daemon import install_root, runtime_dir
from yurios.migrate import migrate_legacy_data
from yurios.security import is_loopback

from .config import Config
from .host import create_host_app
from .main import build_server


def _local_host(cfg: Config) -> str:
    """Where a window on this machine reaches the server: never the wildcard."""
    return "127.0.0.1" if cfg.host in ("0.0.0.0", "") else cfg.host


def _body(cfg: Config) -> str:
    body = (cfg.desktop_body or "vrm").strip().lower()
    if body not in ("vrm", "live2d"):
        raise SystemExit(f"DESKTOP_BODY must be 'vrm' or 'live2d', not {body!r}")
    return body


def desktop_url(cfg: Config) -> str:
    """The page the native window loads — the same app, told to set the room aside.

    Two bodies answer the flag (SPEC §6.5–§6.6): the VRM stage at `/`, and the
    Build #2 Live2D client at `/live2d/` — both already carry their own
    `?desktop=1` handling, so the launcher only picks the path.
    """
    path = "/live2d/" if _body(cfg) == "live2d" else "/"
    return f"http://{_local_host(cfg)}:{cfg.port}{path}?desktop=1"


def character_url(cfg: Config, character_id: str) -> str:
    """`desktop_url` for one named character on a multi-character house.

    The same two bodies, addressed the way the switchboard addresses them: the
    VRM stage by path, Live2D by `?character=` (SPEC §29.7). On a house bound
    beyond loopback every request needs the owner, including this window's —
    so it goes in through the pairing link (`/auth?token=…`, SPEC §11.1) with
    the token read from this process's own `.env`, never handed over the wire.
    """
    cid = quote(character_id, safe="")
    path = (f"/live2d/?character={cid}&desktop=1" if _body(cfg) == "live2d"
            else f"/characters/{cid}/sanctuary/?desktop=1")
    origin = f"http://{_local_host(cfg)}:{cfg.port}"
    token = str(getattr(cfg, "owner_token", "") or "")
    if token and not is_loopback(cfg.host):
        return f"{origin}/auth?token={quote(token, safe='')}&next={quote(path, safe='')}"
    return f"{origin}{path}"


def _host_app(cfg: Config):
    migrate_legacy_data(cfg, cfg.data_dir)
    return create_host_app(cfg, CharacterRegistry(cfg.data_dir))


def _serve(cfg: Config) -> tuple[threading.Thread, list[BaseException]]:
    """B2's pattern, re-aimed: create_app() must run INSIDE the daemon thread
    (the brain opens SQLite connections at construction, and sqlite3 objects
    refuse use from any other thread). Crashes land in the returned error box
    so run() reports them instead of a generic timeout."""
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            build_server(_host_app(cfg), cfg).run()
        except BaseException as e:                      # surfaced by run()
            errors.append(e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t, errors


def run(cfg: Config | None = None) -> None:
    cfg = cfg or Config()
    wsl = _is_wsl()
    if wsl:
        cfg = _wsl_bind_host(cfg)   # the window is outside the VM (B2's window.py)

    # A foreign server on our port would hand the window a stale instance (old
    # code, old .env) — refuse loudly instead (B2's rule; the incident is
    # documented in the desktop/window.py).
    if _wait_for_server(cfg.host, cfg.port, timeout=0.5):
        raise SystemExit(
            f"port {cfg.port} is already in use — an earlier `python -m yurios.world` is "
            "probably still running. Close it (e.g. pkill -f 'python -m yurios.world') "
            "or set PORT in .env, then relaunch.")

    # WebKitGTK's DMA-BUF renderer smears stale frames on NVIDIA (B2 saw ghost
    # trails on her hair; with a full 3D canvas it's worse). The shared-memory
    # path fixes it and WebGL stays hardware-accelerated. Harmless on non-GTK
    # backends / other GPUs; setdefault means the shell can override.
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

    if not wsl:
        _require_webview()          # WSL's window is a Windows browser, not pywebview

    print("starting her up… (her voice keeps loading in the background)", flush=True)
    thread, errors = _serve(cfg)
    deadline = time.monotonic() + 180
    while not _wait_for_server(cfg.host, cfg.port, timeout=1.0):
        if errors or not thread.is_alive():
            raise SystemExit(f"server failed to start: "
                             f"{errors[0] if errors else 'server thread exited'}")
        if time.monotonic() > deadline:
            raise SystemExit(f"server didn't come up on {cfg.host}:{cfg.port} "
                             "within 3 minutes")

    if wsl:
        url = _wsl_window_url(desktop_url(cfg))
        print(f"[window] WSL: opening her in a Windows browser window — {url}\n"
              "[window] (a browser frame can't go transparent; close the window to stop her)",
              flush=True)
        _run_wsl_window(url, cfg)
        return

    _float(cfg, desktop_url(cfg), title="yuri")


def _float(cfg: Config, url: str, *, title: str, storage: Path | None = None) -> None:
    """The window itself: frameless, transparent, on top. Blocks until closed."""
    import webview                  # the caller ran _require_webview first
    # No js_api: pywebview builds its bridge with `new Function`, which the page's
    # CSP forbids (SPEC §11.6). The page's ✕ asks the host instead (desktop-close.js).
    webview.create_window(
        title,
        url,
        width=cfg.window_width, height=cfg.window_height,
        frameless=True,            # no title bar / border — just her
        easy_drag=False,           # dragging is scoped to her (pywebview-drag-region)
        transparent=True,          # the stage clears to alpha 0 (web/js/stage/VrmStage.js)
        on_top=cfg.window_on_top,
        resizable=True,
    )
    # private_mode=False: see the module docstring — localStorage must exist
    # (voice.js keeps the session id there: same someone every relaunch).
    options: dict = {"private_mode": False, "gui": _pick_gui(cfg)}
    if storage is not None:
        storage.mkdir(parents=True, exist_ok=True)
        options["storage_path"] = str(storage)
    webview.start(**options)        # blocks until the window closes


def window_storage(character_id: str) -> Path:
    """Each attached window's own browser profile, under `.yurios/windows/`.

    Separate per character because two of them may be open at once, from two
    processes, and a web engine's profile is not something two processes can be
    trusted to share; and stable per character so her session id, and a pairing
    cookie, survive the window being closed and opened again.
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in character_id)
    return runtime_dir(install_root()) / "windows" / (safe or "_")


def attach(cfg: Config | None, character_id: str, *, title: str | None = None) -> None:
    """Float one character from the house that is already running (SPEC §6.5).

    No server here: the daemon is the server, and this process is only the
    frame. That is the whole difference from `run()`, which exists for a house
    started *as* a window and refuses the port when one is already up.
    """
    cfg = cfg or Config()
    if not _wait_for_server(cfg.host, cfg.port, timeout=2.0):
        raise SystemExit(f"nothing is answering on {_local_host(cfg)}:{cfg.port} — "
                         "start the house first (`yurios start`)")
    url = character_url(cfg, character_id)
    if _is_wsl():
        _run_wsl_window(_wsl_window_url(url), cfg)
        return
    _require_webview()
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")   # see run()
    _float(cfg, url, title=title or character_id,
           storage=window_storage(character_id))
