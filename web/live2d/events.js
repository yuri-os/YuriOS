/* FORK(B2): the bus adapter (Build #4 SPEC §10) — the one file that makes the
 * vendored Build #2 client a citizen of the single event stream.
 *
 * Expressions used to arrive on /ws/voice; they now ride /api/events as
 * `avatar` events, so this adapter maps the `expression` op onto the pixi
 * body's realisation table (window.Avatar, avatar.js — untouched). The other
 * puppet ops (gaze, bones, mouth) are still not realised here: the Live2D body
 * remains a guest, not a second puppet (SPEC §6.6) — wiring them is the reader
 * exercise it always was, now with the events already arriving on this page.
 *
 * It also boots the shared chat panel (/js/chat.js) and, in desktop-pet mode,
 * moves the composer back onto the hover bar (the chat column is hidden there).
 */
(() => {
  if (document.documentElement.classList.contains('desktop')) {
    const text = document.getElementById('text');
    const bar = document.querySelector('.controls');
    if (text && bar) bar.appendChild(text);
  }

  window.addEventListener('world-ev', (e) => {
    const m = e.detail;
    if (m && m.type === 'avatar' && m.op === 'expression') {
      Avatar.setExpression?.(m.name);
    }
  });

  WorldChat.connect({});
})();
