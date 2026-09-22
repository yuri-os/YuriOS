/* The inner-life panel (SPEC §24.3) — "what did you do while I was gone?"
 * as a page, not a vibe.
 *
 * Second tab of the chat column. Reads /api/mind (activity state, budget,
 * goals, queued self-edits), /api/timers, and /api/mind/journal (her [she]
 * lines out of the shared episodic journal), and refreshes live off the same
 * one bus chat.js already subscribes to: every event is re-dispatched as a
 * `world-ev` CustomEvent, and this panel reacts to the relevant state events. The
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
  const filesPanel = document.getElementById('files');
  const galleryPanel = document.getElementById('gallery');
  const messagesEl = document.getElementById('messages');
  const tabChat = document.getElementById('tab-chat');
  const tabMind = document.getElementById('tab-mind');
  const tabFiles = document.getElementById('tab-files');
  const tabGallery = document.getElementById('tab-gallery');
  // The gallery is the one optional panel: a page that carries the transcript
  // and the inner life but no shelf (a future room, a cut-down client) should
  // lose the tab, not the whole script.
  if (!panel || !filesPanel || !tabChat || !tabMind || !tabFiles) return;

  let open = false;
  let activeView = 'now';
  let refreshTimer = null;
  let busy = false;               // is she reading? decides the refresh cadence
  const droppingGoals = new Set();
  const openDesks = new Set();    // goal ids whose desk file is unfolded
  const deskCache = new Map();    // id -> { text, missing }
  const deskPending = new Map();  // id -> request token; superseded fetches are ignored
  const deskPaths = new Map();
  // A queued self-edit you are rewriting: id -> the text so far. Kept here, not
  // in the DOM, because the panel is rebuilt every few seconds while you type.
  const editDrafts = new Map();
  const editErrors = new Map();   // id -> why the server refused your version
  const wholeOpen = new Set();    // ids whose "whole file" fold is open
  const pendingById = new Map();  // id -> the entry as last fetched
  let lastHtml = '';
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

  function navigation(counts) {
    const item = (id, label, count) => {
      const on = activeView === id;
      return `<button type="button" role="tab" class="${on ? 'on' : ''}" ` +
        `data-il-view="${id}" aria-controls="il-page-${id}" ` +
        `aria-selected="${on}"><span>${label}</span>` +
        (count ? `<small>${count}</small>` : '') + '</button>';
    };
    return '<nav class="il-nav" role="tablist" aria-label="Inner life sections">' +
      item('now', 'now', counts.now) + item('plans', 'plans', counts.plans) +
      item('history', 'history', counts.history) + '</nav>';
  }

  function page(id, html) {
    const on = activeView === id;
    return `<div id="il-page-${id}" class="il-page" role="tabpanel" ` +
      `data-il-page="${id}"${on ? '' : ' hidden'}>${html}</div>`;
  }

  function remaining(due) {
    const seconds = Math.max(0, Number(due) - Date.now() / 1000);
    if (seconds < 60) return seconds > 0 ? 'in less than a minute' : 'due now';
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60) return `in ${plural(minutes, 'minute', 'minutes')}`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return `in ${plural(hours, 'hour', 'hours')}${rest ? ` ${rest}m` : ''}`;
  }

  function timerSection(timerState) {
    const timers = [...(timerState?.timers || [])]
      .filter(timer => Number.isFinite(Number(timer.due)))
      .sort((a, b) => a.due - b.due);
    const body = timers.length
      ? '<ol class="il-timers">' + timers.map(timer => {
          const at = new Date(Number(timer.due) * 1000);
          const clock = Number.isFinite(at.getTime())
            ? at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
          return `<li><span class="il-timer-label">${esc(timer.label || 'timer')}</span>` +
            `<strong>${esc(remaining(timer.due))}</strong>` +
            (clock ? `<time datetime="${at.toISOString()}">${esc(clock)}</time>` : '') +
            '</li>';
        }).join('') + '</ol>'
      : '<p class="il-off">no timers pending</p>';
    return { html: section('timers', body), count: timers.length };
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

  function readingSections(read) {
    const n = countLive(read);
    busy = n > 0;
    markTab(n);
    if (!read) return { now: '', history: '', live: 0, held: 0 };
    // forget the optimistic notes for anything that has since finished — a run
    // id that comes round again must not inherit an old click's "pausing"
    if (!read.reading) asked.delete('');
    const ids = new Set(liveRuns(read).map(r => r.id));
    for (const key of [...asked]) if (key && !ids.has(key)) asked.delete(key);

    const runs = (read.runs || []).slice().reverse();
    const live = runs.filter(r => !OVER.includes(r.stage));
    let now = '';
    if (read.reading || live.length) {
      now += section('she is reading',
        (readingNow(read.reading) + live.map(runRow).join('')) ||
        '<p class="il-off">looking things up</p>');
    }
    if ((read.held || []).length) {
      now += section('held — waiting on you',
        '<p class="il-off">stopped, and kept. Nothing here is read again ' +
        'until you say so.</p>' + read.held.map(heldRow).join(''));
    }
    const past = runs.filter(r => OVER.includes(r.stage));
    let history = '';
    if (past.length && !live.length) {
      history = section('what she looked up', past.slice(0, 3).map(runRow).join(''));
    }
    return { now, history, live: n, held: (read.held || []).length };
  }

  // Her own goals are marked as hers and nothing else about them is hidden:
  // the raw provenance stays visible beside the plain-language tag, because
  // `strategy:2026-08-23` is the thing you would grep for and "she filed this"
  // is the thing you can read at a glance. Both, not one.
  const HERS = 'strategy:';

  function visibleIntentions(goals) {
    const crossed = (g) => g.state === 'abandoned' || droppingGoals.has(g.id);
    return [
      ...goals.filter(g => !crossed(g)),
      ...goals.filter(crossed).slice(-5),
    ];
  }

  function deskPath(g) {
    // MindLoop.GOAL_DESK — the snapshot names it so this panel does not invent it.
    return g.desk || `goals/${g.id}.md`;
  }

  function deskBody(id) {
    const cached = deskCache.get(id);
    if (!cached) return 'opening…';
    if (cached.missing) return "she hasn't written this one up yet.";
    return cached.text;
  }

  function goalRow(g) {
    deskPaths.set(g.id, deskPath(g));
    const hers = String(g.provenance || '').startsWith(HERS);
    const abandoned = g.state === 'abandoned';
    if (abandoned) droppingGoals.delete(g.id);
    const dropping = !abandoned && droppingGoals.has(g.id);
    const open = !abandoned && !dropping;
    const looking = openDesks.has(g.id);
    return `<li class="g-${esc(open ? g.state : 'abandoned')}` +
      `${hers ? ' g-hers' : ''}">` +
      `<span class="il-goal-line">` +
      (hers ? '<span class="il-hers">she filed this</span> ' : '') +
      `${esc(g.text)}` +
      `</span>` +
      `<span class="il-goal-actions">` +
      `<button type="button" class="il-look" data-desk="${esc(g.id)}" ` +
      `data-path="${esc(deskPath(g))}">` +
      `${looking ? 'fold file away' : 'view file'}</button>` +
      (open ? `<button type="button" class="il-drop" data-goal="${esc(g.id)}">let go` +
              '</button>' : '') +
      `</span>` +
      `<span class="il-prov">(${esc(g.kind)} · ${esc(g.provenance)}` +
      `${open ? '' : dropping ? ' · letting go' : ' · let go'})</span>` +
      (looking
        ? `<pre class="il-content il-desk">${esc(deskBody(g.id))}</pre>`
        : '') +
      '</li>';
  }

  // The switch sits here, on the list it governs, rather than in the settings
  // dialog — a permission you can only find by leaving the page that shows you
  // why you'd want it is a permission nobody revokes in time. Two buttons side
  // by side, with the live one marked: no hunting for which way a checkbox
  // means "off".
  function filingSwitch(f) {
    if (!f) return '';
    const btn = (on, label) =>
      `<button class="il-sw${f.enabled === on ? ' on' : ''}" ` +
      `data-filing="${on}"${f.enabled === on ? ' disabled' : ''}>${label}</button>`;
    return '<p class="il-filing">goals of her own ' +
      btn(true, 'on') + btn(false, 'off') +
      `<span class="il-prov"> ${f.open} of ${f.max} open</span></p>`;
  }

  async function render() {
    await runtimeReady;
    let state, journal, timerState, read = null;
    try {
      // Timers live even when the mind is off and have their own runtime route.
      // Treat an older/degraded host without that route as an empty board rather
      // than losing the rest of this surface with it.
      const timerRequest = Promise.resolve()
        .then(() => fetch(apiPath('/api/timers')))
        .catch(() => null);
      const [a, b, c, d] = await Promise.all([
        fetch(apiPath('/api/mind')), fetch(apiPath('/api/mind/journal?days=3')),
        fetch(apiPath('/api/mind/reading')), timerRequest]);
      if (!a.ok) throw new Error(await a.text());
      state = await a.json();
      journal = b.ok ? await b.json() : { days: [] };
      read = c.ok ? await c.json() : null;
      timerState = d?.ok ? await d.json() : { timers: [] };
    } catch {
      busy = false;              // nothing to watch; back to the slow cadence
      markTab(0);
      lastHtml = ''; panel.innerHTML = '<p class="il-off">the mind isn’t running — ' +
        'MIND_ENABLED=false, or she booted without a brain</p>';
      return;
    }

    const stateLabel = STATE_META[canonicalState(state.state)].label;
    let nowHtml = section('right now',
      `<p class="il-state"><b>${esc(stateLabel)}</b> · a heartbeat every ` +
      `${Math.round(state.cadence_s)}s · spoke first ` +
      `${state.interrupts_today}× today` +
      (state.dream_backlog.length
        ? ` · ${state.dream_backlog.length} day(s) to dream on` : '') +
      `</p><p class="il-budget">budget: ${state.budget.spent_tokens} / ` +
      `${state.budget.daily_tokens} tokens today</p>`);

    const timers = timerSection(timerState);
    nowHtml += timers.html;

    // Above decisions and plans on purpose: this is the only block with
    // something spending the machine *while you read it*.
    const reading = readingSections(read);
    nowHtml += reading.now;

    const edits = state.pending_edits || [];
    pendingById.clear();
    for (const e of edits) pendingById.set(e.id, e);
    for (const id of [...editDrafts.keys()]) {
      if (!pendingById.has(id)) { editDrafts.delete(id); editErrors.delete(id); }
    }
    if (edits.length) {
      nowHtml += section('she asks — edits waiting on you', edits.map(editBlock).join(''));
    }

    const goals = (state.goals || []).filter(g => g.state !== 'done');
    const maintenance = goals.filter(g => g.kind === 'maintenance' ||
      String(g.provenance || '').startsWith('maintenance:'));
    const intentions = visibleIntentions(goals.filter(g => !maintenance.includes(g)));
    const filing = state.goal_filing;
    let plansHtml = '';
    if (intentions.length || filing) {
      plansHtml += section('on her mind', filingSwitch(filing) +
        (intentions.length
          ? '<ul class="il-goals">' + intentions.map(goalRow).join('') + '</ul>'
          : '<p class="il-off">nothing she means to do right now</p>'));
    }
    if (maintenance.length) {
      plansHtml += section('system upkeep',
        '<p class="il-off">automatic work kept separate from her own intentions</p>' +
        '<ul class="il-goals il-maintenance">' + maintenance.map(goalRow).join('') +
        '</ul>');
    }

    if ((state.shelf || []).length) {
      plansHtml += section('the shelf',
        '<ul class="il-shelf">' + state.shelf.map(d =>
          `<li>${esc(d)}</li>`).join('') + '</ul>');
    }

    if (!plansHtml) plansHtml = section('plans', '<p class="il-off">nothing queued</p>');

    const days = journal.days || [];
    const historyHtml = reading.history + section('the journal',
      days.map(d =>
        `<h4>${esc(d.day)}</h4><ul class="il-journal">` +
        collapse(d.entries.filter(e => e.hers)).map(journalLine).join('') + '</ul>'
      ).join('') || '<p class="il-off">nothing yet — she hasn’t been ' +
        'alone with her thoughts long enough</p>');

    const openGoals = goals.filter(g => g.state !== 'abandoned').length;
    const needsAttention = timers.count + reading.live + reading.held + edits.length;
    const html = navigation({
      now: needsAttention,
      plans: openGoals,
      history: days.length,
    }) + page('now', nowHtml) + page('plans', plansHtml) + page('history', historyHtml);
    repaint(html);
  }

  // What a queued edit changes, then the whole file folded under it. A persona
  // proposal is the entire new file; the change is usually a few lines at the
  // end of it, and the reader came to rule on the change.
  function diffHtml(e) {
    const lines = e.diff || [];
    if (!lines.length) return '<p class="il-off">no change to the file as it stands</p>';
    // One block per line and nothing between them: the marker is drawn by the
    // stylesheet, in a gutter, so a wrapped line hangs under its own text.
    return `<div class="il-diff" data-keep="diff-${esc(e.id)}">` + lines.map(line => {
      if (line === '…') return '<span class="il-gap">…</span>';
      const cls = line[0] === '+' ? 'il-add' : line[0] === '-' ? 'il-del' : 'il-ctx';
      return `<span class="${cls}">${esc(line.slice(1))}</span>`;
    }).join('') + '</div>';
  }

  function editBlock(e) {
    const id = esc(e.id);
    const drafting = editDrafts.has(e.id);
    const error = editErrors.get(e.id);
    const body = drafting
      ? `<textarea class="il-draft" data-draft="${id}" data-keep="draft-${id}" ` +
        `spellcheck="false" aria-label="your version of ${esc(e.surface)}">` +
        `${esc(editDrafts.get(e.id))}</textarea>` +
        '<p class="il-off">approving applies your version instead of hers, ' +
        'checked by the same rules hers passed</p>'
      : '<p class="il-label">what changes</p>' + diffHtml(e) +
        `<details class="il-whole" data-whole="${id}"${wholeOpen.has(e.id) ? ' open' : ''}>` +
        '<summary>the whole file after</summary>' +
        `<pre class="il-content" data-keep="whole-${id}">${esc(e.content)}</pre></details>`;
    return `<div class="il-edit" data-id="${id}">` +
      `<p class="il-surface">${esc(e.surface)}</p>` +
      `<p class="il-reason">${esc(e.reason)}</p>` + body +
      (error ? `<p class="il-err" role="alert">${esc(error)}</p>` : '') +
      '<div class="il-actions">' +
      `<button class="il-ok" data-id="${id}">${drafting ? 'approve my version' : 'approve'}</button>` +
      `<button class="il-no" data-id="${id}">reject</button>` +
      (drafting
        ? `<button class="il-aside" data-unrevise="${id}">discard my changes</button>`
        : `<button class="il-aside" data-revise="${id}">edit</button>`) +
      '</div></div>';
  }

  // The panel is rebuilt on every refresh (seconds apart while she reads) and
  // on every journal or state event. Whatever you were reading or typing in it
  // must survive that: nothing is touched when nothing changed, and otherwise
  // each scroll box keeps its place and the box you were typing in keeps its
  // focus and caret.
  function repaint(html) {
    if (html === lastHtml) return;
    lastHtml = html;
    const scrolls = new Map();
    panel.querySelectorAll('[data-keep]').forEach(el => scrolls.set(el.dataset.keep, el.scrollTop));
    const active = document.activeElement;
    const focusKey = active && panel.contains(active) ? active.dataset?.keep : null;
    const caret = focusKey && typeof active.selectionStart === 'number'
      ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
    panel.innerHTML = html;
    panel.querySelectorAll('[data-keep]').forEach(el => {
      if (scrolls.has(el.dataset.keep)) el.scrollTop = scrolls.get(el.dataset.keep);
    });
    if (focusKey) {
      const el = panel.querySelector(`[data-keep="${CSS.escape(focusKey)}"]`);
      if (el) {
        el.focus({ preventScroll: true });
        if (caret) el.setSelectionRange(...caret);
      }
    }
  }

  panel.addEventListener('click', (ev) => {
    const next = ev.target.closest?.('[data-il-view]')?.dataset.ilView;
    if (!next || next === activeView) return;
    activeView = next;
    panel.querySelectorAll('[data-il-view]').forEach(button => {
      const on = button.dataset.ilView === activeView;
      button.classList.toggle('on', on);
      button.setAttribute('aria-selected', String(on));
    });
    panel.querySelectorAll('[data-il-page]').forEach(view => {
      view.hidden = view.dataset.ilPage !== activeView;
    });
    panel.scrollTop = 0;
  });

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
    const approve = ev.target.classList.contains('il-ok');
    const decision = { approve };
    if (approve && editDrafts.has(id)) decision.content = editDrafts.get(id);
    ev.target.disabled = true;
    try {
      await runtimeReady;
      const res = await fetch(apiPath(`/api/mind/edits/${encodeURIComponent(id)}`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(decision),
      });
      if (res.status === 422) {
        // your version was refused: keep it, say why, let you fix it
        const why = await res.json().catch(() => ({}));
        editErrors.set(id, `not applied — ${why.detail || 'the server refused it'}`);
        ev.target.disabled = false;
        render();
        return;
      }
      editDrafts.delete(id);
      editErrors.delete(id);
    } catch { /* the next refresh shows the truth either way */ }
    setTimeout(render, 1500);           // the loop applies it on its next tick
  });

  // Rewrite a queued edit before ruling on it: the proposal becomes a text box
  // holding her whole file, and approving sends your version instead.
  panel.addEventListener('click', (ev) => {
    const revise = ev.target?.dataset?.revise;
    const unrevise = ev.target?.dataset?.unrevise;
    if (revise && pendingById.has(revise)) {
      editDrafts.set(revise, String(pendingById.get(revise).content ?? ''));
      render().then(() => panel.querySelector(
        `[data-draft="${CSS.escape(revise)}"]`)?.focus({ preventScroll: true }));
    } else if (unrevise) {
      editDrafts.delete(unrevise);
      editErrors.delete(unrevise);
      render();
    }
  });
  panel.addEventListener('input', (ev) => {
    const id = ev.target?.dataset?.draft;
    if (id && editDrafts.has(id)) editDrafts.set(id, ev.target.value);
  });
  // `toggle` doesn't bubble; capture it so an open fold stays open on refresh
  panel.addEventListener('toggle', (ev) => {
    const id = ev.target?.dataset?.whole;
    if (!id) return;
    if (ev.target.open) wholeOpen.add(id); else wholeOpen.delete(id);
  }, true);

  // The desk file behind a one-line goal (SPEC §22.3, §24.3). Same fetch as
  // a report card in the transcript: folded until asked for, cached after.
  panel.addEventListener('click', (ev) => {
    const el = ev.target.closest?.('.il-look');
    if (!el || el.disabled) return;
    const id = el.dataset.desk;
    const path = el.dataset.path;
    if (!id) return;
    toggleDesk(id, path);
  });

  async function toggleDesk(id, path) {
    if (openDesks.has(id)) {
      openDesks.delete(id);
      render();
      return;
    }
    openDesks.add(id);
    if (deskCache.has(id) && !deskCache.get(id).missing) {
      render();
      return;
    }
    await loadDesk(id, path);
  }

  async function loadDesk(id, path) {
    if (deskPending.has(id)) return;
    const request = Symbol();
    deskPending.set(id, request);
    deskCache.delete(id);
    render();
    try {
      await runtimeReady;
      const res = await fetch(apiPath(
        '/api/mind/workspace/file?path=' + encodeURIComponent(path || '')));
      if (res.ok) {
        const data = await res.json();
        if (deskPending.get(id) !== request) return;
        deskCache.set(id, { text: data.text || '(it is empty)', missing: false });
      } else {
        if (deskPending.get(id) !== request) return;
        deskCache.set(id, { text: '', missing: true });
      }
    } catch {
      if (deskPending.get(id) !== request) return;
      deskCache.set(id, { text: '', missing: true });
    } finally {
      if (deskPending.get(id) === request) deskPending.delete(id);
    }
    if (openDesks.has(id)) render();
  }

  // Letting go of a goal, and the filing switch. Separate from the self-edit
  // handler above because these two are not rulings on something she asked for
  // — she did not ask, and that is exactly why they have to be one click.
  panel.addEventListener('click', async (ev) => {
    const el = ev.target.closest?.('button');
    if (!el) return;
    const goal = el.classList.contains('il-drop') ? el.dataset.goal : undefined;
    const filing = el.dataset.filing;
    if (goal === undefined && filing === undefined) return;
    if (el.disabled) return;
    el.disabled = true;
    const row = goal !== undefined ? el.closest('li') : null;
    if (goal !== undefined) {
      // The route queues a decision for the mind's next tick. Show the decision
      // now, and remember it across API-driven rerenders, rather than restoring
      // an apparently live button while the signal is still in flight.
      droppingGoals.add(goal);
      row?.classList.add('g-abandoned');
      el.textContent = 'letting go…';
    }
    try {
      await runtimeReady;
      const [path, body] = goal !== undefined
        ? [`/api/mind/goals/${encodeURIComponent(goal)}/abandon`, {}]
        : ['/api/mind/goals/filing', { enabled: filing === 'true' }];
      const res = await fetch(apiPath(path), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        if (goal !== undefined) droppingGoals.delete(goal);
        row?.classList.remove('g-abandoned');
        el.textContent = 'that didn’t take';
        el.disabled = false;
      } else if (goal !== undefined) {
        render();
      }
    } catch {
      if (goal !== undefined) droppingGoals.delete(goal);
      row?.classList.remove('g-abandoned');
      el.textContent = 'no answer from her';
      el.disabled = false;
    }
    // A dropped goal lands on her next tick; the switch has already landed.
    setTimeout(render, goal !== undefined ? 1500 : 300);
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

  function show(view) {
    const mind = view === 'mind';
    const files = view === 'files';
    const gallery = view === 'gallery';
    open = mind;
    panel.hidden = !mind;
    filesPanel.hidden = !files;
    if (galleryPanel) galleryPanel.hidden = !gallery;
    if (messagesEl) messagesEl.style.display = view === 'chat' ? '' : 'none';
    tabMind.classList.toggle('on', mind);
    tabFiles.classList.toggle('on', files);
    tabGallery?.classList.toggle('on', gallery);
    tabChat.classList.toggle('on', view === 'chat');
    clearTimeout(refreshTimer);
    refreshTimer = null;
    clearTimeout(watchTimer);
    watchTimer = null;
    markTab(liveN);
    if (mind) {
      render().then(pace);
    } else if (files) {
      window.dispatchEvent(new Event('files-open'));
    } else if (gallery) {
      // js/gallery.js loads its first page here and nowhere else: a shelf of
      // full-size PNGs is not something a room should fetch to keep hidden.
      window.dispatchEvent(new Event('gallery-open'));
    } else {
      // anything she said while this panel covered the transcript couldn't be
      // scrolled to — a hidden box has no height. Pin the bottom on the way back.
      window.WorldChat?.scrollToLatest?.();
      watch();
    }
  }

  tabChat.addEventListener('click', () => show('chat'));
  tabMind.addEventListener('click', () => show('mind'));
  tabFiles.addEventListener('click', () => show('files'));
  tabGallery?.addEventListener('click', () => show('gallery'));

  // live nudges off the one bus: a journal line, a state change, or a research
  // run starting or ending while the panel is open re-renders it — and re-paces
  // it, because a run beginning is exactly when the slow cadence stops doing.
  window.addEventListener('world-ev', (ev) => {
    const t = ev.detail?.type;
    if (t === 'workspace') {
      // A write may land before an older fetch completes. Retire both cached
      // results and pending requests; only the fresh response may repaint.
      deskCache.clear();
      deskPending.clear();
      for (const id of openDesks) loadDesk(id, deskPaths.get(id));
    }
    if (open && (t === 'journal' || t === 'mind' || t === 'research_status' ||
                 t === 'timers')) {
      render().then(pace);
    } else if (!open && !filesPanel.hidden && t === 'workspace') {
      window.dispatchEvent(new Event('files-refresh'));
    } else if (!open && filesPanel.hidden && (t === 'research_status' || t === 'mind')) {
      // a run starting is the whole reason for the mark; a tick is the only
      // beat a read off the shelf — which fires no research event — arrives on
      watch();
    }
  });

  // a run that was already going when this page loaded still lights the tab
  watch();
})();
