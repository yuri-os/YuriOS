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
"""
from __future__ import annotations

import os
import threading
import time

# Build #2 launcher internals, called not copied (§2.2's discipline):
from yurios.desktop.window import _pick_gui, _wait_for_server

from .config import Config
from .main import build_server, create_app


def desktop_url(cfg: Config) -> str:
    """The page the native window loads — the same app, told to set the room aside.

    Two bodies answer the flag (SPEC §6.5–§6.6): the VRM stage at `/`, and the
    Build #2 Live2D client at `/live2d/` — both already carry their own
    `?desktop=1` handling, so the launcher only picks the path.
    """
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "") else cfg.host
    body = (cfg.desktop_body or "vrm").strip().lower()
    if body not in ("vrm", "live2d"):
        raise SystemExit(f"DESKTOP_BODY must be 'vrm' or 'live2d', not {body!r}")
    path = "/live2d/" if body == "live2d" else "/"
    return f"http://{host}:{cfg.port}{path}?desktop=1"


def _serve(cfg: Config) -> tuple[threading.Thread, list[BaseException]]:
    """B2's pattern, re-aimed: create_app() must run INSIDE the daemon thread
    (the brain opens SQLite connections at construction, and sqlite3 objects
    refuse use from any other thread). Crashes land in the returned error box
    so run() reports them instead of a generic timeout."""
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            build_server(create_app(cfg), cfg).run()
        except BaseException as e:                      # surfaced by run()
            errors.append(e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t, errors


def run(cfg: Config | None = None) -> None:
    cfg = cfg or Config()

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

    try:
        import webview                       # the [desktop] extra (pywebview)
    except ImportError as e:
        raise SystemExit(
            "desktop-window mode needs pywebview — install the extra:\n"
            '    pip install -e ".[desktop]"   # or: pip install "pywebview[gtk]"\n'
            f"(import failed: {e})")

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

    webview.create_window(
        "yuri",
        desktop_url(cfg),
        width=cfg.window_width, height=cfg.window_height,
        frameless=True,            # no title bar / border — just her
        easy_drag=False,           # dragging is scoped to her (pywebview-drag-region)
        transparent=True,          # the stage clears to alpha 0 (web/js/stage/VrmStage.js)
        on_top=cfg.window_on_top,
        resizable=True,
    )
    # private_mode=False: see the module docstring — localStorage must exist
    # (voice.js keeps the session id there: same someone every relaunch).
    webview.start(private_mode=False, gui=_pick_gui(cfg))   # blocks until the window closes
