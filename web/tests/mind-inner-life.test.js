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
