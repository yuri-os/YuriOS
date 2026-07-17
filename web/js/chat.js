/* The chat panel + the event stream (SPEC §2.6, §10) — one consumer for the
 * one bus. Classic script on purpose: both pages load it — the VRM sanctuary
 * (/, an ES-module app) and the vendored Live2D client (/live2d/, plain
 * scripts) — so the chat renderer and the SSE plumbing exist exactly once.
 *
 * It does two jobs, the YuriOS frontend split (frontends/sanctuary/app.js):
 *
 *   1. subscribe to /api/events and re-dispatch EVERY event as a
 *      `world-ev` CustomEvent on window — the page's stage adapter (bridge.js
 *      on the VRM page, events.js on the Live2D page) picks the `avatar` ones
 *      off that; this file never touches a body;
 *   2. render the chat: history backfill, you/her bubbles, the accumulating
 *      draft while she speaks, the `proactive` tag when she spoke first, and
 *      an <img> when a message carries `image_url` (a selfie — SPEC §7.6).
 *
 * Sending is not here: typed input rides the voice socket exactly as before
 * (voice.js owns #text), so a typed turn keeps TurnController semantics —
 * TTS, barge-in, the works. The user bubble arrives back over the bus.
 */
(() => {
  const messages = document.getElementById('messages');
  let draftEl = null;
  let charName = '';

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  function scroll() {
    if (messages) messages.scrollTop = messages.scrollHeight;
  }

  function dropDraft() {
    if (draftEl) { draftEl.remove(); draftEl = null; }
  }

  const seen = new Set();               // a message can arrive live AND in the
                                        // history backfill — the id resolves it
  function addMsg(m) {
    if (!messages) return;
    if (m.id) {
      if (seen.has(m.id)) return;
      seen.add(m.id);
    }
    dropDraft();
    const her = m.role !== 'user';
    const div = document.createElement('div');
    div.className = 'msg ' + (her ? 'her' : 'you') + (m.proactive ? ' proactive' : '');
    let body = `<span class="who">${her ? esc(charName || 'her') : 'you'}` +
               (m.proactive ? ' <em>· she spoke first</em>' : '') + '</span>';
    if (m.image_url) {
      body += `<a href="${esc(m.image_url)}" target="_blank" rel="noopener">` +
              `<img class="msg-img" src="${esc(m.image_url)}" alt="a selfie from her"></a>`;
    }
    if (m.text) body += esc(m.text);
    div.innerHTML = body;
    messages.appendChild(div);
    scroll();
    // an <img> has no height until it loads, so the scroll above lands short
    // and the photo bottom sits below the fold. Re-pin when it arrives —
    // unless the user has scrolled away meanwhile (the load can be slow).
    const img = div.querySelector('img');
    if (img) img.addEventListener('load', () => {
      const away = messages.scrollHeight - messages.clientHeight - messages.scrollTop;
      if (away - img.clientHeight < 160) scroll();
    });
  }

  function addDraft(text) {
    if (!messages) return;
    if (!draftEl) {
      draftEl = document.createElement('div');
      draftEl.className = 'msg her draft';
      messages.appendChild(draftEl);
    }
    draftEl.innerHTML = `<span class="who">${esc(charName || 'her')} · …</span>` + esc(text);
    scroll();
  }

  let es = null;

  function connect({ onStatus } = {}) {
    if (es) return;                       // one stream per page
    es = new EventSource('/api/events');
    es.onopen = () => onStatus?.(true);
    es.onerror = () => onStatus?.(false); // EventSource auto-reconnects
    es.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      // the stage adapters listen here (the YuriOS `yurios-ev` pattern)
      window.dispatchEvent(new CustomEvent('world-ev', { detail: m }));
      if (m.type === 'hello') {
        charName = m.character || '';
        const el = document.getElementById('chat-name');
        if (el && charName) el.textContent = charName;
      } else if (m.type === 'message') addMsg(m);
      else if (m.type === 'draft') addDraft(m.text);
      else if (m.type === 'draft_cancel') dropDraft();
    };
    // backfill what was said before this page opened (SPEC §2.6)
    fetch('/api/history').then((r) => r.json())
      .then((d) => (d.messages || []).forEach(addMsg))
      .catch(() => {});
  }

  window.WorldChat = { connect };
})();
