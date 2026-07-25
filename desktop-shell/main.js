/**
 * The Windows frame for desktop mode (SPEC §6.5) — the half WSL can't draw.
 *
 * `python -m yurios.world --window` gives her a frameless, transparent,
 * always-on-top window through pywebview. That works on Linux and macOS, where
 * the launcher and the compositor are on the same machine. Under WSL they are
 * not: the process is in the VM and the desktop belongs to Windows, so the
 * launcher can only ask Windows to open something — and a browser window, even
 * in --app mode, cannot be transparent or frameless. She ends up in a titled box.
 *
 * This is that missing window, and nothing more: an Electron shell pointed at
 * the SAME page the server already serves. No renderer of its own, no second
 * copy of the app, no state — the page is still web/index.html at `?desktop=1`,
 * the brain is still the one in WSL. Electron is here purely because Chromium on
 * Win32 will composite a per-pixel-alpha window and a browser tab will not.
 *
 * It is launched for you: world/window.py finds this folder's electron.exe and
 * hands the window to it, falling back to the Edge window when it isn't
 * installed. Run it by hand if you like — see README.md.
 */
const { app, BrowserWindow, globalShortcut } = require('electron');

const DEFAULT_URL = 'http://127.0.0.1:8768/?desktop=1';
const PASS_THROUGH_HOTKEY = 'Control+Alt+Y';

/** `--name=value`, else the environment, else the built-in default. */
function argOf(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit === undefined ? fallback : hit.slice(name.length + 3);
}

const url = argOf('url', process.env.YURI_URL || DEFAULT_URL);
const width = Number(argOf('width', process.env.WINDOW_WIDTH || 360));
const height = Number(argOf('height', process.env.WINDOW_HEIGHT || 640));
const onTop = String(argOf('on-top', process.env.WINDOW_ON_TOP || 'true'))
  .toLowerCase() !== 'false';
const devTools = process.argv.includes('--dev');
let passingThrough = process.argv.includes('--click-through');

// Desktop mode hides the enter gate (web/js/main.js), so nothing clicks before
// she speaks — without this her greeting is swallowed by the autoplay policy.
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');

// Her ears are a getUserMedia stream, and Chromium hands those only to secure
// contexts. `localhost` is exempt by fiat; the WSL VM's address is not, and that
// is exactly the URL the launcher falls back to when Windows can't reach
// loopback — so name the origin explicitly or the mic never opens.
try {
  app.commandLine.appendSwitch(
    'unsafely-treat-insecure-origin-as-secure', new URL(url).origin);
} catch { /* a malformed --url will fail louder at load time */ }

// A second instance would open a second her over the first.
if (!app.requestSingleInstanceLock()) {
  app.quit();
}

function createWindow() {
  const win = new BrowserWindow({
    title: 'yuri',
    width, height,
    frame: false,               // no title bar, no border — just her
    transparent: true,          // the point of this program (the stage clears to alpha 0)
    hasShadow: false,           // a drop shadow on a cut-out is a grey rectangle
    backgroundColor: '#00000000',
    resizable: true,
    alwaysOnTop: onTop,
    webPreferences: {
      contextIsolation: true,   // she is a web page here, not a privileged app
      nodeIntegration: false,
      backgroundThrottling: false,   // she keeps swaying when she isn't focused
    },
  });
  // 'screen-saver' keeps her over full-screen windows too; plain alwaysOnTop
  // loses to them, which is where a pet is most missed.
  if (onTop) win.setAlwaysOnTop(true, 'screen-saver');

  // Only the mic. Everything else a page might ask for stays denied.
  win.webContents.session.setPermissionRequestHandler((_wc, permission, done) => {
    done(permission === 'media');
  });

  // The page already marks its drag surface for pywebview (index.html adds
  // .pywebview-drag-region to #scene in desktop mode) — teach Electron the same
  // class, and keep the controls clickable inside it.
  win.webContents.on('dom-ready', () => {
    win.webContents.insertCSS(`
      .pywebview-drag-region { -webkit-app-region: drag; }
      .controls, .controls *, .caption, #text, button, input, select, textarea,
      a, .settings, .settings * { -webkit-app-region: no-drag; }
    `);
  });

  // She may be started before the server finishes waking (the voice stack takes
  // the long minute), or the WSL address may come up a moment late. Keep asking
  // rather than showing Chromium's error page.
  const load = () => { win.loadURL(url).catch(() => {}); };
  win.webContents.on('did-fail-load', (_e, code, desc, _u, isMainFrame) => {
    if (!isMainFrame) return;
    console.log(`[shell] ${url} not answering yet (${desc || code}); retrying…`);
    setTimeout(load, 2000);
  });

  if (passingThrough) win.setIgnoreMouseEvents(true, { forward: true });
  if (devTools) win.webContents.openDevTools({ mode: 'detach' });

  win.on('closed', () => app.quit());
  load();
  return win;
}

app.whenReady().then(() => {
  const win = createWindow();

  // Click-through: she stays on screen but the mouse goes to whatever is behind
  // her. There is no frame to click when it's on, so the toggle has to be global.
  globalShortcut.register(PASS_THROUGH_HOTKEY, () => {
    passingThrough = !passingThrough;
    win.setIgnoreMouseEvents(passingThrough, { forward: true });
    console.log(`[shell] click-through ${passingThrough ? 'on' : 'off'}`);
  });

  console.log(`[shell] ${url}`);
  console.log(`[shell] drag her to move · ${PASS_THROUGH_HOTKEY} toggles click-through`);
});

app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('window-all-closed', () => app.quit());
