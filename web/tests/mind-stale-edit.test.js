/** @vitest-environment jsdom */
// A queued self-edit built against a file that has since changed (SPEC §23.2,
// §24.3): approving it as she wrote it would undo what landed in between, so
// the panel says why and offers reject or your own version — never approve.
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
  vi.stubGlobal('CSS', { escape: (s) => String(s) });      // not in jsdom
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

const FRESH = {
  id: 'e_fresh', surface: 'soul/PERSONA.md', reason: 'I settled',
  content: '# Persona\n\nSteady.\n', draft: '# Persona\n\nSteady.\n',
  diff: ['-Warm.', '+Steady.'], stale: null,
};
const STALE = {
  id: 'e_stale', surface: 'soul/PERSONA.md', reason: 'second thoughts',
  content: '# Persona\n\nDark hair, long.\n', draft: '# Persona\n\nShort hair.\n',
  diff: ['-Dark hair.', '+Dark hair, long.'],
  stale: 'the file has changed since she asked, and the passage she changed is no longer in it',
};

function stub(edits, decide) {
  const state = {
    state: 'IDLE', cadence_s: 60, interrupts_today: 0, dream_backlog: [],
    budget: { spent_tokens: 0, daily_tokens: 1000 }, pending_edits: edits,
    goals: [], goal_filing: { enabled: true, open: 0, max: 3 }, shelf: [],
  };
  const fetch = vi.fn(async (url, init) => {
    if (url === '/api/mind') return { ok: true, json: async () => state };
    if (url.startsWith('/api/mind/journal')) return { ok: true, json: async () => ({ days: [] }) };
    if (url === '/api/mind/reading') return { ok: true, json: async () => ({ reading: null, runs: [], held: [] }) };
    if (url === '/api/timers') return { ok: true, json: async () => ({ timers: [] }) };
    if (url.startsWith('/api/mind/edits/')) return decide(url, JSON.parse(init.body));
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

async function open() {
  await import('../js/mind.js');
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(document.querySelectorAll('.il-edit')).toHaveLength(2));
}

const block = (id) => document.querySelector(`.il-edit[data-id="${id}"]`);

it('says why a stale edit cannot be approved, and shows what she asked to change', async () => {
  stub([FRESH, STALE], () => ({ ok: true, status: 200, json: async () => ({}) }));
  await open();

  expect(block('e_fresh').querySelector('.il-stale')).toBeNull();
  expect(block('e_fresh').querySelector('.il-ok').disabled).toBe(false);

  const stale = block('e_stale');
  expect(stale.querySelector('.il-stale').textContent).toContain('no longer in it');
  expect(stale.querySelector('.il-label').textContent).toBe('what she asked to change');
  expect(stale.querySelector('.il-ok').disabled).toBe(true);
  expect(stale.querySelector('.il-no').disabled).toBe(false);
  expect(stale.querySelector('.il-whole summary').textContent)
    .toBe('her whole version, as she wrote it');
});

it('starts your version of a stale patch from the file as it is', async () => {
  const fetch = stub([FRESH, STALE],
    () => ({ ok: true, status: 200, json: async () => ({ queued: true }) }));
  await open();

  block('e_stale').querySelector('[data-revise]').click();
  await vi.waitFor(() => expect(block('e_stale').querySelector('.il-draft')).not.toBeNull());
  expect(block('e_stale').querySelector('.il-draft').value).toBe(STALE.draft);
  const approve = block('e_stale').querySelector('.il-ok');
  expect(approve.disabled).toBe(false);
  expect(approve.textContent).toBe('approve my version');

  approve.click();
  await vi.waitFor(() => expect(fetch.mock.calls.some(([u]) => u === '/api/mind/edits/e_stale'))
    .toBe(true));
  const [, init] = fetch.mock.calls.find(([u]) => u === '/api/mind/edits/e_stale');
  expect(JSON.parse(init.body)).toEqual({ approve: true, content: STALE.draft });
});

it('keeps an edit and says why when the server answers 409', async () => {
  // the file changed between the snapshot and the click
  const edits = [{ ...STALE, id: 'e_fresh', stale: null }, { ...STALE, stale: null }];
  stub(edits,
    () => ({ ok: false, status: 409, json: async () => ({ detail: 'the file has changed since she asked' }) }));
  await open();

  block('e_stale').querySelector('.il-ok').click();
  await vi.waitFor(() => expect(block('e_stale').querySelector('.il-err')).not.toBeNull());
  expect(block('e_stale').querySelector('.il-err').textContent).toContain('file has changed');
  expect(block('e_stale').querySelector('.il-ok').disabled).toBe(false);

  // the next snapshot knows it is stale: one notice, not the notice and the error
  edits[1] = STALE;
  window.dispatchEvent(new CustomEvent('world-ev', { detail: { type: 'mind' } }));
  document.getElementById('tab-mind').click();
  await vi.waitFor(() => expect(block('e_stale').querySelector('.il-stale')).not.toBeNull());
  expect(block('e_stale').querySelector('.il-err')).toBeNull();
});
