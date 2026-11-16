/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const PAGE = readFileSync(resolve(process.cwd(), 'dashboard/index.html'), 'utf8');
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
});
let calls;
let profiles;
let routes;
let keyEnvs;

beforeEach(async () => {
  document.body.innerHTML = PAGE.slice(PAGE.indexOf('<body'), PAGE.lastIndexOf('</body>'))
    .replace(/^<body[^>]*>/, '').replace(/<script[\s\S]*?<\/script>/g, '');
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  };
  calls = [];
  profiles = [
    { name: 'default', endpoint: 'http://localhost:1234/v1', api_key_env: '', secret_configured: false,
      house_key: 'LMSTUDIO_API_KEY', house_key_set: false, used_by: ['yuri', 'iris'] },
    { name: 'spare', endpoint: 'http://gpu.lan:1234/v1', api_key_env: '', secret_configured: false,
      house_key: '', house_key_set: false, used_by: [] },
    { name: 'legacy', endpoint: 'http://localhost:1234/v1', api_key_env: 'YURIOS_MODEL_API_KEY_LEGACY',
      secret_configured: true, house_key: 'LMSTUDIO_API_KEY', house_key_set: true, used_by: [] },
  ];
  keyEnvs = ['YURIOS_MODEL_API_KEY_LEGACY', 'YURIOS_MODEL_API_KEY_LMSTUDIO'];
  routes = {
    'GET /api/characters': () => json({ characters: [{ id: 'yuri', name: 'Yuri', connection_profile: 'default' }] }),
    'GET /api/characters/yuri/profile': () => json({ settings: { name: 'Yuri', connection_profile: 'default' } }),
    'GET /api/connections': () => json({ profiles, key_envs: keyEnvs }),
    'DELETE /api/connections/keys/YURIOS_MODEL_API_KEY_LEGACY?profile=legacy': () => {
      profiles[2] = { ...profiles[2], api_key_env: '', secret_configured: false };
      keyEnvs = ['YURIOS_MODEL_API_KEY_LMSTUDIO'];
      return json({ forgotten: 'YURIOS_MODEL_API_KEY_LEGACY', removed: true, warning: '' });
    },
    'GET /api/connections/legacy/check?character=yuri': () => json({
      provider: 'lmstudio', url: 'http://localhost:1234/v1', models: ['gemma'],
    }),
    'PUT /api/connections/default': () => {
      profiles[0] = { ...profiles[0], api_key_env: 'YURIOS_MODEL_API_KEY_DEFAULT', secret_configured: true };
      return json({ profile: profiles[0], applied: {}, warning: '' });
    },
    'GET /api/connections/default/check?character=yuri': () => json({
      provider: 'lmstudio', url: 'http://localhost:1234/v1', models: ['gemma', 'qwen'],
    }),
    'DELETE /api/connections/spare': () => {
      profiles = profiles.filter((profile) => profile.name !== 'spare');
      return json({ deleted: 'spare' });
    },
  };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const key = `${options.method || 'GET'} ${url}`;
    calls.push({ key, body: options.body });
    return (routes[key] ?? (() => json({})))();
  }));
  vi.resetModules();
  await import('../dashboard/dashboard.js');
  await vi.waitFor(() => expect(document.querySelector('[data-character-id="yuri"]')).not.toBeNull());
  document.querySelector('[data-character-id="yuri"] [data-action="details"]').click();
  document.getElementById('profile-open').click();
  await vi.waitFor(() => expect(document.getElementById('connection-summary').textContent).toContain('localhost:1234'));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

const sent = (key) => calls.filter((call) => call.key === key);
const form = () => document.getElementById('connection-form');
const choose = (value) => {
  form().elements.key_source.value = value;
  form().elements.key_source.dispatchEvent(new Event('change'));
};

describe('her connection, beside the picker', () => {
  it('says where she points and that no key goes with it', () => {
    expect(document.getElementById('connection-summary').textContent)
      .toBe('http://localhost:1234/v1 · no API key (LMSTUDIO_API_KEY is not set)');
  });

  it("offers the house's key by name on the house's own address", () => {
    document.getElementById('connection-edit').click();
    expect(form().elements.key_source.options[0].textContent)
      .toBe("LMSTUDIO_API_KEY — the house's key for this address (not set)");
  });

  it('forgets a saved key no other profile uses, on the second press', async () => {
    const picker = document.getElementById('profile-form').elements.connection_profile;
    picker.value = 'legacy';
    picker.dispatchEvent(new Event('change'));
    document.getElementById('connection-edit').click();
    expect(form().elements.key_source.value).toBe('YURIOS_MODEL_API_KEY_LEGACY');
    const forget = document.getElementById('connection-key-forget');
    expect(forget.hidden).toBe(false);
    forget.click();
    expect(calls.some((call) => call.key.startsWith('DELETE'))).toBe(false);
    forget.click();
    await vi.waitFor(() => expect(form().elements.key_source.value).toBe(''));
    expect([...form().elements.key_source.options].map((o) => o.value))
      .toEqual(['', 'YURIOS_MODEL_API_KEY_LMSTUDIO', '__new__']);
    expect(document.getElementById('connection-summary').textContent)
      .toBe('http://localhost:1234/v1 · house key: LMSTUDIO_API_KEY');
  });

  it('never offers to forget a key another profile points at', () => {
    document.getElementById('connection-edit').click();          // default, not legacy
    choose('YURIOS_MODEL_API_KEY_LEGACY');
    expect(document.getElementById('connection-key-forget').hidden).toBe(true);
    choose('YURIOS_MODEL_API_KEY_LMSTUDIO');
    expect(document.getElementById('connection-key-forget').hidden).toBe(false);
  });

  it('saves a key typed on the board, checks her server, and never shows the key again', async () => {
    document.getElementById('connection-edit').click();
    expect(document.getElementById('connection-dialog').open).toBe(true);
    expect(document.getElementById('connection-key-note').textContent).toMatch(/2 characters use this profile/);
    expect(document.getElementById('connection-new-key').hidden).toBe(true);
    choose('__new__');
    expect(document.getElementById('connection-new-key').hidden).toBe(false);
    form().elements.api_key.value = 'sk-lm-secret';
    form().requestSubmit();

    await vi.waitFor(() => expect(sent('GET /api/connections/default/check?character=yuri')).toHaveLength(1));
    expect(JSON.parse(sent('PUT /api/connections/default')[0].body)).toEqual({
      endpoint: 'http://localhost:1234/v1', api_key_env: '', api_key: 'sk-lm-secret',
    });
    await vi.waitFor(() => expect(document.getElementById('connection-dialog').open).toBe(false));
    expect(document.getElementById('connection-summary').textContent).toContain('key: YURIOS_MODEL_API_KEY_DEFAULT');
    expect(document.getElementById('toast-region').textContent).toContain('LM Studio at http://localhost:1234/v1 answered — 2 models');
    expect(form().elements.api_key.value).toBe('');
  });

  it('keeps the dialog open with the reason when her server still refuses', async () => {
    routes['GET /api/connections/default/check?character=yuri'] = () => json({
      provider: 'lmstudio', url: 'http://localhost:1234/v1', models: [],
      error: 'LM Studio refused the API key it was sent (HTTP 401)',
    });
    document.getElementById('connection-edit').click();
    choose('__new__');
    form().elements.api_key.value = 'sk-wrong';
    form().requestSubmit();
    await vi.waitFor(() => expect(document.getElementById('connection-check').textContent).toMatch(/refused the API key/));
    expect(document.getElementById('connection-dialog').open).toBe(true);
  });

  it('sends no key at all when the field is left blank', async () => {
    document.getElementById('connection-edit').click();
    form().elements.endpoint.value = 'http://localhost:1235/v1';
    form().requestSubmit();
    await vi.waitFor(() => expect(sent('PUT /api/connections/default')).toHaveLength(1));
    expect(JSON.parse(sent('PUT /api/connections/default')[0].body)).not.toHaveProperty('api_key');
  });

  it('points the profile at a key already on this node, without its value', async () => {
    document.getElementById('connection-edit').click();
    const options = [...form().elements.key_source.options].map((option) => option.value);
    expect(options).toEqual(['', 'YURIOS_MODEL_API_KEY_LEGACY', 'YURIOS_MODEL_API_KEY_LMSTUDIO', '__new__']);
    choose('YURIOS_MODEL_API_KEY_LMSTUDIO');
    expect(document.getElementById('connection-new-key').hidden).toBe(true);
    form().requestSubmit();
    await vi.waitFor(() => expect(sent('PUT /api/connections/default')).toHaveLength(1));
    expect(JSON.parse(sent('PUT /api/connections/default')[0].body)).toEqual({
      endpoint: 'http://localhost:1234/v1', api_key_env: 'YURIOS_MODEL_API_KEY_LMSTUDIO',
    });
  });

  it('asks for the key when a new one was chosen and none was typed', async () => {
    document.getElementById('connection-edit').click();
    choose('__new__');
    form().requestSubmit();
    await Promise.resolve();
    expect(document.getElementById('connection-error').textContent).toMatch(/Paste the server's key/);
    expect(sent('PUT /api/connections/default')).toHaveLength(0);
  });

  it('offers to delete only a profile nobody uses, and only on the second press', async () => {
    document.getElementById('connection-edit').click();
    expect(document.getElementById('connection-delete').hidden).toBe(true);
    document.getElementById('connection-dialog').close();

    const picker = document.getElementById('profile-form').elements.connection_profile;
    picker.value = 'spare';
    picker.dispatchEvent(new Event('change'));
    document.getElementById('connection-edit').click();
    const remove = document.getElementById('connection-delete');
    expect(remove.hidden).toBe(false);
    remove.click();
    expect(sent('DELETE /api/connections/spare')).toHaveLength(0);
    remove.click();
    await vi.waitFor(() => expect(sent('DELETE /api/connections/spare')).toHaveLength(1));
  });

  it('refuses a new profile under a name already taken', async () => {
    document.getElementById('connection-new').click();
    form().elements.name.value = 'spare';
    form().requestSubmit();
    await Promise.resolve();
    expect(document.getElementById('connection-error').textContent).toMatch(/already a profile named spare/);
    expect(calls.some((call) => call.key.startsWith('PUT'))).toBe(false);
  });
});
