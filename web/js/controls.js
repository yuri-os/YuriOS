/* The local voice switch shared by all three browser rooms. Telegram forwarding
 * is a server setting, not a per-room chat control.
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
 * It is remembered per character in `localStorage`, because a mute you have to
 * re-press every time you open the page is not a setting, it is a chore.
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

  /** true / false, or null for "never set here". */
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

  /* The page's own audio graph, and the holds currently asking to be heard
   * through a mute. Kept out here rather than inside wireVoice because the
   * replay button (js/voice.js) needs the gain, and it is not the switch. */
  let audioGraph = null;
  let currentMuted = true;
  let holds = 0;

  function pushGain() {
    audioGraph?.(currentMuted && holds === 0);
  }

  /** @param setMuted  (muted: boolean) => void — the page's own audio graph.
   *  Called once at wiring time with the remembered value, before she has said
   *  anything, so a muted page is muted from her first syllable. */
  function wireVoice(setMuted) {
    const btn = document.getElementById('voice-mute');
    if (!btn) return;
    audioGraph = setMuted;
    let muted = readPref('voiceMuted') ?? true;
    const apply = () => {
      currentMuted = muted;
      pushGain();
      btn.classList.toggle('muted', muted);
      btn.setAttribute('aria-pressed', String(muted));
      btn.title = muted ? 'unmute her voice' : 'mute her voice';
      btn.setAttribute('aria-label', btn.title);
      window.dispatchEvent(new CustomEvent('voice-mute-change', {
        detail: { muted },
      }));
    };
    btn.addEventListener('click', () => {
      muted = !muted;
      writePref('voiceMuted', muted);
      apply();
    });
    apply();
  }

  window.WorldControls = {
    isVoiceMuted: () => readPref('voiceMuted') ?? true,
    /* Let one thing be heard while the room is muted — the per-message replay
     * button (SPEC §9.11, js/voice.js). Pressing "read it out" is somebody
     * asking to hear *that line*, which is not the same as asking to hear
     * everything she says next; so this holds the gain open rather than
     * flipping the switch, and the switch keeps its glyph, its aria state and
     * its stored value throughout. Returns the release, idempotent — call it
     * however the line ends and the gain goes back to whatever the switch
     * says it should be. Nesting counts, so two overlapping holds don't
     * un-mute the room when the first one lets go. */
    hearThrough() {
      holds += 1;
      pushGain();
      let released = false;
      return () => {
        if (released) return;
        released = true;
        holds = Math.max(0, holds - 1);
        pushGain();
      };
    },
    /** @param setMuted the page's audio-graph hook. */
    async init({ setMuted = null } = {}) {
      await runtimeReady;
      if (setMuted) wireVoice(setMuted);
    },
  };
})();
