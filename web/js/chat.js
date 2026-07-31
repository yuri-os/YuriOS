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
  const pending = new Map();
  const pendingExpiry = new Map();

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

  // Whoever actually owns the overflow. In the two rooms with a body #messages
  // scrolls itself; in the text room (SPEC §6.7) it is a plain flex list inside
  // <main class="reader">, and pinning #messages there moves nothing — her reply
  // lands below the fold and you have to chase it. So walk up to the scrolling
  // ancestor once and pin that, and the one renderer keeps the latest line in
  // view on all three pages.
  let scrollEl = null;
  function scroller() {
    if (scrollEl && scrollEl.isConnected) return scrollEl;
    for (let el = messages; el && el !== document.body; el = el.parentElement) {
      const oy = getComputedStyle(el).overflowY;
      if (oy === 'auto' || oy === 'scroll') return (scrollEl = el);
    }
    return (scrollEl = document.scrollingElement || document.body);
  }

  function scroll() {
    if (!messages) return;
    const el = scroller();
    el.scrollTop = el.scrollHeight;
  }

  function dropDraft() {
    if (draftEl) { draftEl.remove(); draftEl = null; }
  }

  const seen = new Set();               // a message can arrive live AND in the
                                        // history backfill — the id resolves it
  function body(m, her, receipt = '') {
    let html = `<span class="who">${her ? esc(charName || 'her') : 'you'}` +
               (m.proactive ? '<em>· she spoke first</em>' : '') +
               (receipt ? `<em class="receipt">${esc(receipt)}</em>` : '') +
               stamp(m.ts) + '</span>';
    if (m.image_url) {
      const imageUrl = httpPath(m.image_url);
      html += `<a href="${esc(imageUrl)}" target="_blank" rel="noopener">` +
              `<img class="msg-img" src="${esc(imageUrl)}" alt="a selfie from her"></a>`;
    }
    if (m.text) html += esc(m.text);
    return html;
  }

  function addMsg(m) {
    if (!messages) return;
    if (m.id) {
      if (seen.has(m.id)) return;
      seen.add(m.id);
    }
    dropDraft();
    const her = m.role !== 'user';
    const optimistic = m.client_id ? (pending.get(m.client_id) ||
      [...messages.children].find((el) => el.dataset.clientId === m.client_id)) : null;
    const div = optimistic || document.createElement('div');
    if (optimistic) {
      pending.delete(m.client_id);
      clearTimeout(pendingExpiry.get(m.client_id));
      pendingExpiry.delete(m.client_id);
    }
    div.className = 'msg ' + (her ? 'her' : 'you') + (m.proactive ? ' proactive' : '');
    delete div.dataset.clientId;
    div.innerHTML = body(m, her, !her && m.client_id ? 'received' : '');
    if (!optimistic) messages.appendChild(div);
    if (optimistic) {
      window.dispatchEvent(new CustomEvent('chat-received', { detail: m }));
    }
    scroll();
    // an <img> has no height until it loads, so the scroll above lands short
    // and the photo bottom sits below the fold. Re-pin when it arrives —
    // unless the user has scrolled away meanwhile (the load can be slow).
    const img = div.querySelector('img');
    if (img) img.addEventListener('load', () => {
      const el = scroller();
      const away = el.scrollHeight - el.clientHeight - el.scrollTop;
      if (away - img.clientHeight < 160) scroll();
    });
  }

  function addPendingUser(text, clientId) {
    if (!messages || !clientId || pending.has(clientId)) return;
    // If the inner-life drawer is open, sending is an explicit return to the
    // conversation. Reveal the transcript before inserting the line.
    document.getElementById('tab-chat')?.click();
    const message = {
      role: 'user', text, client_id: clientId,
      ts: new Date().toISOString(),
    };
    const div = document.createElement('div');
    div.className = 'msg you pending';
    div.dataset.clientId = clientId;
    div.innerHTML = body(message, false, 'sending…');
    pending.set(clientId, div);
    pendingExpiry.set(clientId, setTimeout(() => {
      if (pending.get(clientId) === div) pending.delete(clientId);
      pendingExpiry.delete(clientId);
    }, 300000));
    messages.appendChild(div);
    window.dispatchEvent(new CustomEvent('chat-sending', {
      detail: { text, client_id: clientId },
    }));
    scroll();
  }

  function failPending(clientId, reason = 'not received') {
    const div = pending.get(clientId);
    if (!div) return;
    div.classList.remove('pending');
    div.classList.add('failed');
    const receipt = div.querySelector('.receipt');
    if (receipt) receipt.textContent = reason;
  }

  function stopPending(clientId) {
    const div = pending.get(clientId);
    if (!div) return;
    div.classList.remove('pending');
    const receipt = div.querySelector('.receipt');
    if (receipt) receipt.textContent = 'stopped';
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
    // Optimistic lines must stay after older history even when the user submits
    // before the initial fetch returns.
    const optimistic = [...pending.values()];
    optimistic.forEach((el) => el.remove());
    history.forEach(addMsg);
    optimistic.forEach((el) => messages?.appendChild(el));
    backfilled = true;
    queued.forEach(addMsg);               // addMsg dedups by id — an overlap is fine
    queued = [];
  }

  async function connect({ onStatus } = {}) {
    await runtimeReady;
    if (es) return;                       // one stream per page
    es = new EventSource(apiPath('/api/events'));
    es.onopen = () => {
      onStatus?.(true);
      // Recover committed selfie terminal messages that may have landed before
      // the first connection or while EventSource was reconnecting. addMsg
      // deduplicates the overlap with the normal initial backfill.
      fetch(apiPath('/api/history')).then((r) => r.json()).then((d) => {
        const history = d.messages || [];
        for (const message of history) {
          window.dispatchEvent(new CustomEvent('chat-history-message', {
            detail: { type: 'message', ...message },
          }));
        }
        if (backfilled) history.forEach(addMsg);
        else flushBackfill(history);
      }).catch(() => {});
    };
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
        // A locally submitted line is already visible. Confirm it immediately,
        // even while old history is still loading, instead of hiding the receipt
        // behind the backfill queue for up to five seconds.
        if (m.client_id && pending.has(m.client_id)) addMsg(m);
        else if (backfilled) addMsg(m); else queued.push(m);
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

  // …and one for the inner-life tab: while that panel is up the transcript is
  // display:none, so the pin above lands on a box with no height. Coming back to
  // the chat re-pins it, or you return to wherever you were before she answered.
  window.WorldChat = {
    connect,
    scrollToLatest: scroll,
    addPendingUser,
    confirmUser: addMsg,
    failPending,
    stopPending,
  };
})();
