/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* A desk path named in the line (SPEC §2.6).
 *
 * She will say `goals/g-7517a5363d42.md` the way a person names a file, and
 * that path used to be inert text — the only way to read it was to hunt the
 * files tab. The same GET a report card already makes opens it in place, folded
 * under the bubble until asked for. chat.js is a classic script, so it is
 * evaluated rather than imported.
 */
const SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

const LINE = {
  id: 'm1', role: 'assistant', ts: '2026-09-17T12:00:00',
  text: "Hey. I finished the thing I said I'd show you — it's in "
    + 'goals/g-7517a5363d42.md, and I want you to read it when you have a minute.',
};

function boot() {
  document.body.innerHTML = '<div id="messages"></div>';
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  return window.WorldChat;
}

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.close = vi.fn();
    FakeEventSource.instances.push(this);
  }
}

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {}))); });
afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
  delete window.WorldChat;
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

describe('a desk path named in the chat', () => {
  it('turns the path into a control and keeps the rest of the line', () => {
    boot().confirmUser(LINE);
    const button = document.querySelector('.msg-file');
    expect(button).not.toBeNull();
    expect(button.textContent).toBe('goals/g-7517a5363d42.md');
    expect(button.dataset.path).toBe('goals/g-7517a5363d42.md');
    expect(document.querySelector('.msg').textContent)
      .toContain("I finished the thing I said I'd show you");
    const card = document.querySelector('.msg-file-card');
    expect(card.hidden).toBe(true);
    expect(card.dataset.path).toBe('goals/g-7517a5363d42.md');
  });

  it('strips a workspace/ prefix so the desk route sees a relative path', () => {
    boot().confirmUser({
      id: 'm2', role: 'assistant',
      text: 'I left it in workspace/goals/g-7517a5363d42.md for you.',
    });
    expect(document.querySelector('.msg-file').dataset.path)
      .toBe('goals/g-7517a5363d42.md');
    expect(document.querySelector('.msg-file').textContent)
      .toBe('workspace/goals/g-7517a5363d42.md');
  });

  it('leaves your own words alone', () => {
    // §2.6 is about a line *she* wrote. A path you typed or pasted is not a
    // pointer she offered, and turning it into one would make what you said
    // into buttons you did not put there.
    boot().confirmUser({
      id: 'm4', role: 'user',
      text: 'have a look at goals/g-7517a5363d42.md when you get a chance',
    });
    expect(document.querySelector('.msg-file')).toBeNull();
    expect(document.querySelector('.msg-file-card')).toBeNull();
    expect(document.querySelector('.msg').textContent)
      .toContain('goals/g-7517a5363d42.md');
  });

  it('does not treat a URL as a desk path', () => {
    boot().confirmUser({
      id: 'm3', role: 'assistant',
      text: 'I read https://example.com/goals/g-7517a5363d42.md already.',
    });
    expect(document.querySelector('.msg-file')).toBeNull();
  });

  it('fetches the document once, then folds it without asking again', async () => {
    const fetched = vi.fn(async () => ({
      ok: true, json: async () => ({ text: '## 2026-09-13\n\nThe almost.' }),
    }));
    vi.stubGlobal('fetch', fetched);
    boot().confirmUser(LINE);
    const button = document.querySelector('.msg-file');
    const card = document.querySelector('.msg-file-card');

    button.click();
    await vi.waitFor(() => expect(card.hidden).toBe(false));
    expect(card.querySelector('.msg-file-body').textContent).toContain('The almost.');
    expect(fetched).toHaveBeenCalledTimes(1);
    expect(fetched.mock.calls[0][0]).toContain(
      encodeURIComponent('goals/g-7517a5363d42.md'));
    expect(button.getAttribute('aria-expanded')).toBe('true');

    button.click();
    await vi.waitFor(() => expect(card.hidden).toBe(true));
    button.click();
    await vi.waitFor(() => expect(card.hidden).toBe(false));
    expect(fetched).toHaveBeenCalledTimes(1);
  });

  it('says so when the file is no longer on her desk', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404 })));
    boot().confirmUser(LINE);
    document.querySelector('.msg-file').click();
    await vi.waitFor(() => expect(
      document.querySelector('.msg-file-body').textContent)
      .toContain("isn't on her desk"));
  });

  it('does not linkify a path still being spoken', async () => {
    window.YuriOSRuntime = { apiPath: (path) => path, httpPath: (path) => path };
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      if (String(url).startsWith('/api/history')) {
        return { ok: true, json: async () => ({ messages: [] }) };
      }
      if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
      throw new Error(`unexpected request: ${url}`);
    }));
    vi.stubGlobal('EventSource', FakeEventSource);
    boot();
    await window.WorldChat.connect();
    FakeEventSource.instances[0].onmessage({
      data: JSON.stringify({
        type: 'draft', text: "it's in goals/g-7517a5363d42.md",
      }),
    });
    expect(document.querySelector('.msg-file')).toBeNull();
    expect(document.querySelector('.draft').textContent)
      .toContain('goals/g-7517a5363d42.md');
  });
});

describe('the path control, in all three rooms', () => {
  const ROOMS = ['sanctuary.css', 'live2d/sanctuary.css', 'text/text.css'];

  it.each(ROOMS)('%s dresses the desk path rather than inheriting it', (room) => {
    const css = readFileSync(resolve(process.cwd(), room), 'utf8');
    const rule = css.match(/\.msg-file\{[^}]*\}/);
    expect(rule, `${room} has no .msg-file rule`).not.toBeNull();
    expect(rule[0]).toMatch(/background:/);
    expect(rule[0]).toMatch(/color:/);
  });
});
