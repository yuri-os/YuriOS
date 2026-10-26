/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const PAGE = readFileSync(resolve(process.cwd(), 'dashboard/index.html'), 'utf8');
const exportResponse = vi.fn();
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
});
const png = () => new Response('png bytes', { headers: {
  'content-type': 'image/png', 'content-disposition': 'attachment; filename="yuri.png"',
} });
const review = () => json({ detail: {
  code: 'review_required', detail: 'Read these passages before sharing.',
  overlaps: [{ surface: 'soul/PERSONA.md', excerpt: '<private passage>' }],
} }, 422);
let downloads;

beforeEach(async () => {
  document.body.innerHTML = PAGE.slice(PAGE.indexOf('<body'), PAGE.lastIndexOf('</body>'))
    .replace(/^<body[^>]*>/, '').replace(/<script[\s\S]*?<\/script>/g, '');
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  };
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:export');
    static revokeObjectURL = vi.fn();
  });
  downloads = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    downloads.push({ href: this.href, filename: this.download });
  });
  exportResponse.mockReset().mockImplementation(async () => png());
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (url.endsWith('/export')) return exportResponse(url, options);
    if (url === '/api/characters') return json({ characters: [
      { id: 'yuri', name: 'Yuri' }, { id: 'other', name: 'Other' },
    ] });
    return json({});
  }));
  vi.resetModules();
  await import('../dashboard/dashboard.js');
  await vi.waitFor(() => expect(document.querySelector('[data-character-id="yuri"]')).not.toBeNull());
  openCharacter('yuri');
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

function openCharacter(id) {
  document.querySelector(`[data-character-id="${id}"] [data-action="details"]`).click();
}
const button = () => document.getElementById('drawer-export');
const dialog = () => document.getElementById('export-dialog');
const confirm = () => document.getElementById('export-review-confirm');

it('downloads a PNG with its server filename and prevents duplicate clicks while exporting', async () => {
  let finish;
  exportResponse.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  button().click();
  expect(button().disabled).toBe(true);
  expect(button().textContent).toContain('Exporting');
  button().click();
  expect(exportResponse).toHaveBeenCalledTimes(1);
  expect(JSON.parse(exportResponse.mock.calls[0][1].body)).toEqual({ fit: 'none', acknowledged: false });
  finish(png());
  await vi.waitFor(() => expect(downloads).toEqual([{ href: 'blob:export', filename: 'yuri.png' }]));
  expect(button().disabled).toBe(false);
  expect(button().textContent).toContain('Export PNG');
});

it('shows passages as text and confirms only the character whose export was reviewed', async () => {
  exportResponse.mockImplementationOnce(async () => review());
  button().click();
  await vi.waitFor(() => expect(dialog().open).toBe(true));
  expect(downloads).toEqual([]);
  expect(exportResponse).toHaveBeenCalledTimes(1);
  expect(document.getElementById('export-review-list').textContent).toContain('<private passage>');
  expect(document.querySelector('#export-review-list private')).toBeNull();
  expect(document.getElementById('export-review-studio').getAttribute('href')).toBe('/studio/?character=yuri');
  // Selection changes must not redirect an acknowledgement to another card.
  openCharacter('other');
  confirm().click();
  await vi.waitFor(() => expect(downloads).toHaveLength(1));
  expect(exportResponse.mock.calls[1][0]).toBe('/api/characters/yuri/export');
  expect(JSON.parse(exportResponse.mock.calls[1][1].body).acknowledged).toBe(true);
  button().click();
  await vi.waitFor(() => expect(exportResponse).toHaveBeenCalledTimes(3));
  expect(JSON.parse(exportResponse.mock.calls[2][1].body).acknowledged).toBe(false);
  await vi.waitFor(() => expect(button().disabled).toBe(false));
});

it('cancels a review without downloading or acknowledging it', async () => {
  exportResponse.mockImplementation(async () => review());
  button().click();
  await vi.waitFor(() => expect(dialog().open).toBe(true));
  dialog().querySelector('.modal-close').click();
  expect(dialog().open).toBe(false);
  expect(downloads).toEqual([]);
  expect(exportResponse).toHaveBeenCalledTimes(1);
  button().click();
  await vi.waitFor(() => expect(dialog().open).toBe(true));
  expect(JSON.parse(exportResponse.mock.calls[1][1].body).acknowledged).toBe(false);
});

it.each(['leak', 'network'])('shows a persistent %s error without downloading a fake PNG', async (kind) => {
  exportResponse.mockImplementationOnce(async () => {
    if (kind === 'network') throw new Error('Connection lost');
    return json({ detail: { code: 'leak', detail: 'Private information cannot be exported.' } }, 422);
  });
  button().click();
  await vi.waitFor(() => expect(document.getElementById('export-error').textContent)
    .toContain(kind === 'network' ? 'Connection lost' : 'Private information cannot be exported.'));
  expect(downloads).toEqual([]);
  expect(dialog().open).toBe(false);
  expect(button().disabled).toBe(false);
});
