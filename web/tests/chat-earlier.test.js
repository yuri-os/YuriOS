/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* The walk back through the column (SPEC §2.6).
 *
 * The bug behind it: the transcript was an in-memory ring, so a restart opened
 * her room onto a blank column. The host keeps it on disk now (world/chatlog.py)
 * and `/api/history` pages back through it — this file pins the client half of
 * that: a page draws the end of the conversation, one button offers the six
 * lines before it, the batch lands *above* what is already drawn and in order,
 * and the button retires itself when the archive runs out.
 *
 * chat.js is a classic script shared by all three rooms, not a module, so it is
 * evaluated rather than imported.
 */
const SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

class FakeEventSource {
  constructor(url) { this.url = url; this.close = vi.fn(); }
}

/** `line 0` … `line n-1`, oldest first, the shape post_message commits. */
const conversation = (n) => Array.from({ length: n }, (_, i) => ({
  id: `m${i}`, role: i % 2 === 0 ? 'user' : 'assistant', text: `line ${i}`,
  ts: `2026-08-2${1 + Math.floor(i / 10)}T09:0${i % 10}:00`,
}));

/** The host: the last six of `all`, then six at a time behind `before`. */
function host(all, { pageSize = 6 } = {}) {
  return vi.fn(async (url) => {
    if (String(url).startsWith('/api/history')) {
      const query = new URL(url, 'http://x').searchParams;
      const before = query.get('before');
      const cut = before ? all.findIndex((m) => m.id === before) : all.length;
      if (cut < 0) return { ok: true, json: async () => ({ messages: [], has_more: false }) };
      const window = all.slice(Math.max(0, cut - pageSize), cut);
      return {
        ok: true,
        json: async () => ({ messages: window, has_more: cut - window.length > 0 }),
      };
    }
    if (String(url) === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    if (String(url) === '/api/inbox/read') return { ok: true, json: async () => ({}) };
    throw new Error(`unexpected request: ${url}`);
  });
}

async function open(all, options) {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (p) => p, httpPath: (p) => p };
  const fetched = host(all, options);
  vi.stubGlobal('fetch', fetched);
  vi.stubGlobal('EventSource', FakeEventSource);
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  await vi.waitFor(() => expect(document.querySelectorAll('.msg').length).toBeGreaterThan(0));
  return fetched;
}

const drawn = () => [...document.querySelectorAll('.msg')].map((el) => el.textContent);
const button = () => document.querySelector('.load-earlier');

beforeEach(() => { vi.useRealTimers(); });
afterEach(() => {
  vi.unstubAllGlobals();
  delete window.WorldChat;
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

describe('the button at the top of the column', () => {
  it('offers the walk back when the host says there is more', async () => {
    await open(conversation(20));
    expect(drawn()).toHaveLength(6);
    expect(button()).not.toBeNull();
    expect(button().textContent).toBe('load 6 earlier messages');
    // it sits above the oldest line drawn, not below the newest
    expect(document.getElementById('messages').firstChild).toBe(button());
  });

  it('stays away when the whole conversation is already on screen', async () => {
    await open(conversation(4));
    expect(drawn()).toHaveLength(4);
    expect(button()).toBeNull();
  });

  it('prepends the previous six, in order, above what was there', async () => {
    await open(conversation(20));
    button().click();
    await vi.waitFor(() => expect(drawn()).toHaveLength(12));
    const texts = drawn();
    expect(texts[0]).toContain('line 8');       // oldest of the new batch, at the top
    expect(texts[5]).toContain('line 13');
    expect(texts[6]).toContain('line 14');      // …and the old top, still below it
    expect(document.getElementById('messages').firstChild).toBe(button());
  });

  it('asks only for the six before the top, never for the whole archive', async () => {
    const fetched = await open(conversation(20));
    button().click();
    await vi.waitFor(() => expect(drawn()).toHaveLength(12));
    const asked = fetched.mock.calls.map(([url]) => String(url))
      .filter((url) => url.includes('before='));
    expect(asked).toEqual(['/api/history?limit=6&before=m14']);
  });

  it('walks all the way to the floor and then retires itself', async () => {
    await open(conversation(20));
    for (let press = 0; press < 3; press += 1) {
      const drawnBefore = drawn().length;
      button().click();
      // eslint-disable-next-line no-await-in-loop
      await vi.waitFor(() => expect(drawn().length).toBeGreaterThan(drawnBefore));
    }
    expect(drawn()).toHaveLength(20);
    expect(drawn()[0]).toContain('line 0');
    expect(button()).toBeNull();            // nothing older left to offer
  });

  it('draws a restored report as a card, the same as a live one', async () => {
    // A restart is exactly when the morning brief is the thing you came for.
    const brief = {
      id: 'r1', role: 'assistant', proactive: true, ts: '2026-08-21T04:10:00',
      text: 'I read the tape while you were out.',
      report_path: 'reports/market-brief/2026-08-20.md',
      report_title: 'Overnight market brief',
    };
    await open([...conversation(10), brief]);
    expect(document.querySelector('.msg-report .report-title').textContent)
      .toBe('Overnight market brief');
  });

  it('keeps a line it has already drawn from arriving twice', async () => {
    // The batch overlaps the backfill after a reconnect; the id resolves it.
    const all = conversation(20);
    await open(all);
    button().click();
    await vi.waitFor(() => expect(drawn()).toHaveLength(12));
    window.WorldChat.receiveMessage(all[8]);
    expect(drawn()).toHaveLength(12);
  });

  it('survives a host that will not answer, and stays pressable', async () => {
    await open(conversation(20));
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503 })));
    button().click();
    await vi.waitFor(() => expect(button().textContent).toContain("couldn't reach"));
    expect(button().disabled).toBe(false);
    expect(drawn()).toHaveLength(6);        // nothing half-rendered
  });
});
