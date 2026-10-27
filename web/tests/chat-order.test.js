/** @vitest-environment jsdom */
/* The column is in the order things were said, not the order they reached the
 * page. Lines come by three roads — the opening backfill, the live stream, a
 * reconnect's catch-up — and the first one can lose the race to the others:
 * your line is confirmed the moment you send it, and a slow history fetch
 * outlives the failsafe. Appending put yesterday under what you just typed. */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.close = vi.fn();
    FakeEventSource.instances.push(this);
  }

  deliver(message) {
    this.onmessage({ data: JSON.stringify({ type: 'message', ...message }) });
  }
}

const OLD_YOU = { id: 'm1', role: 'user', text: 'yesterday, me', ts: '2026-10-27T21:00:00' };
const OLD_HER = { id: 'm2', role: 'assistant', text: 'yesterday, her', ts: '2026-10-27T21:00:05' };

let resolveHistory;

beforeEach(() => {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path, httpPath: (path) => path };
  const history = new Promise((done) => { resolveHistory = done; });
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).startsWith('/api/history')) return history;
    if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    return { ok: true, json: async () => ({}) };
  }));
  vi.stubGlobal('EventSource', FakeEventSource);
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
  delete window.WorldChat;
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

const column = () => [...document.querySelectorAll('#messages .msg')]
  .map((el) => el.textContent);

const said = (...texts) => texts.map((t) => expect.stringContaining(t));

it('keeps the history above a line you sent before it loaded', async () => {
  await window.WorldChat.connect();
  const stream = FakeEventSource.instances[0];
  stream.onopen();

  window.WorldChat.addPendingUser('hello again', 'c1');
  // confirmed at once — before the backfill, which is still in flight
  stream.deliver({ id: 'm3', role: 'user', text: 'hello again', client_id: 'c1',
    ts: '2026-10-28T08:00:00' });
  stream.deliver({ id: 'm4', role: 'assistant', text: 'morning',
    ts: '2026-10-28T08:00:03' });

  resolveHistory({ ok: true, json: async () => ({ messages: [OLD_YOU, OLD_HER] }) });

  await vi.waitFor(() => expect(column()).toHaveLength(4));
  expect(column()).toEqual(said('yesterday, me', 'yesterday, her', 'hello again', 'morning'));
});

it('lays a history that outlived the failsafe above the live lines', async () => {
  vi.useFakeTimers();
  await window.WorldChat.connect();
  const stream = FakeEventSource.instances[0];
  stream.onopen();

  await vi.advanceTimersByTimeAsync(5000);         // gave up waiting
  stream.deliver({ id: 'm4', role: 'assistant', text: 'morning',
    ts: '2026-10-28T08:00:03' });
  expect(column()).toEqual(said('morning'));

  resolveHistory({ ok: true, json: async () => ({ messages: [OLD_YOU, OLD_HER],
    has_more: true }) });
  await vi.waitFor(() => expect(column()).toHaveLength(3));
  expect(column()).toEqual(said('yesterday, me', 'yesterday, her', 'morning'));
  // …and the walk back is armed from it, at the top
  const button = document.querySelector('.load-earlier');
  expect(button).not.toBeNull();
  expect(button.nextElementSibling.textContent).toContain('yesterday, me');
});

it('places a late line among the ones already drawn, above her draft', async () => {
  await window.WorldChat.connect();
  resolveHistory({ ok: true, json: async () => ({ messages: [OLD_YOU] }) });
  await vi.waitFor(() => expect(column()).toHaveLength(1));
  const stream = FakeEventSource.instances[0];

  stream.deliver({ id: 'm5', role: 'assistant', text: 'later', ts: '2026-10-28T09:00:00' });
  // a catch-up line from between them, and a hand she used just before "later"
  stream.deliver({ id: 'm2', role: 'assistant', text: 'yesterday, her', ts: OLD_HER.ts });
  stream.onmessage({ data: JSON.stringify({ type: 'draft', text: 'and then' }) });
  stream.deliver({ id: 't1', role: 'tool', tool: 'read_page', text: 'read a page',
    ts: '2026-10-28T08:59:59' });

  expect(column()).toEqual(said('yesterday, me', 'yesterday, her', 'read a page',
    'later', 'and then'));
});
