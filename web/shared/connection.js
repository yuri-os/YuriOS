/* The banner that says her model's server isn't answering (SPEC §31.5).
 *
 * A companion whose LM Studio is closed used to open her room as if nothing
 * were wrong and fail the first turn. The host now asks the server at boot,
 * after a failed turn, and on a slow beat while it is down, and says what it
 * found as the sticky `connection` event; this draws it, in every room that
 * runs chat.js, at the top of the page.
 *
 * It shows only when her replies will not work — a server that is down, a key
 * it refuses, a model it does not have, a turn that failed for a reason the
 * server check could not explain — or while one is being started. Two actions:
 * "Start <server>" when the server is on this machine and the host knows how to
 * run it, and "Check again" always. Both answer with the new state, and the
 * event says the same thing to every other open room. A dismissed banner
 * returns when the state changes: hiding one problem must not hide the next.
 *
 * Classic IIFE, like reply-length.js: the bundled rooms and the raw Live2D
 * client run the same file from /shared/.
 */
(() => {
  if (window.ConnectionBanner) return;
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;

  const TROUBLE = new Set(['down', 'refused', 'missing', 'failed']);
  const state = { snap: null, busy: '', error: '', dismissed: '' };
  let banner = null;

  /** The link a person would want fixed first: the worst one, by the host's
   *  order (the snapshot's `state` is the worst; find the link that has it). */
  function culprit(snap) {
    const links = snap?.links || [];
    return links.find((l) => l.state === snap.state) || links[0] || null;
  }

  /** What the banner says and offers for a snapshot, or null for no banner. */
  function view(snap, busy = '') {
    if (!snap) return null;
    const link = culprit(snap);
    const starting = busy === 'starting' || snap.busy === 'starting' || snap.state === 'starting';
    if (!starting && !TROUBLE.has(snap.state)) return null;
    const label = link?.label || 'model server';
    const text = starting ? `Starting ${label}…` : (snap.detail || `${label} isn't answering`);
    const canStart = !starting && snap.state === 'down' && Boolean(link?.startable);
    return {
      text,
      note: starting ? '' : (snap.fallback || ''),
      start: canStart ? `Start ${label}` : '',
      busy: starting || busy === 'checking' || snap.busy === 'checking',
      tone: starting ? 'busy' : (snap.state === 'missing' ? 'warn' : 'error'),
      key: `${snap.state}|${snap.detail}`,
    };
  }

  function build() {
    banner = document.createElement('div');
    banner.className = 'connection-banner';
    banner.setAttribute('role', 'alert');
    banner.hidden = true;
    banner.innerHTML =
      `<span class="connection-banner-dot" aria-hidden="true"></span>
       <span class="connection-banner-text"></span>
       <span class="connection-banner-note"></span>
       <span class="connection-banner-actions">
         <button type="button" data-act="start"></button>
         <button type="button" data-act="check">Check again</button>
         <button type="button" data-act="dismiss" aria-label="hide this"
                 title="hide until something changes">×</button>
       </span>`;
    banner.querySelector('[data-act="start"]').addEventListener('click', () => act('start'));
    banner.querySelector('[data-act="check"]').addEventListener('click', () => act('check'));
    banner.querySelector('[data-act="dismiss"]').addEventListener('click', () => {
      state.dismissed = view(state.snap, state.busy)?.key || '';
      render();
    });
    document.body.prepend(banner);
  }

  function render() {
    if (!banner) return;
    const v = view(state.snap, state.busy);
    if (!v || (state.dismissed && state.dismissed === v.key && !state.error)) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.dataset.tone = state.error ? 'error' : v.tone;
    banner.querySelector('.connection-banner-text').textContent = state.error || v.text;
    const note = banner.querySelector('.connection-banner-note');
    note.textContent = v.note;
    note.hidden = !v.note;
    const start = banner.querySelector('[data-act="start"]');
    start.textContent = v.start;
    start.hidden = !v.start;
    for (const button of banner.querySelectorAll('button')) button.disabled = v.busy;
  }

  async function act(which) {
    if (state.busy) return;
    state.busy = which === 'start' ? 'starting' : 'checking';
    state.error = '';
    render();
    try {
      const res = await fetch(apiPath(`/api/connection/${which}`), {
        method: 'POST', headers: { Accept: 'application/json' },
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      state.snap = body;
    } catch (err) {
      state.error = `couldn't ${which === 'start' ? 'start it' : 'check'}: ${err.message || err}`;
    } finally {
      state.busy = '';
      render();
    }
  }

  function receive(m) {
    if (!m || m.type !== 'connection') return;
    state.snap = m;
    state.error = '';
    render();
  }

  function init() {
    build();
    addEventListener('world-ev', (e) => receive(e.detail));
    // the stream may have opened before this ran (chat.js holds the sticky one)
    receive(window.WorldChat?.connection);
  }

  window.ConnectionBanner = { view, receive };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
