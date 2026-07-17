/* The boot log (SPEC §6.4) — the kernel-style wake-up panel shown while her
 * local voice models load (world/boot.py). It polls /api/boot, NOT the
 * /api/events bus: that stream only opens after the enter gesture (main.js),
 * and this is what fills the ~minute of cold-model loading *before* it.
 *
 * Classic script, like chat.js, so both pages get it. It mounts into the enter
 * card's #boot-list when there is one (the sanctuary page), and otherwise —
 * desktop-pet mode, where there's no gate — floats a small overlay that fades
 * itself out once she's ready. */
(() => {
  const DESKTOP = document.documentElement.classList.contains('desktop');
  const POLL_MS = 500;

  // state → how it reads in the log: a fixed-width tag + a colour class
  const TAG = {
    pending: ['wait', 'pend'],
    loading: ['····', 'load'],
    ready:   [' ok ', 'ok'],
    failed:  ['fail', 'fail'],
    skipped: ['skip', 'skip'],
  };

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  function row(svc) {
    const [tag, cls] = TAG[svc.state] || TAG.pending;
    const el = document.createElement('div');
    el.className = 'bt-row';
    let detail = svc.detail || '';
    if (svc.state === 'loading' && detail) detail += '…';
    const secs = svc.seconds != null ? `${svc.seconds.toFixed(1)}s` : '';
    el.innerHTML =
      `<span class="bt-tag bt-${cls}">[${tag}]</span>` +
      `<span class="bt-label">${esc(svc.label)}</span>` +
      `<span class="bt-detail">${esc(detail)}</span>` +
      `<span class="bt-secs">${secs}</span>`;
    return el;
  }

  function build(mount) {
    const panel = document.createElement('div');
    panel.className = 'boot-panel';
    const head = document.createElement('div');
    head.className = 'bt-head';
    const list = document.createElement('div');
    list.className = 'bt-list';
    panel.append(head, list);
    mount.appendChild(panel);
    return { panel, head, list };
  }

  function render(ui, snap) {
    ui.list.replaceChildren(...snap.services.map(row));
    const secs = `${snap.elapsed.toFixed(1)}s`;
    if (snap.done) {
      const failed = snap.services.some((s) => s.state === 'failed');
      ui.head.textContent = failed
        ? `awake — some services degraded · ${secs}`
        : `she's awake · ${secs}`;
      ui.panel.classList.add(failed ? 'bt-degraded' : 'bt-ready');
    } else {
      ui.head.textContent = `waking her up… · ${secs}`;
    }
  }

  function start() {
    // sanctuary page: fill the enter card. desktop/anything else: float one.
    let mount = document.getElementById('boot-list');
    let overlay = null;
    if (!mount || DESKTOP) {
      overlay = document.createElement('div');
      overlay.id = 'boot-overlay';
      document.body.appendChild(overlay);
      mount = overlay;
    }
    const ui = build(mount);

    let stopped = false;
    async function tick() {
      if (stopped) return;
      try {
        const r = await fetch('/api/boot', { cache: 'no-store' });
        if (r.status === 404) { stopped = true; ui.panel.remove(); return; }
        const snap = await r.json();
        render(ui, snap);
        window.dispatchEvent(new CustomEvent('boot-status', { detail: snap }));
        if (snap.done) {
          stopped = true;
          // in the enter card the panel goes when the gate does; a floating
          // overlay bows out on its own a beat after she's ready
          if (overlay) {
            overlay.classList.add('leaving');
            setTimeout(() => overlay.remove(), 2500);
          }
          return;
        }
      } catch {
        /* server still coming up, or a blip — keep polling */
      }
      setTimeout(tick, POLL_MS);
    }
    tick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
