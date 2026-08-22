/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/* The gallery panel (js/gallery.js, SPEC §7.6) — the three properties that are
 * the whole reason it is its own file: it fetches nothing until the tab is
 * opened, it pages instead of pulling the shelf, and a score out of ten lands
 * on an append-only sidecar behind an optimistic click.
 *
 * jsdom, because unlike the other files here this one *is* the DOM — the thing
 * worth pinning is what the panel does to the page, not a value it returns.
 * The script is an IIFE with no exports (every client here is, so the raw
 * Live2D page can run them too), so each case builds the page, stubs fetch,
 * and re-imports the module as if the room had just loaded.
 */

const shot = (name, extra = {}) => ({
  name, url: `/selfies/${name}`, caption: `${name} caption`,
  created_at: '2026-08-20T21:00:00', backend: 'diffusers', model: 'mock/sdxl',
  seed: 42, prompt: 'a prompt', negative: '', bytes: 4096,
  score: null, rated_at: null, ...extra,
});

const shelf = (items, over = {}) => ({
  items, page: 0, limit: 12, has_more: false, total: items.length,
  rated: items.filter((item) => Number.isInteger(item.score)).length, ...over,
});

let calls;
// The panel subscribes to `gallery-open` and `world-ev` on window, and window
// outlives resetModules — so a previous case's instance would keep answering
// the next case's events, on a detached panel, through the same fetch stub.
// Record what each import subscribes and take it back off afterwards.
let listeners = [];

/** Build the page, stub the shelf, and load the panel into it. */
async function room(answer) {
  calls = [];
  document.body.innerHTML = '<button id="tab-gallery"></button>' +
    '<div id="gallery" hidden></div>';
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    // the per-character mapping the real runtime does for image bytes
    httpPath: (path) => `/api/characters/yuri${path}`,
  };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    calls.push({ url, method: init?.method || 'GET',
                 body: init?.body ? JSON.parse(init.body) : null });
    return answer(url, init);
  }));
  const subscribe = window.addEventListener;
  window.addEventListener = (type, handler, options) => {
    listeners.push([type, handler, options]);
    subscribe.call(window, type, handler, options);
  };
  try {
    vi.resetModules();
    await import('../js/gallery.js');
  } finally {
    window.addEventListener = subscribe;
  }
  return document.getElementById('gallery');
}

const ok = (payload) => ({ ok: true, json: async () => payload });
const fine = (payload) => async () => ok(payload);

/** The tab click, as js/mind.js delivers it — reveal, then `gallery-open`. */
async function open() {
  document.getElementById('gallery').hidden = false;
  window.dispatchEvent(new Event('gallery-open'));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

const tiles = (panel) => [...panel.querySelectorAll('.gl-shot')];
const pips = (panel, index = 0) => [...tiles(panel)[index].querySelectorAll('.gl-pip')];

beforeEach(() => {
  delete window.YuriOSRuntime;
  // the fold is remembered in localStorage, and jsdom's survives the module
  window.localStorage?.clear();
});
afterEach(() => {
  for (const [type, handler, options] of listeners.splice(0)) {
    window.removeEventListener(type, handler, options);
  }
  vi.unstubAllGlobals();
});

describe('opening the tab', () => {
  it('fetches nothing until the tab is actually opened', async () => {
    await room(fine(shelf([shot('a.png')])));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toEqual([]);            // a hidden panel is not a download
  });

  it('asks for the newest page and lays the shelf out lazily', async () => {
    const panel = await room(fine(shelf([shot('a.png'), shot('b.png')])));
    await open();

    expect(calls[0].url).toBe('/api/gallery?page=0&limit=12');
    expect(tiles(panel)).toHaveLength(2);
    const image = panel.querySelector('.gl-frame img');
    expect(image.getAttribute('loading')).toBe('lazy');
    // the bytes come from the character-scoped selfie route, like the transcript
    expect(image.getAttribute('src')).toBe('/api/characters/yuri/selfies/a.png');
    expect(panel.querySelector('.gl-caption').textContent).toBe('a.png caption');
    expect(panel.querySelector('.gl-count').textContent).toBe('2 pictures');
  });

  it('says so plainly when she has never taken one', async () => {
    const panel = await room(fine(shelf([])));
    await open();
    expect(panel.querySelector('.gl-empty').textContent).toContain('no photographs yet');
  });

  it('keeps the room usable when the shelf does not answer', async () => {
    const panel = await room(async () => ({ ok: false, text: async () => 'nope' }));
    await open();
    expect(panel.querySelector('.gl-empty').textContent).toContain("didn't answer");
  });
});

describe('paging', () => {
  it('walks back a page at a time and stops at both ends', async () => {
    const pages = [
      shelf([shot('c.png')], { page: 0, limit: 1, has_more: true, total: 2 }),
      shelf([shot('b.png')], { page: 1, limit: 1, has_more: false, total: 2 }),
    ];
    const panel = await room(async (url) =>
      ok(pages[Number(new URL(url, 'http://x').searchParams.get('page'))]));
    await open();

    const [newer, older] = panel.querySelectorAll('.gl-step');
    expect(newer.disabled).toBe(true);          // page 0 has nothing newer
    expect(older.disabled).toBe(false);

    older.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls[1].url).toBe('/api/gallery?page=1&limit=1');
    expect(panel.querySelector('.gl-page').textContent).toBe('page 2');
    expect(panel.querySelectorAll('.gl-step')[1].disabled).toBe(true);

    panel.querySelectorAll('.gl-step')[0].click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls[2].url).toBe('/api/gallery?page=0&limit=1');
  });
});

describe('a score out of ten', () => {
  it('fills the pips and posts the score', async () => {
    const panel = await room(fine(shelf([shot('a.png')])));
    await open();

    pips(panel)[6].click();                      // "7"
    expect(panel.querySelector('.gl-verdict').textContent).toBe('7/10');
    expect(pips(panel).filter((pip) => pip.classList.contains('on'))).toHaveLength(7);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(calls[1]).toMatchObject({
      url: '/api/gallery/rate', method: 'POST',
      body: { name: 'a.png', score: 7 },
    });
    expect(panel.querySelector('.gl-count').textContent).toBe('1 picture · 1 rated');
  });

  it('takes the rating back when you click the score it already has', async () => {
    const panel = await room(fine(shelf([shot('a.png', { score: 4, rated_at: 'x' })])));
    await open();
    expect(panel.querySelector('.gl-verdict').textContent).toBe('4/10');

    pips(panel)[3].click();                      // "4" again
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls[1].body).toEqual({ name: 'a.png', score: null });
    expect(panel.querySelector('.gl-verdict').textContent).toBe('not rated');
  });

  it('puts the old score back, and says so, when the save fails', async () => {
    const panel = await room(async (url) => url.startsWith('/api/gallery/rate')
      ? { ok: false, text: async () => 'disk is full' }
      : ok(shelf([shot('a.png', { score: 2 })])));
    await open();

    pips(panel)[8].click();                      // "9"
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(panel.querySelector('.gl-verdict').textContent).toBe('2/10');
    expect(panel.querySelector('.gl-error').textContent).toContain('disk is full');
  });
});

describe('the folded console', () => {
  it('opens with the pips folded away and the score still readable', async () => {
    const panel = await room(fine(shelf([shot('a.png', { score: 8 })])));
    await open();

    expect(panel.classList.contains('gl-rating')).toBe(false);
    expect(panel.querySelector('.gl-mode').getAttribute('aria-expanded')).toBe('false');
    expect(panel.querySelector('.gl-verdict').textContent).toBe('8/10');
    // an unrated shot says nothing at all until you ask
    expect(panel.querySelector('.gl-verdict').classList.contains('gl-none')).toBe(false);
  });

  it('unfolds the whole grid from one switch, without re-fetching', async () => {
    const panel = await room(fine(shelf([shot('a.png'), shot('b.png')])));
    await open();
    expect(panel.querySelector('.gl-verdict').classList.contains('gl-none')).toBe(true);

    panel.querySelector('.gl-mode').click();
    expect(panel.classList.contains('gl-rating')).toBe(true);
    expect(panel.querySelector('.gl-mode').getAttribute('aria-expanded')).toBe('true');
    expect(tiles(panel)).toHaveLength(2);      // the same tiles, same <img>s
    expect(calls).toHaveLength(1);             // a fold is not a round trip

    panel.querySelector('.gl-mode').click();
    expect(panel.classList.contains('gl-rating')).toBe(false);
  });

  it('remembers the switch the next time the room loads', async () => {
    let panel = await room(fine(shelf([shot('a.png')])));
    await open();
    panel.querySelector('.gl-mode').click();

    panel = await room(fine(shelf([shot('a.png')])));
    await open();
    expect(panel.classList.contains('gl-rating')).toBe(true);
  });

  it('offers no switch over a shelf with nothing on it', async () => {
    const panel = await room(fine(shelf([])));
    await open();
    expect(panel.querySelector('.gl-mode')).toBe(null);
  });

  it('drops the gl-none hook the moment a score lands', async () => {
    const panel = await room(fine(shelf([shot('a.png')])));
    await open();
    panel.querySelector('.gl-mode').click();

    pips(panel)[4].click();                    // "5"
    expect(panel.querySelector('.gl-verdict').classList.contains('gl-none')).toBe(false);
    await new Promise((resolve) => setTimeout(resolve, 0));

    pips(panel)[4].click();                    // and back off again
    expect(panel.querySelector('.gl-verdict').classList.contains('gl-none')).toBe(true);
  });
});

describe('getting out of the way', () => {
  /* The panel's own stylesheet, in the page, because this is a cascade bug and
     nothing else can see it: `#gallery{ display:flex }` outranks the UA's
     `[hidden]{ display:none }`, so for a while the shelf stayed on screen over
     the transcript once you had opened it — the tab switched, the panel did
     not. jsdom resolves enough of the cascade to hold the guard in place. */
  const styled = () => {
    document.head.innerHTML =
      `<style>${readFileSync(resolve(process.cwd(), 'gallery.css'), 'utf8')}</style>`;
  };

  it('takes no room at all while the tab is closed', async () => {
    const panel = await room(fine(shelf([shot('a.png')])));
    styled();
    await open();
    expect(getComputedStyle(panel).display).toBe('flex');

    panel.hidden = true;                       // js/mind.js, on the chat tab
    expect(getComputedStyle(panel).display).toBe('none');
  });
});

describe('the one bus', () => {
  it('follows a score set in another open room', async () => {
    const panel = await room(fine(shelf([shot('a.png')])));
    await open();

    window.dispatchEvent(new CustomEvent('world-ev', {
      detail: { type: 'gallery', action: 'rate', image: 'a.png', score: 6 },
    }));
    expect(panel.querySelector('.gl-verdict').textContent).toBe('6/10');
    expect(calls).toHaveLength(1);               // an echo is not a round trip
  });

  it('picks up a shot she takes while the shelf is open', async () => {
    const panel = await room(fine(shelf([shot('a.png')])));
    await open();

    window.dispatchEvent(new CustomEvent('world-ev', {
      detail: { type: 'message', role: 'assistant', image_url: '/selfies/new.png' },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toHaveLength(2);
    expect(calls[1].url).toBe('/api/gallery?page=0&limit=12');
  });

  it('ignores the bus entirely while the tab is closed', async () => {
    const panel = await room(fine(shelf([shot('a.png')])));
    await open();
    panel.hidden = true;

    window.dispatchEvent(new CustomEvent('world-ev', {
      detail: { type: 'message', role: 'assistant', image_url: '/selfies/new.png' },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toHaveLength(1);
  });
});
