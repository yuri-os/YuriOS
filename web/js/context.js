/* The context gauge (SPEC §11) — `context` events off the one bus
 * (world/context.py), rendered into whatever element a page hands it.
 *
 * The event is sticky, so a page opened mid-conversation gets the last reading
 * on connect rather than a blank. Shown as used / window, because "you are near
 * the ceiling" is only readable if the ceiling is on screen next to it; with no
 * window known (a hosted route never says) the used side stands alone.
 *
 * What must fit is the prompt PLUS the reply she hasn't written yet, so the
 * thresholds count `reserve` (MAX_REPLY_TOKENS) in: amber at 75% of the window,
 * magenta once prompt + reply no longer fit — the state that used to surface as
 * the server refusing the turn ("Context size has been exceeded").
 *
 * Its own module because two pages show it now — the sanctuary (js/main.js) and
 * the text room (text/text.js) — and a gauge whose thresholds disagree between
 * two views of the same runtime is worse than no gauge.
 */

// 8192 → "8.2k", 128000 → "128k" (world/context.short_tokens, kept in step)
export const short = (n) => {
  if (n < 1000) return `${n}`;
  const k = n / 1000;
  return `${k.toFixed(Math.abs(k - Math.round(k)) < 0.05 ? 0 : 1)}k`;
};

export function renderContext(el, c) {
  if (!el || !c) return;
  const { used = 0, limit, reserve = 0, exact, pct, limit_source: src } = c;
  const approx = exact ? '' : '~';
  el.textContent = limit
    ? `ctx ${approx}${short(used)}/${short(limit)}`
    : `ctx ${approx}${short(used)}`;
  const near = limit ? used >= 0.75 * limit : false;
  const over = limit ? used + reserve > limit : false;
  el.classList.toggle('near', near && !over);
  el.classList.toggle('over', over);
  el.title = [
    `${exact ? 'prompt' : 'estimated prompt'}: ${used.toLocaleString()} tokens`,
    limit ? `window: ${limit.toLocaleString()} tokens${src ? ` (${src})` : ''}`
          : 'window: unknown — set CONTEXT_LENGTH in .env to show it',
    reserve ? `reserved for her reply: ${reserve.toLocaleString()}` : '',
    pct != null ? `${pct}% used` : '',
    over ? 'over the window — raise CONTEXT_LENGTH in .env and restart' : '',
  ].filter(Boolean).join('\n');
}
