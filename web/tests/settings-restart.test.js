/** @vitest-environment jsdom */
/**
 * A save in the room's settings dialog that waits for a restart offers the
 * restart right there (SPEC §11.3), rather than leaving you to find out how —
 * and says how instead when nothing would start YuriOS again.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const read = (path) => readFileSync(resolve(process.cwd(), path), 'utf8');
const SETTINGS = read('shared/settings.js');

const DIALOG = `
  <button id="settings-open" type="button"></button>
  <dialog id="settings"><form id="settings-form">
    <span id="settings-path"></span><button type="button" id="settings-close"></button>
    <div id="settings-body"></div>
    <footer class="settings-foot">
      <span class="settings-note" id="settings-note"></span>
      <button type="button" id="settings-save">save</button>
    </footer>
  </form></dialog>`;

let house;
let calls;

beforeEach(async () => {
  document.body.innerHTML = DIALOG;
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
  calls = [];
  house = { restartable: true, boot_id: 'a', why_not: '' };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const key = `${options.method || 'GET'} ${url}`;
    calls.push(key);
    const body = {
      'GET /api/settings': { env_path: '/x/.env', groups: [{ title: 'You', advanced: false,
        fields: [{ key: 'USER_NAME', type: 'text', value: 'Sam', default: 'you' }] }] },
      'POST /api/settings': { ok: true, written: ['USER_NAME'], restart_required: true },
      'GET /api/house': house,
      'POST /api/house/restart': { restarting: true, boot_id: 'a' },
    }[key];
    if (key === 'GET /api/brain') return new Response('{}', { status: 404 });
    // the next boot answers with a new id, which is what ends the wait
    if (key === 'POST /api/house/restart') house = { ...house, boot_id: 'b' };
    if (!body) throw new Error(`unexpected request: ${key}`);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  window.YuriOSRuntime = { apiPath: (p) => p };
  delete window.YuriOSSettings;
  // eslint-disable-next-line no-new-func
  new Function(SETTINGS)();
  document.getElementById('settings-open').click();
  await vi.waitFor(() => expect(document.getElementById('set-USER_NAME')).not.toBeNull());
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

async function saveAName() {
  const input = document.getElementById('set-USER_NAME');
  input.value = 'Alex';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  document.getElementById('settings-save').click();
  await vi.waitFor(() => expect(calls).toContain('GET /api/house'));
}

const restartButton = () => document.querySelector('.settings-restart');

it('offers the restart beside the save that needs one', async () => {
  expect(restartButton().hidden).toBe(true);
  await saveAName();
  await vi.waitFor(() => expect(restartButton().hidden).toBe(false));
  expect(document.getElementById('settings-note').textContent)
    .toBe('1 .env setting saved — restart to apply');
  expect(restartButton().nextElementSibling.id).toBe('settings-save');
  restartButton().click();
  await vi.waitFor(() => expect(calls).toContain('POST /api/house/restart'));
  expect(document.getElementById('settings-note').textContent).toMatch(/restarting YuriOS/);
  // it waits for the new boot, then reloads into it
  await vi.waitFor(() => expect(calls.filter((c) => c === 'GET /api/house').length)
    .toBeGreaterThanOrEqual(3), { timeout: 3000 });
});

it('says how when nothing would start YuriOS again', async () => {
  house = { restartable: false, boot_id: 'a', why_not: 'it runs in the foreground' };
  await saveAName();
  await vi.waitFor(() => expect(document.getElementById('settings-note').textContent)
    .toBe('1 .env setting saved — restart to apply — it runs in the foreground'));
  expect(restartButton().hidden).toBe(true);
});
