/* Boot (SPEC §6.4, §6.5, §10) — build the room, load her, wait for the enter
 * click, then dial in both sockets. Order matters twice:
 *
 *   - the stage + model load BEFORE the gesture, so the click reveals a lit
 *     room with her already in it, not a loading bar;
 *   - the sockets connect AFTER the gesture, so the AudioContext is
 *     user-activated and the greeting (B2 §7) is actually audible.
 *
 * Desktop mode (`?desktop=1`, opened by world/window.py — SPEC §6.5) is this
 * file's decision, not Python's: the sanctuary is never built (the desktop is
 * the room), a neutral light rig replaces the lamp (an unlit MToon face shades
 * toward black), the stage clears to alpha 0 and frames the full body, and the
 * enter gate is skipped — a native window's engines don't demand a gesture, so
 * she can greet the moment the socket opens. Everything on the wire is
 * identical in both modes.
 */
import { DirectionalLight, HemisphereLight } from 'three';

import { ControlBridge } from './bridge.js';
import { renderContext } from './context.js';
import { Music } from './music.js';
import { SanctuaryScene } from './stage/SanctuaryScene.js';
import { VrmStage } from './stage/VrmStage.js';
import { initVoice } from './voice.js';
import { VisemeDriver } from './viseme.js';

// set before first paint by the inline script in index.html
const DESKTOP = document.documentElement.classList.contains('desktop');

const els = {
  status: document.getElementById('status'),
  avatarStatus: document.getElementById('avatar-status'),
  context: document.getElementById('context'),
  latency: document.getElementById('latency'),
  caption: document.getElementById('caption'),
  mic: document.getElementById('mic'),
  micLabel: document.getElementById('mic-label'),
  text: document.getElementById('text'),
  send: document.getElementById('send'),
  rainMute: document.getElementById('rain-mute'),
  ambience: document.getElementById('ambience'),
  enter: document.getElementById('enter'),
  enterBtn: document.getElementById('enter-btn'),
};

// The ?v tag busts caches that predate the Cache-Control fix (world/main.py):
// a different URL is a guaranteed cache miss. Bump it when the bundled body
// changes (and BUNDLED_VRM_URL in world/host/studio.py with it).
// v2 = AvatarSample_B (the YuriOS body).
const BUNDLED_VRM_URL = '/models/avatar.vrm?v=2';
const IDLE_URL = '/models/idle.vrma?v=2';

/** The body she wears (SPEC §6.8): her own `.vrm` if the switchboard gave her
 *  one, else the bundled body. A page with no character behind it, or a host
 *  that cannot say, gets the bundled body rather than an empty room. */
async function bodyUrl() {
  const runtime = window.YuriOSRuntime;
  if (!runtime?.characterId) return BUNDLED_VRM_URL;
  try {
    const response = await fetch(runtime.apiPath('/api/body'), { cache: 'no-store' });
    if (!response.ok) return BUNDLED_VRM_URL;
    const body = await response.json();
    return typeof body?.vrm_url === 'string' && body.vrm_url ? body.vrm_url : BUNDLED_VRM_URL;
  } catch (_) {
    return BUNDLED_VRM_URL;
  }
}

async function boot() {
  const stage = new VrmStage(document.getElementById('scene'),
    { transparent: DESKTOP, frameBody: DESKTOP });
  let room = null;
  if (DESKTOP) {
    // the room, set aside (SPEC §6.5) — the lamp went with it, so light her
    // with a neutral rig: soft sky/ground fill + one warm key from front-left.
    const hemi = new HemisphereLight(0xf5eeff, 0x35304a, 0.85);
    const key = new DirectionalLight(0xfff0dc, 1.15);
    key.position.set(-1.2, 1.9, -1.6);
    stage.scene.add(hemi, key);
  } else {
    room = new SanctuaryScene(stage.scene, stage.renderer, stage.camera);
    stage.environment = room;                  // room.update(dt) joins the frame
    stage.post = room.post;                    // …and the frame goes through bloom
  }
  stage.start();                               // renders while she loads

  const viseme = new VisemeDriver();
  stage.setVisemeSource(viseme.level);         // loop step 9 reads this (SPEC §5)
  const music = new Music(() => viseme.context());

  // the rain-mute button (footer): silences just the audible rain bed; the
  // window keeps raining and the brain's `rain` commands still land, they're
  // simply inaudible until you toggle back.
  els.rainMute?.addEventListener('click', () => {
    const muted = music.setRainMuted();
    els.rainMute.classList.toggle('muted', muted);
    els.rainMute.setAttribute('aria-pressed', String(muted));
    els.rainMute.title = muted ? 'unmute the rain' : 'mute the rain';
    els.rainMute.setAttribute('aria-label', els.rainMute.title);
    els.rainMute.querySelector('[data-rain-label]').textContent = muted ? 'rain muted' : 'rain on';
  });

  // The local speaker switch is shared with the Live2D body and text room.
  // Voice starts muted and its heavy socket is demand-driven in voice.js.
  window.WorldControls.init({ setMuted: (m) => viseme.setMuted(m) });

  // The gate's button only becomes real once her body is in the room (the click
  // handler is attached below, after the load): disable it until then so a slow
  // wake *reads* as one instead of silently swallowing the click — the boot log
  // beside it already says what's still loading (SPEC §6.4).
  if (els.enterBtn) els.enterBtn.disabled = true;
  try {
    await stage.loadModel(await bodyUrl());
    await stage.playAnimation(IDLE_URL);
  } catch (e) {
    console.error('model load failed:', e);
    els.caption.textContent = 'her body failed to load — check web/models/';
  }
  // …and enabled either way: a body that failed to load must not lock her out
  // of her own room — the voice loop is the part that matters.
  if (els.enterBtn) els.enterBtn.disabled = false;

  const enter = () => {
    viseme.context().resume();                 // she can speak now

    // the one bus (SPEC §10): chat.js opens /api/events and re-dispatches every
    // event as `world-ev`; the bridge realises the `avatar` ones on the stage.
    // The rain is off unless the house sends weather (SPEC §6.2), and a switch
    // for a sound that isn't playing is only clutter — so it appears with the rain.
    const onRain = (intensity) => {
      if (els.ambience) els.ambience.hidden = !(intensity > 0);
    };
    // A body swapped in the switchboard arrives as `load_model` (SPEC §6.8): the
    // new skeleton starts with an empty mixer, so give her the idle back.
    const onModel = () => stage.playAnimation(IDLE_URL)
      .catch((e) => console.warn('idle after a body swap failed:', e));
    const bridge = new ControlBridge(stage, { room, music, onRain, onModel });
    bridge.listen();
    addEventListener('world-ev', (e) => {
      if (e.detail?.type === 'context') renderContext(els.context, e.detail);
    });
    window.WorldChat.connect({
      onStatus: (up) => {
        els.avatarStatus.classList.toggle('live', up);
        // words, not symbols: the masthead reads as a row of switchboard
        // micro-labels now, and no glyph can go tofu in one (Chromium/Linux)
        els.avatarStatus.textContent = up ? 'bus live' : 'bus down';
      },
    });

    initVoice({ viseme, els });
  };

  if (DESKTOP) {
    // no gate (SPEC §6.5): auto-enter; the first click still resumes a context
    // a stricter engine left suspended, so the worst case is a quiet greeting,
    // never a dead one.
    els.enter?.remove();
    // shared/chat-options.js places the complete composer on the hover bar.
    enter();
    addEventListener('pointerdown', () => viseme.context().resume(), { once: true });
  } else {
    // the enter gesture (SPEC §6.4): one click, then the sockets
    els.enterBtn.addEventListener('click', () => {
      els.enter.classList.add('leaving');
      setTimeout(() => els.enter.remove(), 700);
      enter();
    }, { once: true });
  }
}

boot();
