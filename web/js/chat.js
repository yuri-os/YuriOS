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
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
  const httpPath = (path) => window.YuriOSRuntime?.httpPath(path) || path;
  const messages = document.getElementById('messages');
  let draftEl = null;
  let charName = '';

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  // Every committed entry carries `ts` — local ISO seconds, stamped by the host
  // clock when the line was posted (main.py post_message). Render it beside the
  // name: short date + wall time, with the full stamp on hover. A line without
  // a ts (or with one the browser can't parse) simply doesn't get one.
  function stamp(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    const when = d.toLocaleDateString([], { month: 'short', day: 'numeric' }) +
                 ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `<time class="when" datetime="${esc(ts)}" title="${esc(d.toLocaleString())}">` +
           `${esc(when)}</time>`;
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
               (m.proactive ? '<em>· she spoke first</em>' : '') +
               stamp(m.ts) + '</span>';
    if (m.image_url) {
      const imageUrl = httpPath(m.image_url);
      body += `<a href="${esc(imageUrl)}" target="_blank" rel="noopener">` +
              `<img class="msg-img" src="${esc(imageUrl)}" alt="a selfie from her"></a>`;
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

  // The backfill lands asynchronously, and the stream is already live by then:
  // a message that arrives in that gap would render ABOVE the older history it
  // follows — she answers before you asked. So live messages queue until the
  // backfill has been laid down, then replay in order behind it. (Only messages:
  // drafts and avatar ops are about *now* and never need re-ordering.)
  let backfilled = false;
  let queued = [];

  function flushBackfill(history) {
    if (backfilled) return;               // the failsafe already fired
    history.forEach(addMsg);
    backfilled = true;
    queued.forEach(addMsg);               // addMsg dedups by id — an overlap is fine
    queued = [];
  }

  async function connect({ onStatus } = {}) {
    await runtimeReady;
    if (es) return;                       // one stream per page
    es = new EventSource(apiPath('/api/events'));
    es.onopen = () => onStatus?.(true);
    es.onerror = () => onStatus?.(false); // EventSource auto-reconnects
    es.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      // the stage adapters listen here (the YuriOS `yurios-ev` pattern)
      window.dispatchEvent(new CustomEvent('world-ev', { detail: m }));
      if (m.type === 'hello') {
        charName = m.character || '';
        // every label that names her at once — the chat head, the masthead, the
        // gate. One attribute, so a page can add a fourth without touching this.
        if (charName) {
          for (const el of document.querySelectorAll('[data-char-name]'))
            el.textContent = charName;
          // …and the tab, which says which of her rooms this is: three pages run
          // this file now (`data-room` on <html>, default the 3D one).
          document.title =
            `${charName} / ${document.documentElement.dataset.room || 'Sanctuary'}`;
        }
      } else if (m.type === 'message') {
        if (backfilled) addMsg(m); else queued.push(m);
      } else if (m.type === 'draft') addDraft(m.text);
      else if (m.type === 'draft_cancel') dropDraft();
    };
    // backfill what was said before this page opened (SPEC §2.6)
    fetch(apiPath('/api/history')).then((r) => r.json())
      .then((d) => flushBackfill(d.messages || []))
      .catch(() => flushBackfill([]));   // no history is still "history is done"
    // …and a hung fetch must never cost her a live word: give up waiting and
    // show what's arriving, out of order but present.
    setTimeout(() => flushBackfill([]), 5000);
  }

  window.WorldChat = { connect };
})();
