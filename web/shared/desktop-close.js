/* The way out of desktop mode (SPEC §6.5).
 *
 * A desktop window is frameless on purpose — no title bar, so no ✕ — which left
 * no way to put her away short of killing a process. This is that ✕: top right,
 * fading in with the rest of the chrome when you hover her.
 *
 * It asks the host to close it rather than asking the window: the switchboard's
 * and the tray's windows are processes the host spawned and keeps
 * (world/desktop_windows.py), and ending one is the same act whatever body or
 * web engine is inside it. The page cannot do it itself — `window.close()` is
 * refused for a window a script did not open, and pywebview's own bridge is
 * built with `new Function`, which the page's CSP (SPEC §11.6) forbids. A window
 * the host did not open (the Electron shell, WSL's app-mode browser) answers
 * `closed: false`, and `window.close()` is the right call for those.
 *
 * A classic script on the raw /shared/ mount, like chat-options.js, so both
 * bodies (the VRM stage and the Live2D client) load the same file. It reads the
 * character from shared/runtime.js, which both pages load first.
 */
(() => {
  const root = document.documentElement;
  if (!root.classList.contains('desktop')) return;

  async function closeWindow() {
    const runtime = window.YuriOSRuntime;
    if (runtime && runtime.characterId != null) {
      try {
        const response = await fetch(runtime.apiPath('/api/desktop'), { method: 'DELETE' });
        const body = response.ok ? await response.json() : null;
        if (body && body.closed) return;      // the host is ending this window
      } catch (_) {
        // The host ended the window mid-request, or is not there: either way,
        // fall through to the one thing the page can still try.
      }
    }
    window.close();
  }

  function mount() {
    if (document.querySelector('.desktop-close')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'desktop-close';
    button.title = 'Close this window';
    button.setAttribute('aria-label', 'Close this window');
    button.innerHTML =
      '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>';
    button.addEventListener('click', () => {
      button.disabled = true;
      closeWindow().finally(() => { button.disabled = false; });
    });
    document.body.append(button);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }
})();
