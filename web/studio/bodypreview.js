/* A look at a body before she wears it (SPEC §6.8) — the studio's Body page preview.
 *
 * Built from the rooms' own parts, so what you see is what she will wear: a VRM
 * goes on the room's `VrmStage`, framed head to feet the way the desktop window
 * frames her, with the room's idle playing; a Live2D rig goes on the Live2D
 * room's own pixi + Cubism runtime, in a frame (`/live2d/preview/`) — Pixi 6
 * needs `unsafe-eval`, and that belongs on a page under /live2d/, not on the
 * studio's policy. Neither is fetched until a body of that kind is first
 * previewed — three.js is a megabyte the rest of the studio has no use for.
 *
 * One stage per kind for the page's lifetime: the canvas is moved between
 * renders rather than rebuilt, because a WebGL context is a scarce thing and a
 * browser starts dropping the oldest after a dozen. While the preview is off the
 * page — another section is showing — its render loops are paused.
 */

const IDLE_URL = '/models/idle.vrma?v=2';
const LIVE2D_FRAME = '/live2d/preview/';

export function createBodyPreview() {
  const node = document.createElement('div');
  node.className = 'body-preview-stage';
  const status = document.createElement('p');
  status.className = 'body-preview-status';
  node.append(status);

  const stages = { vrm: null, live2d: null };   // { host, ...engine } per kind
  let shown = null;                             // "<kind>:<url>" on screen or loading
  let token = 0;                                // the newest request wins

  const say = (text) => { status.textContent = text; status.hidden = !text; };

  function host(kind) {
    const div = document.createElement('div');
    div.className = `body-preview-canvas body-preview-${kind}`;
    node.prepend(div);
    return div;
  }

  async function vrmStage() {
    if (stages.vrm) return stages.vrm;
    const [{ VrmStage }, { DirectionalLight, HemisphereLight }] = await Promise.all([
      import('../js/stage/VrmStage.js'), import('three')]);
    const div = host('vrm');
    const stage = new VrmStage(div, { transparent: true, frameBody: true });
    // the desktop window's neutral rig (js/main.js): an unlit MToon face shades to black
    const key = new DirectionalLight(0xfff0dc, 1.15);
    key.position.set(-1.2, 1.9, -1.6);
    stage.scene.add(new HemisphereLight(0xf5eeff, 0x35304a, 0.85), key);
    stages.vrm = { div, stage, start: () => stage.start(), stop: () => stage.stop() };
    return stages.vrm;
  }

  async function live2dStage() {
    if (stages.live2d) return stages.live2d;
    const div = host('live2d');
    const frame = document.createElement('iframe');
    frame.title = 'Live2D preview';
    frame.src = LIVE2D_FRAME;
    div.append(frame);
    let pending = null;                       // { url, resolve, reject } awaiting the frame
    const loaded = new Promise((resolve) => frame.addEventListener('load', resolve, { once: true }));
    addEventListener('message', (event) => {
      if (event.source !== frame.contentWindow || event.origin !== location.origin) return;
      const { type, state, text } = event.data || {};
      if (type !== 'status' || !pending) return;
      if (state === 'ready') { pending.resolve(); pending = null; }
      if (state === 'error') { pending.reject(new Error(text || 'the rig would not load')); pending = null; }
    });
    const post = (message) => frame.contentWindow?.postMessage(message, location.origin);
    stages.live2d = {
      div,
      start: () => post({ type: 'resume' }),
      stop: () => post({ type: 'pause' }),
      async load(url) {
        await loaded;
        pending?.reject(new Error('superseded'));
        return new Promise((resolve, reject) => {
          pending = { resolve, reject };
          post({ type: 'model', url });
        });
      },
    };
    return stages.live2d;
  }

  function only(kind) {
    for (const [name, entry] of Object.entries(stages)) {
      if (!entry) continue;
      entry.div.hidden = name !== kind;
      if (name === kind) entry.start(); else entry.stop();
    }
  }

  async function show(entry) {
    if (!entry) { say('Nothing to show.'); return; }
    const key = `${entry.kind}:${entry.url}`;
    if (key === shown) return;
    shown = key;
    const mine = ++token;
    say(`Loading ${entry.title}…`);
    try {
      if (entry.kind === 'vrm') {
        const { stage } = await vrmStage();
        if (mine !== token) return;
        only('vrm');
        await stage.loadModel(entry.url, (fraction) => {
          if (mine === token) say(`Loading ${entry.title}… ${Math.round(fraction * 100)}%`);
        });
        if (mine !== token) return;
        say('');
        // a fresh skeleton has an empty mixer: give her the room's idle, so the
        // preview is her standing, not a T-pose
        stage.playAnimation(IDLE_URL).catch(() => {});
      } else {
        const live2d = await live2dStage();
        if (mine !== token) return;
        only('live2d');
        await live2d.load(entry.url);
        if (mine !== token) return;
        say('');
      }
    } catch (error) {
      if (mine !== token) return;
      shown = null;                 // let a retry through
      say(`Could not show ${entry.title}: ${error.message || error}`);
    }
  }

  // Off the page (another section showing): stop drawing. Back on: resume the
  // kind that is showing. Checked on a slow timer rather than wired into the
  // studio's renderer, which knows nothing about canvases.
  setInterval(() => {
    const kind = shown?.split(':')[0];
    for (const [name, entry] of Object.entries(stages)) {
      if (!entry) continue;
      if (node.isConnected && name === kind) entry.start(); else entry.stop();
    }
  }, 1000);

  return { node, show };
}
