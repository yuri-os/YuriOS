/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* The model-server banner (shared/connection.js, SPEC §31.5).
 *
 * What it decides: whether to show at all (only for a state her replies will
 * not work in, or while a server is being started), whether "Start" is offered
 * (a down server the host can run here), what a press posts, and that a
 * dismissed banner comes back when the state changes.
 */

let calls;

async function load(reply = { state: 'ok', links: [], detail: '' }, { ok = true } = {}) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    calls.push({ url: String(url), init });
    return ok
      ? { ok: true, status: 200, json: async () => reply }
      : { ok: false, status: 403, json: async () => ({ detail: 'owner authentication required' }) };
  }));
  document.body.innerHTML = '<main></main>';
  delete window.ConnectionBanner;
  delete window.WorldChat;
  vi.resetModules();
  await import('../shared/connection.js');
}

const banner = () => document.querySelector('.connection-banner');
const text = () => banner().querySelector('.connection-banner-text').textContent;
const button = (act) => banner().querySelector(`[data-act="${act}"]`);
const send = (snap) => window.dispatchEvent(new CustomEvent('world-ev', {
  detail: { type: 'connection', busy: '', fallback: '', ...snap } }));
const flush = () => new Promise((r) => setTimeout(r, 0));

const LMSTUDIO = { provider: 'lmstudio', label: 'LM Studio', state: 'down',
                   startable: true, where: 'localhost:1234' };
const DOWN = { state: 'down', links: [LMSTUDIO],
               detail: "LM Studio isn't answering at localhost:1234 — it isn't running" };

beforeEach(() => { vi.resetModules(); });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('the model-server banner', () => {
  it('stays hidden while the server answers', async () => {
    await load();
    send({ state: 'ok', links: [{ ...LMSTUDIO, state: 'ok' }] });
    expect(banner().hidden).toBe(true);
    send({ state: 'unchecked', links: [] });
    expect(banner().hidden).toBe(true);
  });

  it('says a down server and offers to start a local one', async () => {
    await load();
    send(DOWN);
    expect(banner().hidden).toBe(false);
    expect(text()).toContain("isn't answering at localhost:1234");
    expect(button('start').hidden).toBe(false);
    expect(button('start').textContent).toBe('Start LM Studio');
  });

  it('offers only "check again" for a server it cannot start', async () => {
    await load();
    send({ ...DOWN, links: [{ ...LMSTUDIO, startable: false }] });
    expect(button('start').hidden).toBe(true);
    expect(button('check').hidden).toBe(false);
    send({ state: 'refused', detail: 'OpenRouter refused the API key (HTTP 401)',
           links: [{ provider: 'openrouter', label: 'OpenRouter', state: 'refused' }] });
    expect(text()).toContain('refused the API key');
    expect(button('start').hidden).toBe(true);
  });

  it('starts it, and goes away when the host says it is up', async () => {
    await load({ state: 'ok', links: [{ ...LMSTUDIO, state: 'ok' }], detail: '', busy: '' });
    send(DOWN);
    button('start').click();
    expect(text()).toBe('Starting LM Studio…');
    expect(button('check').disabled).toBe(true);
    await flush();
    expect(calls[0].url).toBe('/api/connection/start');
    expect(calls[0].init.method).toBe('POST');
    expect(banner().hidden).toBe(true);
  });

  it('says a refused start rather than pretending', async () => {
    await load({}, { ok: false });
    send(DOWN);
    button('start').click();
    await flush();
    expect(banner().hidden).toBe(false);
    expect(text()).toContain('owner authentication required');
  });

  it('names the fallback she is answering through meanwhile', async () => {
    await load();
    send({ ...DOWN, fallback: 'her replies are going to the built-in GGUF runner until it is back' });
    const note = banner().querySelector('.connection-banner-note');
    expect(note.hidden).toBe(false);
    expect(note.textContent).toContain('GGUF runner');
  });

  it('comes back after a dismissal when something changes', async () => {
    await load();
    send(DOWN);
    button('dismiss').click();
    expect(banner().hidden).toBe(true);
    send(DOWN);                                   // the same news: stays hidden
    expect(banner().hidden).toBe(true);
    send({ state: 'missing', detail: "LM Studio is running at localhost:1234, but has no model called 'x'",
           links: [{ ...LMSTUDIO, state: 'missing' }] });
    expect(banner().hidden).toBe(false);
    expect(banner().dataset.tone).toBe('warn');
  });

  it('starts on the state chat.js held for a listener that came late', async () => {
    vi.stubGlobal('fetch', vi.fn());
    document.body.innerHTML = '<main></main>';
    delete window.ConnectionBanner;
    window.WorldChat = { connection: { type: 'connection', busy: '', fallback: '', ...DOWN } };
    vi.resetModules();
    await import('../shared/connection.js');
    expect(banner().hidden).toBe(false);
  });
});
