/** @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

beforeEach(() => {
  vi.spyOn(window, 'addEventListener');
  vi.spyOn(Date, 'now').mockReturnValue(1_000_000_000);
  document.body.innerHTML = `
    <button id="tab-chat"></button><button id="tab-mind"></button>
    <button id="tab-files"></button><button id="tab-gallery"></button>
    <div id="messages"></div><div id="innerlife" hidden></div>
    <div id="files" hidden></div><div id="gallery" hidden></div>
  `;
  window.YuriOSRuntime = { apiPath: (path) => path };
});

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

it('organizes live state, plans, and history into persistent subviews', async () => {
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 40, daily_tokens: 1000 }, pending_edits: [],
    goals: [{
      id: 'goal-1', text: 'finish the field notes', kind: 'task', state: 'active',
      provenance: 'strategy:test',
    }],
    goal_filing: { enabled: true, open: 1, max: 3 }, shelf: ['field-notes.md'],
  };
  let timerState = { timers: [
    { id: 'later', label: 'check the oven', due: 1_000_000 + 3600 },
    { id: 'sooner', label: 'tea', due: 1_000_000 + 120 },
  ] };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (url === '/api/mind') return { ok: true, json: async () => state };
    if (url === '/api/mind/journal?days=3') return { ok: true, json: async () => ({
      days: [{ day: '2026-09-22', entries: [
        { time: '09:00', text: 'made a note', hers: true },
      ] }],
    }) };
    if (url === '/api/mind/reading') return {
      ok: true, json: async () => ({ reading: null, runs: [], held: [] }),
    };
    if (url === '/api/timers') return { ok: true, json: async () => timerState };
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelectorAll('.il-timers li')).toHaveLength(2));

  const tabs = [...document.querySelectorAll('.il-nav button')];
  expect(tabs.map(tab => tab.querySelector('span').textContent)).toEqual([
    'now', 'plans', 'history',
  ]);
  expect(document.querySelector('[data-il-page="now"]').hidden).toBe(false);
  expect(document.querySelector('[data-il-page="plans"]').hidden).toBe(true);
  expect([...document.querySelectorAll('.il-timer-label')].map(el => el.textContent))
    .toEqual(['tea', 'check the oven']);
  expect(document.querySelector('.il-timers strong').textContent).toBe('in 2 minutes');

  document.querySelector('[data-il-view="plans"]').click();
  expect(document.querySelector('[data-il-page="now"]').hidden).toBe(true);
  expect(document.querySelector('[data-il-page="plans"]').hidden).toBe(false);
  expect(document.querySelector('[data-il-page="plans"]').textContent)
    .toContain('finish the field notes');

  document.querySelector('[data-il-view="history"]').click();
  expect(document.querySelector('[data-il-page="history"]').hidden).toBe(false);
  expect(document.querySelector('[data-il-page="history"]').textContent).toContain('made a note');

  timerState = { timers: [] };
  window.dispatchEvent(new CustomEvent('world-ev', { detail: { type: 'timers', timers: [] } }));
  await vi.waitFor(() => expect(document.querySelector('.il-timers')).toBeNull());
  expect(document.querySelector('[data-il-view="history"]').classList).toContain('on');
  expect(document.querySelector('[data-il-page="history"]').hidden).toBe(false);
});

it('lists documents handed to her, newest first, read and not yet', async () => {
  const state = {
    state: 'DORMANT', cadence_s: 900, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: [], goals: [],
    goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [], inbox_wake: false,
    handed: [
      { path: 'inbox/q2.md', name: 'Q2 numbers.pdf', at: '2026-09-20T09:00:00',
        state: 'read', read_at: '2026-09-20T09:01:00', goal: 'g-1',
        outcome: 'decided to “fold inbox/q2.md into my quarterly report”' },
      { path: 'inbox/old.md', name: 'old.md', at: '2026-09-21T09:00:00', state: 'gone' },
      { path: 'inbox/q3.md', name: 'Q3 numbers.pdf', at: '2026-09-22T09:00:00',
        state: 'waiting' },
    ],
  };
  const fetched = [];
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    fetched.push(url);
    if (url === '/api/mind') return { ok: true, json: async () => state };
    if (url === '/api/mind/journal?days=3') return { ok: true, json: async () => ({ days: [] }) };
    if (url === '/api/mind/reading') {
      return { ok: true, json: async () => ({ reading: null, runs: [], held: [] }) };
    }
    if (url === '/api/timers') return { ok: true, json: async () => ({ timers: [] }) };
    if (url === '/api/mind/workspace/file?path=inbox%2Fq3.md') {
      return { ok: true, json: async () => ({ text: '# Q3\n\nRevenue rose.' }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelector('.il-handed')).not.toBeNull());

  // an unread document is something on her plate, and counts on the tab
  expect(document.querySelector('[data-il-view="plans"]').textContent).toContain('1');
  const rows = [...document.querySelectorAll('.il-handed li')];
  expect(rows.map(r => r.className)).toEqual(['h-waiting', 'h-gone', 'h-read']);
  expect(rows[0].querySelector('.il-unread')).not.toBeNull();
  expect(rows[0].textContent).toContain("not read yet — she'll read it when you're back");
  expect(rows[1].textContent).toContain('gone from her desk before she read it');
  expect(rows[1].querySelector('.il-look')).toBeNull();
  expect(rows[2].querySelector('.il-unread')).toBeNull();
  expect(rows[2].textContent).toContain('read ');
  expect(rows[2].querySelector('.il-outcome').textContent)
    .toBe('decided to “fold inbox/q2.md into my quarterly report”');
  expect(document.querySelector('.il-handed').previousElementSibling.textContent)
    .toContain('1 not read yet');

  rows[0].querySelector('.il-look').click();
  await vi.waitFor(() => expect(document.querySelector('.il-handed .il-desk')?.textContent)
    .toBe('# Q3\n\nRevenue rose.'));
  expect(fetched).toContain('/api/mind/workspace/file?path=inbox%2Fq3.md');
});
