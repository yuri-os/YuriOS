/** @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

/* The files tab listing: folders stamp the newest change among their
 * contents, and the directory is newest-first — files and folders in one
 * list, the date is the order.
 */
beforeEach(() => vi.spyOn(window, 'addEventListener'));

afterEach(() => {
  for (const [type, listener, options] of window.addEventListener.mock.calls) {
    window.removeEventListener(type, listener, options);
  }
  vi.restoreAllMocks();
  vi.resetModules();
  vi.unstubAllGlobals();
  delete window.YuriOSRuntime;
  document.body.innerHTML = '';
});

function names() {
  return [...document.querySelectorAll('.fs-row:not([data-up]) .fs-name')]
    .map((el) => el.textContent);
}

it('stamps a folder with the newest content change and lists newest first', async () => {
  document.body.innerHTML = '<div id="files"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (url === '/api/mind/workspace') {
      return {
        ok: true, json: async () => ({
          files: [
            { path: 'goals', bytes: 0, mtime: 900, dir: true },
            { path: 'goals/g-old.md', bytes: 12, mtime: 1000 },
            { path: 'goals/g-new.md', bytes: 40, mtime: 5000 },
            { path: 'alpha.md', bytes: 8, mtime: 4000 },
            { path: 'notes/x.md', bytes: 4, mtime: 2000 },
          ],
        }),
      };
    }
    if (url === '/api/mind/research') {
      return { ok: true, json: async () => ({ files: [] }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/files.js');
  window.dispatchEvent(new Event('files-open'));
  await vi.waitFor(() => expect(document.querySelector('.fs-vol')).not.toBeNull());
  document.querySelector('[data-vol="workspace"]').click();
  await vi.waitFor(() => expect(document.querySelector('.fs-row')).not.toBeNull());

  expect(names()).toEqual(['goals', 'alpha.md', 'notes']);
  const goals = document.querySelector('.fs-row[data-dir="goals"]');
  expect(goals.querySelector('.fs-mtime').textContent).not.toBe('');
  // 5000s after epoch → a real locale date, not the directory's own 900.
  const stamped = new Date(5000 * 1000).toLocaleDateString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  expect(goals.querySelector('.fs-mtime').textContent).toBe(stamped);
  expect(document.querySelector('.fs-row[data-path="alpha.md"] .fs-mtime')
    .textContent).toBe(new Date(4000 * 1000).toLocaleDateString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }));

  goals.click();
  await vi.waitFor(() => expect(names()).toEqual(['g-new.md', 'g-old.md']));
});
