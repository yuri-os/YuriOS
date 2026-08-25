/** @vitest-environment jsdom */
/**
 * What a refused Create does with the reason.
 *
 * Both irreversible buttons sit in a sticky header over a form thousands of
 * pixels tall, so "write the banner and return" is indistinguishable from a
 * button that does nothing: the banner renders near the top of the document and
 * the person is nowhere near it. Export had always taken you to the offending
 * field; create had not, and that asymmetry is the whole bug.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const PAGE = readFileSync(resolve(process.cwd(), 'studio/index.html'), 'utf8');

function bodyOf(html) {
  return html.slice(html.indexOf('<body'), html.lastIndexOf('</body>'))
    .replace(/^<body[^>]*>/, '')
    .replace(/<script[\s\S]*?<\/script>/g, '');
}

beforeEach(() => {
  document.body.innerHTML = bodyOf(PAGE);
  // jsdom has no dialog support and no layout; neither is what this asserts.
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
  document.body.innerHTML = '';
});

/** Open the studio on a new character whose draft is `draft`. */
async function studio(draft = {}) {
  vi.stubGlobal('fetch', vi.fn(async (url) => ({
    ok: true,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => (String(url).includes('/api/studio/template')
      ? { draft }
      : {}),
  })));
  vi.resetModules();
  await import('../studio/studio.js');
  // boot() is async and un-awaited by the module; let it settle.
  await vi.waitFor(() => expect(document.querySelector('[data-field="name"]')).not.toBeNull());
}

const create = () => document.getElementById('primary-action');
const notice = () => document.getElementById('notice');

/** Type into a field the way the form's own listeners see it. */
function typeInto(field, value) {
  const box = document.querySelector(
    `[data-field="${field}"] input, [data-field="${field}"] textarea`);
  box.value = value;
  box.dispatchEvent(new Event('input', { bubbles: true }));
  return box;
}

it('sends you to the name box instead of only writing a banner', async () => {
  await studio();
  create().click();

  expect(notice().hidden).toBe(false);
  expect(notice().textContent).toBe('She needs a name.');
  // the half that was missing: the banner is near the top of a very tall form,
  // and the button that refused is sticky. Focus is what moves the viewport.
  expect(document.activeElement)
    .toBe(document.querySelector('[data-field="name"] input'));
});

it('sends you to the cold open when that is the missing half', async () => {
  // `boot()` blanks the name on a new character however the template arrived,
  // so it is typed; identity is what `description()` reads.
  await studio({ identity: 'A diagnostic companion.' });
  typeInto('name', 'Testra');
  create().click();

  expect(notice().textContent)
    .toContain('cannot be imported anywhere');
  expect(document.activeElement)
    .toBe(document.querySelector('[data-field="first_mes"] textarea'));
});

it('does not block a draft with nothing wrong with it', async () => {
  await studio({
    identity: 'A diagnostic companion.',
    first_mes: 'You must be the one who started me.',
  });
  typeInto('name', 'Testra');
  create().click();

  // The gate's only job is to get out of the way. What happens after the POST
  // is the redirect's business, and jsdom cannot follow one.
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/characters', expect.objectContaining({ method: 'POST' })));
});
