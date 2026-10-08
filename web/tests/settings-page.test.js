/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  changedFromDefault, glance, indexFields, mergeBooks, parseRoute, pendingRestart,
  serversFromJson, textToArgs, textToEnv,
} from '../settings/model.js';

/* House settings as a page (web/settings/, SPEC §11.2).
 *
 * The server sends the table already laid out — `pages`, each a few sections of
 * fields — and the rows are /shared/settings.js's, the same ones every room's
 * gear draws. What the page itself decides is pinned here: where a URL lands,
 * what the overview says about the house, that a search answers from every page
 * at once, and that a save sends only what changed.
 */

const field = (key, extra = {}) => ({ key, label: key.toLowerCase(), help: `about ${key}`,
  type: 'text', value: '', default: '', ...extra });

const DATA = {
  env_path: '/home/me/yurios/.env',
  overview: [field('USER_NAME', { value: 'Sam', default: 'you' })],
  pages: [
    { id: 'models', title: 'Models', icon: 'chip', note: 'which model',
      sections: [{ title: 'Her models', advanced: false, fields: [
        field('CHAT_MODEL', { value: 'lm_studio/gemma', default: 'NONE' }),
        field('TEMPERATURE', { type: 'number', value: 0.9, default: 0.9 }),
      ] }] },
    { id: 'web', title: 'Web', icon: 'globe', note: 'the web',
      sections: [
        { title: 'Search', advanced: false, fields: [
          field('SEARCH_BACKEND', { type: 'select', options: ['off', 'searxng'],
            value: 'off', default: 'off' }),
          field('SEARXNG_URL', { value: 'http://localhost:8080', default: 'http://localhost:8080',
            relevant_if: { SEARCH_BACKEND: ['searxng'] } }),
        ] },
        { title: 'Limits', advanced: true, fields: [
          field('FETCH_TIMEOUT_S', { type: 'number', value: 8, default: 8, pending: true }),
        ] },
      ] },
  ],
};

describe('the page model', () => {
  const index = indexFields(DATA);

  it('routes to a known page and a row on it, and anything else to the overview', () => {
    expect(parseRoute('#/web/SEARXNG_URL', DATA.pages)).toEqual({ page: 'web', key: 'SEARXNG_URL' });
    expect(parseRoute('#/models', DATA.pages)).toEqual({ page: 'models', key: '' });
    expect(parseRoute('#/nowhere/X', DATA.pages)).toEqual({ page: 'overview', key: '' });
    expect(parseRoute('', DATA.pages).page).toBe('overview');
    expect(parseRoute('#/mcp', DATA.pages).page).toBe('mcp');
  });

  it('routes to a character and her tab, and never to a character that is not there', () => {
    const cast = [{ id: 'mia' }];
    expect(parseRoute('#/character/mia/jobs', DATA.pages, cast))
      .toMatchObject({ page: 'character', character: 'mia', tab: 'jobs' });
    expect(parseRoute('#/character/mia', DATA.pages, cast).tab).toBe('settings');
    expect(parseRoute('#/character/mia/nonsense', DATA.pages, cast).tab).toBe('settings');
    expect(parseRoute('#/character/ghost', DATA.pages, cast).page).toBe('overview');
  });

  it('merges a scene library into hers, the incoming row winning a shared name', () => {
    const hers = { tool_hint: 'mine', slots: { scenes: [{ key: 'room', prompt: 'her room' },
      { key: 'roof', prompt: 'old roof' }], moods: [] } };
    const theirs = { tool_hint: '', slots: { scenes: [{ key: 'roof', prompt: 'new roof' },
      { key: 'beach', prompt: 'a beach' }] } };
    const merged = mergeBooks(hers, theirs);
    expect(merged.slots.scenes.map((r) => `${r.key}:${r.prompt}`))
      .toEqual(['room:her room', 'roof:new roof', 'beach:a beach']);
    expect(merged.tool_hint).toBe('mine');
  });

  it('reads an MCP config in the shapes people paste it in', () => {
    expect(Object.keys(serversFromJson('{"mcpServers": {"a": {"command": "x"}}}'))).toEqual(['a']);
    expect(Object.keys(serversFromJson('{"b": {"command": "y"}}'))).toEqual(['b']);
    expect(() => serversFromJson('[1, 2]')).toThrow();
    expect(textToArgs('one\n  two words \n\n')).toEqual(['one', 'two words']);
    expect(textToEnv('A=1\nB = x=y\nnot a pair')).toEqual({ A: '1', B: 'x=y' });
  });

  it('says what moved off its default, and what waits for a restart', () => {
    expect(changedFromDefault(index).map((e) => e.key)).toEqual(['USER_NAME', 'CHAT_MODEL']);
    expect(pendingRestart(index).map((e) => e.key)).toEqual(['FETCH_TIMEOUT_S']);
  });

  it('draws a tile only for the knobs this build has', () => {
    const tiles = glance(index);
    expect(tiles.map((t) => t.label)).toEqual(['Her model', 'Web search']);
    expect(tiles[0]).toMatchObject({ value: 'lm_studio/gemma', page: 'models', key: 'CHAT_MODEL' });
  });

  it('warns when she has no model, or is open to the network with no token', () => {
    const bare = indexFields({ overview: [], pages: [{ id: 'x', sections: [{ fields: [
      field('CHAT_MODEL', { value: 'NONE' }), field('HOST', { value: '0.0.0.0' }),
      field('PORT', { value: 8768 }), field('OWNER_TOKEN', { type: 'password', configured: false }),
    ] }] }] });
    const tiles = Object.fromEntries(glance(bare).map((t) => [t.label, t]));
    expect(tiles['Her model']).toMatchObject({ value: 'none chosen', tone: 'warn' });
    expect(tiles.Access).toMatchObject({ value: 'your network', tone: 'warn' });
  });
});

/* ---- the page itself, over a stubbed /api/settings ------------------------ */

let posted;

function markup() {
  document.body.innerHTML = `
    <button id="restart" disabled><span>Restart</span></button>
    <nav id="rail"><input id="search"><a class="rail-item" href="#/overview" data-page="overview"></a>
      <span>House</span><div id="rail-pages"></div>
      <span>Add-ons</span><div id="rail-addons"></div>
      <span hidden>Characters</span><div id="rail-characters"></div></nav>
    <h1><span id="page-title"></span></h1><p id="page-note"></p>
    <div id="stage-body"></div><div id="stage-extra" hidden></div><span id="env-path"></span>
    <footer id="savebar" hidden><span id="save-note"></span>
      <button id="savebar-restart" hidden></button>
      <button id="discard"></button><button id="save"></button></footer>
    <div id="restarting" hidden><p></p></div>`;
  window.scrollTo = () => {};       // jsdom has no layout to scroll
}

const CAST = [{ id: 'mia', name: 'Mia', state: 'idle', portrait_url: '/p.png', model: '' }];
const BRAIN = { running: true, fields: [
  { key: 'temperature', type: 'number', value: '0.4', inherited: 0.9, help: 'wander' }],
  effective: {}, key_configured: true };
const PROFILE = { settings: { name: 'Mia', voice: '', connection_profile: 'default',
  body_backend: '', body_model: '', enabled: true, mind: true, utility: true, dream: true,
  hands: true, notify: false, hands_available: true, notify_available: false,
  review_required: false } };
let calls;
const json = (data, ok = true) => ({ ok, status: ok ? 200 : 409, json: async () => structuredClone(data) });

async function open(hash = '', { house = { boot_id: 'a', restartable: true, why_not: '' } } = {}) {
  posted = [];
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const path = String(url);
    const method = init?.method || 'GET';
    calls.push(`${method} ${path}`);
    if (method !== 'GET') {
      const body = init.body && typeof init.body === 'string' ? JSON.parse(init.body) : {};
      posted.push({ path, method, body });
      if (path === '/api/house/restart') return json({ restarting: true });
      return json({ ok: true, written: Object.keys(body), restart_required: true, applied: [] });
    }
    if (path === '/api/characters') return json({ characters: CAST });
    if (path === '/api/house') return json(house);
    if (path.endsWith('/profile')) return json(PROFILE);
    if (path.endsWith('/brain')) return json(BRAIN);
    if (path === '/api/connections') return json({ profiles: [{ name: 'default', backend: 'local' }] });
    if (path.endsWith('/mia/settings')) return json({ groups: [] });
    if (path === '/api/house/mcp-servers')
      return json({ path: '/x/mcp-servers.json', configured: true, servers: {
        fetch: { command: 'uvx', args: ['mcp-server-fetch'], env: {} } } });
    return json(DATA);
  }));
  history.replaceState(null, '', '/settings/' + hash);
  markup();
  vi.resetModules();
  await import('../shared/settings.js');
  await import('../settings/settings.js');
  await vi.waitFor(() => expect(document.querySelector('.page[data-page="models"]')).toBeTruthy());
}

const visible = (page) => !document.querySelector(`.page[data-page="${page}"]`).hidden;

beforeEach(() => { vi.resetModules(); });
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('the page', () => {
  it('draws a rail item per page and shows one page at a time', async () => {
    await open('#/models');
    const items = [...document.querySelectorAll('#rail-pages .rail-item')].map((a) => a.dataset.page);
    expect(items).toEqual(['models', 'web']);
    expect(visible('models')).toBe(true);
    expect(visible('web')).toBe(false);
    expect(visible('overview')).toBe(false);
    expect(document.getElementById('page-title').textContent).toBe('Models');
    expect(document.getElementById('env-path').textContent).toBe('/home/me/yurios/.env');
  });

  it('hides a row that only matters under another setting, until it does', async () => {
    await open('#/web');
    const url = document.querySelector('.set-row[data-key="SEARXNG_URL"]');
    expect(url.hidden).toBe(true);
    const backend = document.getElementById('set-SEARCH_BACKEND');
    backend.value = 'searxng';
    backend.dispatchEvent(new Event('change', { bubbles: true }));
    expect(url.hidden).toBe(false);
  });

  it('answers a search from every page at once', async () => {
    await open('#/models');
    const search = document.getElementById('search');
    search.value = 'timeout';
    search.dispatchEvent(new Event('input'));
    expect(visible('web')).toBe(true);
    expect(visible('models')).toBe(false);
    expect(document.querySelector('.set-row[data-key="FETCH_TIMEOUT_S"]').hidden).toBe(false);
    expect(document.querySelector('.set-row[data-key="FETCH_TIMEOUT_S"]').closest('details').open).toBe(true);
    expect(document.querySelector('.rail-item[data-page="web"]').dataset.hits).toBe('1');
  });

  it('marks what is unsaved, and saves only that', async () => {
    await open('#/models');
    const temp = document.getElementById('set-TEMPERATURE');
    temp.value = '0.5';
    temp.dispatchEvent(new Event('input', { bubbles: true }));
    expect(document.querySelector('.set-row[data-key="TEMPERATURE"]').classList.contains('is-dirty')).toBe(true);
    expect(document.querySelector('.rail-item[data-page="models"]').classList.contains('is-dirty')).toBe(true);
    expect(document.getElementById('savebar').hidden).toBe(false);

    document.getElementById('save').click();
    await vi.waitFor(() => expect(posted.map((p) => p.body)).toEqual([{ TEMPERATURE: 0.5 }]));
    await vi.waitFor(() => expect(document.getElementById('save-note').textContent)
      .toMatch(/restart/));
  });

  it('says on the overview what waits for a restart, and what moved off its default', async () => {
    await open('');
    expect(visible('overview')).toBe(true);
    const overview = document.querySelector('.page[data-page="overview"]');
    expect(overview.querySelector('.notice').textContent).toMatch(/1 saved setting waiting/);
    const changed = [...overview.querySelectorAll('tbody code')].map((c) => c.textContent);
    expect(changed).toEqual(['USER_NAME', 'CHAT_MODEL']);
  });

  it('puts a moved setting back to its default with one press', async () => {
    await open('#/models');
    const row = document.querySelector('.set-row[data-key="CHAT_MODEL"]');
    const note = row.querySelector('.set-default');
    expect(note.hidden).toBe(false);
    expect(note.textContent).toMatch(/default NONE/);
    note.querySelector('.set-reset').click();
    expect(note.hidden).toBe(true);
    expect(row.classList.contains('is-dirty')).toBe(true);
    // and a field at its default offers nothing to go back to
    expect(document.querySelector('.set-row[data-key="TEMPERATURE"] .set-default').hidden).toBe(true);
  });

  it('restarts only on the second press, and waits for a new boot', async () => {
    await open('');
    const restart = document.getElementById('restart');
    expect(restart.disabled).toBe(false);
    restart.click();
    expect(restart.textContent).toMatch(/Restart now\?/);
    expect(posted).toEqual([]);
    restart.click();
    await vi.waitFor(() => expect(posted.map((p) => p.path)).toEqual(['/api/house/restart']));
    await vi.waitFor(() => expect(document.getElementById('restarting').hidden).toBe(false));
  });

  it('cannot restart a server nobody supervises, and says how instead', async () => {
    await open('', { house: { boot_id: 'a', restartable: false, why_not: 'run yurios restart' } });
    const restart = document.getElementById('restart');
    expect(restart.disabled).toBe(true);
    expect(restart.title).toBe('run yurios restart');
  });

  it('lists the characters and saves her own model with the house edits', async () => {
    await open('#/character/mia');
    expect(document.querySelector('#rail-characters .rail-item').dataset.character).toBe('mia');
    await vi.waitFor(() => expect(document.querySelector('.char-settings')).toBeTruthy());
    expect(document.getElementById('page-title').textContent).toBe('Mia');
    // her override, with the way back to the house value beside it
    const temp = document.getElementById('brain-temperature');
    expect(temp.value).toBe('0.4');
    temp.value = '0.6';
    temp.dispatchEvent(new Event('input', { bubbles: true }));
    expect(document.querySelector('.rail-item[data-character="mia"]').classList.contains('is-dirty')).toBe(true);
    document.getElementById('save').click();
    await vi.waitFor(() => expect(posted).toContainEqual(
      { path: '/api/characters/mia/brain', method: 'PATCH', body: { temperature: '0.6' } }));
  });

  it('draws the MCP servers file as editable cards', async () => {
    await open('#/mcp');
    await vi.waitFor(() => expect(document.querySelector('.mcp-card')).toBeTruthy());
    expect(document.getElementById('stage-extra').hidden).toBe(false);
    expect(document.querySelector('.mcp-card input.set-input').value).toBe('fetch');
  });
});
