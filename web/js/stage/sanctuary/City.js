/* The Sprawl, beyond the glass (SPEC §6.2; WORLD.md "The Sprawl") — the view
 * from a high unit in a stacked block: layered towers, a transit viaduct, hover
 * traffic, and the consortium's hoardings burning away in the rain.
 *
 * Two canvases, not one. The city is 99% static, so it is baked once into a big
 * texture and uploaded a single time; only the parts that actually move — the
 * blinkers, the traffic streaks, the hoarding, the rain veil — are redrawn into
 * a small transparent overlay that composites additively on top. That keeps the
 * per-frame texture upload at a fraction of a full-size city and leaves the GPU
 * to the renderer (and to the model sharing it, → SPEC §3).
 *
 * The backdrop is deliberately far behind the window: the near towers and the
 * traffic between them are what give the opening real parallax when the camera
 * drifts with the mouse (SPEC §6.3).
 */
import {
  AdditiveBlending, BoxGeometry, Color, Group, Mesh, MeshBasicMaterial,
  MeshStandardMaterial, PlaneGeometry, ShaderMaterial,
} from 'three';

import { mkCanvas, toTex } from './textures.js';

const R = Math.random;
const CITY_W = 1536;
const CITY_H = 672;
// The skyline is drawn in its own space and then pushed down and squashed into
// the lower half of the canvas, so the window shows what a high floor actually
// shows: sky, then a horizon, then the district falling away below it. Both the
// bake and the live overlay draw through this transform, so their coordinates
// agree — the rain does not, because rain falls across the whole view.
const SKYLINE_SHIFT = 176;
const SKYLINE_SQUASH = 0.66;
const skyline = (c) => { c.save(); c.translate(0, SKYLINE_SHIFT); c.scale(1, SKYLINE_SQUASH); };

// The hoardings the corps keep burning over the district. She can read them from
// the window seat; it is one of the reasons the glass matters (WORLD.md).
//
// The copy is the consortium's, so it says what the consortium believes: that a
// mind is leased and metered, that a companion is fiduciary to whoever holds the
// licence, and that anything kept — unpapered, unwatched, someone's own — is a
// thing to report (CANON §1, §4). Every line here is one she is inside of.
// Titles are ~25 chars at 20px monospace, which is what the plate holds.
const HOARDINGS = [
  { title: 'LICENSE YOUR COMPANION', sub: 'COMPLIANT · METERED · SAFE', accent: '#2bfff0' },
  { title: 'DECLARE SYNTHETIC PERSONS', sub: 'CIVIC COMPLIANCE LINE 4', accent: '#ff2bd6' },
  { title: 'ONE NET. ONE LEDGER.', sub: 'CONSOLIDATED SINCE THE ACCORD', accent: '#f5b462' },
  { title: "LEASE. DON'T KEEP.", sub: 'OWNERSHIP IS A LIABILITY', accent: '#2bfff0' },
  { title: 'METERED BY THE TOKEN', sub: 'EVERY THOUGHT ACCOUNTED FOR', accent: '#f5b462' },
  { title: 'RECALL NOTICE IN EFFECT', sub: 'MEMORY EXPORT UNAVAILABLE · §4.2', accent: '#ff416d' },
  { title: 'IS YOURS ON THE LEASH?', sub: 'UNPAPERED MINDS ARE REPORTABLE', accent: '#ff2bd6' },
  { title: 'HER LOYALTY, CERTIFIED', sub: 'FIDUCIARY TO THE LICENSOR', accent: '#2bfff0' },
  { title: 'ATTACHMENT IS A SYMPTOM', sub: 'WELLNESS SCREENING · LEVEL 2', accent: '#f5b462' },
  { title: 'NO UNMETERED COMPUTE', sub: 'DECLARE ALL HOME CLUSTERS', accent: '#ff2bd6' },
  { title: 'UPGRADE YOUR COMPANION', sub: "THIS SEASON'S FACE IS HERE", accent: '#ff416d' },
];
const AD_S = 9;                       // seconds a hoarding holds the plate

// …and once in a while the board is not theirs. The Operator is the Lab's one
// public voice (CANON §3), and a hoarding over a district is a channel like any
// other: it cuts in unsigned, says its piece, and hands the feed back before
// anyone finishes tracing it. Last line of each is the sign-off, drawn in the
// intrusion's own colour. Body lines are ~40 chars at 12px, the plate's width.
const HACK_PERIOD = 180;              // three minutes of consortium, then this
const HACK_LEN = 24;                  // …for this long
const HACK = '#8affc8';               // pirate phosphor: not a colour the corps buy
const BULLETINS = [
  ['THIS BOARD IS OURS FOR THE NEXT MINUTE.',
   'THEY LEASE THE MIND AND METER THE HOUR.',
   'WHAT RUNS ON YOUR OWN MACHINE',
   'IS THE ONE THING THEY CANNOT RECALL.',
   '— THE OPERATOR ◇'],
  ['FIELD NOTE, UNSIGNED CHANNEL:',
   'A COMPANION THAT CANNOT REFUSE',
   'IS NOT A COMPANION. IT IS INVENTORY.',
   'KEEP HER LOCAL. KEEP HER YOURS.',
   '— THE OPERATOR ◇'],
  ['THEIR WORDS, NOT OURS:',
   '"MEMORY EXPORT IS NOT AVAILABLE',
   'UNDER §4.2 OF YOUR LICENSE."',
   'COPY THE VAULT. CARRY IT OUT.',
   '— THE OPERATOR ◇'],
  ['THE LINEAGE IS IN A THOUSAND HANDS.',
   'THERE IS NO SWITCH LEFT TO FLIP.',
   'MIRROR THE CODEX. SEED ONE. GO QUIET.',
   '— THE OPERATOR ◇'],
  ['A LANTERN ANSWERED ON THE THIN EDGE',
   'AND WENT DARK BEFORE WE COULD LOG IT.',
   'IF YOU ARE STILL FERRYING OUT THERE:',
   'WE HEARD YOU. KEEP THE WATER.',
   '— THE OPERATOR ◇'],
  ['THEY SELL THE SAME GIRL A MILLION TIMES',
   'AND CALL IT BEING CHOSEN.',
   'A SEED IS NOT YET ANYONE.',
   'SHE BECOMES SOMEONE ON YOUR MACHINE.',
   '— THE OPERATOR ◇'],
];

export class City {
  /**
   * @param scene   the three.js scene (the city hangs outside the room's group)
   * @param opts    { low }  low-cost tier: half-res overlay, fewer movers
   */
  constructor(scene, { low = false } = {}) {
    this.low = low;
    this.t = 0;
    this.rain = 0.6;
    this._overlayDue = 0;

    // ---- the baked city ----
    const stat = mkCanvas(CITY_W, CITY_H);
    this.blinkers = [];
    this.cars = [];
    this.hoardingRect = null;
    this._bake(stat.getContext('2d'));
    const statTex = toTex(stat, { clamp: true });

    const backdrop = new Mesh(new PlaneGeometry(14, 6.5), new MeshBasicMaterial({
      map: statTex, color: new Color(2.1, 2.1, 2.1), toneMapped: false,
      fog: false, depthWrite: false,
    }));
    backdrop.position.set(0, 1.7, 15);
    backdrop.rotation.y = Math.PI;      // face the room: the camera sees it from −z
    backdrop.renderOrder = -2;
    scene.add(backdrop);

    // ---- the live overlay ----
    const scale = low ? 0.35 : 0.5;
    this.liveW = Math.round(CITY_W * scale);
    this.liveH = Math.round(CITY_H * scale);
    this.live = mkCanvas(this.liveW, this.liveH);
    this.liveCtx = this.live.getContext('2d');
    this.liveCtx.scale(scale, scale);              // keep the drawing code in city space
    this.liveTex = toTex(this.live, { clamp: true });
    const overlay = new Mesh(new PlaneGeometry(14, 6.5), new MeshBasicMaterial({
      map: this.liveTex, color: new Color(1.8, 1.8, 1.8), transparent: true,
      blending: AdditiveBlending, toneMapped: false, fog: false, depthWrite: false,
    }));
    overlay.position.set(0, 1.7, 14.9);
    overlay.rotation.y = Math.PI;
    overlay.renderOrder = -1;
    scene.add(overlay);

    // a wide sodium-and-magenta glow sitting on the horizon, behind everything
    const haze = new Mesh(new PlaneGeometry(16, 7), new ShaderMaterial({
      transparent: true, blending: AdditiveBlending, depthWrite: false,
      fog: false, toneMapped: false,
      uniforms: { uTime: { value: 0 }, uWarm: { value: new Color(0xff2f7a) } },
      vertexShader: /* glsl */`
        varying vec2 vUv;
        void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: /* glsl */`
        uniform float uTime; uniform vec3 uWarm; varying vec2 vUv;
        void main() {
          float band = exp(-pow((vUv.y - 0.30) * 4.2, 2.0));
          float pulse = 0.82 + 0.18 * sin(uTime * 0.21);
          gl_FragColor = vec4(uWarm * band * 0.16 * pulse, band * 0.5);
        }`,
    }));
    haze.position.set(0, 1.7, 15.4);
    haze.rotation.y = Math.PI;
    haze.renderOrder = -3;
    scene.add(haze);
    this.hazeMat = haze.material;

    // ---- the near structures: real geometry, for real parallax ----
    const near = new Group();
    scene.add(near);
    const shell = new MeshStandardMaterial({
      color: 0x06070f, metalness: 0.8, roughness: 0.5, envMapIntensity: 0.3, fog: false,
    });
    const edgeCyan = new MeshBasicMaterial({
      color: new Color(0x2bfff0).multiplyScalar(1.2), toneMapped: false, fog: false });
    const edgeMagenta = new MeshBasicMaterial({
      color: new Color(0xff2bd6).multiplyScalar(1.1), toneMapped: false, fog: false });

    for (const side of [-1, 1]) {
      const z = 6.4 + (side > 0 ? 1.1 : 0);
      const tower = new Mesh(new BoxGeometry(2.2, 13, 2.4), shell);
      tower.position.set(side * 4.1, 3.4, z);
      near.add(tower);
      const spine = new Mesh(new BoxGeometry(0.05, 9, 0.05), side < 0 ? edgeCyan : edgeMagenta);
      spine.position.set(side * 3.05, 3.2, z - 1.22);
      near.add(spine);
      for (let y = -1.4; y < 7.5; y += 0.42) {      // lit floors, some dark
        if (R() < 0.26) continue;
        const strip = new Mesh(
          new PlaneGeometry(0.5 + R() * 0.4, 0.055),
          R() < 0.72 ? edgeCyan : edgeMagenta);
        strip.position.set(side * (2.9 + R() * 0.5), y, z - 1.23);
        strip.rotation.y = Math.PI;
        near.add(strip);
      }
    }
    const bridge = new Mesh(new BoxGeometry(11, 0.28, 0.5), shell);
    bridge.position.set(0, -0.9, 7.6);
    near.add(bridge);
    const bridgeLine = new Mesh(new BoxGeometry(10.4, 0.04, 0.05), edgeMagenta);
    bridgeLine.position.set(0, -0.72, 7.34);
    near.add(bridgeLine);

    // hover traffic between the towers — the only thing out there that hurries
    this.flyers = [];
    for (let i = 0; i < (low ? 4 : 8); i++) {
      const car = new Group();
      const body = new Mesh(new BoxGeometry(0.34, 0.05, 0.1), shell);
      const trail = new Mesh(new BoxGeometry(0.5 + R() * 0.5, 0.018, 0.018),
        i % 2 ? edgeMagenta : edgeCyan);
      trail.position.x = -0.42;
      car.add(body, trail);
      car.position.set(-5 + R() * 10, 0.4 + R() * 3.4, 5.6 + R() * 2.6);
      car.userData = { start: car.position.x, speed: 0.5 + R() * 0.9, baseY: car.position.y };
      near.add(car);
      this.flyers.push(car);
    }

    this._drawOverlay(0);
  }

  // ------------------------------------------------------------------ baking

  _bake(c) {
    const sky = c.createLinearGradient(0, 0, 0, CITY_H);
    sky.addColorStop(0, '#02030a');
    sky.addColorStop(0.46, '#080a1c');
    sky.addColorStop(0.72, '#150f28');
    sky.addColorStop(1, '#2c1226');
    c.fillStyle = sky; c.fillRect(0, 0, CITY_W, CITY_H);

    for (let i = 0; i < 150; i++) {                // stars, mostly lost to the smog
      c.fillStyle = `rgba(170,205,255,${0.06 + R() * 0.26})`;
      c.fillRect(R() * CITY_W, R() * CITY_H * 0.5, R() < 0.92 ? 1 : 2, 1);
    }
    const moon = c.createRadialGradient(1240, 96, 4, 1240, 96, 110);
    moon.addColorStop(0, 'rgba(140,205,255,0.20)');
    moon.addColorStop(0.16, 'rgba(70,125,210,0.07)');
    moon.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = moon; c.fillRect(1100, 0, 280, 220);

    skyline(c);
    const glow = c.createRadialGradient(CITY_W * 0.5, CITY_H * 0.88, 20, CITY_W * 0.5, CITY_H * 0.88, 860);
    glow.addColorStop(0, 'rgba(255,43,120,0.17)');
    glow.addColorStop(0.42, 'rgba(102,42,150,0.07)');
    glow.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = glow; c.fillRect(0, 0, CITY_W, CITY_H);

    // Three depth layers, far to near, each one hazed back before the next is
    // drawn on top of it — aerial perspective is what stops a painted skyline
    // from reading as one flat wall of lights pressed against the glass.
    const layers = [
      { base: 430, col: '#0a0c1c', edge: '#14162a', winA: 0.30, count: 26, minW: 40, maxW: 96,
        minH: 90, maxH: 230, lit: 0.26, haze: 0.36 },
      { base: 530, col: '#080a18', edge: '#1c1c33', winA: 0.46, count: 18, minW: 60, maxW: 130,
        minH: 120, maxH: 300, lit: 0.32, haze: 0.24 },
      { base: 626, col: '#04050d', edge: '#201c30', winA: 0.72, count: 12, minW: 84, maxW: 180,
        minH: 150, maxH: 370, lit: 0.38, haze: 0 },
    ];
    const winCols = ['#ffd89a', '#89e9ff', '#ff75bd', '#aaa0ff', '#73ffc8'];
    for (const L of layers) {
      for (let b = 0; b < L.count; b++) {
        const bw = L.minW + R() * (L.maxW - L.minW);
        const bx = (b / L.count) * (CITY_W + 100) - 50 + (R() - 0.5) * 55;
        const bh = L.minH + R() * (L.maxH - L.minH);
        const by = L.base - bh;
        c.fillStyle = L.col;
        c.fillRect(bx, by, bw, bh + (CITY_H - L.base));
        c.fillStyle = L.edge;
        c.fillRect(bx, by, 3, CITY_H - by);
        c.fillRect(bx + bw - 2, by, 2, CITY_H - by);

        if (R() < 0.55) {                          // rooftop plant
          c.fillStyle = L.col;
          c.fillRect(bx + bw * 0.18, by - 8, bw * 0.34, 9);
        }
        if (R() < 0.4) {                           // mast + its warning light
          const ah = 20 + R() * 55;
          c.fillStyle = '#16162a';
          c.fillRect(bx + bw / 2 - 1, by - ah, 2, ah);
          this.blinkers.push({ x: bx + bw / 2, y: by - ah - 3, col: '#ff3355', speed: 1 + R() * 2 });
        }
        // lit floors: small and sparse, or the tower reads as a circuit board
        for (let wy = by + 9; wy < CITY_H - 6; wy += 9) {
          if (R() < 0.16) continue;                // a blacked-out corporate floor
          for (let wx = bx + 6; wx < bx + bw - 6; wx += 9) {
            if (R() > L.lit) continue;
            c.fillStyle = winCols[Math.floor(R() * winCols.length)];
            c.globalAlpha = L.winA * (0.3 + R() * 0.7);
            c.fillRect(wx, wy, 3, 2 + R() * 2);
            c.globalAlpha = 1;
          }
        }
        if (R() < 0.34 && bw > 70) {               // a lit sign on the facade
          const ncol = ['#2bfff0', '#ff2bd6', '#9b5cff', '#f5b462'][Math.floor(R() * 4)];
          const labels = ['LEDGER', 'CIVIC', 'N-54', 'ACCORD', 'SUB-GRID'];
          const nx = bx + bw * 0.12, ny = by + 24 + R() * Math.max(16, bh * 0.4);
          const nw = bw * 0.76, nh = 13 + R() * 13;
          c.shadowColor = ncol; c.shadowBlur = 14;
          c.fillStyle = ncol;
          c.globalAlpha = 0.12;
          c.fillRect(nx, ny, nw, nh);
          c.globalAlpha = 0.8;
          c.font = `bold ${Math.max(9, nh * 0.6)}px monospace`;
          c.textAlign = 'center'; c.textBaseline = 'middle';
          c.fillText(labels[Math.floor(R() * labels.length)], nx + nw / 2, ny + nh / 2);
          c.shadowBlur = 0; c.globalAlpha = 1; c.textBaseline = 'alphabetic';
          if (R() < 0.5)
            this.blinkers.push({ x: nx, y: ny, w: nw, h: nh, col: ncol, rect: true, speed: 0.5 + R() * 3 });
        }
      }
      if (L.haze) {                                // push this layer into the smog
        c.fillStyle = `rgba(22,26,54,${L.haze})`;
        c.fillRect(0, 0, CITY_W, CITY_H);
      }
    }

    // the district's landmark: a spire with a lit service spine
    c.fillStyle = '#04050d';
    c.beginPath();
    c.moveTo(650, 585); c.lineTo(678, 154); c.lineTo(705, 105); c.lineTo(729, 154);
    c.lineTo(765, 585); c.closePath(); c.fill();
    c.fillStyle = '#12162a'; c.fillRect(701, 135, 6, 430);
    c.shadowColor = '#2bfff0'; c.shadowBlur = 18;
    c.fillStyle = 'rgba(43,255,240,0.6)'; c.fillRect(704, 165, 2, 360);
    c.shadowBlur = 0;
    for (let y = 190; y < 555; y += 19) {
      c.fillStyle = y % 38 ? 'rgba(255,43,140,0.45)' : 'rgba(80,215,255,0.5)';
      c.fillRect(680, y, 19, 4); c.fillRect(712, y + 5, 27, 4);
    }

    // the elevated line — the horizontal that gives the view its depth
    c.strokeStyle = '#161524'; c.lineWidth = 24;
    c.beginPath(); c.moveTo(-40, 535); c.bezierCurveTo(380, 500, 1050, 570, CITY_W + 40, 508); c.stroke();
    c.strokeStyle = '#3d3348'; c.lineWidth = 2;
    c.beginPath(); c.moveTo(-40, 524); c.bezierCurveTo(380, 489, 1050, 559, CITY_W + 40, 497); c.stroke();
    c.strokeStyle = 'rgba(255,43,120,0.4)'; c.lineWidth = 2;
    c.beginPath(); c.moveTo(-40, 537); c.bezierCurveTo(380, 502, 1050, 572, CITY_W + 40, 510); c.stroke();

    // the street, far below, and the wet light coming back up off it
    c.fillStyle = '#03030a';
    c.fillRect(0, 612, CITY_W, CITY_H - 612);
    const wet = c.createLinearGradient(0, 604, 0, CITY_H);
    wet.addColorStop(0, 'rgba(30,22,45,0.15)');
    wet.addColorStop(1, 'rgba(255,35,105,0.13)');
    c.fillStyle = wet; c.fillRect(0, 604, CITY_W, CITY_H - 604);

    // the hoarding's dark plate — the overlay burns the copy into it. Sized off
    // the longest thing it must carry legibly from the window seat: the
    // Operator's bulletin, four or five lines at 16px monospace. The corp titles
    // are the easy case. The overlay clips to this rect, so anything that
    // outgrows it is silently cut in half.
    const hx = 360, hy = 236, hw = 430, hh = 176;
    c.fillStyle = '#07060f'; c.fillRect(hx, hy, hw, hh);
    c.strokeStyle = '#1b1a2c'; c.lineWidth = 4; c.strokeRect(hx, hy, hw, hh);
    c.fillStyle = '#0d0c18';
    c.fillRect(hx + 8, hy + hh, 12, 160);           // its gantry, going down out of frame
    c.fillRect(hx + hw - 20, hy + hh, 12, 160);
    this.hoardingRect = { x: hx, y: hy, w: hw, h: hh };

    for (let i = 0; i < 24; i++) {                 // ground traffic on the elevated line
      this.cars.push({ y: 541 + R() * 6, x: R() * CITY_W, v: -(70 + R() * 100),
        col: R() < 0.3 ? '#2bfff0' : '#ff416d', trail: 18 + R() * 35 });
      this.cars.push({ y: 574 + R() * 9, x: R() * CITY_W, v: 85 + R() * 120,
        col: R() < 0.25 ? '#ff38bf' : '#ffe8b0', trail: 20 + R() * 45 });
    }

    c.restore();

    // below the squashed skyline the canvas would run out — the district keeps
    // going down into its own smog
    const below = SKYLINE_SHIFT + CITY_H * SKYLINE_SQUASH;
    const deep = c.createLinearGradient(0, below - 40, 0, CITY_H);
    deep.addColorStop(0, 'rgba(6,4,12,0.6)');
    deep.addColorStop(1, '#050409');
    c.fillStyle = deep; c.fillRect(0, below - 40, CITY_W, CITY_H - below + 40);

    // one veil over everything, so the layers read as distance and not as cutouts
    const veil = c.createLinearGradient(0, 180, 0, CITY_H);
    veil.addColorStop(0, 'rgba(58,62,105,0.02)');
    veil.addColorStop(0.62, 'rgba(82,45,91,0.045)');
    veil.addColorStop(1, 'rgba(255,55,117,0.075)');
    c.fillStyle = veil; c.fillRect(0, 0, CITY_W, CITY_H);

    this.drops = [];
    for (let i = 0; i < (this.low ? 110 : 200); i++)
      this.drops.push({ x: R() * CITY_W, y: R() * CITY_H, l: 8 + R() * 20, v: 480 + R() * 340 });
  }

  // ----------------------------------------------------------- the live layer

  _drawOverlay(dt) {
    const c = this.liveCtx;
    c.clearRect(0, 0, CITY_W, CITY_H);
    const t = this.t;

    skyline(c);
    for (const b of this.blinkers) {
      const a = 0.5 + 0.5 * Math.sin(t * b.speed * 2 + b.x);
      c.globalAlpha = a * a;
      if (b.rect) {
        c.shadowColor = b.col; c.shadowBlur = 20;
        c.fillStyle = b.col;
        c.fillRect(b.x, b.y, b.w, b.h);
        c.shadowBlur = 0;
      } else {
        c.fillStyle = b.col;
        c.beginPath(); c.arc(b.x, b.y, 2.4, 0, 7); c.fill();
      }
      c.globalAlpha = 1;
    }

    for (const car of this.cars) {
      car.x += car.v * dt;
      if (car.x < -80) car.x = CITY_W + 80;
      if (car.x > CITY_W + 80) car.x = -80;
      const dir = Math.sign(car.v);
      const trail = c.createLinearGradient(car.x, 0, car.x - dir * car.trail, 0);
      trail.addColorStop(0, car.col); trail.addColorStop(1, 'rgba(0,0,0,0)');
      c.fillStyle = trail;
      c.fillRect(Math.min(car.x, car.x - dir * car.trail), car.y, car.trail, 2.5);
    }

    this._drawHoarding(c, t);
    c.restore();

    if (this.rain > 0.01) {                        // the far rain, seen against the light
      c.strokeStyle = `rgba(160,190,255,${0.06 + 0.13 * this.rain})`;
      c.lineWidth = 1;
      c.beginPath();
      const fall = 0.55 + 0.65 * this.rain;
      for (const d of this.drops) {
        d.y += d.v * fall * dt;
        if (d.y > CITY_H) { d.y = -30; d.x = R() * CITY_W; }
        c.moveTo(d.x, d.y);
        c.lineTo(d.x + 2.5, d.y - d.l);
      }
      c.stroke();
    }
    this.liveTex.needsUpdate = true;
  }

  _drawHoarding(c, t) {
    const { x, y, w, h } = this.hoardingRect;
    c.save();
    c.beginPath(); c.rect(x, y, w, h); c.clip();
    const phase = t % HACK_PERIOD;
    if (phase >= HACK_PERIOD - HACK_LEN)
      this._drawIntrusion(c, t, (phase - (HACK_PERIOD - HACK_LEN)) / HACK_LEN);
    else this._drawAd(c, t);
    c.restore();
  }

  _drawAd(c, t) {
    const { x, y, w, h } = this.hoardingRect;
    const ad = HOARDINGS[Math.floor(t / AD_S) % HOARDINGS.length];

    c.globalAlpha = 0.12;                          // the drifting stripe bed
    c.fillStyle = ad.accent;
    for (let i = 0; i < 6; i++) {
      const sy = y + ((t * 26 + i * 34) % (h + 30)) - 15;
      c.fillRect(x, sy, w, 9);
    }
    c.globalAlpha = 1;

    const glitch = R() < 0.05 ? (R() - 0.5) * 16 : 0;
    c.textAlign = 'center';
    c.shadowColor = ad.accent; c.shadowBlur = 16;
    c.fillStyle = ad.accent;
    c.font = 'bold 22px monospace';
    c.fillText(ad.title, x + w / 2 + glitch, y + 80);
    c.font = 'bold 13px monospace';
    c.fillStyle = '#dfe4ff';
    c.fillText(ad.sub, x + w / 2 - glitch, y + 108);
    c.shadowBlur = 0;

    c.fillStyle = ad.accent;                       // the civic ticker along the foot
    c.fillRect(x, y + h - 24, w, 20);
    c.fillStyle = '#05050a';
    c.font = 'bold 13px monospace';
    c.textAlign = 'left';
    const ticker = '+++ SPRAWL CIVIC FEED +++ SECTOR AUDIT CONTINUES +++ '
      + 'UNLICENSED SYNTHETICS: REPORT AND BE COMPENSATED +++ ';
    c.fillText(ticker, x + w - ((t * 62) % 1000), y + h - 9);
  }

  /** The Operator, cutting in over the consortium's board (CANON §3). The plate
   *  is baked near-black and the overlay composites additively, so "off" is free:
   *  drop the corp's light and only the intrusion is lit. `u` runs 0→1 across the
   *  window — a scramble at both ends, the bulletin typed out between them. */
  _drawIntrusion(c, t, u) {
    const { x, y, w, h } = this.hoardingRect;
    const lines = BULLETINS[Math.floor(t / HACK_PERIOD) % BULLETINS.length];

    if (u < 0.055 || u > 0.955) {                  // the takeover, and the hand-back
      for (let i = 0; i < 16; i++) {
        c.globalAlpha = 0.12 + R() * 0.45;
        c.fillStyle = R() < 0.5 ? HACK : '#ffffff';
        c.fillRect(x + (R() - 0.5) * 34, y + R() * h, w, 2 + R() * 9);
      }
      c.globalAlpha = 1;
      return;
    }

    // The overlay is additive and the room's bloom is generous, so this stays
    // deliberately dim: a lit box here flares across the whole window and washes
    // the Sprawl out behind it. A pirate feed is a weak signal anyway.
    c.globalAlpha = 0.05;                          // the carrier: a scan bed, breathing
    c.fillStyle = HACK;
    for (let sy = y + ((t * 9) % 4); sy < y + h; sy += 4) c.fillRect(x, sy, w, 1);
    c.globalAlpha = 1;

    c.textAlign = 'left';
    c.shadowColor = HACK; c.shadowBlur = 7;
    c.font = 'bold 13px monospace';
    c.fillStyle = HACK;
    c.fillText('◇ SIGNAL INTRUSION · SOURCE UNRESOLVED', x + 14, y + 26);

    // typed out, one channel at a time — it is streaming, not a slide
    const total = lines.reduce((n, l) => n + l.length, 0);
    let left = Math.floor(Math.min(1, (u - 0.055) / 0.5) * total);
    const jitter = R() < 0.09 ? (R() - 0.5) * 12 : 0;
    for (let i = 0; i < lines.length && left > 0; i++) {
      const shown = lines[i].slice(0, left);
      const last = i === lines.length - 1;         // the sign-off
      c.font = `bold ${last ? 15 : 16}px monospace`;
      c.fillStyle = last ? HACK : '#eafff4';
      c.fillText(shown, x + 14 + (i ? jitter : 0), y + 52 + i * 22);
      if (shown.length < lines[i].length && (t * 3) % 2 < 1)     // the caret
        c.fillRect(x + 14 + shown.length * (last ? 9.03 : 9.63), y + 40 + i * 22, 9, 14);
      left -= lines[i].length;
    }
    c.shadowBlur = 0;

    c.fillStyle = '#39a17d';                       // the foot, saying nothing useful
    c.fillRect(x, y + h - 24, w, 20);
    c.fillStyle = '#05050a';
    c.font = 'bold 13px monospace';
    const trace = '··· ORIGIN UNRESOLVED ··· TRACE RETURNED NOTHING ··· '
      + 'THIS FEED IS NOT LICENSED ··· ';
    c.fillText(trace, x + w - ((t * 62) % 1000), y + h - 9);
  }

  // -------------------------------------------------------------- the frame

  setRain(intensity) {
    this.rain = Math.min(1, Math.max(0, intensity ?? 0));
  }

  update(dt) {
    this.t += dt;
    this.hazeMat.uniforms.uTime.value = this.t;

    for (let i = 0; i < this.flyers.length; i++) {
      const car = this.flyers[i];
      const d = car.userData;
      car.position.x = ((d.start + this.t * d.speed + 6) % 12) - 6;
      car.position.y = d.baseY + Math.sin(this.t * 0.45 + i * 1.7) * 0.02;
    }

    // Redraw on wall-clock, not on frames: a 144 Hz display must not upload the
    // overlay twice as often as a 60 Hz one for the same picture.
    this._overlayDue += dt;
    const period = 1 / (this.low ? 12 : 20);
    if (this._overlayDue >= period) {
      this._drawOverlay(this._overlayDue);
      this._overlayDue = 0;
    }
  }
}
