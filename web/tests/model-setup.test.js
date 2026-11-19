/** @vitest-environment jsdom */
/**
 * The first-run panel (SPEC §11.3.4): a fresh install is chosen into a running
 * house from the browser — the model saved, a GGUF waited out, the house
 * restarted and the page reloaded onto the new boot — and a server nobody
 * supervises says what to do instead of pretending.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const SETUP = {
  configured: false,
  model: 'NONE',
  recommendations: [{ id: 'gguf/a/B-GGUF', label: 'B', hardware: '8 GB', quant: 'Q4_K_M' }],
  providers: [
    { id: 'gguf', label: 'Download a GGUF' },
    { id: 'lmstudio', label: 'LM Studio', prefix: 'lm_studio/',
      base_url: 'http://localhost:1234/v1', key_saved: false },
    { id: 'openrouter', label: 'OpenRouter', prefix: 'openrouter/', key_saved: true },
    { id: 'custom', label: 'Another LiteLLM model id' },
  ],
  download: { state: 'idle', detail: '' },
};

let routes;
let calls;

function serve(table) {
  routes = table;
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const key = `${options.method || 'GET'} ${url}`;
    calls.push({ key, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = routes[key];
    if (answer === undefined) throw new Error(`unexpected request: ${key}`);
    const value = typeof answer === 'function' ? answer() : answer;
    const [status, body] = Array.isArray(value) ? value : [200, value];
    return new Response(JSON.stringify(body), { status });
  }));
}

async function mount(setup = SETUP) {
  window.__YURIOS_MODEL_SETUP_TEST__ = true;
  vi.resetModules();
  const { mountModelSetup } = await import('../shared/model-setup.js');
  const reload = vi.fn();
  const mounted = mountModelSetup(setup, { reload });
  return { ...mounted, reload };
}

const $ = (selector) => document.querySelector(selector);
const status = () => $('.model-setup-status').textContent;

beforeEach(() => {
  document.body.innerHTML = '';
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  delete window.__YURIOS_MODEL_SETUP_TEST__;
});

it('saves the choice, restarts the house and reloads onto the new boot', async () => {
  let boot = 'old';
  serve({
    'POST /api/onboarding': { ok: true, model: 'openrouter/v/m', state: 'ready', detail: 'ok' },
    'GET /api/house': () => ({ restartable: true, boot_id: boot, why_not: '' }),
    'POST /api/house/restart': () => { boot = 'new'; return { restarting: true }; },
  });
  const { reload } = await mount();
  $('#model-setup-provider').value = 'openrouter';
  $('#model-setup-provider').dispatchEvent(new Event('change'));
  $('#model-setup-model').value = 'v/m';
  $('#model-setup-key').value = 'sk-typed';
  $('.model-setup-form').dispatchEvent(new Event('submit', { cancelable: true }));

  await vi.advanceTimersByTimeAsync(2000);

  const saved = calls.find((c) => c.key === 'POST /api/onboarding');
  expect(saved.body).toMatchObject({ provider: 'openrouter', model: 'v/m', api_key: 'sk-typed' });
  expect(calls.map((c) => c.key)).toContain('POST /api/house/restart');
  expect(reload).toHaveBeenCalledOnce();
});

it('waits for a GGUF download before it restarts anything', async () => {
  let polls = 0;
  let boot = 'old';
  serve({
    'POST /api/onboarding': { ok: true, model: 'gguf/a/B-GGUF', state: 'downloading',
                              detail: 'Downloading gguf/a/B-GGUF…' },
    'GET /api/onboarding': () => ({ ...SETUP, download: polls++ < 2
      ? { state: 'downloading', detail: 'Downloading gguf/a/B-GGUF…' }
      : { state: 'ready', detail: 'Ready' } }),
    'GET /api/house': () => ({ restartable: true, boot_id: boot }),
    'POST /api/house/restart': () => { boot = 'new'; return { restarting: true }; },
  });
  const { reload } = await mount();
  $('.model-recommendation').click();

  await vi.advanceTimersByTimeAsync(1600);
  expect(calls.map((c) => c.key)).not.toContain('POST /api/house/restart');
  expect(status()).toMatch(/one-time download/);

  polls = 5;                                   // the download has finished
  await vi.advanceTimersByTimeAsync(5000);
  expect(calls.map((c) => c.key)).toContain('POST /api/house/restart');
  expect(reload).toHaveBeenCalledOnce();
});

it('says what to do when nothing would start YuriOS again', async () => {
  serve({
    'POST /api/onboarding': { ok: true, model: 'ollama/qwen3', state: 'ready', detail: 'ok' },
    'GET /api/house': { restartable: false, boot_id: 'x',
                        why_not: 'YuriOS is running in the foreground' },
  });
  const { reload, choose } = await mount();
  await choose({ provider: 'custom', model: 'ollama/qwen3' });

  expect(status()).toMatch(/Saved ollama\/qwen3\. YuriOS is running in the foreground/);
  expect(calls.map((c) => c.key)).not.toContain('POST /api/house/restart');
  expect(reload).not.toHaveBeenCalled();
});

it('shows a refused connection and gives the controls back', async () => {
  serve({
    'POST /api/onboarding': [400, { detail: 'reachable, but selected model q is absent' }],
  });
  const { choose } = await mount();
  await choose({ provider: 'custom', model: 'ollama/q' });

  expect(status()).toBe('reachable, but selected model q is absent');
  expect($('.model-setup-primary').disabled).toBe(false);
});

it('offers what a server lists, loaded models first, from the address typed', async () => {
  serve({
    'POST /api/onboarding/models': { models: ['a', 'b'], loaded: ['b'] },
  });
  await mount();
  $('#model-setup-provider').value = 'lmstudio';
  $('#model-setup-provider').dispatchEvent(new Event('change'));
  await vi.advanceTimersByTimeAsync(0);

  const asked = calls.find((c) => c.key === 'POST /api/onboarding/models');
  expect(asked.body).toMatchObject({ provider: 'lmstudio', base_url: 'http://localhost:1234/v1' });
  expect([...document.querySelectorAll('#model-setup-models option')].map((o) => o.value))
    .toEqual(['b', 'a']);
  expect($('#model-setup-model').value).toBe('b');
  expect($('#model-setup-key').closest('label').hidden).toBe(false);
});

it('hides the address and key for a GGUF and leaves the server untouched', async () => {
  serve({});
  await mount();

  expect($('#model-setup-url').closest('label').hidden).toBe(true);
  expect($('#model-setup-key').closest('label').hidden).toBe(true);
  expect(calls).toEqual([]);
});
