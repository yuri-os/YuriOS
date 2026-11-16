/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { describeBackup, formatBytes, normalizeSnapshots } from '../dashboard/model.js';

const PAGE = readFileSync(resolve(process.cwd(), 'dashboard/index.html'), 'utf8');
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
});
const SNAPSHOT = {
  id: 'yuri', name: 'Yuri', snapshot: 'yuri-20261116-100000.zip', created_at: '2026-11-16T10:00:00+00:00',
  size: 3 * 1024 * 1024, files: 812, bodies: [{ kind: 'vrm', title: 'Aria' }],
};
let calls;
let downloads;
let routes;

class FakeXHR {
  static last = null;
  constructor() {
    this.upload = { listeners: {}, addEventListener: (type, fn) => { this.upload.listeners[type] = fn; } };
    this.listeners = {};
    this.headers = {};
    FakeXHR.last = this;
  }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(key, value) { this.headers[key] = value; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  send(body) {
    this.body = body;
    this.upload.listeners.progress?.({ lengthComputable: true, loaded: 1, total: 1 });
    this.status = 200;
    this.responseText = JSON.stringify({
      upload: 'tok', exists: true, existing_name: 'Yuri',
      backup: { id: 'yuri', name: 'Yuri', created_at: '2026-11-01T09:00:00+00:00', bytes: 2048, files: 3, bodies: [] },
    });
    queueMicrotask(() => this.listeners.load());
  }
}

beforeEach(async () => {
  document.body.innerHTML = PAGE.slice(PAGE.indexOf('<body'), PAGE.lastIndexOf('</body>'))
    .replace(/^<body[^>]*>/, '').replace(/<script[\s\S]*?<\/script>/g, '');
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  };
  downloads = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    downloads.push({ href: this.getAttribute('href'), filename: this.download });
  });
  calls = [];
  routes = {
    'GET /api/characters': () => json({ characters: [{ id: 'yuri', name: 'Yuri' }] }),
    'GET /api/characters/yuri/snapshots': () => json({ snapshots: [SNAPSHOT] }),
    'POST /api/characters/yuri/snapshots': () => json({ snapshot: SNAPSHOT, source_error: null }, 201),
    'POST /api/characters/yuri/backup': () => json({
      url: '/api/backups/downloads/abc', filename: 'yurios-yuri-20261116-100000.zip', size: 4096,
    }),
    [`POST /api/characters/yuri/snapshots/${SNAPSHOT.snapshot}/restore`]: () => json({
      character: { id: 'yuri', name: 'Yuri' }, started: false, archived: 'yuri-20261116-110000', warnings: [],
    }, 201),
    'POST /api/characters/yuri/clone': () => json({ character: { id: 'yuri_copy', name: 'Yuri (copy)' } }, 201),
    'GET /api/archives': () => json({ archives: [] }),
    'GET /api/snapshots': () => json({ snapshots: [] }),
    'POST /api/backups/uploads/tok/restore': () => json({
      character: { id: 'yuri', name: 'Yuri' }, archived: 'yuri-20261116-120000', warnings: [],
    }, 201),
  };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const key = `${options.method || 'GET'} ${url}`;
    calls.push({ key, body: options.body });
    return (routes[key] ?? (() => json({})))();
  }));
  vi.stubGlobal('XMLHttpRequest', FakeXHR);
  vi.resetModules();
  await import('../dashboard/dashboard.js');
  await vi.waitFor(() => expect(document.querySelector('[data-character-id="yuri"]')).not.toBeNull());
  document.querySelector('[data-character-id="yuri"] [data-action="details"]').click();
  document.querySelector('[data-tab="backups"]').click();
  await vi.waitFor(() => expect(document.querySelector('[data-snapshot]')).not.toBeNull());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

const action = (name) => document.querySelector(`[data-backup-action="${name}"]`);
const posted = (key) => calls.filter((call) => call.key === key);

describe('the Backups tab', () => {
  it('lists her snapshots with what each one holds', () => {
    const row = document.querySelector('[data-snapshot]');
    expect(row.dataset.snapshot).toBe(SNAPSHOT.snapshot);
    expect(row.textContent).toContain('3.0 MB');
    expect(row.textContent).toContain('812 files');
    expect(row.textContent).toContain('wearing Aria');
  });

  it('takes a snapshot and reads the shelf again', async () => {
    action('snapshot').click();
    await vi.waitFor(() => expect(posted('POST /api/characters/yuri/snapshots')).toHaveLength(1));
    await vi.waitFor(() => expect(posted('GET /api/characters/yuri/snapshots').length).toBeGreaterThan(1));
  });

  it('downloads a backup through the URL the host prepared, not a blob', async () => {
    action('backup').click();
    await vi.waitFor(() => expect(downloads).toHaveLength(1));
    expect(downloads[0]).toEqual({
      href: '/api/backups/downloads/abc', filename: 'yurios-yuri-20261116-100000.zip',
    });
  });

  it('rolls back only on the second click, in her place', async () => {
    const key = `POST /api/characters/yuri/snapshots/${SNAPSHOT.snapshot}/restore`;
    action('rollback').click();
    await Promise.resolve();
    expect(posted(key)).toHaveLength(0);
    expect(action('rollback').textContent).toMatch(/again/i);
    action('rollback').click();
    await vi.waitFor(() => expect(posted(key)).toHaveLength(1));
    expect(JSON.parse(posted(key)[0].body)).toEqual({ mode: 'replace' });
  });

  it('restores a snapshot as a copy in one click', async () => {
    const key = `POST /api/characters/yuri/snapshots/${SNAPSHOT.snapshot}/restore`;
    action('restore-copy').click();
    await vi.waitFor(() => expect(posted(key)).toHaveLength(1));
    expect(JSON.parse(posted(key)[0].body)).toEqual({ mode: 'new' });
  });

  it('clones her under the name typed', async () => {
    const form = document.querySelector('[data-clone-form]');
    expect(form.elements.name.value).toBe('Yuri (copy)');
    form.elements.name.value = 'Yuri two';
    form.requestSubmit();
    await vi.waitFor(() => expect(posted('POST /api/characters/yuri/clone')).toHaveLength(1));
    expect(JSON.parse(posted('POST /api/characters/yuri/clone')[0].body)).toEqual({ name: 'Yuri two' });
  });
});

describe('the restore dialog', () => {
  it('reads an upload first, then offers both choices side by side', async () => {
    document.getElementById('restore-open').click();
    const input = document.getElementById('backup-file');
    const file = new File(['zip'], 'yurios-yuri.zip', { type: 'application/zip' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    document.getElementById('restore-form').requestSubmit();

    await vi.waitFor(() => expect(document.getElementById('restore-found').hidden).toBe(false));
    expect(FakeXHR.last.url).toBe('/api/backups/uploads');
    expect(FakeXHR.last.body).toBe(file);
    expect(posted('POST /api/backups/uploads/tok/restore')).toHaveLength(0);
    const replace = document.getElementById('restore-replace');
    expect(replace.hidden).toBe(false);
    expect(replace.textContent).toContain('Replace Yuri');
    expect(document.getElementById('restore-found-copy').textContent).toMatch(/under review/);

    replace.click();
    await vi.waitFor(() => expect(posted('POST /api/backups/uploads/tok/restore')).toHaveLength(1));
    expect(JSON.parse(posted('POST /api/backups/uploads/tok/restore')[0].body)).toEqual({ mode: 'replace' });
    await vi.waitFor(() => expect(document.getElementById('restore-dialog').open).toBe(false));
  });

  it('refuses something that is not a backup before uploading it', async () => {
    document.getElementById('restore-open').click();
    const input = document.getElementById('backup-file');
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'card.png')], configurable: true });
    FakeXHR.last = null;
    document.getElementById('restore-form').requestSubmit();
    await Promise.resolve();
    expect(FakeXHR.last).toBeNull();
    expect(document.getElementById('restore-error').textContent).toMatch(/\.zip/);
  });
});

describe('model helpers', () => {
  it('formats sizes the way a file manager would', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(400 * 1024 * 1024)).toBe('400 MB');
    expect(formatBytes(-1)).toBe('');
  });

  it('keeps an unreadable snapshot with its reason, and drops a row with no file', () => {
    const rows = normalizeSnapshots({ snapshots: [
      { id: 'yuri', snapshot: 'yuri-1.zip', error: 'not a zip' }, { id: 'yuri' },
    ] });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ file: 'yuri-1.zip', error: 'not a zip', onBoard: true });
  });

  it('sizes a backup by its file when it knows it, as the drawer does', () => {
    expect(describeBackup({ bytes: 2048, size: 3 * 1024 * 1024 })).toBe('3.0 MB');
  });

  it('describes a backup by what it holds', () => {
    const text = describeBackup({ created_at: '2026-11-01T09:00:00Z', bytes: 2048, files: 3,
      bodies: [{ title: 'Aria' }] });
    expect(text).toContain('2.0 KB');
    expect(text).toContain('3 files');
    expect(text).toContain('wearing Aria');
  });
});
