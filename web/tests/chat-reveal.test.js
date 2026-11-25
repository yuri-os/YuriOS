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

/* A room with its transcript mounted and the bus attached. */
async function room({ reduced = false } = {}) {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    httpPath: (path) => path,
  };
  if (reduced) {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })));
  }
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

const bubble = () => document.querySelector('.msg.her.draft');
const draft = (es, text) =>
  es.onmessage({ data: JSON.stringify({ type: 'draft', text }) });
const settle = (ms = 260) => new Promise((resolve) => setTimeout(resolve, ms));

/* Long enough to take more than one frame, with a last word to look for. */
const LONG = `She is here, and she is talking. ${'word '.repeat(40)}zebra`;

it('unfolds a draft into the bubble instead of swapping the sentence in', async () => {
  const es = await room();
  draft(es, LONG);

  // the first frame is the first slice, not the whole sentence
  await settle(48);
  expect(bubble().textContent).not.toContain('zebra');

  await settle();
  expect(bubble().textContent).toContain('zebra');
});

it('never invents or reorders: the bubble always ends as the published text', async () => {
  const es = await room();
  draft(es, 'Hello, Dane.');
  await settle();
  draft(es, 'Hello, Dane. I am here.');
  await settle();

  expect(bubble().textContent.endsWith('Hello, Dane. I am here.')).toBe(true);
  expect(bubble().textContent.match(/Hello, Dane\./g)).toHaveLength(1);
});

it('a reply too long to unfold is simply shown', async () => {
  const es = await room();
  const huge = 'word '.repeat(400);          // past REVEAL_MAX_CHARS
  draft(es, huge);
  expect(bubble().textContent.endsWith(huge)).toBe(true);
});

it('prefers-reduced-motion gets it whole, at once', async () => {
  const es = await room({ reduced: true });
  draft(es, 'Hello, Dane. I am here.');
  expect(bubble().textContent).toContain('I am here.');
});

it('her committed line takes the unfolded draft down', async () => {
  const es = await room();
  draft(es, 'Hello, Dane.');
  await settle(32);
  es.onmessage({
    data: JSON.stringify({
      type: 'message', id: 'm1', role: 'assistant', text: 'Hello, Dane.',
      ts: '2026-11-25T20:00:00',
    }),
  });
  expect(document.querySelector('.msg.her.draft')).toBeNull();
});
