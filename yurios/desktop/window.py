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

WSL is the exception (_is_wsl → _run_wsl_window): the window has to be drawn by
Windows, not by the VM, so there is no pywebview at all. Two frames can be had
from over there, and the launcher prefers the first it finds installed:

  1. desktop-shell/ — an Electron window on the Windows side, transparent,
     frameless, on top: the same window Linux gets, drawn by the right OS.
  2. an app-mode Edge/Chrome window — always available, never transparent, so
     the page is told to paint its own background (`framed=1`).

Either way it is the same server, the same page, the same her; only the frame
changes.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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


def _is_wsl() -> bool:
    """One answer, asked in several places: are we inside WSL?

    WSL sets WSL_DISTRO_NAME in every session it starts. It decides the whole
    shape of the window path — no pywebview import, no Qt, a Windows-side
    browser instead — so the two run()s ask this once and branch on the answer
    rather than re-testing the environment at each fork.
    """
    return bool(os.environ.get("WSL_DISTRO_NAME"))


def _require_webview() -> None:
    """Fail on a missing [desktop] extra now — not three minutes into the boot.

    Both launchers call this before starting the server so the error arrives
    while the user is still watching, and import it from here rather than
    repeating the instructions (§2.2).
    """
    try:
        import webview                       # noqa: F401  (the [desktop] extra)
    except ImportError as e:
        raise SystemExit(
            "desktop-window mode needs pywebview — install the extra:\n"
            '    pip install -e ".[desktop]"   # or: pip install "pywebview[gtk]"\n'
            f"(import failed: {e})")


def _wsl_bind_host(cfg: Config) -> Config:
    """Under WSL, serve on the VM's address too — the window is outside the VM.

    Everywhere else the window is a process on this machine and loopback is the
    right, closed answer. On WSL the browser that carries her runs on Windows,
    so a 127.0.0.1-only bind is reachable *only* through WSL's localhost
    forwarding — the thing that is off in .wslconfig on some setups and broken by
    a VPN on others. 0.0.0.0 inside the VM adds one route: the Windows host over
    the NAT adapter. It is not a LAN address; nothing outside this PC gains a way
    in without a portproxy the user would have to add by hand.
    """
    if not _is_wsl() or cfg.host not in ("127.0.0.1", "localhost", "::1", ""):
        return cfg
    return cfg.model_copy(update={"host": "0.0.0.0"})


def _wsl_vm_ip() -> str | None:
    """This VM's address on the Windows-facing adapter (no packet is sent —
    connect() on a UDP socket only consults the routing table)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))
            return s.getsockname()[0]
    except OSError:
        return None


def _windows_can_reach(host: str, port: int) -> bool:
    """Ask WINDOWS to open the socket. Whether *we* can reach it proves nothing:
    the window lives on the other side of the VM boundary."""
    script = (f"$c = New-Object Net.Sockets.TcpClient\n"
              f"try {{ $c.Connect('{host}', {port}); 'open' }} "
              f"catch {{ 'shut' }} finally {{ $c.Dispose() }}")
    try:
        probe = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "open" in (probe.stdout or "")


def _wsl_window_url(url: str) -> str:
    """Rewrite the window's URL to an address the Windows side can actually open.

    `127.0.0.1` means the Windows host to a Windows browser, so it only finds her
    when WSL's localhost forwarding is up. When it isn't, the symptom is a window
    that opens on a connection error and looks like "desktop mode is broken" —
    hence the probe: prefer loopback, fall back to the VM's own address (which
    _wsl_bind_host made sure she answers on), and say plainly when neither works.

    (Whether the page is also told `framed=1` depends on which frame ends up
    carrying her — that is _run_wsl_window's call, not this one's.)
    """
    if shutil.which("powershell.exe") is None:
        # Without interop there is no Windows side to open anything on, and the
        # probes below would blame the network for it.
        raise SystemExit(
            "WSL can't see powershell.exe, so it has no way to open a window on "
            "Windows. Enable interop (/etc/wsl.conf → [interop]\\nenabled=true, "
            "then `wsl --shutdown` from Windows), or drop --window and open her "
            "in a browser.")
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    if host not in ("127.0.0.1", "localhost", "::1"):
        return url                                   # already an explicit address
    if _windows_can_reach(host, port):
        return url
    vm_ip = _wsl_vm_ip()
    if vm_ip and _windows_can_reach(vm_ip, port):
        print(f"[window] WSL: Windows can't reach {host}:{port} (localhost "
              f"forwarding is off) — going through the VM address {vm_ip}",
              flush=True)
        return urlunsplit(parts._replace(netloc=f"{vm_ip}:{port}"))
    raise SystemExit(
        f"she is up inside WSL, but Windows can reach neither {host}:{port} nor "
        f"{vm_ip or 'this VM'}:{port} — the window runs on the Windows side, so "
        "it has nothing to open.\n"
        "  · HOST in .env must be 127.0.0.1 or 0.0.0.0 (the launcher widens it "
        "to 0.0.0.0 on WSL for exactly this),\n"
        "  · Windows Defender may be blocking inbound traffic on the WSL "
        "adapter — allow it for the private/vEthernet profile,\n"
        "  · `wsl --shutdown` from Windows and relaunch fixes a wedged NAT."
        "\nUntil then she still answers a browser started inside WSL.")


def _wsl_electron_shell() -> tuple[str, str] | None:
    """desktop-shell/, if it has been installed from the Windows side.

    Its electron.exe is a Windows binary, so it exists only when `npm install`
    was run by Windows' node — which is exactly the condition for being able to
    launch it. Returns (electron.exe, the app directory as Windows sees it):
    Electron is a Windows process and cannot follow /mnt/c paths.
    """
    shell = Path(__file__).resolve().parents[2] / "desktop-shell"
    exe = shell / "node_modules" / "electron" / "dist" / "electron.exe"
    if not exe.exists():
        return None
    try:
        win_dir = subprocess.run(["wslpath", "-w", str(shell)],
                                 capture_output=True, text=True,
                                 check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return str(exe), win_dir


def _run_electron_shell(exe: str, app_dir: str, url: str, cfg: Config) -> None:
    """Hand the window to desktop-shell/ — the frame Windows can actually draw:
    transparent, frameless, on top, the same page. Blocks until she is closed."""
    try:
        subprocess.run(
            [exe, app_dir, f"--url={url}",
             f"--width={cfg.window_width}", f"--height={cfg.window_height}",
             f"--on-top={'true' if cfg.window_on_top else 'false'}"],
            check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"the Windows desktop shell wouldn't run: {e}\n"
                         "(reinstall it from Windows: cd desktop-shell && npm install)"
                         ) from e


def _framed(url: str) -> str:
    """Tell the page its frame can't be transparent, so it paints its own night
    instead of the white a browser puts behind a transparent page."""
    parts = urlsplit(url)
    if "framed=1" in parts.query:
        return url
    return urlunsplit(parts._replace(
        query=f"{parts.query}&framed=1" if parts.query else "framed=1"))


def _run_wsl_window(url: str, cfg: Config) -> None:
    """Open her window on the Windows side.

    The Electron shell first (desktop-shell/ — see the module docstring): it is
    the real thing, a transparent cut-out on the wallpaper. Otherwise an app-mode
    browser window, which is always there but opaque and titled — she stands on
    the sanctuary's night in a small window rather than on your desktop.
    """
    shell = _wsl_electron_shell()
    if shell is not None:
        exe, app_dir = shell
        print("[window] WSL: handing her to the Windows shell (desktop-shell/)",
              flush=True)
        _run_electron_shell(exe, app_dir, url, cfg)
        return

    print("[window] WSL: no Windows shell installed — using a browser window "
          "(opaque, titled).\n"
          "[window] For the transparent one: from Windows, "
          "`cd desktop-shell && npm install` (see desktop-shell/README.md).",
          flush=True)
    url = _framed(url)
    url_literal = url.replace("'", "''")
    on_top = "$true" if cfg.window_on_top else "$false"
    # Her ears are getUserMedia, which Chromium grants only to secure contexts.
    # `localhost` is exempt; the VM address we fall back to is not, so the origin
    # is named — one origin, one throwaway profile, this run only.
    origin = urlsplit(url)._replace(path="", query="", fragment="").geturl()
    script = (f"$url = '{url_literal}'\n"
              f"$size = '{cfg.window_width},{cfg.window_height}'\n"
              f"$origin = '{origin}'\n"
              f"$onTop = {on_top}\n") + r"""
$ErrorActionPreference = 'Stop'
$browser = @(
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { throw "neither Microsoft Edge nor Google Chrome was found on Windows" }
Write-Host "[window] browser: $browser"

# $profile and $args are PowerShell's own automatic variables; ours are named
# apart so this script never fights the host for them.
$profileDir = Join-Path $env:LOCALAPPDATA "YuriOS\EdgeWindow"
$exeName = [IO.Path]::GetFileNameWithoutExtension($browser)
$browserArgs = @(
  "--user-data-dir=$profileDir",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-features=msEdgeSidebarV2",
  "--autoplay-policy=no-user-gesture-required",
  "--unsafely-treat-insecure-origin-as-secure=$origin",
  "--app=$url",
  "--window-size=$size"
)

# What Start-Process hands back is only a launcher: it passes the URL to whatever
# browser process owns this profile and then exits, often within the second.
# Waiting on THAT pid would end the run — and take her server down with it —
# while the window is still on screen. Track the profile instead: these are the
# only browser processes started with this --user-data-dir.
function Get-YuriProcesses {
  Get-CimInstance Win32_Process -Filter "Name = '$exeName.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profileDir) }
}
Start-Process $browser -ArgumentList $browserArgs
$deadline = (Get-Date).AddSeconds(30)
while (-not (Get-YuriProcesses) -and (Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 200
}
if (-not (Get-YuriProcesses)) { throw "$exeName never opened the window" }
Write-Host "[window] her window is up on the Windows side"

if ($onTop) {
  Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class YuriWindow {
  [DllImport("user32.dll")]
  public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y,
    int cx, int cy, uint flags);
}
'@
  # The window belongs to whichever of the profile's processes is the browser
  # (not the launcher, not a renderer), so look for the one that has one.
  $handle = [IntPtr]::Zero
  $deadline = (Get-Date).AddSeconds(20)
  while ($handle -eq [IntPtr]::Zero -and (Get-Date) -lt $deadline) {
    foreach ($proc in Get-YuriProcesses) {
      $p = Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
      if ($p -and $p.MainWindowHandle -ne 0) { $handle = $p.MainWindowHandle; break }
    }
    if ($handle -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 200 }
  }
  if ($handle -ne [IntPtr]::Zero) {
    # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    [void][YuriWindow]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0,
      0x0001 -bor 0x0002 -bor 0x0010)
  } else {
    Write-Warning "could not find her window to pin it on top"
  }
}

# Block until she is closed, so this launcher owns the session the way pywebview
# does everywhere else: closing the window ends the run and stops the server.
while (Get-YuriProcesses) { Start-Sleep -Seconds 1 }
"""
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"could not open the Windows desktop window: {e}") from e


def run(cfg: Config | None = None) -> None:
    cfg = cfg or Config()
    wsl = _is_wsl()
    if wsl:
        cfg = _wsl_bind_host(cfg)

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

    if not wsl:
        _require_webview()          # WSL's window is a Windows browser, not pywebview

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

    if wsl:
        url = _wsl_window_url(desktop_url(cfg))
        print(f"[window] WSL: opening her in a Windows browser window — {url}\n"
              "[window] (a browser frame can't go transparent; close the window to stop her)",
              flush=True)
        _run_wsl_window(url, cfg)
        return

    import webview                  # already imported by _require_webview above
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
