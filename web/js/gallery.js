/* Her gallery (SPEC §7.6) — the chat column's fourth tab: everything her
 * camera has made, newest first, and what you thought of each one.
 *
 * Three deliberate properties, because a shelf of full-size PNGs is the one
 * panel here that can genuinely cost something:
 *
 *   1. It loads on open and never before. There is no fetch at page load, no
 *      poll behind the tab, no prefetch "while you're chatting" — js/mind.js
 *      fires `gallery-open` when the tab is actually clicked, and that is the
 *      only thing that pulls a page. Thumbnails are the saved PNGs themselves
 *      (there is no thumbnailer, and a local socket does not need one), so
 *      every image also carries `loading="lazy"`: the ones below the fold of
 *      the column arrive when you scroll to them.
 *   2. It pages. /api/gallery is newest-first over the forge's own ledger
 *      (world/gallery.py), twelve to a page, so the thousandth shot costs what
 *      the tenth did.
 *   3. It rates. A score out of ten per picture, POSTed to an append-only
 *      sidecar — the missing feedback loop on a camera with a dozen knobs and
 *      no record of which settings ever took a good photograph. Clicking the
 *      score she already has takes it back off.
 */
(() => {
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
  const httpPath = (path) => window.YuriOSRuntime?.httpPath(path) || path;
  const panel = document.getElementById('gallery');
  if (!panel) return;

  const SCORES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

  const state = {
    page: 0, limit: 12, items: [], total: 0, rated: 0,
    hasMore: false, loading: false, error: '',
  };
  let serial = 0;                   // the page you asked for last is the one
                                    // that gets to paint (fast clicks on next)

  function esc(value) {
    const node = document.createElement('div');
    node.textContent = value ?? '';
    return node.innerHTML;
  }

  function bytes(value = 0) {
    if (!value) return '';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  // The ledger stamps local ISO seconds (forge/types.py). A line the browser
  // can't parse simply doesn't get a date, the way the transcript does it.
  function stamp(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' }) +
      ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  async function request(path, init) {
    await runtimeReady;
    const response = await fetch(apiPath(path), init);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  // ------------------------------------------------------------------ render
  function pips(shot) {
    const scored = Number.isInteger(shot.score);
    return SCORES.map((n) => {
      const on = scored && n <= shot.score;
      const now = scored && n === shot.score;
      return `<button class="gl-pip${on ? ' on' : ''}" data-score="${n}"` +
        ` aria-pressed="${now ? 'true' : 'false'}"` +
        ` title="${now ? 'click again to un-rate' : `rate ${n} out of 10`}">` +
        `${n}</button>`;
    }).join('');
  }

  function tile(shot) {
    const url = httpPath(shot.url);
    const caption = shot.caption || 'untitled';
    const meta = [stamp(shot.created_at), shot.backend,
                  Number.isInteger(shot.seed) ? `seed ${shot.seed}` : '',
                  bytes(shot.bytes)].filter(Boolean).join(' · ');
    const verdict = Number.isInteger(shot.score)
      ? `<b>${shot.score}</b>/10` : 'not rated';
    return `<figure class="gl-shot" data-name="${esc(shot.name)}">` +
      `<a class="gl-frame" href="${esc(url)}" target="_blank" rel="noopener">` +
      `<img loading="lazy" decoding="async" src="${esc(url)}"` +
      ` alt="${esc(caption)}"></a>` +
      `<figcaption><p class="gl-caption">${esc(caption)}</p>` +
      `<p class="gl-meta">${esc(meta)}</p>` +
      `<div class="gl-rate" role="group" aria-label="rate this picture out of ten">` +
      `<span class="gl-verdict">${verdict}</span><span class="gl-pips">` +
      `${pips(shot)}</span></div></figcaption></figure>`;
  }

  function counted() {
    return (state.total === 1 ? '1 picture' : `${state.total} pictures`) +
      (state.rated ? ` · ${state.rated} rated` : '');
  }

  function head() {
    const page = `page ${state.page + 1}`;
    return `<header class="gl-head"><span class="gl-count">${esc(counted())}` +
      `</span><span class="gl-pager">` +
      `<button class="gl-step" data-step="-1"${state.page ? '' : ' disabled'}` +
      ` aria-label="newer">&lt;</button><span class="gl-page">${esc(page)}</span>` +
      `<button class="gl-step" data-step="1"${state.hasMore ? '' : ' disabled'}` +
      ` aria-label="older">&gt;</button></span></header>`;
  }

  function body() {
    if (state.error) {
      return `<p class="gl-empty">the shelf didn't answer — ` +
        `${esc(state.error)}</p>`;
    }
    if (state.loading && !state.items.length) {
      return '<p class="gl-empty">opening the shelf…</p>';
    }
    if (!state.items.length) {
      return state.page
        ? '<p class="gl-empty">nothing further back than this.</p>'
        : '<p class="gl-empty">no photographs yet. Everything her camera ' +
          'makes lands here.</p>';
    }
    return `<div class="gl-grid">${state.items.map(tile).join('')}</div>`;
  }

  function render() {
    panel.innerHTML = head() + body();
    panel.classList.toggle('gl-busy', state.loading);
  }

  // -------------------------------------------------------------------- load
  async function load(page = state.page) {
    const token = ++serial;
    state.loading = true;
    state.error = '';
    render();
    try {
      const data = await request(
        `/api/gallery?page=${page}&limit=${state.limit}`);
      if (token !== serial) return;
      state.page = data.page ?? page;
      state.limit = data.limit || state.limit;
      state.items = data.items || [];
      state.total = data.total || 0;
      state.rated = data.rated || 0;
      state.hasMore = !!data.has_more;
    } catch (error) {
      if (token !== serial) return;
      state.items = [];
      state.error = error.message || 'no answer from the local service';
    } finally {
      if (token === serial) {
        state.loading = false;
        render();
      }
    }
  }

  // ------------------------------------------------------------------ rating
  //
  // Optimistic: the pips light the moment you click, because the round trip is
  // a local file append and waiting for it would make a ten-click session feel
  // like network. A failure puts the old score back and says so — a rating
  // that silently didn't land is worse than one that visibly didn't.

  // Found by walking, not by an attribute selector: a file name is not a
  // selector, and quoting one that contains a quote is a bug waiting to happen.
  function figureFor(name) {
    return [...panel.querySelectorAll('.gl-shot')]
      .find((figure) => figure.dataset.name === name);
  }

  function repaint(shot) {
    const figure = figureFor(shot.name);
    if (!figure) return;
    figure.querySelector('.gl-pips').innerHTML = pips(shot);
    figure.querySelector('.gl-verdict').innerHTML = Number.isInteger(shot.score)
      ? `<b>${shot.score}</b>/10` : 'not rated';
    figure.querySelector('.gl-error')?.remove();   // the last failure, if any
    const line = panel.querySelector('.gl-count');
    if (line) line.textContent = counted();
  }

  async function rate(name, score) {
    const shot = state.items.find((item) => item.name === name);
    if (!shot) return;
    const was = shot.score ?? null;
    // clicking the score it already has is how you take a rating back
    const next = was === score ? null : score;
    shot.score = next;
    state.rated += (next === null ? -1 : 0) + (was === null ? 1 : 0);
    repaint(shot);
    try {
      await request('/api/gallery/rate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, score: next }),
      });
    } catch (error) {
      shot.score = was;
      state.rated -= (next === null ? -1 : 0) + (was === null ? 1 : 0);
      repaint(shot);
      figureFor(name)?.querySelector('.gl-rate')
        ?.insertAdjacentHTML('beforeend',
          `<span class="gl-error">${esc(error.message || 'the score didn\'t save')}</span>`);
    }
  }

  panel.addEventListener('click', (event) => {
    const step = event.target.closest('.gl-step');
    if (step) {
      const next = state.page + Number(step.dataset.step);
      if (next >= 0) load(next);
      return;
    }
    const pip = event.target.closest('.gl-pip');
    if (pip) {
      const figure = pip.closest('.gl-shot');
      if (figure) rate(figure.dataset.name, Number(pip.dataset.score));
    }
  });

  // The tab, and only the tab (js/mind.js). Re-reading the same page on every
  // open is one small JSON request and it is how a shot taken while you were
  // in the transcript is already here when you come back.
  window.addEventListener('gallery-open', () => load());

  window.addEventListener('world-ev', (event) => {
    if (panel.hidden) return;
    const detail = event.detail || {};
    if (detail.type === 'gallery' && detail.image) {
      // a score set in another open room — including this one's own echo,
      // which is why applying it is idempotent rather than a toggle
      const shot = state.items.find((item) => item.name === detail.image);
      if (shot && shot.score !== (detail.score ?? null)) {
        state.rated += (detail.score == null ? -1 : 0) +
                       (shot.score == null ? 1 : 0);
        shot.score = detail.score ?? null;
        repaint(shot);
      }
    } else if (detail.type === 'message' && detail.image_url &&
               detail.role !== 'user' && state.page === 0) {
      load(0);                      // she took one while the shelf was open
    }
  });
})();
