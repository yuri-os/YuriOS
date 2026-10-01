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

it('adds documents to the shelf one at a time and says what each came to', async () => {
  document.body.innerHTML = '<div id="files"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path };
  const shelf = [];
  const posted = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (url === '/api/mind/workspace') {
      return { ok: true, json: async () => ({ files: [] }) };
    }
    if (url === '/api/mind/research' && init?.method === 'POST') {
      const file = init.body.get('file');
      posted.push(file.name);
      if (file.name.endsWith('.docx')) {
        return {
          ok: false,
          text: async () => JSON.stringify({ detail: 'she reads .md, .txt and .pdf files' }),
        };
      }
      // A PDF goes on the shelf as the .md of its text.
      const name = file.name.replace(/\.pdf$/, '.md');
      shelf.push({ name, bytes: file.size, mtime: 5000 + shelf.length });
      return {
        ok: true,
        json: async () => ({ name, calls: 4, replaced: false, digested: false }),
      };
    }
    if (url === '/api/mind/research') {
      return { ok: true, json: async () => ({ files: [...shelf] }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/files.js');
  window.dispatchEvent(new Event('files-open'));
  await vi.waitFor(() => expect(document.querySelector('.fs-vol')).not.toBeNull());
  // The mount table takes nothing: a document goes to a volume.
  expect(document.querySelector('.fs-add')).toBeNull();
  document.querySelector('[data-vol="shelf"]').click();

  const pick = document.querySelector('.fs-pick');
  const clicked = vi.spyOn(pick, 'click').mockImplementation(() => {});
  document.querySelector('.fs-add').click();
  expect(clicked).toHaveBeenCalled();

  Object.defineProperty(pick, 'files', {
    value: [new File(['PK'], 'paper.docx'), new File(['# Tea'], 'tea.md'),
      new File(['%PDF'], 'field notes.pdf')],
  });
  pick.dispatchEvent(new Event('change', { bubbles: true }));

  await vi.waitFor(() => expect(names()).toEqual(['field notes.md', 'tea.md']));
  expect(posted).toEqual(['paper.docx', 'tea.md', 'field notes.pdf']);
  const notes = [...document.querySelectorAll('.fs-shelf-note span')];
  expect(notes.map((n) => n.textContent)).toEqual([
    'paper.docx: she reads .md, .txt and .pdf files',
    'tea.md · shelved · about 4 model calls',
    'field notes.pdf → field notes.md · shelved · about 4 model calls',
  ]);
  expect(notes[0].classList.contains('fs-error')).toBe(true);
  expect(document.querySelector('.fs-add').disabled).toBe(false);
});

it('hands a document to her desk inbox and says when she will read it', async () => {
  document.body.innerHTML = '<div id="files"></div>';
  window.YuriOSRuntime = { apiPath: (path) => path };
  const desk = [];
  const posted = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (url === '/api/mind/workspace/inbox' && init?.method === 'POST') {
      const file = init.body.get('file');
      posted.push(file.name);
      const path = `inbox/${file.name.replace(/\.pdf$/, '.md').replace(/ /g, '_')}`;
      desk.push({ path: 'inbox', bytes: 0, mtime: 4000, dir: true },
        { path, bytes: file.size, mtime: 5000 });
      return {
        ok: true,
        json: async () => ({ path, name: path.slice(6), noticed: true,
          wakes: !file.name.startsWith('quiet'), replaced: false }),
      };
    }
    if (url === '/api/mind/workspace') return { ok: true, json: async () => ({ files: [...desk] }) };
    if (url === '/api/mind/research') return { ok: true, json: async () => ({ files: [] }) };
    throw new Error(`unexpected request: ${url}`);
  }));

  await import('../js/files.js');
  window.dispatchEvent(new Event('files-open'));
  await vi.waitFor(() => expect(document.querySelector('.fs-vol')).not.toBeNull());
  document.querySelector('[data-vol="workspace"]').click();
  expect(document.querySelector('.fs-shelf-note').textContent).toContain('lands in inbox/');

  const pick = document.querySelector('.fs-pick');
  Object.defineProperty(pick, 'files', {
    value: [new File(['%PDF'], 'Q3 numbers.pdf'), new File(['# hi'], 'quiet note.md')],
  });
  pick.dispatchEvent(new Event('change', { bubbles: true }));

  await vi.waitFor(() => expect(names()).toEqual(['inbox']));
  expect(posted).toEqual(['Q3 numbers.pdf', 'quiet note.md']);
  expect([...document.querySelectorAll('.fs-shelf-note span')].map((n) => n.textContent)).toEqual([
    "Q3 numbers.pdf → inbox/Q3_numbers.md · she'll read it now",
    "quiet note.md → inbox/quiet_note.md · she'll read it when you're back",
  ]);
});
