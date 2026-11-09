/* The reply-length nudge in the composer (SPEC §38).
 *
 * Two buttons beside each other — shorter, longer — with the rung she is on
 * between them, in every room that has a composer. The ladder is the host's
 * (`terse … expansive`, shortest first) and so is the current rung: both arrive
 * as the sticky `reply_length` event, so the control appears only once the host
 * has said it has one, a room opened later starts on the right word, and a nudge
 * from one room moves the word in every other open one.
 *
 * A press is a save of her own brain setting — `PATCH /api/brain`, which the
 * runtime path scopes to this character — the same override the settings panel
 * edits, applied live with no restart. The word moves when the host answers,
 * never before: a refused save must not leave the control saying something
 * that is not true.
 *
 * Classic IIFE, like chat.js and settings.js: the bundled rooms and the raw
 * Live2D client run the same file from /shared/.
 */
(() => {
  if (window.ReplyLength) return;
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;

  const state = { levels: [], level: null, busy: false, error: '' };
  const controls = [];

  /** The rung one step from `level` in `dir` (-1 shorter, +1 longer), or null
   *  at either end of the ladder or for a rung it does not know. */
  function step(levels, level, dir) {
    const at = levels.indexOf(level);
    if (at < 0) return null;
    return levels[at + dir] ?? null;
  }

  function glyph(path) {
    return `<svg aria-hidden="true" viewBox="0 0 24 24"><path d="${path}"/></svg>`;
  }

  function build(composer) {
    const group = document.createElement('div');
    group.className = 'reply-length';
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'reply length');
    group.hidden = true;
    group.innerHTML =
      `<button type="button" class="reply-length-step" data-dir="-1"
               title="shorter replies" aria-label="shorter replies">${glyph('M6 12h12')}</button>
       <span class="reply-length-level" aria-live="polite"></span>
       <button type="button" class="reply-length-step" data-dir="1"
               title="longer replies" aria-label="longer replies">${glyph('M6 12h12M12 6v12')}</button>`;
    for (const button of group.querySelectorAll('button')) {
      button.addEventListener('click', () => nudge(Number(button.dataset.dir)));
    }
    // The text room gives options their own drawer; the body rooms keep the
    // control before the text box. Sending stays on the same path in both.
    const input = composer.querySelector('#text');
    const slot = composer.querySelector('[data-reply-length]');
    if (slot) slot.append(group);
    else composer.insertBefore(group, input || composer.firstChild);
    controls.push(group);
  }

  function render() {
    for (const group of controls) {
      group.hidden = !state.level;
      group.classList.toggle('busy', state.busy);
      group.querySelector('.reply-length-level').textContent = state.level || '';
      group.title = state.error ||
        `her replies: ${state.level} — shorter or longer`;
      group.classList.toggle('failed', Boolean(state.error));
      for (const button of group.querySelectorAll('button')) {
        const dir = Number(button.dataset.dir);
        button.disabled = state.busy || !step(state.levels, state.level, dir);
      }
    }
  }

  async function nudge(dir) {
    const next = step(state.levels, state.level, dir);
    if (!next || state.busy) return;
    state.busy = true;
    state.error = '';
    render();
    try {
      const res = await fetch(apiPath('/api/brain'), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ reply_length: next }),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json())?.detail || ''; } catch { /* not json */ }
        throw new Error(detail || `could not save (${res.status})`);
      }
      state.level = next;            // the event will say the same; this is the
    } catch (err) {                  // room that asked, so it need not wait
      state.error = `reply length not changed: ${err.message || err}`;
    } finally {
      state.busy = false;
      render();
    }
  }

  function receive(m) {
    if (!m || m.type !== 'reply_length') return;
    if (Array.isArray(m.levels) && m.levels.length) state.levels = m.levels;
    if (typeof m.level === 'string') state.level = m.level;
    state.error = '';
    render();
  }

  function init() {
    for (const composer of document.querySelectorAll('.composer')) build(composer);
    addEventListener('world-ev', (e) => receive(e.detail));
    // the stream may have opened before this ran (chat.js holds the sticky one)
    receive(window.WorldChat?.replyLength);
  }

  window.ReplyLength = { step, receive };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
