/* The inner-life panel (SPEC §24.3) — "what did you do while I was gone?"
 * as a page, not a vibe.
 *
 * Second tab of the chat column. Reads /api/mind (activity state, budget,
 * goals, queued self-edits) and /api/mind/journal (her [she] lines out of the
 * shared episodic journal), and refreshes live off the same one bus chat.js
 * already subscribes to: every event is re-dispatched as a `world-ev`
 * CustomEvent, and this panel reacts to the `journal` and `mind` ones. The
 * approve/reject buttons on a queued self-edit POST a decision — which lands
 * as a signal the loop consumes on its next tick, exactly like everything
 * else that happens to her.
 */
(() => {
  const panel = document.getElementById('innerlife');
  const messagesEl = document.getElementById('messages');
  const tabChat = document.getElementById('tab-chat');
  const tabMind = document.getElementById('tab-mind');
  if (!panel || !tabChat || !tabMind) return;

  let open = false;
  let refreshTimer = null;

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  function section(title, bodyHtml) {
    return `<section class="il-sec"><h3>${title}</h3>${bodyHtml}</section>`;
  }

  async function render() {
    let state, journal;
    try {
      const [a, b] = await Promise.all([
        fetch('/api/mind'), fetch('/api/mind/journal?days=3')]);
      if (!a.ok) throw new Error(await a.text());
      state = await a.json();
      journal = b.ok ? await b.json() : { days: [] };
    } catch {
      panel.innerHTML = '<p class="il-off">the mind isn’t running — ' +
        'MIND_ENABLED=false, or she booted without a brain</p>';
      return;
    }

    let html = section('right now',
      `<p class="il-state"><b>${esc(state.state)}</b> · a heartbeat every ` +
      `${Math.round(state.cadence_s)}s · spoke first ` +
      `${state.interrupts_today}× today` +
      (state.dream_backlog.length
        ? ` · ${state.dream_backlog.length} day(s) to dream on` : '') +
      `</p><p class="il-budget">budget: ${state.budget.spent_tokens} / ` +
      `${state.budget.daily_tokens} tokens today</p>`);

    if ((state.pending_edits || []).length) {
      html += section('she asks — edits waiting on you',
        state.pending_edits.map(e =>
          `<div class="il-edit" data-id="${esc(e.id)}">` +
          `<p class="il-surface">${esc(e.surface)}</p>` +
          `<p class="il-reason">${esc(e.reason)}</p>` +
          `<pre class="il-content">${esc(e.content).slice(0, 1200)}</pre>` +
          `<button class="il-ok" data-id="${esc(e.id)}">approve</button> ` +
          `<button class="il-no" data-id="${esc(e.id)}">reject</button></div>`
        ).join(''));
    }

    const goals = (state.goals || []).filter(g => g.state !== 'done');
    if (goals.length) {
      html += section('on her mind',
        '<ul class="il-goals">' + goals.map(g =>
          `<li class="g-${esc(g.state)}">${esc(g.text)} ` +
          `<span class="il-prov">(${esc(g.kind)} · ${esc(g.provenance)}` +
          `${g.state === 'abandoned' ? ' · let go' : ''})</span></li>`
        ).join('') + '</ul>');
    }

    if ((state.shelf || []).length) {
      html += section('the shelf',
        '<ul class="il-shelf">' + state.shelf.map(d =>
          `<li>${esc(d)}</li>`).join('') + '</ul>');
    }

    html += section('the journal',
      (journal.days || []).map(d =>
        `<h4>${esc(d.day)}</h4><ul class="il-journal">` +
        d.entries.filter(e => e.hers).map(e =>
          `<li><span class="il-t">${esc(e.time)}</span> ${esc(e.text)}</li>`
        ).join('') + '</ul>'
      ).join('') || '<p class="il-off">nothing yet — she hasn’t been ' +
        'alone with her thoughts long enough</p>');

    panel.innerHTML = html;
  }

  panel.addEventListener('click', async (ev) => {
    const id = ev.target?.dataset?.id;
    if (!id || !(ev.target.classList.contains('il-ok') ||
                 ev.target.classList.contains('il-no'))) return;
    ev.target.disabled = true;
    try {
      await fetch(`/api/mind/edits/${encodeURIComponent(id)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: ev.target.classList.contains('il-ok') }),
      });
    } catch { /* the next refresh shows the truth either way */ }
    setTimeout(render, 1500);           // the loop applies it on its next tick
  });

  function show(mind) {
    open = mind;
    panel.hidden = !mind;
    if (messagesEl) messagesEl.style.display = mind ? 'none' : '';
    tabMind.classList.toggle('on', mind);
    tabChat.classList.toggle('on', !mind);
    clearInterval(refreshTimer);
    refreshTimer = null;
    if (mind) {
      render();
      refreshTimer = setInterval(render, 20000);  // DORMANT ticks are slow
    }
  }

  tabChat.addEventListener('click', () => show(false));
  tabMind.addEventListener('click', () => show(true));

  // live nudges off the one bus: a journal line or a state change while the
  // panel is open re-renders it
  window.addEventListener('world-ev', (ev) => {
    const t = ev.detail?.type;
    if (open && (t === 'journal' || t === 'mind')) render();
  });
})();
