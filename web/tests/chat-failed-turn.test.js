/** @vitest-environment jsdom */
/**
 * A turn that fails has to say so in the room.
 *
 * The user's line lands twice: optimistically from the composer, then over SSE
 * as the committed message — and the second one adopts the first, which is what
 * takes it out of `pending`. A turn that then breaks server-side (turns.py
 * rolls it back and the route answers 502) used to find nothing left to mark,
 * so the room showed a question sitting there looking answered, forever.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, expect, it, vi } from 'vitest';

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

async function room() {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (p) => p, httpPath: (p) => p };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).startsWith('/api/history')) {
      return { ok: true, json: async () => ({ messages: [] }) };
    }
    if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  return FakeEventSource.instances[0];
}

it('marks a line whose turn failed after SSE already confirmed it', async () => {
  const source = await room();
  source.onopen();

  window.WorldChat.addPendingUser('did the install take?', 'client-1');
  // the committed user message, ahead of the failure — this is the event that
  // empties `pending`
  source.onmessage({ data: JSON.stringify({
    type: 'message', id: 'user-1', role: 'user',
    text: 'did the install take?', client_id: 'client-1',
  }) });
  expect(document.querySelector('.msg.you .receipt').textContent).toBe('received');

  window.WorldChat.failPending('client-1', 'no reply');

  const line = document.querySelector('.msg.you');
  expect(line.classList.contains('failed')).toBe(true);
  expect(line.classList.contains('pending')).toBe(false);
  expect(line.querySelector('.receipt').textContent).toBe('no reply');
});

it('still marks a line that never got its confirmation', async () => {
  const source = await room();
  source.onopen();

  window.WorldChat.addPendingUser('anyone home?', 'client-2');
  window.WorldChat.failPending('client-2', 'not received');

  const line = document.querySelector('.msg.you');
  expect(line.classList.contains('failed')).toBe(true);
  expect(line.querySelector('.receipt').textContent).toBe('not received');
});

it('leaves the room alone when the id belongs to no line', async () => {
  const source = await room();
  source.onopen();
  window.WorldChat.failPending('client-nobody', 'no reply');
  expect(document.querySelectorAll('.msg')).toHaveLength(0);
});
