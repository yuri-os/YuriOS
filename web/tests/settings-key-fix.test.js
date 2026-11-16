/** @vitest-environment jsdom */
/**
 * A model picker refused for want of a key takes the key right there (SPEC
 * §31.1) — in her room's brain panel and on the board's pages alike, which all
 * draw the rows of /shared/settings.js. A refusal that only names a setting on
 * another page is the dead end this replaced.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const read = (path) => readFileSync(resolve(process.cwd(), path), 'utf8');
const KEY_FIX = read('shared/key-fix.js');
const SETTINGS = read('shared/settings.js');

const REFUSED = {
  models: [], error: 'LM Studio requires an API key (HTTP 401)',
  key: { provider: 'lmstudio', label: 'LM Studio', refused: false, scope: 'profile',
    profile: 'legacy-default', current: '', key_envs: ['YURIOS_MODEL_API_KEY_LMSTUDIO'],
    shared_with: ['Yuri'], save: '/api/characters/yuri_copy/connection/key' },
};

let calls;
let routes;

beforeEach(() => {
  calls = [];
  routes = {
    'GET /api/models?provider=lmstudio': () => REFUSED,
    'PUT /api/characters/yuri_copy/connection/key': () => ({
      profile: {}, applied: {}, warning: '', listing: { models: ['gemma', 'qwen'] } }),
    'POST /api/settings': () => ({ ok: true, written: ['LMSTUDIO_API_KEY'] }),
  };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const key = `${options.method || 'GET'} ${url}`;
    calls.push({ key, body: options.body && JSON.parse(options.body) });
    const route = routes[key];
    if (!route) throw new Error(`unexpected request: ${key}`);
    return new Response(JSON.stringify(route()), { status: 200 });
  }));
  window.YuriOSRuntime = { apiPath: (p) => p };
  delete window.YuriOSKeyFix;
  delete window.YuriOSSettings;
  // eslint-disable-next-line no-new-func
  new Function(KEY_FIX)();
  // eslint-disable-next-line no-new-func
  new Function(SETTINGS)();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

function chatRow() {
  const form = window.YuriOSSettings.createForm();
  const row = form.brainRow({ key: 'chat_model', type: 'model', value: null,
    inherited: 'lm_studio/old' });
  document.body.append(row);
  return row;
}

async function browse(row) {
  row.querySelector('.set-browse').click();
  await vi.waitFor(() => expect(row.querySelector('.key-fix')).not.toBeNull());
  return row.querySelector('.key-fix');
}

it('offers the key in place, a key already on this node first', async () => {
  const row = chatRow();
  const fix = await browse(row);
  expect(row.querySelector('.set-model-status').textContent)
    .toBe('LM Studio requires an API key (HTTP 401)');
  expect(row.textContent).not.toMatch(/switchboard|Edit connection/);
  const source = fix.querySelector('.key-fix-source');
  expect([...source.options].map((o) => o.value))
    .toEqual(['YURIOS_MODEL_API_KEY_LMSTUDIO', '__new__']);
  expect(source.value).toBe('YURIOS_MODEL_API_KEY_LMSTUDIO');
  expect(fix.querySelector('.key-fix-key').hidden).toBe(true);
  expect(fix.querySelector('.key-fix-note').textContent)
    .toBe('Given to her “legacy-default” connection — Yuri uses it too.');
});

it('points her profile at the chosen key and fills the list in the same step', async () => {
  const row = chatRow();
  const fix = await browse(row);
  fix.querySelector('.key-fix-save').click();
  await vi.waitFor(() => expect(row.querySelector('.set-model-status').textContent)
    .toBe('2 available'));
  expect(calls.at(-1)).toEqual({ key: 'PUT /api/characters/yuri_copy/connection/key',
    body: { api_key_env: 'YURIOS_MODEL_API_KEY_LMSTUDIO', provider: 'lmstudio' } });
  expect(row.querySelector('.key-fix')).toBeNull();
  expect([...row.querySelectorAll('.set-model-opt')].map((b) => b.textContent))
    .toEqual(['gemma', 'qwen']);
});

it('takes a new key typed in place, and Enter saves it rather than the panel', async () => {
  const row = chatRow();
  const fix = await browse(row);
  const source = fix.querySelector('.key-fix-source');
  source.value = '__new__';
  source.dispatchEvent(new Event('change'));
  const typed = fix.querySelector('.key-fix-key');
  expect(typed.hidden).toBe(false);
  typed.value = 'sk-lm';
  const enter = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true });
  typed.dispatchEvent(enter);
  expect(enter.defaultPrevented).toBe(true);
  await vi.waitFor(() => expect(calls.at(-1).body)
    .toEqual({ api_key: 'sk-lm', provider: 'lmstudio' }));
});

it('asks for the key rather than sending an empty one', async () => {
  const row = chatRow();
  const fix = await browse(row);
  const source = fix.querySelector('.key-fix-source');
  source.value = '__new__';
  source.dispatchEvent(new Event('change'));
  fix.querySelector('.key-fix-save').click();
  expect(fix.querySelector('.key-fix-error').textContent).toMatch(/Paste LM Studio's API key/);
  expect(calls.filter((c) => c.key.startsWith('PUT'))).toHaveLength(0);
});

it('does not offer the key that was just refused as the fix', async () => {
  routes['GET /api/models?provider=lmstudio'] = () => ({ ...REFUSED,
    error: 'LM Studio refused the API key it was sent (HTTP 401)',
    key: { ...REFUSED.key, refused: true, current: 'YURIOS_MODEL_API_KEY_LMSTUDIO' } });
  const fix = await browse(chatRow());
  const source = fix.querySelector('.key-fix-source');
  expect(source.value).toBe('__new__');
  expect(source.options[0].textContent).toMatch(/\(refused\)/);
});

it('saves the house key on the house listing, then browses again', async () => {
  let refusals = 1;
  routes['GET /api/models?provider=lmstudio'] = () => (refusals-- > 0
    ? { models: [], error: 'LM Studio requires an API key (HTTP 401)',
      key: { provider: 'lmstudio', label: 'LM Studio', refused: false,
        scope: 'house', field: 'LMSTUDIO_API_KEY' } }
    : { models: ['gemma'] });
  const form = window.YuriOSSettings.createForm();
  const row = form.fieldRow({ key: 'CHAT_MODEL', type: 'model', value: 'lm_studio/old',
    default: '' });
  document.body.append(row);
  const fix = await browse(row);
  expect(fix.querySelector('.key-fix-source').hidden).toBe(true);
  expect(fix.querySelector('.key-fix-note').textContent).toMatch(/LMSTUDIO_API_KEY/);
  fix.querySelector('.key-fix-key').value = 'sk-house';
  fix.querySelector('.key-fix-save').click();
  await vi.waitFor(() => expect(row.querySelector('.set-model-status').textContent)
    .toBe('1 available'));
  expect(calls.find((c) => c.key === 'POST /api/settings').body)
    .toEqual({ LMSTUDIO_API_KEY: 'sk-house' });
});

it('lists what the server has loaded first, marked active', async () => {
  routes['GET /api/models?provider=lmstudio'] = () => ({
    models: ['alpha', 'qwen3.5-9b-yurillm-coser@q4_k_m', 'qwen3.5-9b-yurillm-coser@q8_0', 'zeta'],
    loaded: ['qwen3.5-9b-yurillm-coser@q8_0'] });
  const row = chatRow();                   // her own model is not what "active" means
  row.querySelector('.set-browse').click();
  await vi.waitFor(() => expect(row.querySelectorAll('.set-model-opt')).toHaveLength(4));
  const options = [...row.querySelectorAll('.set-model-opt')];
  expect(options[0].firstChild.textContent).toBe('qwen3.5-9b-yurillm-coser@q8_0');
  expect(options[0].querySelector('.set-model-active').textContent).toBe('active');
  expect(options.slice(1).map((o) => o.textContent))
    .toEqual(['alpha', 'qwen3.5-9b-yurillm-coser@q4_k_m', 'zeta']);
  // picking it puts the bare id in the box, without the mark
  options[0].dispatchEvent(new MouseEvent('mousedown', { cancelable: true }));
  expect(row.querySelector('.set-model-name').value).toBe('qwen3.5-9b-yurillm-coser@q8_0');
});

it('starts a new provider on an empty model box', () => {
  const form = window.YuriOSSettings.createForm();
  const row = form.fieldRow({ key: 'CHAT_MODEL', type: 'model', value: 'lm_studio/gemma',
    default: '' });
  document.body.append(row);
  const box = row.querySelector('.set-model-name');
  expect(box.value).toBe('gemma');
  const provider = row.querySelector('.set-model-provider');
  provider.value = 'openrouter';
  provider.dispatchEvent(new Event('change'));
  expect(box.value).toBe('');
});

it("offers the house's key when her profile's own was refused on the house's address", async () => {
  routes['GET /api/models?provider=lmstudio'] = () => ({ ...REFUSED,
    error: 'LM Studio refused the API key it was sent (HTTP 401)',
    key: { ...REFUSED.key, refused: true, current: 'YURIOS_MODEL_API_KEY_LEGACY_DEFAULT',
      house_key: 'LMSTUDIO_API_KEY', house_key_set: true } });
  const fix = await browse(chatRow());
  const source = fix.querySelector('.key-fix-source');
  expect(source.options[0].textContent).toBe("use LMSTUDIO_API_KEY, the house's key");
  expect(source.value).toBe('__house__');
  fix.querySelector('.key-fix-save').click();
  await vi.waitFor(() => expect(calls.at(-1).body)
    .toEqual({ house_key: true, provider: 'lmstudio' }));
});
