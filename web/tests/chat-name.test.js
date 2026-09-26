/** @vitest-environment jsdom */
/* Her name on a line she said. History is drawn before the event stream's
 * `hello` says who she is, so those rows were labelled "her" while every line
 * after them carried her name — two speakers, apparently, in one column. */
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

it('names the lines history drew before she said who she is', async () => {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path, httpPath: (path) => path };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).startsWith('/api/history')) {
      return { ok: true, json: async () => ({ messages: [
        { id: 'a1', role: 'assistant', text: 'You came back.' },
      ] }) };
    }
    if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);

  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  await vi.waitFor(() => expect(document.querySelectorAll('.msg.her')).toHaveLength(1));
  const who = () => [...document.querySelectorAll('.msg.her .who [data-char-name]')]
    .map((el) => el.textContent);
  expect(who()).toEqual(['her']);

  const stream = FakeEventSource.instances[0];
  stream.onopen();
  stream.onmessage({ data: JSON.stringify({ type: 'hello', character: 'Iris' }) });
  stream.onmessage({ data: JSON.stringify({
    type: 'message', id: 'a2', role: 'assistant', text: 'It is late.' }) });

  await vi.waitFor(() => expect(document.querySelectorAll('.msg.her')).toHaveLength(2));
  expect(who()).toEqual(['Iris', 'Iris']);
});
