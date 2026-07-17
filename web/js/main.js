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
  latency: document.getElementById('latency'),
  caption: document.getElementById('caption'),
  mic: document.getElementById('mic'),
  micLabel: document.getElementById('mic-label'),
  text: document.getElementById('text'),
  enter: document.getElementById('enter'),
  enterBtn: document.getElementById('enter-btn'),
};

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
    room = new SanctuaryScene(stage.scene);
    stage.environment = room;                  // room.update(dt) joins the frame
  }
  stage.start();                               // renders while she loads

  const viseme = new VisemeDriver();
  stage.setVisemeSource(viseme.level);         // loop step 9 reads this (SPEC §5)
  const music = new Music(() => viseme.context());

  try {
    // The ?v tag busts caches that predate the Cache-Control fix (world/main.py):
    // a different URL is a guaranteed cache miss. Bump it when the bundled body
    // changes. v2 = AvatarSample_B (the YuriOS body).
    await stage.loadModel('/models/avatar.vrm?v=2');
    await stage.playAnimation('/models/idle.vrma?v=2');
  } catch (e) {
    console.error('model load failed:', e);
    els.caption.textContent = 'her body failed to load — check web/models/';
  }

  const enter = () => {
    viseme.context().resume();                 // she can speak now

    // the one bus (SPEC §10): chat.js opens /api/events and re-dispatches every
    // event as `world-ev`; the bridge realises the `avatar` ones on the stage.
    const bridge = new ControlBridge(stage, { room, music });
    bridge.listen();
    window.WorldChat.connect({
      onStatus: (up) => {
        els.avatarStatus.classList.toggle('live', up);
        els.avatarStatus.textContent = up ? 'bus ·' : 'bus ✕';
      },
    });

    initVoice({ viseme, els });
  };

  if (DESKTOP) {
    // no gate (SPEC §6.5): auto-enter; the first click still resumes a context
    // a stricter engine left suspended, so the worst case is a quiet greeting,
    // never a dead one.
    els.enter?.remove();
    // the chat column is hidden on the desktop (§6.5) — the composer moves to
    // the hover bar so typing to her still works
    document.querySelector('.controls')?.appendChild(els.text);
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
