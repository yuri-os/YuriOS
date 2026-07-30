/* The two switches every client carries (SPEC §10.5) — how loud she is here, and
 * whether she also leaves for Telegram. Both are "how she reaches me" decisions
 * rather than scene decisions, so all three pages own them: the VRM sanctuary,
 * the Live2D body (§6.6) and the text room (§6.7).
 *
 * Classic IIFE on `window`, the same shape as chat.js and boot.js and for the
 * same reason: the Live2D client is raw scripts served from /js/, while the other
 * two pages are Vite bundles. One file, three pages, no per-frontend copy.
 *
 *   voice mute   the speakers, not the microphone. A gain node at the end of the
 *                playback graph, so her mouth still moves, her captions still
 *                arrive and the transcript still fills — she is talking, you just
 *                cannot hear her. Each page hands us `setMuted` because each page
 *                owns its own audio graph (js/viseme.js here, live2d/voice.js
 *                there).
 *   telegram     the live adapter's outbound flag (routes/channels.py). Server
 *                state, not ours: she keeps *reading* the chat either way. The
 *                button only exists when this character has a channel at all,
 *                which the server answers.
 *
 * Both are remembered per character in `localStorage` and re-asserted on load,
 * because a mute you have to re-press every time you open the page is not a
 * setting, it is a chore. The telegram flag lives on the server and resets when
 * the runtime does, so "remembered" means: whatever you last chose here, pushed
 * back up on the next load.
 *
 * `window.localStorage?.` and never a bare `localStorage`: engines without web
 * storage — WebKitGTK in pywebview's default private mode — do not define the
 * global at all, and a bare reference would be a ReferenceError that takes the
 * whole script with it. Without storage the switches still work; they just stop
 * remembering.
 */
(() => {
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});

  /** Per character, like the session key: two characters on one node do not share
   *  a mute (shared/runtime.js sessionKey). */
  const prefKey = (name) =>
    window.YuriOSRuntime?.sessionKey(`yurios.pref.${name}`) ?? `yurios.pref.${name}`;

  /** true / false, or null for "never set here" — which is a different answer and
   *  the reason the text room can default one way and the rooms another. */
  function readPref(name) {
    try {
      const raw = window.localStorage?.getItem(prefKey(name));
      return raw == null ? null : raw === '1';
    } catch (_) { return null; }
  }

  function writePref(name, on) {
    try { window.localStorage?.setItem(prefKey(name), on ? '1' : '0'); }
    catch (_) { /* full, or storage denied: the switch still works this session */ }
  }

  /** @param setMuted  (muted: boolean) => void — the page's own audio graph.
   *  Called once at wiring time with the remembered value, before she has said
   *  anything, so a muted page is muted from her first syllable. */
  function wireVoice(setMuted) {
    const btn = document.getElementById('voice-mute');
    if (!btn) return;
    let muted = readPref('voiceMuted') ?? false;
    const apply = () => {
      setMuted(muted);
      btn.classList.toggle('muted', muted);
      btn.setAttribute('aria-pressed', String(muted));
      btn.title = muted ? 'unmute her voice' : 'mute her voice';
    };
    btn.addEventListener('click', () => {
      muted = !muted;
      writePref('voiceMuted', muted);
      apply();
    });
    apply();
  }

  /** @param whenUnset  what to do the first time on this browser: `null` adopts
   *  whatever the runtime currently says (the rooms), `false` asserts off (the
   *  text room — see SPEC §6.7). An explicit press always wins over both. */
  async function wireTelegram(whenUnset) {
    const btn = document.getElementById('telegram-mute');
    if (!btn) return;
    const api = window.YuriOSRuntime?.apiPath ?? ((p) => p);
    const url = api('/api/channels/telegram/sending');

    let sending = false;
    const render = () => {
      btn.classList.toggle('muted', !sending);
      btn.setAttribute('aria-pressed', String(!sending));
      btn.title = sending
        ? 'stop sending her messages to telegram'
        : 'resume sending her messages to telegram';
    };
    const push = async (enabled) => {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!r.ok) throw new Error(`sending switch refused: ${r.status}`);
      return (await r.json()).sending_enabled;
    };

    let state;
    try { state = await (await fetch(url)).json(); }
    catch (_) { return; }                    // no server, no switch — stay hidden
    if (!state.configured) return;           // no channel: no switch
    btn.hidden = false;
    sending = Boolean(state.sending_enabled);

    const want = readPref('telegramSending') ?? whenUnset;
    if (want != null && want !== sending) {
      // The remembered choice (or this page's default) is the truth; the runtime
      // has just been restarted or was last set from another page.
      try { sending = await push(want); }
      catch (e) { console.warn('[controls]', e.message); }
    }
    render();

    btn.addEventListener('click', async () => {
      const next = !sending;
      writePref('telegramSending', next);     // remembered whether or not it lands
      try { sending = await push(next); }
      catch (e) { console.warn('[controls]', e.message); }
      render();
    });
  }

  window.WorldControls = {
    /** @param setMuted        the page's audio-graph hook; omit and no voice
     *                         switch is wired (the button, if any, stays inert).
     *  @param telegramWhenUnset  see wireTelegram. */
    async init({ setMuted = null, telegramWhenUnset = null } = {}) {
      await runtimeReady;
      if (setMuted) wireVoice(setMuted);
      await wireTelegram(telegramWhenUnset);
    },
  };
})();
