/** @vitest-environment jsdom */
/* Asking her again is a turn (SPEC §39.5): the real chat.js and voice.js
 * together, as a room runs them. The ↻ goes through the composer — locked,
 * Send turned to Stop — the new reply streams *into* her line from drafts
 * tagged `again`, and a redo that is stopped or fails puts the line back. */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const CHAT_SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.close = vi.fn();
    FakeEventSource.instances.push(this);
  }
}

const TEA = { id: 'h1', role: 'assistant', text: 'Tea?', ts: '2026-11-09T10:00:05',
              regenerable: true, rateable: true };

let routes;
let fetchMock;
let els;

beforeEach(() => {
  document.body.innerHTML = `
    <div id="messages"></div>
    <div class="composer"><input id="text"><button id="send">send</button></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>`;
  routes = {
    '/api/greeting': () => ({ session_id: 's1' }),
    '/api/inbox': () => ({ entries: [] }),
    '/api/history': () => ({ messages: [TEA] }),
  };
  fetchMock = vi.fn((url, init) => {
    const path = String(url).split('?')[0];
    const route = routes[path];
    if (!route) return Promise.reject(new Error(`unexpected request: ${url}`));
    return Promise.resolve(route(init)).then((body) => typeof body?.json === 'function' ? body : ({
      ok: true, status: 200, json: async () => body,
    }));
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', FakeEventSource);
  window.YuriOSRuntime = {
    apiPath: (p) => p, httpPath: (p) => p, wsUrl: (p) => p,
    sessionKey: () => 'test.session',
  };
  window.WorldControls = { isVoiceMuted: () => true };
});

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
  delete window.WorldChat;
  delete window.WorldVoice;
  delete window.WorldControls;
  delete window.YuriOSRuntime;
  window.localStorage.clear();
  document.body.innerHTML = '';
});

async function openRoom() {
  // eslint-disable-next-line no-new-func
  new Function(CHAT_SOURCE)();
  await window.WorldChat.connect();
  const sse = FakeEventSource.instances[0];
  sse.onopen?.();
  await vi.waitFor(() => expect(line('h1')).not.toBeNull());
  const { initVoice } = await import('../js/voice.js');
  els = {
    text: document.getElementById('text'),
    send: document.getElementById('send'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    status: document.getElementById('status'),
    caption: document.getElementById('caption'),
    latency: document.getElementById('latency'),
  };
  initVoice({ viseme: { context: vi.fn() }, els });
  await vi.waitFor(() => expect(els.text.disabled).toBe(false));   // the greeting is done
  return (event) => sse.onmessage({ data: JSON.stringify(event) });
}

const line = (id) => [...document.querySelectorAll('#messages .msg.her')]
  .find((el) => el.dataset.messageId === id) || null;
const again = () => line('h1').querySelector('.msg-swipe [data-dir="again"]');
const posts = (url) => fetchMock.mock.calls
  .filter(([u]) => u === url).map(([, init]) => JSON.parse(init.body));
const locked = () => ({
  input: els.text.disabled,
  stop: els.send.classList.contains('processing'),
  title: els.send.title,
});

function deferred() {
  let settle;
  const promise = new Promise((resolve) => { settle = resolve; });
  return { promise, settle };
}

it('locks the composer like any turn and streams the new reply into her line', async () => {
  const send = await openRoom();
  const answer = deferred();
  routes['/api/regenerate'] = () => answer.promise;

  again().click();
  // the composer, exactly as for a line you typed
  expect(locked()).toEqual({ input: true, stop: true, title: 'stop processing' });
  const [asked] = posts('/api/regenerate');
  expect(asked).toMatchObject({ id: 'h1', channel: 'web' });
  expect(asked.client_id).toBeTruthy();
  // her line wears a draft's head over the reply it is replacing, no controls
  expect(line('h1').classList.contains('regenerating')).toBe(true);
  expect(line('h1').querySelector('.who').textContent).toMatch(/· …$/);
  expect(line('h1').querySelector('.was').textContent).toContain('Tea?');
  expect(line('h1').querySelector('.msg-swipe')).toBeNull();

  send({ type: 'draft', text: '', again: 'h1' });
  send({ type: 'draft', text: 'Coffee,', again: 'h1' });
  expect(line('h1').classList.contains('draft')).toBe(true);
  expect(line('h1').textContent).toContain('Coffee,');
  expect(line('h1').textContent).not.toContain('Tea?');
  // in place: no second bubble under it
  expect(document.querySelectorAll('#messages .msg.her')).toHaveLength(1);

  answer.settle({ session_id: 's1', message: {
    ...TEA, text: 'Coffee, then.', swipe: 1, swipes: 2 } });
  await vi.waitFor(() => expect(locked().input).toBe(false));
  expect(locked().stop).toBe(false);
  expect(line('h1').className).toBe('msg her');
  expect(line('h1').textContent).toContain('Coffee, then.');
  expect(line('h1').querySelector('.count').textContent).toBe('2/2');
  const enabled = [...line('h1').querySelectorAll('.msg-swipe button')]
    .map((b) => [b.dataset.dir, b.disabled]);
  expect(enabled).toEqual([['prev', false], ['again', false]]);
});

it('Stop cancels the redo and puts her line back as it was', async () => {
  await openRoom();
  const before = line('h1').innerHTML;
  routes['/api/regenerate'] = (init) => new Promise((_, reject) => {
    init.signal.addEventListener('abort', () =>
      reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  });
  routes['/api/chat/cancel'] = () => ({});

  again().click();
  const { client_id: clientId } = posts('/api/regenerate')[0];
  els.send.click();                                   // it is the Stop button now
  expect(posts('/api/chat/cancel')).toEqual([{ client_id: clientId, selfie_ids: [] }]);
  expect(locked().input).toBe(false);
  expect(line('h1').innerHTML).toBe(before);
  expect(line('h1').className).toBe('msg her');
  expect(again().disabled).toBe(false);
});

it('a redo that fails says so and leaves the reply it had', async () => {
  const send = await openRoom();
  routes['/api/regenerate'] = () => ({
    ok: false, status: 502, json: async () => ({ detail: 'her reply had nothing in it' }),
  });
  again().click();
  send({ type: 'draft', text: 'Half a', again: 'h1' });
  await vi.waitFor(() => expect(locked().input).toBe(false));
  expect(line('h1').textContent).toContain('Tea?');
  expect(line('h1').textContent).not.toContain('Half a');
  const control = line('h1').querySelector('.msg-swipe');
  expect(control.classList.contains('failed')).toBe(true);
  expect(control.title).toContain('her reply had nothing in it');
  expect(els.text.placeholder).toContain('her reply had nothing in it');
  // the late cancel from the bus finds nothing left to undo
  send({ type: 'draft_cancel', again: 'h1' });
  expect(line('h1').textContent).toContain('Tea?');
});

it('another room sees the redo stream into the line and land, or be taken back', async () => {
  const send = await openRoom();
  send({ type: 'draft', text: '', again: 'h1' });
  expect(line('h1').classList.contains('regenerating')).toBe(true);
  send({ type: 'draft', text: 'Coffee,', again: 'h1' });
  send({ type: 'draft_cancel', again: 'h1' });
  expect(line('h1').textContent).toContain('Tea?');
  expect(line('h1').className).toBe('msg her');

  send({ type: 'draft', text: 'Water.', again: 'h1' });
  send({ type: 'swipe', ...TEA, text: 'Water.', swipe: 1, swipes: 2 });
  expect(line('h1').className).toBe('msg her');
  expect(line('h1').textContent).toContain('Water.');
  expect(line('h1').querySelector('.count').textContent).toBe('2/2');
});

it('the controls wait while the composer is busy with a line of yours', async () => {
  await openRoom();
  const answer = deferred();
  routes['/api/chat'] = () => answer.promise;
  els.text.value = 'tea it is';
  els.send.click();
  expect(again().disabled).toBe(true);
  again().click();
  expect(posts('/api/regenerate')).toHaveLength(0);
  answer.settle({ session_id: 's1' });
  await vi.waitFor(() => expect(again().disabled).toBe(false));
});

it('while a new line is arriving from anywhere, the redo waits for it', async () => {
  const send = await openRoom();
  send({ type: 'draft', text: 'Grant. It is late' });     // another room's greeting
  expect(again().disabled).toBe(true);
  again().click();
  expect(posts('/api/regenerate')).toHaveLength(0);
  send({ type: 'draft_cancel' });
  expect(again().disabled).toBe(false);
});
