import { afterEach, describe, expect, it, vi } from 'vitest';

/* The room's one quality decision (SPEC §6.2), made once at boot and read from
 * everywhere else. It is a module-level constant, so each case here has to stub
 * the device BEFORE importing — hence resetModules + a dynamic import rather than
 * a top-level one.
 *
 * `node`, not jsdom: this file replaces every browser global it reads anyway, and
 * jsdom's `location` is unforgeable — it cannot be swapped per case, which is
 * exactly what testing `?fx=` requires.
 */

/** Stub a device, then load quality.js as if the page had just opened on it. */
async function boot({ search = '', coarse = false, screen = { width: 2560, height: 1440 },
                      cores = 16, innerWidth = 1600 } = {}) {
  vi.stubGlobal('location', { search });
  vi.stubGlobal('matchMedia', (query) => ({ matches: query.includes('coarse') && coarse }));
  vi.stubGlobal('screen', screen);
  vi.stubGlobal('navigator', { hardwareConcurrency: cores });
  vi.stubGlobal('innerWidth', innerWidth);
  vi.resetModules();
  return (await import('../js/stage/quality.js')).QUALITY;
}

const PHONE = { coarse: true, screen: { width: 390, height: 844 }, cores: 8, innerWidth: 390 };

afterEach(() => { vi.unstubAllGlobals(); });

describe('the tier a device lands on', () => {
  it('gives a desktop the whole room', async () => {
    expect((await boot()).tier).toBe('full');
  });

  it('reads a coarse pointer on a small screen as a handset', async () => {
    expect((await boot(PHONE)).tier).toBe('phone');
  });

  it('reads a thin core count behind a coarse pointer as a handset too', async () => {
    // Coarse pointers also arrive on kiosk panels and TVs, which render like
    // handsets however wide the panel is.
    const quality = await boot({ coarse: true, screen: { width: 1920, height: 1080 }, cores: 4 });
    expect(quality.tier).toBe('phone');
  });

  it('keeps a large touch display on the middle tier', async () => {
    const quality = await boot({ coarse: true, screen: { width: 1920, height: 1080 }, cores: 16 });
    expect(quality.tier).toBe('low');
  });

  it('measures the screen, not the window, so a sliding address bar changes nothing', async () => {
    // `innerWidth` moves when a mobile browser's chrome slides away; `screen`
    // does not, and a phone in landscape is still a phone.
    const landscape = await boot({ ...PHONE, screen: { width: 844, height: 390 }, innerWidth: 844 });
    expect(landscape.tier).toBe('phone');
  });

  it('drops a narrow desktop window to the middle tier', async () => {
    expect((await boot({ innerWidth: 700 })).tier).toBe('low');
  });

  it('assumes eight cores when the browser will not say', async () => {
    expect((await boot({ coarse: true, screen: { width: 1920, height: 1080 }, cores: undefined })).tier)
      .toBe('low');
  });
});

describe('the escape hatches', () => {
  it('lets ?fx= force a tier in both directions', async () => {
    expect((await boot({ search: '?fx=phone' })).tier).toBe('phone');       // from a desk
    expect((await boot({ ...PHONE, search: '?fx=full' })).tier).toBe('full');
  });

  it('ignores a tier name that does not exist and measures instead', async () => {
    expect((await boot({ search: '?fx=ultra' })).tier).toBe('full');
  });

  it('gives desktop-pet mode the full tier — it builds no room to pay for', async () => {
    // SPEC §6.5: just her body in a small frameless window, so the narrow-window
    // rule (a statement about what the room costs) has nothing to say about it.
    const quality = await boot({ search: '?desktop=1', innerWidth: 420 });
    expect(quality.tier).toBe('full');
  });
});

describe('what a tier hands the room', () => {
  it('says low for every reduced tier, and phone only for the handset', async () => {
    const full = await boot();
    expect([full.low, full.phone]).toEqual([false, false]);
    const low = await boot({ innerWidth: 700 });
    expect([low.low, low.phone]).toEqual([true, false]);
    const phone = await boot(PHONE);
    expect([phone.low, phone.phone]).toEqual([true, true]);
  });

  it('spends strictly less the further down the tiers it goes', async () => {
    const [full, low, phone] = [await boot(), await boot({ innerWidth: 700 }), await boot(PHONE)];
    for (const key of ['drops', 'dust', 'farRain', 'flyers', 'overlayHz', 'terminalHz', 'anisotropy']) {
      expect(full[key], key).toBeGreaterThan(low[key]);
      expect(low[key], key).toBeGreaterThan(phone[key]);
    }
    expect(phone.pixelBudget).toBeLessThan(low.pixelBudget);
    expect(low.pixelBudget).toBeLessThan(full.pixelBudget);
  });

  it('leaves the adaptive scaler room to move around the cap', async () => {
    // VrmStage watches the frame clock and moves the render scale between these,
    // because "a phone" spans an order of magnitude.
    for (const quality of [await boot(), await boot({ innerWidth: 700 }), await boot(PHONE)]) {
      expect(quality.minScale, quality.tier).toBeLessThan(quality.maxScale);
      expect(quality.minScale, quality.tier).toBeGreaterThan(0);
    }
  });

  it('is frozen — the tier is one decision, not a setting anything may edit', async () => {
    const quality = await boot();
    expect(Object.isFrozen(quality)).toBe(true);
  });
});
