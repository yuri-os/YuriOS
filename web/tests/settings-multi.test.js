/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* The settings panel's vocabulary control (shared/settings.js, SPEC §11).
 *
 * MIND_TOOL_ALLOWLIST is a comma-separated list of tool names that appear
 * nowhere a person would look — not in the config, not in the panel, only in
 * `yurios/mind/hands.py`. Rendered as the text box its `str` annotation implies,
 * it is a field nobody can fill in. So the server sends the whole vocabulary
 * (`type: "multi"`, `options`, `option_help`) and this is what the panel does
 * with it: every name on screen with what it does, ticked from the file, and
 * saved back as the list it came from.
 *
 * jsdom and a re-import per case, like gallery.test.js: the script is an IIFE
 * with no exports — the same source runs on the bundled page and on the raw
 * Live2D one — so a case builds the markup, stubs fetch, and loads it as if the
 * page had just opened.
 */

const MULTI = {
  key: 'MIND_TOOL_ALLOWLIST', type: 'multi', help: 'the hands she may reach for unasked',
  value: 'read_note,rug_pull',
  options: ['write_note', 'read_note', 'research'],
  option_detail: {
    write_note: { group: 'cheap', help: 'start a new note in her Vault' },
    read_note: { group: 'cheap', help: 'read one of her notes back' },
    research: { group: 'expensive', help: 'read several pages on a topic',
                note: 'needs SEARCH_BACKEND, which is off' },
  },
  option_groups: { cheap: 'a step in her goal work', expensive: "a whole tick's intention" },
};

const settings = (fields) => ({
  env_path: '/tmp/.env',
  groups: [{ group: 'her hands in the loop', advanced: false, fields }],
});

let posted;

function page() {
  document.body.innerHTML = `
    <button id="settings-open"></button>
    <dialog id="settings">
      <span id="settings-path"></span>
      <button id="settings-close"></button>
      <div id="settings-body"></div>
      <span id="settings-note"></span>
      <button id="settings-save"></button>
    </dialog>`;
  // jsdom has no dialog methods; the panel only ever opens and closes one.
  const dialog = document.getElementById('settings');
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; };
}

/** Open the panel over one .env table, with no character brain behind it. */
async function open(fields = [MULTI]) {
  posted = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/api/brain')) return { ok: false, status: 404, json: async () => ({}) };
    if (init?.method === 'POST') {
      posted.push(JSON.parse(init.body));
      return { ok: true, json: async () => ({ ok: true, written: Object.keys(JSON.parse(init.body)), restart_required: true }) };
    }
    return { ok: true, json: async () => settings(fields) };
  }));
  page();
  vi.resetModules();
  await import('../shared/settings.js');
  document.getElementById('settings-open').click();
  await vi.waitFor(() => expect(document.querySelector('.set-multi')).toBeTruthy());
}

const ticked = () => [...document.querySelectorAll('.set-multi input:checked')]
  .map((box) => box.closest('label').querySelector('.set-multi-name').textContent);

beforeEach(() => { vi.resetModules(); });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('a closed vocabulary in the settings panel', () => {
  it('puts every name on screen with what it does', async () => {
    await open();
    const options = [...document.querySelectorAll('.set-multi-opt')];
    const names = options.map((o) => o.querySelector('.set-multi-name').textContent);
    expect(names).toContain('write_note');
    expect(names).toContain('research');
    const research = options.find((o) => o.textContent.includes('research'));
    // the reason a ticked hand might still never fire, on the row it is ticked on
    expect(research.querySelector('.set-multi-note').textContent)
      .toContain('SEARCH_BACKEND');
    expect(research.classList.contains('is-inert')).toBe(true);
  });

  it('heads each class once instead of labelling every row with it', async () => {
    await open();
    const heads = [...document.querySelectorAll('.set-multi-head')]
      .map((h) => h.querySelector('.set-multi-head-name').textContent);
    expect(heads).toEqual(['cheap', 'expensive', 'unknown']);
    // what ticking anything in that half commits to, said once at its head
    expect(document.querySelector('.set-multi-head').textContent)
      .toContain('a step in her goal work');
    expect(document.querySelector('.set-multi-opt').textContent)
      .not.toContain('cheap');
  });

  it('ticks what the file already says', async () => {
    await open();
    expect(ticked()).toContain('read_note');
    expect(ticked()).not.toContain('write_note');
  });

  it('keeps a name this build has never heard of rather than dropping it', async () => {
    await open();
    // Silently losing it would save the removal of a setting nobody touched.
    expect(ticked()).toContain('rug_pull');
    expect(document.querySelector('.set-multi').textContent)
      .toContain('not a name this build knows');
  });

  it('is the one row that is not itself a <label>', async () => {
    await open();
    // Every other row wraps its single control in a <label>. This one holds a
    // label per option, and nesting those is invalid HTML the browser makes
    // visible: an outer label with no `for` claims its FIRST labelable
    // descendant, so a click on "research" would tick research and toggle
    // write_note with it. (jsdom does not implement that activation, which is
    // why this pins the structure rather than clicking and hoping.)
    expect(document.querySelector('.set-row').tagName).toBe('DIV');
    expect(document.querySelectorAll('.set-multi-opt label').length).toBe(0);
    expect([...document.querySelectorAll('.set-multi-opt')]
      .every((o) => o.tagName === 'LABEL' && o.htmlFor)).toBe(true);
  });

  it('saves the ticks as the list the file wants', async () => {
    await open();
    const box = [...document.querySelectorAll('.set-multi-opt')]
      .find((o) => o.textContent.startsWith('write_note')).querySelector('input');
    box.click();
    document.getElementById('settings-save').click();
    await vi.waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0]).toEqual({ MIND_TOOL_ALLOWLIST: 'write_note,read_note,rug_pull' });
  });

  it('sends nothing when the ticks are left alone', async () => {
    await open();
    document.getElementById('settings-save').click();
    await vi.waitFor(() => expect(document.getElementById('settings-note').textContent)
      .toBe('no changes'));
    expect(posted).toEqual([]);
  });

  it('finds the row by a name inside it, not just by the key', async () => {
    await open();
    const filter = document.querySelector('.settings-filter');
    filter.value = 'research';
    filter.dispatchEvent(new Event('input'));
    expect(document.querySelector('.set-row').hidden).toBe(false);
    filter.value = 'reverb';
    filter.dispatchEvent(new Event('input'));
    expect(document.querySelector('.set-row').hidden).toBe(true);
  });
});
