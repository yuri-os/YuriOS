/** @vitest-environment jsdom */
/**
 * The studio's Body page (SPEC §6.8): what each button sends, and where a
 * refusal is said. The library and the choice live on the host; this page only
 * decides which request a click is, so that is what is asserted.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { createBodyPage } from '../studio/bodies.js';

const BUNDLED = { kind: 'vrm', id: 'bundled', title: 'AvatarSample_B', detail: 'VRM 0.x',
  bytes: 0, builtin: true, url: '/models/avatar.vrm?v=2', worn_by: [{ id: 'yuri', name: 'Yuri' }] };
const ARIA = { kind: 'vrm', id: 'aria-abc123', title: 'Aria', detail: 'VRM 1.0', bytes: 2e7,
  builtin: false, url: '/api/bodies/vrm/aria-abc123.vrm?v=1', worn_by: [] };
const HIYORI = { kind: 'live2d', id: 'hiyori', title: 'Hiyori', detail: 'Cubism 3+', bytes: 0,
  builtin: true, url: '/live2d/vendor/hiyori/x.model3.json', worn_by: [{ id: 'yuri', name: 'Yuri' }] };

let calls;
let replies;

function respond(status, body) {
  return { ok: status < 400, status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body };
}

beforeEach(() => {
  calls = [];
  replies = {
    list: { vrm: [BUNDLED, ARIA], live2d: [HIYORI], limits: { vrm: 128 * 2 ** 20, live2d: 256 * 2 ** 20 } },
    worn: { backend: '', desktop_body: 'vrm', vrm: BUNDLED, live2d: HIYORI },
  };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push({ url: String(url), method, body: options.body });
    if (method === 'GET' && url === '/api/bodies') return respond(200, replies.list);
    if (method === 'GET') return respond(200, replies.worn);
    if (method === 'PUT') {
      const choice = JSON.parse(options.body);
      replies.worn = { ...replies.worn, backend: choice.backend ?? replies.worn.backend,
        desktop_body: choice.backend || replies.worn.desktop_body,
        vrm: choice.vrm === ARIA.id ? ARIA : replies.worn.vrm };
      return respond(200, replies.worn);
    }
    if (method === 'POST') return respond(400, { detail: 'that is a glTF model but not a VRM' });
    if (method === 'DELETE') return respond(200, { ...replies.list, vrm: [BUNDLED] });
    return respond(404, {});
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

const el = (tag, options = {}, ...children) => {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text != null) node.textContent = options.text;
  for (const [k, v] of Object.entries(options.attrs || {})) {
    if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
};

async function open({ preview = null } = {}) {
  const toast = vi.fn();
  const page = createBodyPage({ characterId: 'yuri', element: el,
    icon: () => document.createElement('i'), toast, preview });
  document.body.append(page.control());
  await vi.waitFor(() => expect(document.querySelector('.body-row')).not.toBeNull());
  return { toast };
}

const row = (title) => [...document.querySelectorAll('.body-row')]
  .find((node) => node.querySelector('strong').textContent === title);

it('shows what she wears and lets her wear something else, floating it', async () => {
  await open();
  expect(row('AvatarSample_B').querySelector('.button').textContent).toBe('Wearing');

  row('Aria').querySelector('.button-primary').click();
  await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));

  const put = calls.find((c) => c.method === 'PUT');
  expect(put.url).toBe('/api/characters/yuri/body');
  // Wearing a body is choosing its kind for the desktop window too.
  expect(JSON.parse(put.body)).toEqual({ vrm: ARIA.id, backend: 'vrm' });
  await vi.waitFor(() => expect(row('Aria').querySelector('.button').textContent).toBe('Wearing'));
});

it('removes a body only on the second click, and never a builtin', async () => {
  await open();
  expect(row('AvatarSample_B').querySelector('.body-remove')).toBeNull();

  row('Aria').querySelector('.body-remove').click();
  await vi.waitFor(() => expect(row('Aria').querySelector('.body-remove.armed')).not.toBeNull());
  expect(calls.some((c) => c.method === 'DELETE')).toBe(false);

  row('Aria').querySelector('.body-remove').click();
  await vi.waitFor(() => expect(calls.some((c) => c.method === 'DELETE'
    && c.url === '/api/bodies/vrm/aria-abc123')).toBe(true));
  await vi.waitFor(() => expect(row('Aria')).toBeUndefined());
});

it('says why an upload was refused beside that upload', async () => {
  await open();
  const shelf = document.querySelector('.body-shelf[data-kind="vrm"]');
  const input = shelf.querySelector('input[type="file"]');
  Object.defineProperty(input, 'files', { value: [new File(['x'], 'shibahu.glb')] });
  input.dispatchEvent(new Event('change'));

  await vi.waitFor(() => expect(
    document.querySelector('.body-shelf[data-kind="vrm"] .form-error').textContent)
    .toContain('not a VRM'));
  expect(document.querySelector('.body-shelf[data-kind="live2d"] .form-error').textContent).toBe('');
  expect(calls.some((c) => c.method === 'PUT')).toBe(false);
});

it('switches which body floats on its own', async () => {
  await open();
  const live2d = [...document.querySelectorAll('.body-float .seg button')]
    .find((b) => b.textContent === 'Live2D');
  live2d.click();
  await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
  expect(JSON.parse(calls.find((c) => c.method === 'PUT').body)).toEqual({ backend: 'live2d' });
});

it('previews the body she floats, then whichever one is clicked, without wearing it', async () => {
  const preview = { node: document.createElement('div'), show: vi.fn() };
  await open({ preview });
  expect(preview.show).toHaveBeenLastCalledWith(BUNDLED);
  expect(document.querySelector('.body-preview-caption small').textContent).toContain('she wears this');

  row('Aria').querySelector('.body-copy').click();
  expect(preview.show).toHaveBeenLastCalledWith(ARIA);
  expect(row('Aria').classList.contains('previewing')).toBe(true);
  expect(document.querySelector('.body-preview-caption small').textContent).toContain('not worn');
  expect(calls.some((c) => c.method === 'PUT')).toBe(false);

  row('Hiyori').querySelector('.body-copy').click();
  expect(preview.show).toHaveBeenLastCalledWith(HIYORI);
});
