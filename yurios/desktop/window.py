"""`python -m desktop --window` — render her on the desktop, not in a browser.

Same app, same page, same WebGL Live2D renderer (web/avatar.js). The only new
piece is the *frame*: instead of you opening a browser tab, we run the FastAPI
server in a background thread and point a native, frameless, transparent,
always-on-top window at it with `?desktop=1`. That query flag tells the page
(web/sanctuary.css `:root.desktop`) to drop its background and chrome, so all
that's left floating on your desktop is the avatar. Her voice loop is unchanged —
the mic/text controls fade in when you hover her.

The window is pywebview (the [desktop] extra). It's imported lazily inside run()
so the rest of `desktop` — and the test suite — never needs a GUI backend
installed. On Linux pywebview[gtk] (WebKit) gives the best transparency; see the
README for the per-platform note.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import uvicorn

from .config import Config
from .main import create_app


def desktop_url(cfg: Config) -> str:
    """The page URL the native window loads — the normal app in desktop mode."""
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "") else cfg.host
    return f"http://{host}:{cfg.port}/?desktop=1"


def _wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    """Block until the background uvicorn is accepting connections (or time out)."""
    host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _serve(cfg: Config) -> tuple[threading.Thread, list[BaseException]]:
    """Run the FastAPI app in a daemon thread so the GUI can own the main thread.

    create_app() must run INSIDE the thread: the brain opens SQLite connections at
    construction, and sqlite3 objects refuse use from any other thread — built on
    the main thread, every turn died with "SQLite objects created in a thread…".
    That also means the slow part (loading the STT/TTS models, ~half a minute cold)
    happens in here, before the port opens — callers must wait accordingly. Any
    crash lands in the returned error box so run() can report it instead of a
    generic timeout.
    """
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            uvicorn.Server(uvicorn.Config(
                create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")).run()
        except BaseException as e:                      # surfaced by run()
            errors.append(e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t, errors


def _pick_gui(cfg: Config) -> str | None:
    """WINDOW_GUI (.env) → pywebview's `gui` arg. Default (empty) = auto: prefer
    Qt (QtWebEngine = Chromium) when its stack is importable, else fall back to the
    platform engine (Linux: GTK/WebKitGTK, kept usable by the DMA-BUF quirk in
    run()). A labeled A/B on the reference rig settled the choice: WebKitGTK caps
    rAF at ~30fps and her sway blurs; Chromium holds 60 and is crisp. QT_API stops
    qtpy grabbing a WebEngine-less PyQt5 when both bindings are installed."""
    gui = (cfg.window_gui or "").strip().lower()
    if gui not in ("", "qt"):
        return gui

    # ORDER MATTERS: qtpy picks its binding when it is FIRST imported, and with
    # both PyQt5 and PyQt6 installed it grabs PyQt5 — which has no WebEngine, so
    # pywebview prints "QT cannot be loaded" and silently drops to GTK (30fps,
    # blurry sway). QT_API must therefore be in the environment before any qtpy
    # import anywhere in the process.
    os.environ.setdefault("QT_API", "pyqt6")
    try:
        import PyQt6.QtWebEngineWidgets          # noqa: F401  (the Chromium widget)
        import qtpy                              # noqa: F401  (pywebview's qt shim)
    except ImportError as e:
        if gui == "qt":                          # explicitly requested — fail loud
            raise SystemExit(
                f"WINDOW_GUI=qt needs the Qt stack (qtpy, PyQt6, PyQt6-WebEngine): {e}")
        return None                              # auto mode → pywebview picks (gtk)
    if qtpy.API_NAME.lower() != "pyqt6":         # bound to the wrong Qt → GTK blur
        msg = (f"qtpy bound to {qtpy.API_NAME}, not PyQt6 — QT_API was set too "
               "late or overridden; the qt engine would silently fall back to GTK")
        if gui == "qt":
            raise SystemExit(msg)
        print(f"[window] {msg}; using the platform engine instead", flush=True)
        return None
    return "qt"


def run(cfg: Config | None = None) -> None:
    cfg = cfg or Config()

    # Refuse to start if something already answers on our port. Without this, our
    # uvicorn fails to bind (a one-line ERROR log), the readiness probe happily
    # connects to the FOREIGN server, and the window renders a stale instance —
    # old code, old .env — which looks like "my settings/model changes don't work".
    if _wait_for_server(cfg.host, cfg.port, timeout=0.5):
        raise SystemExit(
            f"port {cfg.port} is already in use — an earlier `python -m desktop` is "
            "probably still running. Close it (e.g. pkill -f 'python -m desktop') "
            "or set PORT in .env, then relaunch.")

    # WebKitGTK's DMA-BUF renderer smears stale frames on NVIDIA (her hair leaves
    # ghost trails as she sways). Falling back to the shared-memory path fixes it;
    # WebGL stays hardware-accelerated. Harmless on non-GTK backends / other GPUs,
    # and setdefault means a user who knows better can override it from the shell.
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

    try:
        import webview                       # the [desktop] extra (pywebview)
    except ImportError as e:
        raise SystemExit(
            "desktop-window mode needs pywebview — install the extra:\n"
            '    pip install -e ".[desktop]"   # or: pip install "pywebview[gtk]"\n'
            f"(import failed: {e})")

    # The brain builds before the port opens (the voice stack warms in the
    # background after — main.Runtime), so this is normally seconds; the deadline
    # is generous anyway, and a crashed thread aborts the wait with its error.
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
        easy_drag=False,           # dragging is scoped to the avatar (pywebview-drag-region)
        transparent=True,          # background alpha comes through (canvas is already alpha:0)
        on_top=cfg.window_on_top,
        resizable=True,
    )
    # private_mode=False: pywebview defaults to a private (ephemeral) session, and
    # on WebKitGTK that session has NO localStorage global at all — which used to
    # crash voice.js on load (no body, no voice, "· offline"). Persistent storage
    # also means the session id survives relaunches: same someone every time.
    webview.start(private_mode=False, gui=_pick_gui(cfg))   # blocks until the window closes
