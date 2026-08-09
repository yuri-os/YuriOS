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
import { STATE_META, canonicalState } from '../shared/activity-state.js';

(() => {
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
  const panel = document.getElementById('innerlife');
  const messagesEl = document.getElementById('messages');
  const tabChat = document.getElementById('tab-chat');
  const tabMind = document.getElementById('tab-mind');
  if (!panel || !tabChat || !tabMind) return;

  let open = false;
  let refreshTimer = null;
  let busy = false;               // is she reading? decides the refresh cadence
  const SLOW = 20000;             // DORMANT ticks are slow
  const FAST = 2000;              // a passage takes seconds; a bar should move

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  function section(title, bodyHtml) {
    return `<section class="il-sec"><h3>${title}</h3>${bodyHtml}</section>`;
  }

  // A quiet stretch writes the same line every time she wakes — "thought about
  // X; chose not to interrupt" four times in an hour is four true entries and
  // one fact. Fold consecutive entries with identical text into one, spanning
  // the stretch. Runs AFTER the `hers` filter, so an entry this panel doesn't
  // show can't break a run the reader sees as continuous. Day files are
  // chronological, so `time` is the first and `until` the last.
  function collapse(entries) {
    const out = [];
    for (const e of entries) {
      const last = out[out.length - 1];
      if (last && last.text === e.text) {
        last.until = e.time;
        last.count += 1;
      } else {
        out.push({ ...e, until: e.time, count: 1 });
      }
    }
    return out;
  }

  function journalLine(e) {
    const when = e.count > 1 ? `${esc(e.time)} – ${esc(e.until)}` : esc(e.time);
    const times = e.count > 1 ? ` <span class="il-x">×${e.count}</span>` : '';
    return `<li><span class="il-t">${when}${times}</span> ${esc(e.text)}</li>`;
  }

  // ---- her reading (SPEC §7.7, §24.3) -------------------------------------
  //
  // A `research` call answers in 12ms and then spends the next half hour of
  // this machine on documents nobody has seen. These three blocks are that
  // made visible: what she's reading, what it costs in model calls, and the
  // buttons that stop it without losing the document.

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function bar(done, total) {
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    return `<span class="il-bar"><i style="width:${pct}%"></i></span>`;
  }

  function readingNow(r) {
    if (!r) return '';
    const how = r.digested ? 'in notes' : 'word for word';
    return `<div class="il-read">` +
      `<p class="il-read-doc">${esc(r.doc)}` +
      `<span class="il-prov"> · ${how}${r.resumed ? ' · resumed' : ''}</span></p>` +
      bar(r.done, r.passages) +
      `<p class="il-read-n">${r.done} / ${r.passages} passages · ` +
      `${r.calls_done} of ~${r.calls} model calls</p>` +
      `<button class="il-stop" data-stop="">stop reading</button></div>`;
  }

  function runRow(run) {
    const live = !['done', 'error', 'stopped'].includes(run.stage);
    const pages = run.pages || [];
    const bits = [esc(run.stage)];
    if (run.found != null) bits.push(plural(run.found, 'result', 'results'));
    if (pages.length) bits.push(`${run.read}/${pages.length} read`);
    if (run.calls) bits.push(`~${run.calls} model calls`);
    bits.push(`${Math.round(run.elapsed_s)}s`);
    return `<div class="il-run${live ? ' on' : ''}">` +
      `<p class="il-run-top"><b>${esc(run.topic)}</b>` +
      (live ? `<button class="il-stop" data-stop="${esc(run.id)}">stop</button>` : '') +
      `</p><p class="il-prov">${bits.map(esc).join(' · ')}</p>` +
      (pages.length
        ? '<ul class="il-pages">' + pages.map(p =>
            `<li class="p-${esc(p.state)}"><span class="il-t">${esc(p.state)}</span> ` +
            `${esc(p.title || p.url)}` +
            (p.calls ? ` <span class="il-prov">(~${p.calls} calls)</span>` : '') +
            '</li>').join('') + '</ul>'
        : '') + '</div>';
  }

  function heldRow(h) {
    const left = h.passages ? `${h.passages - h.done} of ${h.passages} passages` :
      'not started';
    return `<div class="il-held"><p class="il-held-doc">${esc(h.doc)}</p>` +
      `<p class="il-prov">${esc(h.reason)} · ${esc(left)} left` +
      (h.remaining_calls ? ` · ~${h.remaining_calls} model calls to finish` : '') +
      `</p><button class="il-go" data-resume="${esc(h.doc)}">resume reading</button>` +
      '</div>';
  }

  function readingSection(read) {
    busy = false;
    if (!read) return '';
    const runs = (read.runs || []).slice().reverse();
    const live = runs.filter(r => !['done', 'error', 'stopped'].includes(r.stage));
    busy = Boolean(read.reading) || live.length > 0;
    let html = '';
    if (read.reading || live.length) {
      html += section('she is reading',
        (readingNow(read.reading) + live.map(runRow).join('')) ||
        '<p class="il-off">looking things up</p>');
    }
    if ((read.held || []).length) {
      html += section('held — waiting on you',
        '<p class="il-off">stopped, and kept. Nothing here is read again ' +
        'until you say so.</p>' + read.held.map(heldRow).join(''));
    }
    const past = runs.filter(r => ['done', 'error', 'stopped'].includes(r.stage));
    if (past.length && !live.length) {
      html += section('what she looked up', past.slice(0, 3).map(runRow).join(''));
    }
    return html;
  }

  async function render() {
    await runtimeReady;
    let state, journal, read = null;
    try {
      const [a, b, c] = await Promise.all([
        fetch(apiPath('/api/mind')), fetch(apiPath('/api/mind/journal?days=3')),
        fetch(apiPath('/api/mind/reading'))]);
      if (!a.ok) throw new Error(await a.text());
      state = await a.json();
      journal = b.ok ? await b.json() : { days: [] };
      read = c.ok ? await c.json() : null;
    } catch {
      busy = false;              // nothing to watch; back to the slow cadence
      panel.innerHTML = '<p class="il-off">the mind isn’t running — ' +
        'MIND_ENABLED=false, or she booted without a brain</p>';
      return;
    }

    const stateLabel = STATE_META[canonicalState(state.state)].label;
    let html = section('right now',
      `<p class="il-state"><b>${esc(stateLabel)}</b> · a heartbeat every ` +
      `${Math.round(state.cadence_s)}s · spoke first ` +
      `${state.interrupts_today}× today` +
      (state.dream_backlog.length
        ? ` · ${state.dream_backlog.length} day(s) to dream on` : '') +
      `</p><p class="il-budget">budget: ${state.budget.spent_tokens} / ` +
      `${state.budget.daily_tokens} tokens today</p>`);

    // above the goals and the journal on purpose: this is the only block with
    // something spending the machine *while you read it*
    html += readingSection(read);

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
        collapse(d.entries.filter(e => e.hers)).map(journalLine).join('') + '</ul>'
      ).join('') || '<p class="il-off">nothing yet — she hasn’t been ' +
        'alone with her thoughts long enough</p>');

    panel.innerHTML = html;
  }

  // stop / resume. The stop button carries the run id, or "" for "whatever she
  // is reading this second" — the two are separate because a run can be stopped
  // before it has started reading anything.
  panel.addEventListener('click', async (ev) => {
    const el = ev.target;
    const stop = el?.dataset?.stop;
    const resume = el?.dataset?.resume;
    if (stop === undefined && resume === undefined) return;
    el.disabled = true;
    el.textContent = stop !== undefined ? 'stopping…' : 'resuming…';
    try {
      await runtimeReady;
      const [path, body] = stop !== undefined
        ? ['/api/mind/reading/stop', stop ? { run: stop } : {}]
        : ['/api/mind/reading/resume', { doc: resume }];
      const res = await fetch(apiPath(path), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) el.textContent = 'that didn’t take — try again';
    } catch {
      el.textContent = 'no answer from her';
    }
    // a stop lands after the passage she's on, so give it a beat before asking
    setTimeout(render, 900);
  }, true);

  panel.addEventListener('click', async (ev) => {
    const id = ev.target?.dataset?.id;
    if (!id || !(ev.target.classList.contains('il-ok') ||
                 ev.target.classList.contains('il-no'))) return;
    ev.target.disabled = true;
    try {
      await runtimeReady;
      await fetch(apiPath(`/api/mind/edits/${encodeURIComponent(id)}`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: ev.target.classList.contains('il-ok') }),
      });
    } catch { /* the next refresh shows the truth either way */ }
    setTimeout(render, 1500);           // the loop applies it on its next tick
  });

  // One timer, re-pitched every render: a progress bar that only moves every
  // 20s isn't progress, and polling every 2s for a panel that has been still
  // all afternoon is a waste of both ends. `busy` is set by readingSection().
  function pace() {
    clearTimeout(refreshTimer);
    refreshTimer = open
      ? setTimeout(() => render().then(pace), busy ? FAST : SLOW)
      : null;
  }

  function show(mind) {
    open = mind;
    panel.hidden = !mind;
    if (messagesEl) messagesEl.style.display = mind ? 'none' : '';
    tabMind.classList.toggle('on', mind);
    tabChat.classList.toggle('on', !mind);
    clearTimeout(refreshTimer);
    refreshTimer = null;
    if (mind) {
      render().then(pace);
    } else {
      // anything she said while this panel covered the transcript couldn't be
      // scrolled to — a hidden box has no height. Pin the bottom on the way back.
      window.WorldChat?.scrollToLatest?.();
    }
  }

  tabChat.addEventListener('click', () => show(false));
  tabMind.addEventListener('click', () => show(true));

  // live nudges off the one bus: a journal line, a state change, or a research
  // run starting or ending while the panel is open re-renders it — and re-paces
  // it, because a run beginning is exactly when the slow cadence stops doing.
  window.addEventListener('world-ev', (ev) => {
    const t = ev.detail?.type;
    if (open && (t === 'journal' || t === 'mind' || t === 'research_status')) {
      render().then(pace);
    }
  });
})();
