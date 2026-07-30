/* Boot for the text room (SPEC §6.7) — the sanctuary's js/main.js with the whole
 * middle removed. There is no stage, no VRM, no SanctuaryScene and no
 * ControlBridge here: `avatar` events still arrive on the bus and this page
 * simply has nothing to realise them on, which is what makes it cheap.
 *
 * What is kept is everything that isn't a body:
 *
 *   - the enter gesture (SPEC §6.4) — autoplay policy, not rendering, is why it
 *     exists, so it stays: one click, then the sockets;
 *   - the audio graph: VisemeDriver owns the AudioContext and the analyser that
 *     voice.js plays her into. Nothing samples its level() here — there is no
 *     mouth to drive — but the graph is the graph;
 *   - the voice loop (js/voice.js), unchanged: mic, barge-in, typed turns;
 *   - the transcript and the inner-life panel, which are their own scripts and
 *     never knew about a body in the first place (js/chat.js, js/mind.js);
 *   - the context gauge (js/context.js), shared with the sanctuary so the two
 *     masthead readouts cannot disagree.
 */
import { renderContext } from '../js/context.js';
import { VisemeDriver } from '../js/viseme.js';
import { initVoice } from '../js/voice.js';

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
  enter: document.getElementById('enter'),
  enterBtn: document.getElementById('enter-btn'),
};

// Her other two rooms. On a per-character route the links have to carry the id
// with them or they land on whoever the node calls primary — the same rule every
// path on this page follows (shared/runtime.js).
const id = window.YuriOSRuntime?.characterId;
if (id) {
  const scoped = `/characters/${encodeURIComponent(id)}`;
  document.getElementById('way-room').href = `${scoped}/sanctuary/`;
  document.getElementById('way-live2d').href = `${scoped}/live2d`;
}

const viseme = new VisemeDriver();

// The two switches every client carries (js/controls.js, SPEC §10.5). Telegram
// starts OFF in here and nowhere else (SPEC §6.7): this is the page you open on
// the device that is already holding the Telegram chat, and hearing her twice is
// not two answers. `telegramWhenUnset` is only the first-visit answer — press it
// once and the choice is remembered, here and in her other rooms.
window.WorldControls.init({
  setMuted: (m) => viseme.setMuted(m),
  telegramWhenUnset: false,
});

function enter() {
  viseme.context().resume();                   // she can speak now

  addEventListener('world-ev', (e) => {
    if (e.detail?.type === 'context') renderContext(els.context, e.detail);
  });
  window.WorldChat.connect({
    onStatus: (up) => {
      els.avatarStatus.classList.toggle('live', up);
      els.avatarStatus.textContent = up ? 'bus live' : 'bus down';
    },
  });

  initVoice({ viseme, els });
  els.text.focus();                            // a text room opens on the cursor
}

els.enterBtn.addEventListener('click', () => {
  els.enter.classList.add('leaving');
  setTimeout(() => els.enter.remove(), 500);
  enter();
}, { once: true });
