/** @vitest-environment jsdom */
/**
 * A line of yours that never got its answer (SPEC §2.6) keeps two buttons —
 * send it again, delete it — wherever the conversation is open: marked live by
 * the failed request in the tab that sent it, by the `unanswered` event in
 * every other room, and by the history row after a reload.
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

let posted;

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
  delete window.WorldChat;
  delete window.WorldVoice;
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

async function room(history = []) {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (p) => p, httpPath: (p) => p };
  window.WorldVoice = { resend: vi.fn(() => true), send: vi.fn(() => true) };
  posted = [];
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    if (String(url).startsWith('/api/history')) {
      return { ok: true, json: async () => ({ messages: history }) };
    }
    if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    if (url === '/api/chat/discard') {
      posted.push(JSON.parse(options.body));
      return { ok: true, json: async () => ({ discarded: 'u1' }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  const source = FakeEventSource.instances[0];
  source.onopen();
  await vi.waitFor(() => expect(document.querySelectorAll('.msg').length).toBe(history.length));
  return source;
}

const send = (source, payload) => source.onmessage({ data: JSON.stringify(payload) });
const line = (id) => [...document.querySelectorAll('.msg')].find((el) => el.dataset.messageId === id);
const button = (div, act) => div.querySelector(`.msg-retry [data-act="${act}"]`);

it('draws a line the host says got no reply with its receipt and both buttons', async () => {
  await room([{ id: 'u1', role: 'user', text: 'did the install take?', unanswered: true,
    ts: '2026-11-16T10:00:00' }]);
  const div = line('u1');
  expect(div.classList.contains('failed')).toBe(true);
  expect(div.querySelector('.receipt').textContent).toBe('no reply');
  expect(button(div, 'resend')).not.toBeNull();
  expect(button(div, 'discard')).not.toBeNull();
});

it('marks a line in every room when the host says its turn broke', async () => {
  const source = await room([{ id: 'u1', role: 'user', text: 'hello', ts: '2026-11-16T10:00:00' }]);
  expect(button(line('u1'), 'resend')).toBeNull();
  send(source, { type: 'unanswered', id: 'u1' });
  expect(line('u1').querySelector('.receipt').textContent).toBe('no reply');
  expect(button(line('u1'), 'resend')).not.toBeNull();
});

it('gives the line that failed in this tab the same buttons', async () => {
  const source = await room();
  window.WorldChat.addPendingUser('did the install take?', 'client-1');
  send(source, { type: 'message', id: 'u1', role: 'user', text: 'did the install take?',
    client_id: 'client-1', ts: '2026-11-16T10:00:00' });
  window.WorldChat.failPending('client-1', 'no reply');
  const div = line('u1');
  expect(div.querySelector('.receipt').textContent).toBe('no reply');
  button(div, 'resend').click();
  expect(window.WorldVoice.resend).toHaveBeenCalledWith('u1', 'did the install take?', undefined);
});

it('sends a picture line again with its picture', async () => {
  await room([{ id: 'u1', role: 'user', text: 'look', image_url: '/api/uploads/a.png',
    unanswered: true, ts: '2026-11-16T10:00:00' }]);
  button(line('u1'), 'resend').click();
  expect(window.WorldVoice.resend).toHaveBeenCalledWith('u1', 'look', '/api/uploads/a.png');
});

it('deletes only on the second press, and takes the line off', async () => {
  await room([{ id: 'u1', role: 'user', text: 'hello', unanswered: true, ts: '2026-11-16T10:00:00' }]);
  const remove = button(line('u1'), 'discard');
  remove.click();
  expect(posted).toEqual([]);
  expect(remove.textContent).toBe('delete?');
  remove.click();
  await vi.waitFor(() => expect(line('u1')).toBeUndefined());
  expect(posted).toEqual([{ id: 'u1' }]);
});

it('takes a line off when another room deletes or resends it', async () => {
  const source = await room([{ id: 'u1', role: 'user', text: 'hello', unanswered: true,
    ts: '2026-11-16T10:00:00' }]);
  send(source, { type: 'message_removed', id: 'u1' });
  expect(line('u1')).toBeUndefined();
});

it('drops its own copy when a resend is refused before the host draws it', async () => {
  const source = await room([{ id: 'u1', role: 'user', text: 'hello', unanswered: true,
    ts: '2026-11-16T10:00:00' }]);
  window.WorldChat.beginResend('client-2', 'u1');
  window.WorldChat.addPendingUser('hello', 'client-2');
  expect(document.querySelectorAll('.msg.you')).toHaveLength(2);
  window.WorldChat.failPending('client-2', 'not received');
  expect(document.querySelectorAll('.msg.you')).toHaveLength(1);
  expect(button(line('u1'), 'resend')).not.toBeNull();
  void source;
});

it('holds the buttons while the composer is busy', async () => {
  await room([{ id: 'u1', role: 'user', text: 'hello', unanswered: true, ts: '2026-11-16T10:00:00' }]);
  window.WorldChat.setBusy(true);
  expect(button(line('u1'), 'resend').disabled).toBe(true);
  button(line('u1'), 'resend').click();
  expect(window.WorldVoice.resend).not.toHaveBeenCalled();
  window.WorldChat.setBusy(false);
  expect(button(line('u1'), 'resend').disabled).toBe(false);
});
