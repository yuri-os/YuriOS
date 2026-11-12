/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, expect, it, vi } from 'vitest';

/* A hand she used is a chip in the column (SPEC §7.3): one `role: "tool"` row
 * per call, drawn above the reply it was made for — and, mid-reply, above the
 * words still arriving rather than in place of them. */

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

async function room(history) {
  document.body.innerHTML = '<div id="messages"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path, httpPath: (path) => path };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).startsWith('/api/history'))
      return { ok: true, json: async () => ({ messages: history }) };
    if (url === '/api/inbox') return { ok: true, json: async () => ({ entries: [] }) };
    throw new Error(`unexpected request: ${url}`);
  }));
  vi.stubGlobal('EventSource', FakeEventSource);
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  await window.WorldChat.connect();
  const stream = FakeEventSource.instances[0];
  stream.onopen();
  return (event) => stream.onmessage({ data: JSON.stringify(event) });
}

it('draws a call as a chip, and one she made alone as a quieter one', async () => {
  await room([
    { id: 't1', role: 'tool', tool: 'write_note', text: 'write_note · notes/a.md',
      verdict: 'ok', background: true, ts: '2026-09-24T03:00:00' },
    { id: 't2', role: 'tool', tool: 'web_search', text: 'web_search · tiles',
      verdict: 'denied', why: 'rate limit', ts: '2026-09-24T09:00:00' },
    { id: 'a1', role: 'assistant', text: 'Found it.', ts: '2026-09-24T09:00:01' },
  ]);
  await vi.waitFor(() => expect(document.querySelectorAll('.msg')).toHaveLength(3));

  const [alone, refused, reply] = document.querySelectorAll('.msg');
  expect(alone.className).toBe('msg tool background');
  expect(alone.textContent).toContain('write_note · notes/a.md');
  expect(refused.classList.contains('refused')).toBe(true);
  expect(refused.textContent).toContain('denied (rate limit)');
  expect(reply.classList.contains('her')).toBe(true);
  // a chip is not her speaking: no speaker line, no bubble
  expect(alone.querySelector('.who')).toBeNull();
});

it('puts a chip that lands mid-reply above the words still arriving', async () => {
  const send = await room([]);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 0));

  send({ type: 'draft', text: 'Let me look' });
  send({ type: 'message', id: 't1', role: 'tool', tool: 'read_note',
         text: 'read_note · notes/a.md', verdict: 'ok' });

  const rows = [...document.querySelectorAll('#messages > *')];
  expect(rows.map((el) => el.className)).toEqual(['msg tool', 'msg her draft']);
  expect(rows[1].textContent).toContain('Let me look');
});

/* A chip on one of her notes opens it (SPEC §7.3): the path is the control,
 * and the popup is the note now, on the lines a read covered. */
async function roomWithDesk(history, files) {
  const send = await room(history);
  const listing = fetch.getMockImplementation();
  fetch.mockImplementation(async (url) => {
    const at = String(url);
    if (at.startsWith('/api/mind/workspace/file?path=')) {
      const path = decodeURIComponent(at.split('=')[1]);
      if (!(path in files)) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, json: async () => ({ path, text: files[path] }) };
    }
    return listing(url);
  });
  return send;
}

it('makes the path on a note chip a control that opens the note on the lines she read', async () => {
  const note = Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join('\n');
  await roomWithDesk([
    { id: 't1', role: 'tool', tool: 'read_note', note: 'notes/post.md', lines: [30, 48],
      text: 'read_note · notes/post.md · lines 30–48 of 60', verdict: 'ok',
      ts: '2026-11-12T22:12:00' },
  ], { 'notes/post.md': note });
  await vi.waitFor(() => expect(document.querySelector('.tool-file')).not.toBeNull());

  const link = document.querySelector('.tool-file');
  expect(link.textContent).toBe('notes/post.md');
  expect(link.closest('.tool-chip').textContent).toContain('lines 30–48 of 60');

  link.click();
  const dialog = document.querySelector('dialog.note-peek');
  await vi.waitFor(() => expect(dialog.querySelectorAll('.note-peek-line')).toHaveLength(60));
  expect(dialog.open).toBe(true);
  expect(dialog.querySelector('.note-peek-path').textContent).toBe('notes/post.md');
  const read = [...dialog.querySelectorAll('.note-peek-line.read')];
  expect(read).toHaveLength(19);
  expect(read[0].textContent).toContain('line 30');
  expect(read.at(-1).textContent).toContain('line 48');
  expect(dialog.querySelector('.note-peek-span').textContent).toBe('she read lines 30–48 of 60');
});

it('links an older chip from its text, a refusal too, but not a clipped path or another tool', async () => {
  await roomWithDesk([
    { id: 't1', role: 'tool', tool: 'write_note', text: 'write_note · notes/old.md',
      verdict: 'ok', ts: '2026-11-12T01:00:00' },
    { id: 't2', role: 'tool', tool: 'write_note', text: 'write_note · research/aaaaaaaa…',
      verdict: 'ok', ts: '2026-11-12T02:00:00' },
    { id: 't3', role: 'tool', tool: 'read_note', text: 'read_note · notes/x.md',
      verdict: 'denied', why: 'rate limit', ts: '2026-11-12T03:00:00' },
    { id: 't4', role: 'tool', tool: 'web_search', text: 'web_search · notes/a.md',
      verdict: 'ok', ts: '2026-11-12T04:00:00' },
  ], {});
  await vi.waitFor(() => expect(document.querySelectorAll('.msg.tool')).toHaveLength(4));

  const links = [...document.querySelectorAll('.tool-file')];
  expect(links.map((el) => el.dataset.note)).toEqual(['notes/old.md', 'notes/x.md']);
  // the reason still follows the path
  expect(links[1].closest('.tool-chip').textContent).toContain('denied (rate limit)');
});

it('says so when the note a chip names is gone', async () => {
  await roomWithDesk([
    { id: 't1', role: 'tool', tool: 'write_note', note: 'notes/gone.md',
      text: 'write_note · notes/gone.md', verdict: 'ok', ts: '2026-11-12T01:00:00' },
  ], {});
  await vi.waitFor(() => expect(document.querySelector('.tool-file')).not.toBeNull());
  document.querySelector('.tool-file').click();
  const dialog = document.querySelector('dialog.note-peek');
  await vi.waitFor(() =>
    expect(dialog.querySelector('.note-peek-status').textContent).toContain("isn't on her desk"));
});
