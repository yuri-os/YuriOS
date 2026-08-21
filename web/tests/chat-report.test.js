/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* The briefing card (SPEC §18.2a, §21.2).
 *
 * A DREAM job told to `deliver: chat` files a *pointer* in her inbox: one line
 * she said, plus the path of the report on her desk. This file pins the two
 * decisions in rendering that — that the document is folded away until asked
 * for, and that asking for it fetches once — because a page of research pasted
 * straight into the transcript buries the conversation it arrived in, and a
 * card that re-fetches on every click hammers the desk route.
 *
 * chat.js is a classic script shared by both rooms, not a module, so it is
 * evaluated rather than imported. `window.WorldChat.confirmUser` is `addMsg`.
 */
// `process.cwd()`, not `import.meta.url`: under jsdom the module URL is an
// http one and cannot be turned back into a path. Vitest runs from `web/`.
const SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

const REPORT = {
  id: 'r1', role: 'assistant', ts: '2026-08-21T04:10:00', proactive: true,
  text: 'I read the tape while you were out.',
  report_path: 'reports/market-brief/2026-08-20.md',
  report_title: 'Overnight market brief',
  report_job: 'market-brief',
};

function boot() {
  document.body.innerHTML = '<div id="messages"></div>';
  // eslint-disable-next-line no-new-func
  new Function(SOURCE)();
  return window.WorldChat;
}

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {}))); });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('a report delivered into the chat', () => {
  it('arrives as a card, not as a wall of text', () => {
    boot().confirmUser(REPORT);
    const card = document.querySelector('.msg-report');
    expect(card).not.toBeNull();
    expect(card.dataset.path).toBe('reports/market-brief/2026-08-20.md');
    expect(card.querySelector('.report-title').textContent)
      .toBe('Overnight market brief');
    // the document itself is not in the page until it is asked for
    expect(card.querySelector('.report-body').hidden).toBe(true);
    expect(document.querySelector('.msg').textContent)
      .toContain('I read the tape while you were out.');
  });

  it('renders an ordinary line without one', () => {
    boot().confirmUser({ id: 'm1', role: 'assistant', text: 'morning' });
    expect(document.querySelector('.msg-report')).toBeNull();
  });

  it('escapes what it is handed', () => {
    boot().confirmUser({ ...REPORT, report_title: '<img src=x onerror=1>' });
    expect(document.querySelector('.msg-report img')).toBeNull();
  });

  it('fetches the document once, then folds it without asking again', async () => {
    const fetched = vi.fn(async () => ({
      ok: true, json: async () => ({ text: '## The tape\nSemis led.' }),
    }));
    vi.stubGlobal('fetch', fetched);
    boot().confirmUser(REPORT);
    const card = document.querySelector('.msg-report');
    const button = card.querySelector('.report-open');

    button.click();
    await vi.waitFor(() => expect(card.querySelector('.report-body').hidden).toBe(false));
    expect(card.querySelector('.report-body').textContent).toContain('Semis led.');
    expect(fetched).toHaveBeenCalledTimes(1);
    expect(fetched.mock.calls[0][0]).toContain(
      encodeURIComponent('reports/market-brief/2026-08-20.md'));

    button.click();                                  // fold it away
    await vi.waitFor(() => expect(card.querySelector('.report-body').hidden).toBe(true));
    button.click();                                  // and back — still one fetch
    await vi.waitFor(() => expect(card.querySelector('.report-body').hidden).toBe(false));
    expect(fetched).toHaveBeenCalledTimes(1);
  });

  it('says so when the file is no longer on her desk', async () => {
    // The desk is the source of truth and the inbox row only points at it, so a
    // report whose file was deleted is a real state, not a bug to hide.
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404 })));
    boot().confirmUser(REPORT);
    const card = document.querySelector('.msg-report');
    card.querySelector('.report-open').click();
    await vi.waitFor(() => expect(card.querySelector('.report-body').textContent)
      .toContain("isn't on her desk"));
  });
});
