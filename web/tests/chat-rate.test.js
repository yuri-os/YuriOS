/** @vitest-environment jsdom */
/* 👍/👎 on her replies (SPEC §37). chat.js draws the control where the host said
 * a line can be rated — never on a guess — keeps every room on the same thumb
 * through the `rating` event, and puts a press back when the host refuses it. */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

// a classic script shared by all three rooms: evaluated, not imported
const CHAT_SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

let fetchMock;

beforeEach(() => {
  document.body.innerHTML = '<div id="messages"></div>';
  fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({}) }));
  vi.stubGlobal('fetch', fetchMock);
  window.YuriOSRuntime = { apiPath: (p) => p, httpPath: (p) => p };
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.WorldChat;
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

function loadChat() {
  // eslint-disable-next-line no-new-func
  new Function(CHAT_SOURCE)();
  return window.WorldChat;
}

const group = (id) => [...document.querySelectorAll('.msg-rate')]
  .find((el) => el.dataset.messageId === id);
const pressed = (id) => [...group(id).querySelectorAll('button')]
  .filter((b) => b.getAttribute('aria-pressed') === 'true')
  .map((b) => Number(b.dataset.thumbs));
const rateCalls = () => fetchMock.mock.calls
  .filter(([url]) => url === '/api/rate')
  .map(([, init]) => JSON.parse(init.body));

it('draws thumbs only on the lines the host called rateable', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', rateable: true });
  chat.confirmUser({ id: 'a2', role: 'assistant', text: 'Oh, there you are.',
                     proactive: true });
  chat.confirmUser({ id: 'u1', role: 'user', text: 'yes', rateable: true });

  const ids = [...document.querySelectorAll('.msg-rate')].map((el) => el.dataset.messageId);
  expect(ids).toEqual(['a1']);
  // in the header row, beside the stamp — not in the words
  expect(group('a1').closest('.who')).not.toBeNull();
  expect(pressed('a1')).toEqual([]);
});

it('opens on the rating that stands', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', rateable: true,
                     thumbs: -1 });
  expect(pressed('a1')).toEqual([-1]);
  expect(group('a1').classList.contains('rated')).toBe(true);
});

it('a filed text turn gains the control in place, without a reload', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Ten minutes.' });
  expect(group('a1')).toBeUndefined();

  chat.receiveRating({ type: 'rating', id: 'a1', thumbs: 0 });
  expect(group('a1')).toBeDefined();
  expect(document.querySelectorAll('.msg-rate')).toHaveLength(1);
});

it('a rating for a line not drawn yet is painted when the line arrives', () => {
  const chat = loadChat();
  chat.receiveRating({ type: 'rating', id: 'a1', thumbs: 1 });
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?' });
  expect(pressed('a1')).toEqual([1]);
});

it('another room\'s press shows here too', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', rateable: true });
  chat.receiveRating({ type: 'rating', id: 'a1', thumbs: -1 });
  expect(pressed('a1')).toEqual([-1]);
  chat.receiveRating({ type: 'rating', id: 'a1', thumbs: 0 });
  expect(pressed('a1')).toEqual([]);
  expect(group('a1').classList.contains('rated')).toBe(false);
});

it('posts by transcript id, and the same thumb again takes it back', async () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', rateable: true });
  const up = group('a1').querySelector('[data-thumbs="1"]');

  up.click();
  expect(pressed('a1')).toEqual([1]);          // shown at once
  await vi.waitFor(() => expect(rateCalls()).toHaveLength(1));
  await vi.waitFor(() => expect(group('a1').dataset.busy).toBeUndefined());

  up.click();
  await vi.waitFor(() => expect(rateCalls()).toHaveLength(2));
  expect(rateCalls()).toEqual([{ id: 'a1', thumbs: 1 }, { id: 'a1', thumbs: 0 }]);
  expect(pressed('a1')).toEqual([]);
});

it('a press the host refuses is put back, visibly', async () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', rateable: true,
                     thumbs: 1 });
  fetchMock.mockImplementationOnce(async () => ({ ok: false, status: 409 }));

  group('a1').querySelector('[data-thumbs="-1"]').click();
  await vi.waitFor(() => expect(group('a1').classList.contains('failed')).toBe(true));
  expect(pressed('a1')).toEqual([1]);
});
