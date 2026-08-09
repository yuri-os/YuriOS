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

  const OVER = ['done', 'error', 'stopped'];
  const liveRuns = (read) => (read?.runs || []).filter(r => !OVER.includes(r.stage));
  const countLive = (read) => liveRuns(read).length + (read?.reading ? 1 : 0);

  // A stop is cooperative: it lands after the passage she's on, which is a
  // model call away — but the click has to look like it landed *now*. This set
  // is what the button reads from in the gap between the POST and the server
  // admitting it; the moment the server says `stopping`, the server is the one
  // telling the truth and the optimistic note is dropped.
  const asked = new Set();          // "" = the read in flight, otherwise a run id

  function pausing(key, serverSays) {
    if (serverSays) asked.delete(key);
    return serverSays || asked.has(key);
  }

  function stopButton(key, off, label) {
    return `<button class="il-stop" data-stop="${esc(key)}"` +
      `${off ? ' disabled' : ''}>${off ? 'busy pausing' : label}</button>`;
  }

  function bar(done, total) {
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    return `<span class="il-bar"><i style="width:${pct}%"></i></span>`;
  }

  function readingNow(r) {
    if (!r) return '';
    const how = r.digested ? 'in notes' : 'word for word';
    const off = pausing('', Boolean(r.stopping));
    return `<div class="il-read">` +
      `<p class="il-read-doc">${esc(r.doc)}` +
      `<span class="il-prov"> · ${how}${r.resumed ? ' · resumed' : ''}` +
      `${off ? ' · stopping after this passage' : ''}</span></p>` +
      bar(r.done, r.passages) +
      `<p class="il-read-n">${r.done} / ${r.passages} passages · ` +
      `${r.calls_done} of ~${r.calls} model calls</p>` +
      stopButton('', off, 'stop reading') + '</div>';
  }

  function runRow(run) {
    const live = !OVER.includes(run.stage);
    const pages = run.pages || [];
    const bits = [esc(run.stage)];
    if (run.found != null) bits.push(plural(run.found, 'result', 'results'));
    if (pages.length) bits.push(`${run.read}/${pages.length} read`);
    if (run.calls) bits.push(`~${run.calls} model calls`);
    bits.push(`${Math.round(run.elapsed_s)}s`);
    return `<div class="il-run${live ? ' on' : ''}">` +
      `<p class="il-run-top"><b>${esc(run.topic)}</b>` +
      (live ? stopButton(run.id, pausing(run.id, run.stage === 'stopping'), 'stop')
            : '') +
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
    const n = countLive(read);
    busy = n > 0;
    markTab(n);
    if (!read) return '';
    // forget the optimistic notes for anything that has since finished — a run
    // id that comes round again must not inherit an old click's "pausing"
    if (!read.reading) asked.delete('');
    const ids = new Set(liveRuns(read).map(r => r.id));
    for (const key of [...asked]) if (key && !ids.has(key)) asked.delete(key);

    const runs = (read.runs || []).slice().reverse();
    const live = runs.filter(r => !OVER.includes(r.stage));
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
    const past = runs.filter(r => OVER.includes(r.stage));
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
      markTab(0);
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
    if (el.disabled) return;
    el.disabled = true;
    // "busy pausing" the instant it is clicked, and it stays that way through
    // every re-render until the passage in flight ends — the wait is the point,
    // and a button that says nothing during it reads as a button that missed.
    if (stop !== undefined) asked.add(stop);
    el.textContent = stop !== undefined ? 'busy pausing' : 'resuming…';
    try {
      await runtimeReady;
      const [path, body] = stop !== undefined
        ? ['/api/mind/reading/stop', stop ? { run: stop } : {}]
        : ['/api/mind/reading/resume', { doc: resume }];
      const res = await fetch(apiPath(path), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        asked.delete(stop);
        el.textContent = 'that didn’t take — try again';
      }
    } catch {
      asked.delete(stop);
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

  // ---- the tab itself, seen from the chat side ----------------------------
  //
  // A research call answers in the transcript in 12ms and then spends the next
  // half hour on the other tab. If you stay in the chat there is nothing at all
  // to tell you any of that is happening — so the tab has to say it: a mark
  // while something of hers is running, and no mark the rest of the time.
  //
  // Event-driven, and only polling while it believes something is live: the
  // point of the pacing below is that a still afternoon costs nothing, and a
  // badge that polls behind a closed panel would give that back.
  let liveN = 0;
  let watchTimer = null;

  function markTab(n) {
    liveN = n;
    const on = n > 0 && !open;
    tabMind.classList.toggle('busy', on);
    if (on) {
      tabMind.title = `she has ${plural(n, 'thing', 'things')} running — ` +
        'reading, or looking something up';
    } else {
      tabMind.removeAttribute('title');
    }
  }

  async function watch() {
    clearTimeout(watchTimer);
    watchTimer = null;
    if (open) return;                  // the panel itself is the notification
    let n = 0;
    try {
      await runtimeReady;
      const res = await fetch(apiPath('/api/mind/reading'));
      n = res.ok ? countLive(await res.json()) : 0;
    } catch { /* no answer is not a run: leave the tab quiet */ }
    markTab(n);
    // again at the end, not only at the top: two events landing together mean
    // two of these in flight, and the loser's timer would otherwise be orphaned
    clearTimeout(watchTimer);
    watchTimer = n > 0 && !open ? setTimeout(watch, SLOW) : null;
  }

  function show(mind) {
    open = mind;
    panel.hidden = !mind;
    if (messagesEl) messagesEl.style.display = mind ? 'none' : '';
    tabMind.classList.toggle('on', mind);
    tabChat.classList.toggle('on', !mind);
    clearTimeout(refreshTimer);
    refreshTimer = null;
    clearTimeout(watchTimer);
    watchTimer = null;
    markTab(liveN);
    if (mind) {
      render().then(pace);
    } else {
      // anything she said while this panel covered the transcript couldn't be
      // scrolled to — a hidden box has no height. Pin the bottom on the way back.
      window.WorldChat?.scrollToLatest?.();
      watch();
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
    } else if (!open && (t === 'research_status' || t === 'mind')) {
      // a run starting is the whole reason for the mark; a tick is the only
      // beat a read off the shelf — which fires no research event — arrives on
      watch();
    }
  });

  // a run that was already going when this page loaded still lights the tab
  watch();
})();
