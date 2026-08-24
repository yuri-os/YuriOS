/** @vitest-environment jsdom */
import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => {
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
