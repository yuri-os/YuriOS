/** @vitest-environment jsdom */
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

it('replaces a suspended stream and recovers the final reply from history', async () => {
  let visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    httpPath: (path) => path,
  };
  const reply = {
    id: 'assistant-1', role: 'assistant', text: 'The reply made it home.',
  };
  const newer = {
    id: 'assistant-2', role: 'assistant', text: 'Then the live line arrived.',
  };
  const inbox = {
    id: 'inbox-1', text: 'This survived the restart.',
    ts: '2026-08-23T08:00:00',
  };
  let resolveInitialHistory;
  const initialHistory = new Promise((resolve) => { resolveInitialHistory = resolve; });
  let historyRequests = 0;
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (url === '/api/history') {
      historyRequests += 1;
      if (historyRequests === 1) return initialHistory;
      return { ok: true, json: async () => ({ messages: [reply, newer] }) };
    }
    if (url === '/api/inbox') {
      return { ok: true, json: async () => ({ entries: [inbox] }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);

  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  await vi.waitFor(() => expect(historyRequests).toBe(1));

  const original = FakeEventSource.instances[0];
  original.onopen();
  original.onmessage({ data: JSON.stringify({ type: 'draft', text: 'The reply' }) });
  expect(document.querySelector('.draft')).not.toBeNull();

  visibility = 'hidden';
  document.dispatchEvent(new Event('visibilitychange'));
  expect(FakeEventSource.instances).toHaveLength(1);

  visibility = 'visible';
  document.dispatchEvent(new Event('visibilitychange'));
  expect(original.close).toHaveBeenCalledOnce();
  expect(FakeEventSource.instances).toHaveLength(2);
  expect(document.querySelector('.draft')).toBeNull();

  FakeEventSource.instances[1].onopen();
  FakeEventSource.instances[1].onmessage({
    data: JSON.stringify({ type: 'message', ...newer }),
  });
  // The initial backfill is still outstanding. Recovery must wait for its
  // history + durable inbox merge instead of suppressing the inbox-only row.
  expect(historyRequests).toBe(1);
  resolveInitialHistory({ ok: true, json: async () => ({ messages: [] }) });

  await vi.waitFor(() => expect(document.querySelectorAll('.msg.her')).toHaveLength(3));
  expect([...document.querySelectorAll('.msg.her')].map((el) => el.textContent))
    .toEqual([
      expect.stringContaining('This survived the restart.'),
      expect.stringContaining('The reply made it home.'),
      expect.stringContaining('Then the live line arrived.'),
    ]);
});
