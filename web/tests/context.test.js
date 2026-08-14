// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';

import { renderContext, short } from '../js/context.js';

/* The context gauge (SPEC §11). Two pages draw it — the sanctuary and the text
 * room — off one module, precisely so the thresholds cannot disagree between two
 * views of the same runtime. The thresholds are the point of the file: amber at
 * 75% of the window, magenta once the prompt PLUS the reply she hasn't written
 * yet no longer fits. That second one is a state that otherwise surfaces only as
 * the server refusing the turn, which is far too late to be a warning.
 */

let el;
beforeEach(() => { el = document.createElement('span'); });

describe('short', () => {
  it('prints a number under a thousand as itself', () => {
    expect(short(0)).toBe('0');
    expect(short(999)).toBe('999');
  });

  it('drops the decimal when a k is within a rounding hair of whole', () => {
    // 128000 → "128k", not "128.0k" — matches world/context.short_tokens.
    expect(short(128000)).toBe('128k');
    expect(short(1000)).toBe('1k');
    expect(short(32020)).toBe('32k');
  });

  it('keeps one decimal when the k is not whole', () => {
    expect(short(8192)).toBe('8.2k');
    expect(short(1500)).toBe('1.5k');
  });
});

describe('renderContext', () => {
  it('shows the ceiling next to the reading', () => {
    // "you are near the ceiling" is only readable with the ceiling on screen.
    renderContext(el, { used: 8192, limit: 32000, exact: true });
    expect(el.textContent).toBe('ctx 8.2k/32k');
  });

  it('marks an estimate with a tilde and an exact count without one', () => {
    renderContext(el, { used: 8192, limit: 32000, exact: false });
    expect(el.textContent).toBe('ctx ~8.2k/32k');
    renderContext(el, { used: 8192, limit: 32000, exact: true });
    expect(el.textContent).toBe('ctx 8.2k/32k');
  });

  it('stands the used side alone when no window is known', () => {
    // A hosted route never says what the window is.
    renderContext(el, { used: 8192, exact: true });
    expect(el.textContent).toBe('ctx 8.2k');
    expect(el.title).toContain('window: unknown');
  });

  it('is plain below three-quarters of the window', () => {
    renderContext(el, { used: 23000, limit: 32000, reserve: 1000 });
    expect(el.classList.contains('near')).toBe(false);
    expect(el.classList.contains('over')).toBe(false);
  });

  it('goes amber at three-quarters exactly', () => {
    renderContext(el, { used: 24000, limit: 32000, reserve: 1000 });
    expect(el.classList.contains('near')).toBe(true);
    expect(el.classList.contains('over')).toBe(false);
  });

  it('goes magenta once the prompt plus her reply no longer fit', () => {
    // 31500 of 32000 used is under the window — but not with 1000 reserved for
    // the reply, and the turn would be refused rather than merely tight.
    renderContext(el, { used: 31500, limit: 32000, reserve: 1000 });
    expect(el.classList.contains('over')).toBe(true);
    expect(el.classList.contains('near')).toBe(false);  // one state, not both
    expect(el.title).toContain('raise CONTEXT_LENGTH');
  });

  it('clears a warning when the next reading comes back down', () => {
    renderContext(el, { used: 31500, limit: 32000, reserve: 1000 });
    renderContext(el, { used: 1000, limit: 32000, reserve: 1000 });
    expect(el.classList.contains('over')).toBe(false);
    expect(el.classList.contains('near')).toBe(false);
  });

  it('names where the window came from when the server said', () => {
    renderContext(el, { used: 10, limit: 32000, limit_source: 'lm studio' });
    // toLocaleString, so the thousands separator is the reader's, not ours.
    expect(el.title).toContain(`window: ${(32000).toLocaleString()} tokens (lm studio)`);
  });

  it('leaves the element alone when there is no reading yet', () => {
    renderContext(el, null);
    expect(el.textContent).toBe('');
    expect(() => renderContext(null, { used: 1 })).not.toThrow();
  });
});
