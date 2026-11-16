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
    { name: 'default', endpoint: 'http://localhost:1234/v1', api_key_env: '', secret_configured: false, used_by: ['yuri', 'iris'] },
    { name: 'spare', endpoint: 'http://gpu.lan:1234/v1', api_key_env: '', secret_configured: false, used_by: [] },
  ];
  routes = {
    'GET /api/characters': () => json({ characters: [{ id: 'yuri', name: 'Yuri', connection_profile: 'default' }] }),
    'GET /api/characters/yuri/profile': () => json({ settings: { name: 'Yuri', connection_profile: 'default' } }),
    'GET /api/connections': () => json({ profiles }),
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

describe('her connection, beside the picker', () => {
  it('says where she points and that no key goes with it', () => {
    expect(document.getElementById('connection-summary').textContent)
      .toBe('http://localhost:1234/v1 · no API key');
  });

  it('saves a key typed on the board, checks her server, and never shows the key again', async () => {
    document.getElementById('connection-edit').click();
    expect(document.getElementById('connection-dialog').open).toBe(true);
    expect(document.getElementById('connection-key-note').textContent).toMatch(/2 characters use this profile/);
    form().elements.api_key.value = 'sk-lm-secret';
    form().requestSubmit();

    await vi.waitFor(() => expect(sent('GET /api/connections/default/check?character=yuri')).toHaveLength(1));
    expect(JSON.parse(sent('PUT /api/connections/default')[0].body)).toEqual({
      endpoint: 'http://localhost:1234/v1', api_key_env: '', api_key: 'sk-lm-secret',
    });
    await vi.waitFor(() => expect(document.getElementById('connection-dialog').open).toBe(false));
    expect(document.getElementById('connection-summary').textContent).toContain('API key saved');
    expect(document.getElementById('toast-region').textContent).toContain('LM Studio at http://localhost:1234/v1 answered — 2 models');
    expect(form().elements.api_key.value).toBe('');
  });

  it('keeps the dialog open with the reason when her server still refuses', async () => {
    routes['GET /api/connections/default/check?character=yuri'] = () => json({
      provider: 'lmstudio', url: 'http://localhost:1234/v1', models: [],
      error: 'LM Studio refused the API key it was sent (HTTP 401)',
    });
    document.getElementById('connection-edit').click();
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
