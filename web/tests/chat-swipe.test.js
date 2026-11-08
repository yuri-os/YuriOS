/** @vitest-environment jsdom */
/* ‹ 2/3 › on her last reply (SPEC §39). chat.js draws the control only on the
 * line the host marked `regenerable`, asks for another reply past the last one
 * and switches between the ones she gave otherwise, and repaints the line in
 * place from the `swipe` event — with the thumb that stands on *that* reply. */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

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

const line = (id) => [...document.querySelectorAll('.msg.her')]
  .find((el) => el.dataset.messageId === id);
const control = (id) => line(id)?.querySelector('.msg-swipe') || null;
const dirs = (id) => [...control(id).querySelectorAll('button')].map((b) => b.dataset.dir);
const posts = (url) => fetchMock.mock.calls
  .filter(([u]) => u === url).map(([, init]) => JSON.parse(init.body));

it('draws the control only where the host said her reply can be redone', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', regenerable: true });
  chat.confirmUser({ id: 'a2', role: 'assistant', text: 'Earlier.' });
  chat.confirmUser({ id: 'u1', role: 'user', text: 'hi', regenerable: true });

  expect(control('a1')).not.toBeNull();
  expect(control('a2')).toBeNull();
  expect(document.querySelectorAll('.msg-swipe')).toHaveLength(1);
  // one reply so far: no count, no way back — just "another"
  expect(dirs('a1')).toEqual(['again']);
  expect(control('a1').querySelector('.count')).toBeNull();
});

it('past the last reply asks for another; within them, switches', async () => {
  const chat = loadChat();
  // a redo is a turn: the composer (voice.js) runs it, not the column
  window.WorldVoice = { regenerate: vi.fn(() => true) };
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', regenerable: true });
  control('a1').querySelector('[data-dir="again"]').click();
  expect(window.WorldVoice.regenerate).toHaveBeenCalledWith('a1');
  expect(posts('/api/regenerate')).toHaveLength(0);
  delete window.WorldVoice;

  chat.receiveSwipe({ id: 'a1', role: 'assistant', text: 'Coffee, then.',
                      swipe: 1, swipes: 2, regenerable: true });
  expect(line('a1').textContent).toContain('Coffee, then.');
  expect(control('a1').querySelector('.count').textContent).toBe('2/2');
  expect(dirs('a1')).toEqual(['prev', 'again']);

  control('a1').querySelector('[data-dir="prev"]').click();
  await vi.waitFor(() => expect(posts('/api/swipe')).toEqual([{ id: 'a1', swipe: 0 }]));
});

it('a swipe repaints the line with the thumb on the reply it now shows', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', regenerable: true,
                     rateable: true, thumbs: 1 });
  chat.receiveSwipe({ id: 'a1', role: 'assistant', text: 'Coffee.', swipe: 1,
                      swipes: 2, regenerable: true, rateable: false });
  expect(line('a1').querySelector('.msg-rate')).toBeNull();   // not filed yet
  chat.receiveSwipe({ id: 'a1', role: 'assistant', text: 'Tea?', swipe: 0,
                      swipes: 2, regenerable: true, rateable: true, thumbs: 1 });
  const lit = [...line('a1').querySelectorAll('.msg-rate button')]
    .filter((b) => b.getAttribute('aria-pressed') === 'true');
  expect(lit.map((b) => b.dataset.thumbs)).toEqual(['1']);
});

it('once you have gone on, the control leaves the line', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', regenerable: true,
                     swipe: 1, swipes: 2 });
  expect(control('a1')).not.toBeNull();
  chat.receiveSwipe({ id: 'a1', role: 'assistant', text: 'Tea?', swipe: 1,
                      swipes: 2, regenerable: false });
  expect(control('a1')).toBeNull();
});

it('a refused switch is put back, visibly', async () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Coffee.', regenerable: true,
                     swipe: 1, swipes: 2 });
  fetchMock.mockImplementationOnce(async () => ({
    ok: false, status: 409, json: async () => ({ detail: 'only her last reply can be said again' }),
  }));
  control('a1').querySelector('[data-dir="prev"]').click();
  await vi.waitFor(() => expect(control('a1').classList.contains('failed')).toBe(true));
  expect(control('a1').title).toContain('only her last reply');
  expect(control('a1').querySelector('[data-dir="prev"]').disabled).toBe(false);
});

it('while the composer is busy nothing on the line can be pressed', () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'Tea?', regenerable: true,
                     swipe: 1, swipes: 2 });
  chat.setBusy(true);
  expect([...control('a1').querySelectorAll('button')].every((b) => b.disabled)).toBe(true);
  chat.receiveSwipe({ id: 'a1', role: 'assistant', text: 'Tea?', swipe: 0, swipes: 2,
                      regenerable: true });          // a repaint keeps it so
  expect([...control('a1').querySelectorAll('button')].every((b) => b.disabled)).toBe(true);
  chat.setBusy(false);
  expect(dirs('a1')).toEqual(['prev', 'next']);
  expect([...control('a1').querySelectorAll('button')].map((b) => b.disabled))
    .toEqual([true, false]);                         // ‹ at the first reply stays off
});
