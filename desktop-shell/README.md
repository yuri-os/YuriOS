# The Windows desktop shell

Her desktop mode is a frameless, transparent, always-on-top window over the same
page the browser gets (`?desktop=1`, SPEC §6.5). On Linux and macOS
`python -m yurios.world --window` opens that window itself, through pywebview.

Under **WSL** it can't. The launcher runs inside the VM; the desktop belongs to
Windows. All it can ask Windows for is a browser window — and a browser window,
even Edge in `--app` mode, is opaque and titled. This folder is the missing
frame: a ~120-line Electron shell that loads the same URL and nothing else. No
second copy of the app, no state of its own, no build step.

## Install (from Windows, not from WSL)

Electron ships a native `electron.exe`, so this has to be installed by the
**Windows** Node — installing it from inside WSL would fetch the Linux build and
the launcher would not find it. In PowerShell or cmd:

```powershell
cd C:\path\to\YuriOS\desktop-shell
npm install
```

(Node for Windows: `winget install OpenJS.NodeJS.LTS`.)

That's all. Next time you run, in WSL:

```bash
python -m yurios.world --window
```

the launcher finds `node_modules/electron/dist/electron.exe`, works out an
address Windows can actually reach her on, and hands the window to this shell.
Without it, it falls back to the Edge window (opaque, titled — she stands on the
sanctuary's night instead of on your wallpaper).

## Running it by hand

```powershell
npm start -- --url=http://172.20.240.9:8768/?desktop=1
```

| flag | default | |
|---|---|---|
| `--url=` | `http://127.0.0.1:8768/?desktop=1` | the page; from WSL this is usually the VM's address, which the launcher prints |
| `--width=` `--height=` | `360` `640` | `WINDOW_WIDTH` / `WINDOW_HEIGHT` in `.env` |
| `--on-top=false` | on top | |
| `--click-through` | off | start with the mouse passing through her |
| `--dev` | | detached devtools |

`Ctrl+Alt+Y` toggles click-through while she's running. Drag her by grabbing
her — the page marks its own drag region. There is no frame to close, so use the
taskbar entry (right-click → Close) or stop the launcher in WSL.

## Notes

- The mic needs a secure context. `localhost` counts; the WSL VM's address does
  not, so the shell passes `--unsafely-treat-insecure-origin-as-secure` for
  exactly the origin it was given. That is a local, per-run switch scoped to one
  address — not a change to any browser you use.
- Autoplay is unblocked for the same reason the enter gate is hidden in desktop
  mode: her greeting has to be audible without a click.
- Only `media` permission requests are granted; everything else is denied.
