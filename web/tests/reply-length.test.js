/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* The composer's shorter/longer nudge (shared/reply-length.js, SPEC §38).
 *
 * What it decides: whether to show at all (only once the host's sticky
 * `reply_length` event has said there is a rung), which way each button may
 * still go, what a press saves, and that the word on screen only moves when the
 * host accepts the save. jsdom and a re-import per case, like settings-multi:
 * the script is a classic IIFE the bundled rooms and the raw Live2D one share.
 */

const LEVELS = ['terse', 'brief', 'natural', 'full', 'expansive'];
let calls;

async function load({ ok = true, detail = '' } = {}) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    calls.push({ url: String(url), init });
    return ok
      ? { ok: true, status: 200, json: async () => ({ applied: ['reply_length'] }) }
      : { ok: false, status: 400, json: async () => ({ detail }) };
  }));
  document.body.innerHTML = `
    <div class="composer"><input id="text"><button id="send"></button></div>`;
  delete window.ReplyLength;
  delete window.WorldChat;
  vi.resetModules();
  await import('../shared/reply-length.js');
}

const group = () => document.querySelector('.reply-length');
const word = () => group().querySelector('.reply-length-level').textContent;
const button = (dir) => group().querySelector(`[data-dir="${dir}"]`);
const announce = (level) => window.dispatchEvent(new CustomEvent('world-ev', {
  detail: { type: 'reply_length', level, levels: LEVELS } }));

beforeEach(() => { vi.resetModules(); });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('the reply-length nudge', () => {
  it('stays hidden until the host says there is a rung', async () => {
    await load();
    expect(group().hidden).toBe(true);
    announce('natural');
    expect(group().hidden).toBe(false);
    expect(word()).toBe('natural');
    // in the composer row, before the text box: one row, send stays put
    expect(group().nextElementSibling.id).toBe('text');
  });

  it('starts on the rung chat.js held for a listener that came late', async () => {
    calls = [];
    vi.stubGlobal('fetch', vi.fn());
    document.body.innerHTML = '<div class="composer"><input id="text"></div>';
    delete window.ReplyLength;
    window.WorldChat = { replyLength: { type: 'reply_length', level: 'brief', levels: LEVELS } };
    vi.resetModules();
    await import('../shared/reply-length.js');
    expect(word()).toBe('brief');
  });

  it('uses the text room options slot above the nested input row', async () => {
    await load();
    document.body.innerHTML = `<div class="composer"><details><div data-reply-length></div></details>
      <div><input id="text"><button id="send"></button></div></div>`;
    delete window.ReplyLength;
    vi.resetModules();
    await import('../shared/reply-length.js');
    announce('natural');
    expect(group().parentElement.hasAttribute('data-reply-length')).toBe(true);
    expect(document.getElementById('text').nextElementSibling.id).toBe('send');
  });

  it('saves one step, as her own override, and then shows it', async () => {
    await load();
    announce('natural');
    button(-1).click();
    await vi.waitFor(() => expect(word()).toBe('brief'));
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/brain');
    expect(calls[0].init.method).toBe('PATCH');
    expect(JSON.parse(calls[0].init.body)).toEqual({ reply_length: 'brief' });
  });

  it('cannot step off either end of the ladder', async () => {
    await load();
    announce('terse');
    expect(button(-1).disabled).toBe(true);
    expect(button(1).disabled).toBe(false);
    announce('expansive');
    expect(button(1).disabled).toBe(true);
    button(1).click();
    expect(calls).toHaveLength(0);
  });

  it('keeps the old word when the host refuses, and says why', async () => {
    await load({ ok: false, detail: 'reply_length must be one of …' });
    announce('natural');
    button(1).click();
    await vi.waitFor(() => expect(group().classList.contains('failed')).toBe(true));
    expect(word()).toBe('natural');
    expect(group().title).toContain('reply_length must be one of');
  });

  it('follows a nudge made in another room', async () => {
    await load();
    announce('natural');
    announce('full');
    expect(word()).toBe('full');
    expect(calls).toHaveLength(0);
  });

  it('knows its own steps', async () => {
    await load();
    const { step } = window.ReplyLength;
    expect(step(LEVELS, 'natural', 1)).toBe('full');
    expect(step(LEVELS, 'terse', -1)).toBeNull();
    expect(step(LEVELS, 'mystery', 1)).toBeNull();
  });
});
