/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, expect, it, vi } from 'vitest';

/* A hand she used is a chip in the column (SPEC §7.3): one `role: "tool"` row
 * per call, drawn above the reply it was made for — and, mid-reply, above the
 * words still arriving rather than in place of them. */

const SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.close = vi.fn();
    FakeEventSource.instances.push(this);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
  delete window.WorldChat;
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

async function room(history) {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path, httpPath: (path) => path };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).startsWith('/api/history'))
      return { ok: true, json: async () => ({ messages: history }) };
    if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  const stream = FakeEventSource.instances[0];
  stream.onopen();
  return (event) => stream.onmessage({ data: JSON.stringify(event) });
}

it('draws a call as a chip, and one she made alone as a quieter one', async () => {
  await room([
    { id: 't1', role: 'tool', tool: 'write_note', text: 'write_note · notes/a.md',
      verdict: 'ok', background: true, ts: '2026-09-24T03:00:00' },
    { id: 't2', role: 'tool', tool: 'web_search', text: 'web_search · tiles',
      verdict: 'denied', why: 'rate limit', ts: '2026-09-24T09:00:00' },
    { id: 'a1', role: 'assistant', text: 'Found it.', ts: '2026-09-24T09:00:01' },
  ]);
  await vi.waitFor(() => expect(document.querySelectorAll('.msg')).toHaveLength(3));

  const [alone, refused, reply] = document.querySelectorAll('.msg');
  expect(alone.className).toBe('msg tool background');
  expect(alone.textContent).toContain('write_note · notes/a.md');
  expect(refused.classList.contains('refused')).toBe(true);
  expect(refused.textContent).toContain('denied (rate limit)');
  expect(reply.classList.contains('her')).toBe(true);
  // a chip is not her speaking: no speaker line, no bubble
  expect(alone.querySelector('.who')).toBeNull();
});

it('puts a chip that lands mid-reply above the words still arriving', async () => {
  const send = await room([]);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 0));

  send({ type: 'draft', text: 'Let me look' });
  send({ type: 'message', id: 't1', role: 'tool', tool: 'read_note',
         text: 'read_note · notes/a.md', verdict: 'ok' });

  const rows = [...document.querySelectorAll('#messages > *')];
  expect(rows.map((el) => el.className)).toEqual(['msg tool', 'msg her draft']);
  expect(rows[1].textContent).toContain('Let me look');
});
