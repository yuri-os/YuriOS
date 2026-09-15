/** @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

beforeEach(() => vi.spyOn(window, 'addEventListener'));

afterEach(() => {
  for (const [type, listener, options] of window.addEventListener.mock.calls) {
    window.removeEventListener(type, listener, options);
  }
  vi.restoreAllMocks();
  vi.resetModules();
  vi.unstubAllGlobals();
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

it('shows Let Go immediately and keeps it across a pending rerender', async () => {
  document.body.innerHTML = `
    <button id="tab-chat"></button><button id="tab-mind"></button>
    <button id="tab-files"></button><button id="tab-gallery"></button>
    <div id="messages"></div><div id="innerlife" hidden></div>
    <div id="files" hidden></div><div id="gallery" hidden></div>
  `;
  window.YuriOSRuntime = { apiPath: (path) => path };
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: [],
    goals: [
      {
        id: 'goal-1', text: 'an accidental promise', kind: 'task', state: 'active',
        provenance: 'promise:her-own-words',
      },
      {
        id: 'maintenance-1', text: 'catch up on consolidation', kind: 'maintenance',
        state: 'pending', provenance: 'maintenance:dream',
      },
    ],
    goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [],
  };
  let finishAbandon;
  const abandon = new Promise((resolve) => { finishAbandon = resolve; });
  vi.stubGlobal('fetch', vi.fn((url, options = {}) => {
    if (url === '/api/mind' && !options.method) {
      return Promise.resolve({ ok: true, json: async () => state });
    }
    if (url === '/api/mind/journal?days=3') {
      return Promise.resolve({ ok: true, json: async () => ({ days: [] }) });
    }
    if (url === '/api/mind/reading') {
      return Promise.resolve({
        ok: true, json: async () => ({ reading: null, runs: [], held: [] }),
      });
    }
    if (url === '/api/mind/goals/goal-1/abandon') return abandon;
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelector('.il-drop')).not.toBeNull());
  const sections = [...document.querySelectorAll('.il-sec')];
  expect(sections.find((section) => section.querySelector('h3').textContent === 'on her mind')
    .textContent).not.toContain('catch up on consolidation');
  expect(sections.find((section) => section.querySelector('h3').textContent === 'system upkeep')
    .textContent).toContain('catch up on consolidation');

  document.querySelector('.il-drop').click();
  expect(document.querySelector('.il-drop').textContent).toBe('letting go…');
  expect(document.querySelector('.il-goals li').classList).toContain('g-abandoned');

  // A refresh can beat the mind tick. The active API snapshot must not restore
  // the button and invite a duplicate decision while the first one is pending.
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(
    document.querySelector('.il-drop[data-goal="goal-1"]')).toBeNull());
  expect(document.querySelector('.il-sec .il-goals').textContent).toContain('letting go');

  finishAbandon({ ok: true, json: async () => ({ queued: true }) });
});

it('lists open intentions first and only the last five abandoned intentions', async () => {
  document.body.innerHTML = `
    <button id="tab-chat"></button><button id="tab-mind"></button>
    <button id="tab-files"></button><button id="tab-gallery"></button>
    <div id="messages"></div><div id="innerlife" hidden></div>
    <div id="files" hidden></div><div id="gallery" hidden></div>
  `;
  window.YuriOSRuntime = { apiPath: (path) => path };
  const goal = (id, state) => ({
    id, text: id, kind: 'task', state, provenance: 'strategy:test',
  });
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: [],
    goals: [
      goal('abandoned-1', 'abandoned'),
      goal('active-first', 'active'),
      goal('abandoned-2', 'abandoned'),
      goal('abandoned-3', 'abandoned'),
      goal('pending-second', 'pending'),
      goal('abandoned-4', 'abandoned'),
      goal('abandoned-5', 'abandoned'),
      goal('abandoned-6', 'abandoned'),
      goal('abandoned-7', 'abandoned'),
    ],
    goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [],
  };
  vi.stubGlobal('fetch', vi.fn((url) => {
    if (url === '/api/mind') {
      return Promise.resolve({ ok: true, json: async () => state });
    }
    if (url === '/api/mind/journal?days=3') {
      return Promise.resolve({ ok: true, json: async () => ({ days: [] }) });
    }
    if (url === '/api/mind/reading') {
      return Promise.resolve({
        ok: true, json: async () => ({ reading: null, runs: [], held: [] }),
      });
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelectorAll('.il-goals li')).toHaveLength(7));

  const rows = [...document.querySelectorAll('.il-goals li')];
  expect(rows.slice(0, 2).map(row => row.textContent)).toEqual([
    expect.stringContaining('active-first'),
    expect.stringContaining('pending-second'),
  ]);
  expect(rows.slice(2).map(row => row.textContent)).toEqual([
    expect.stringContaining('abandoned-3'),
    expect.stringContaining('abandoned-4'),
    expect.stringContaining('abandoned-5'),
    expect.stringContaining('abandoned-6'),
    expect.stringContaining('abandoned-7'),
  ]);
  expect(document.getElementById('innerlife').textContent).not.toContain('abandoned-1');
  expect(document.getElementById('innerlife').textContent).not.toContain('abandoned-2');
});

it('opens the desk file next to a goal and folds it without fetching again', async () => {
  document.body.innerHTML = `
    <button id="tab-chat"></button><button id="tab-mind"></button>
    <button id="tab-files"></button><button id="tab-gallery"></button>
    <div id="messages"></div><div id="innerlife" hidden></div>
    <div id="files" hidden></div><div id="gallery" hidden></div>
  `;
  window.YuriOSRuntime = { apiPath: (path) => path };
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: [],
    goals: [{
      id: 'g-7517a5363d42', text: 'show you what I mean', kind: 'task',
      state: 'waiting', provenance: 'promise:her-own-words',
      desk: 'goals/g-7517a5363d42.md',
    }],
    goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [],
  };
  const fetched = vi.fn(async () => ({
    ok: true, json: async () => ({ text: '## 2026-09-13\n\nThe almost.' }),
  }));
  vi.stubGlobal('fetch', vi.fn((url, options = {}) => {
    if (url === '/api/mind' && !options.method) {
      return Promise.resolve({ ok: true, json: async () => state });
    }
    if (url === '/api/mind/journal?days=3') {
      return Promise.resolve({ ok: true, json: async () => ({ days: [] }) });
    }
    if (url === '/api/mind/reading') {
      return Promise.resolve({
        ok: true, json: async () => ({ reading: null, runs: [], held: [] }),
      });
    }
    if (String(url).startsWith('/api/mind/workspace/file?')) return fetched();
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelector('.il-look')).not.toBeNull());
  const look = document.querySelector('.il-look');
  expect(look.textContent).toBe('view file');
  expect(look.dataset.path).toBe('goals/g-7517a5363d42.md');
  expect(document.querySelector('.il-desk')).toBeNull();

  look.click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk')?.textContent)
    .toContain('The almost.'));
  expect(fetched).toHaveBeenCalledTimes(1);
  expect(document.querySelector('.il-look').textContent).toBe('fold file away');
  expect(vi.mocked(fetch).mock.calls.some(([url]) =>
    String(url).includes(encodeURIComponent('goals/g-7517a5363d42.md')))).toBe(true);

  document.querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk')).toBeNull());
  expect(document.querySelector('.il-look').textContent).toBe('view file');

  document.querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk')?.textContent)
    .toContain('The almost.'));
  expect(fetched).toHaveBeenCalledTimes(1);
});

it('refreshes an open file on writes and ignores a superseded response', async () => {
  document.body.innerHTML = `
    <button id="tab-chat"></button><button id="tab-mind"></button>
    <button id="tab-files"></button><div id="messages"></div>
    <div id="innerlife" hidden></div><div id="files" hidden></div>`;
  window.YuriOSRuntime = { apiPath: (path) => path };
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: [],
    goals: [{id: 'g-live', text: 'live goal', kind: 'task', state: 'active', provenance: 'user'}],
    goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [],
  };
  let finishOld;
  const file = vi.fn()
    .mockResolvedValueOnce({ok: true, json: async () => ({text: 'original'})})
    .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
    .mockResolvedValue({ok: true, json: async () => ({text: 'newest'})});
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url === '/api/mind') return {ok: true, json: async () => state};
    if (url === '/api/mind/journal?days=3') return {ok: true, json: async () => ({days: []})};
    if (url === '/api/mind/reading') return {ok: true, json: async () => ({runs: [], held: []})};
    if (String(url).startsWith('/api/mind/workspace/file?')) return file();
    throw new Error(`unexpected request: ${url}`);
  }));
  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelector('.il-look')).not.toBeNull());
  document.querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk').textContent).toBe('original'));
  const changed = () => window.dispatchEvent(new CustomEvent('world-ev', {
    detail: {type: 'workspace', action: 'write', path: 'goals/g-live.md'},
  }));
  changed();
  await vi.waitFor(() => expect(file).toHaveBeenCalledTimes(2));
  changed();
  await vi.waitFor(() => expect(document.querySelector('.il-desk').textContent).toBe('newest'));
  finishOld({ok: true, json: async () => ({text: 'stale'})});
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(document.querySelector('.il-desk').textContent).toBe('newest');
});

it('retries a missing goal file when reopened', async () => {
  document.body.innerHTML = `
    <button id="tab-chat"></button><button id="tab-mind"></button>
    <button id="tab-files"></button><button id="tab-gallery"></button>
    <div id="messages"></div><div id="innerlife" hidden></div>
    <div id="files" hidden></div><div id="gallery" hidden></div>
  `;
  window.YuriOSRuntime = { apiPath: (path) => path };
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: [],
    goals: [{
      id: 'goal-1', text: 'an accidental promise', kind: 'task',
      state: 'active', provenance: 'promise:her-own-words',
    }],
    goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [],
  };
  let exists = false;
  vi.stubGlobal('fetch', vi.fn((url, options = {}) => {
    if (url === '/api/mind' && !options.method) {
      return Promise.resolve({ ok: true, json: async () => state });
    }
    if (url === '/api/mind/journal?days=3') {
      return Promise.resolve({ ok: true, json: async () => ({ days: [] }) });
    }
    if (url === '/api/mind/reading') {
      return Promise.resolve({
        ok: true, json: async () => ({ reading: null, runs: [], held: [] }),
      });
    }
    if (String(url).startsWith('/api/mind/workspace/file?')) {
      return Promise.resolve({ ok: exists, status: exists ? 200 : 404,
        json: async () => ({text: 'written now'}), text: async () => 'missing' });
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelector('.il-look')).not.toBeNull());
  document.querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk')?.textContent)
    .toBe("she hasn't written this one up yet."));
  expect(document.querySelector('.il-drop')).not.toBeNull();
  exists = true;
  document.querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk')).toBeNull());
  document.querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-desk')?.textContent).toBe('written now'));
});
