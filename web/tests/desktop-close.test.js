/** @vitest-environment jsdom */
// The desktop window's ✕ (SPEC §6.5): a frameless window has no title bar, so
// the page is the only place a way out can be.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

// A fresh evaluation each time: the script decides once, at load, whether it
// is in a desktop window.
const load = () => {
  vi.resetModules();
  return import('../shared/desktop-close.js');
};
const button = () => document.querySelector('.desktop-close');

beforeEach(() => {
  document.body.innerHTML = '';
  document.documentElement.classList.remove('desktop');
});
afterEach(() => {
  delete window.YuriOSRuntime;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it('stays out of a page in an ordinary browser tab', async () => {
  await load();
  expect(button()).toBeNull();
});

it('puts a labelled close button on a desktop window', async () => {
  document.documentElement.classList.add('desktop');
  await load();
  expect(button()).not.toBeNull();
  expect(button().getAttribute('aria-label')).toBe('Close this window');
});

it('asks the host to end the window it spawned', async () => {
  document.documentElement.classList.add('desktop');
  window.YuriOSRuntime = { characterId: 'yuri', apiPath: (p) => p.replace('/api/', '/api/characters/yuri/') };
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ closed: true }) }));
  vi.stubGlobal('fetch', fetch);
  const fallback = vi.spyOn(window, 'close').mockImplementation(() => {});
  await load();
  button().click();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  expect(fetch).toHaveBeenCalledWith('/api/characters/yuri/desktop', { method: 'DELETE' });
  await new Promise((resolve) => setTimeout(resolve));
  expect(fallback).not.toHaveBeenCalled();
});

it('falls back to window.close for a window the host did not open', async () => {
  document.documentElement.classList.add('desktop');
  window.YuriOSRuntime = { characterId: 'yuri', apiPath: (p) => p };
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ closed: false }) })));
  const fallback = vi.spyOn(window, 'close').mockImplementation(() => {});
  await load();
  button().click();
  await vi.waitFor(() => expect(fallback).toHaveBeenCalledOnce());
});

it('falls back to window.close when the host cannot be reached', async () => {
  document.documentElement.classList.add('desktop');
  window.YuriOSRuntime = { characterId: 'yuri', apiPath: (p) => p };
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('connection reset'); }));
  const fallback = vi.spyOn(window, 'close').mockImplementation(() => {});
  await load();
  button().click();
  await vi.waitFor(() => expect(fallback).toHaveBeenCalledOnce());
});
