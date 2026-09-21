/** @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

/* Opening `#/vault/file/…` is how you read a vault file. The edit list on a
 * chatty path such as state/engine.json is long enough to push that text
 * below the fold, so the contents panel has to come first. */

function mount() {
  document.body.innerHTML = `
    <nav id="rail"><a class="rail-item" data-section="vault"></a></nav>
    <span id="section-title"></span>
    <p id="section-note"></p>
    <div id="stage-body"></div>
    <button id="refresh" type="button"></button>
    <span id="state-chip"></span><span id="state-label"></span>
    <span id="stream-chip"></span><span id="stream-label"></span>
    <small id="brand-sub"></small>
    <div id="toast-region"></div>
  `;
}

beforeEach(() => {
  mount();
  window.YuriOSRuntime = {
    characterId: 'yuri',
    apiPath: (path) => path,
  };
  location.hash = '#/vault/file/state/engine.json';
  vi.stubGlobal('EventSource', class {
    addEventListener() {}
    close() {}
  });
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const path = String(url);
    let body = { activity: { state: 'IDLE' } };
    if (path.includes('/vault/file')) {
      body = { path: 'state/engine.json', rev: null, text: 'THE FILE BODY', truncated: false, bytes: 13 };
    } else if (path.includes('/vault/history')) {
      body = {
        path: 'state/engine.json',
        items: [{
          sha: 'abc12345deadbeef', short: 'abc12345', at: 1700000000,
          subject: 'tick t-1: REST', insertions: 2, deletions: 1,
        }],
      };
    }
    return {
      ok: true,
      headers: { get: () => 'application/json' },
      json: async () => body,
    };
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
  document.body.innerHTML = '';
  location.hash = '';
});

it('shows the vault file above its edit history', async () => {
  await import('../mind/mind.js');
  await vi.waitFor(() => {
    expect(document.querySelector('#stage-body .msg-body')?.textContent).toBe('THE FILE BODY');
  });
  const headings = [...document.querySelectorAll('#stage-body h2')].map((node) => node.textContent);
  const contents = headings.findIndex((title) => title.startsWith('Contents'));
  const history = headings.findIndex((title) => title === 'Every edit to this file');
  expect(contents).toBeGreaterThanOrEqual(0);
  expect(history).toBeGreaterThan(contents);
});
