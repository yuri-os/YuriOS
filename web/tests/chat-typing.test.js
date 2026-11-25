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

/* A room with its transcript mounted and the bus attached — the same harness
 * the stream test uses, minus the history drama. */
async function room() {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    httpPath: (path) => path,
  };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).startsWith('/api/history')) {
      return { ok: true, json: async () => ({ messages: [] }) };
    }
    if (url === '/api/inbox') {
      return { ok: true, json: async () => ({ entries: [] }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);

  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  const es = FakeEventSource.instances[0];
  es.onopen();
  return es;
}

const withDots = () => document.querySelector('.msg.her.typing');

it('draws her typing dots while the host says she is composing', async () => {
  const es = await room();
  es.onmessage({ data: JSON.stringify({ type: 'typing', on: true }) });

  const dots = withDots();
  expect(dots).not.toBeNull();
  // three dots, and a label a screen reader can hear — the dots are decoration
  expect(dots.querySelectorAll('.typing-dots span')).toHaveLength(3);
  expect(dots.getAttribute('role')).toBe('status');
  expect(dots.getAttribute('aria-label')).toBe('She is typing');

  es.onmessage({ data: JSON.stringify({ type: 'typing', on: false }) });
  expect(withDots()).toBeNull();
});

it('names her with live text, not with the markup that carries the name', async () => {
  const es = await room();
  es.onmessage({ data: JSON.stringify({ type: 'hello', character: 'Yuri' }) });
  es.onmessage({ data: JSON.stringify({ type: 'typing', on: true }) });

  const who = withDots().querySelector('.who');
  // a `data-char-name` span, so the hello that renames the column renames this
  // too — escaped into innerHTML it would print `<span data-char-name>Yuri…`
  expect(who.querySelector('[data-char-name]')).not.toBeNull();
  expect(who.textContent).toBe('Yuri');
  expect(withDots().getAttribute('aria-label')).toBe('Yuri is typing');
  es.onmessage({ data: JSON.stringify({ type: 'hello', character: 'Mira' }) });
  expect(withDots().getAttribute('aria-label')).toBe('Mira is typing');
  expect(withDots().textContent).not.toContain('<');
});

it('her first draft takes the dots down — the wait is over when there is something to read', async () => {
  const es = await room();
  es.onmessage({ data: JSON.stringify({ type: 'typing', on: true }) });
  es.onmessage({ data: JSON.stringify({ type: 'draft', text: 'I am here' }) });

  expect(withDots()).toBeNull();
  await vi.waitFor(() => expect(document.querySelector('.msg.her.draft').textContent)
    .toContain('I am here'));
});

it('her committed line takes the dots down too', async () => {
  const es = await room();
  es.onmessage({ data: JSON.stringify({ type: 'typing', on: true }) });
  es.onmessage({
    data: JSON.stringify({
      type: 'message', id: 'm1', role: 'assistant', text: 'Hello.',
      ts: '2026-11-25T20:00:00',
    }),
  });
  expect(withDots()).toBeNull();
});

it('the composer raises them locally, so a send shows the wait before any event', async () => {
  const es = await room();
  window.WorldChat.typing(true);
  expect(withDots()).not.toBeNull();
  // one indicator, however many times the wait is announced
  es.onmessage({ data: JSON.stringify({ type: 'typing', on: true }) });
  expect(document.querySelectorAll('.msg.her.typing')).toHaveLength(1);
  window.WorldChat.typing(false);
  expect(withDots()).toBeNull();
});
