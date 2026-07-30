/* The cat (SPEC §6.1 — furnishing, not canon: the five normative elements are
 * the room, the lamp, the window, the seat and the plant, and none of them is
 * this animal). It lives in the sanctuary the way a cat lives anywhere: it
 * sleeps most of the evening, it takes the warm places, it watches the rain
 * when the rain is worth watching, and once in a while it looks straight down
 * the lens at you.
 *
 * It is an NPC and not a prop, which here means exactly two things: it decides
 * where to be — a weighted choice over the room's perches, a floor path around
 * the furniture, a jump at the end of it — and she knows it exists (WORLD.md,
 * "The cat"), so it can be talked about instead of only looked at. It has no
 * name in the scene on purpose: naming it is {{user}}'s, and the promise to
 * sleep on cat names is already a worked example in SPEC §27.
 *
 * Every mesh is generated geometry and its coat is a canvas (§6.2). The rig is
 * a spine group, a forequarter pivot hanging off it (so the thing can curl),
 * four leg pivots and a seven-segment tail; poses are target angles the frame
 * eases toward, so no two transitions are the same and nothing is keyframed.
 * World space is the room's (see SanctuaryScene): heading 0 faces +z, the
 * window; heading π faces the camera. The perches are placed to clear her
 * silhouette — she stands at the origin and the camera sits at −z, so anything
 * parked near x = 0 is a cat nobody ever sees.
 */
import {
  ConeGeometry, CylinderGeometry, Group, Mesh, MeshBasicMaterial,
  MeshStandardMaterial, SphereGeometry, Vector3,
} from 'three';
import { QUALITY } from '../quality.js';
import { PALETTE, createFurTex } from './textures.js';

const R = Math.random;
const rand = (a, b) => a + R() * (b - a);
const pick = (arr) => arr[Math.floor(R() * arr.length)];
const lerp = (a, b, t) => a + (b - a) * t;
/** Frame-rate independent ease toward a target — the whole animation system. */
const damp = (a, b, rate, dt) => lerp(a, b, 1 - Math.exp(-rate * dt));
/** Shortest signed angle from a to b. */
const delta = (a, b) => Math.atan2(Math.sin(b - a), Math.cos(b - a));
const turn = (a, b, max) => a + Math.max(-max, Math.min(max, delta(a, b)));

/* The perches. `pos` is where the cat settles, `approach` the floor point it
 * walks to before jumping up (clear of the furniture below), `heading` which
 * way it ends up facing. Poses are what makes sense in that spot: nothing
 * sleeps on the desk, nothing sits upright on a 19 cm sill. */
const SPOTS = [
  {
    name: 'seat',                                  // the window seat, beside her cushions
    pos: [-0.45, 0.52, 2.15], heading: -1.25, approach: [-0.4, 1.55],
    poses: ['sleep', 'loaf', 'sit'], weight: 3, wet: 2.2,
  },
  {
    name: 'sill',                                  // lengthwise on the sill — it is that narrow
    pos: [-0.92, 0.66, 2.28], heading: -1.5, approach: [-0.9, 1.5],
    poses: ['loaf', 'sit'], weight: 2, wet: 2.6,
  },
  {
    name: 'rug',                                   // the lamp's warm patch, the floor
    pos: [0.62, 0, 0.30], heading: 2.7, approach: [0.62, 0.30],
    poses: ['sleep', 'loaf', 'groom', 'sit'], weight: 3, wet: 1,
  },
  {
    name: 'desk',                                  // between her keyboard and the room
    pos: [1.48, 0.78, 2.12], heading: 3.0, approach: [1.0, 2.25],
    poses: ['sit', 'loaf'], weight: 2, wet: 0.8,
  },
  {
    name: 'stool',                                 // her stool, taken
    pos: [0.95, 0.505, 1.66], heading: 2.2, approach: [0.62, 1.2],
    poses: ['sleep', 'loaf'], weight: 2, wet: 1,
  },
];

/* What the floor path has to go around. Discs, because the cat only needs to
 * not walk through the holo table — it does not need a navmesh. */
const OBSTACLES = [
  [-0.74, 0.55, 0.40],   // holo table
  [-1.28, 1.72, 0.26],   // lamp
  [0.95, 1.66, 0.30],    // stool
  [1.50, 1.72, 0.55],    // desk
  [-0.66, 2.17, 0.62],   // window seat
];
const LANE = [0.15, 0.95];   // the open middle of the room, when a straight line won't do

/* Poses, as world-space angles. `front`/`rear` are leg angles from vertical and
 * `pitch` is nose-up: both are compensated for the spine's own pitch when they
 * are applied, so a sitting cat's forelegs stay upright instead of leaning with
 * its chest. `flen`/`rlen` shorten the legs — one bone per leg cannot fold a
 * hock, so a cat that is down draws its legs *into* the body silhouette, which
 * is what a folded leg looks like from outside anyway. */
const POSES = {
  stand: { y: 0.200, pitch: 0, bend: 0, front: 0, rear: 0, flen: 1, rlen: 1, tail: 0.75, curl: 0.05, sway: 0.10, eye: 1, breath: 0.9 },
  leap: { y: 0.215, pitch: 0.10, bend: 0, front: -0.75, rear: 0.85, flen: 1, rlen: 1, tail: 0.95, curl: 0.02, sway: 0.05, eye: 1, breath: 1.3 },
  sit: { y: 0.140, pitch: 0.75, bend: 0, front: 0, rear: 1.25, flen: 1, rlen: 0.55, tail: -0.38, curl: 0.26, sway: 0.09, eye: 1, breath: 0.8 },
  loaf: { y: 0.078, pitch: 0, bend: 0.20, front: -1.55, rear: 1.25, flen: 0.5, rlen: 0.36, tail: -0.48, curl: 0.24, sway: 0.05, eye: 0.72, breath: 0.55 },
  sleep: { y: 0.070, pitch: 0, bend: 0.62, front: -1.62, rear: 1.25, flen: 0.42, rlen: 0.30, tail: -0.52, curl: 0.26, sway: 0.02, eye: 0.05, breath: 0.35 },
  groom: { y: 0.140, pitch: 0.75, bend: 0, front: 0, rear: 1.25, flen: 1, rlen: 0.55, tail: -0.38, curl: 0.26, sway: 0.12, eye: 0.55, breath: 1.0 },
  stretch: { y: 0.145, pitch: -0.30, bend: 0, front: -1.05, rear: 0.55, flen: 1, rlen: 1, tail: 1.10, curl: 0.02, sway: 0.06, eye: 0.85, breath: 1.4 },
};

const HOLD = {
  sleep: [28, 70], loaf: [18, 40], sit: [10, 24],
  groom: [5, 11], stretch: [1.1, 1.8], stand: [1.5, 4],
};

const WINDOW = new Vector3(0, 1.45, 2.4);          // what "watching the rain" means

export class Cat {
  /** @param room  the sanctuary's group — the cat is furniture, it moves with it
   *  @param camera  so it can find you, occasionally, and hold it a second */
  constructor(room, { camera = null } = {}) {
    this.low = QUALITY.low;                        // shadow casting, and that is all
    this.camera = camera;
    this.rain = 0.6;
    this._t = 0;
    this._walk = 0;          // 0 resting, 1 mid-stride — drives the leg swing
    this._legPhase = 0;
    this._blink = 1;
    this._blinkAt = rand(2, 6);
    this._earFlick = 0;
    this._look = new Vector3();
    this._lookYaw = 0;
    this._lookPitch = 0;
    this._lookAt = 'ahead';
    this._lookFor = rand(3, 7);

    this.root = new Group();
    this.root.scale.setScalar(0.96);
    room.add(this.root);
    this._build();

    // she has had it a while: it starts asleep on the window seat, and the
    // first thing the room ever sees it do is wake up
    this.spot = SPOTS[0];
    this.root.position.set(...this.spot.pos);
    this.root.rotation.y = this.spot.heading;
    this.mode = 'idle';
    this.path = [];
    this.pose = { ...POSES.sleep };
    this.poseName = 'sleep';
    this.target = { ...POSES.sleep };
    this._hold = rand(10, 25);
  }

  // ------------------------------------------------------------------- build

  _build() {
    const furTex = createFurTex();
    const fur = new MeshStandardMaterial({ map: furTex, roughness: 0.95, metalness: 0 });
    const pale = new MeshStandardMaterial({ color: 0x9a93a6, roughness: 0.95 });
    const sock = new MeshStandardMaterial({ color: 0x8a8394, roughness: 0.95 });
    const pink = new MeshStandardMaterial({ color: 0x9a5f6d, roughness: 0.8 });
    const dark = new MeshBasicMaterial({ color: 0x08070c });
    // the eyeshine: the one part of the animal that is a light source, because
    // in a room like this it is the part you actually see first
    const eyeMat = new MeshBasicMaterial({ color: PALETTE.amber, toneMapped: false });

    const ball = (mat, sx, sy, sz, x, y, z, parent, shadow = true) => {
      const m = new Mesh(new SphereGeometry(1, 12, 10), mat);
      m.scale.set(sx, sy, sz);
      m.position.set(x, y, z);
      m.castShadow = shadow && !this.low;
      parent.add(m);
      return m;
    };

    const body = new Group();
    body.position.y = POSES.sleep.y;
    this.root.add(body);
    this.body = body;

    this.torso = ball(fur, 0.062, 0.072, 0.135, 0, 0, 0, body);
    ball(fur, 0.062, 0.070, 0.078, 0, 0.008, -0.085, body);      // haunches

    // The forequarters hang off their own pivot at the shoulder, which is the
    // whole reason a sleeping cat here reads as a comma and not a caterpillar:
    // one rigid spine can lie down but it cannot curl around itself.
    const fore = new Group();
    fore.position.set(0, 0, 0.060);
    body.add(fore);
    this.fore = fore;
    ball(fur, 0.056, 0.064, 0.070, 0, 0.004, 0.025, fore);       // chest
    ball(pale, 0.038, 0.040, 0.050, 0, -0.046, 0.012, fore, false); // the white bib
    const neck = new Mesh(new CylinderGeometry(0.032, 0.042, 0.06, 10), fur);
    neck.position.set(0, 0.030, 0.065);
    neck.rotation.x = 0.75;
    fore.add(neck);

    const head = new Group();
    head.position.set(0, 0.055, 0.105);
    fore.add(head);
    this.head = head;
    ball(fur, 0.048, 0.048, 0.050, 0, 0, 0, head);               // skull
    ball(fur, 0.029, 0.023, 0.026, 0, -0.022, 0.034, head, false); // muzzle
    ball(pale, 0.018, 0.014, 0.016, 0, -0.030, 0.030, head, false); // chin
    ball(pink, 0.006, 0.0045, 0.0045, 0, -0.020, 0.057, head, false); // nose

    this.ears = [];
    for (const s of [-1, 1]) {
      const ear = new Group();
      ear.position.set(0.030 * s, 0.038, 0.002);
      ear.rotation.z = -0.30 * s;
      head.add(ear);
      const outer = new Mesh(new ConeGeometry(0.024, 0.050, 4), fur);
      outer.scale.z = 0.5;
      outer.rotation.y = Math.PI / 4;
      outer.castShadow = !this.low;
      ear.add(outer);
      const inner = new Mesh(new ConeGeometry(0.013, 0.030, 4), pink);
      inner.scale.z = 0.35;
      inner.rotation.y = Math.PI / 4;
      inner.position.z = 0.006;
      ear.add(inner);
      this.ears.push(ear);
    }

    this.eyes = [];
    for (const s of [-1, 1]) {
      const eye = ball(eyeMat, 0.0085, 0.009, 0.0085, 0.019 * s, 0.006, 0.040, head, false);
      ball(dark, 0.0056, 0.0064, 0.005, 0.019 * s, 0.006, 0.0455, head, false);  // the pupil
      this.eyes.push(eye);
    }

    // Four legs on pivots at the shoulder and hip. One tapered bone each: at
    // this size a knee is three more meshes and no more animal.
    this.legs = [];
    for (const [x, z, front] of [[0.036, 0.025, 1], [-0.036, 0.025, 1],
      [0.042, -0.085, 0], [-0.042, -0.085, 0]]) {
      const leg = new Group();
      leg.position.set(x, -0.03, z);
      (front ? fore : body).add(leg);   // the forelegs swing with the shoulders
      const bone = new Mesh(new CylinderGeometry(0.023, 0.012, 0.15, 8), fur);
      bone.position.y = -0.075;
      bone.castShadow = !this.low;
      leg.add(bone);
      const paw = ball(sock, 0.015, 0.012, 0.021, 0, -0.156, 0.005, leg, false);
      this.legs.push({
        group: leg, bone, paw, front: !!front,
        phase: (x > 0) === !!front ? 0 : Math.PI,
      });
    }

    // The tail: eight nested segments, so lift, curl and sway all accumulate
    // down its length instead of pivoting one stiff rod at the root.
    this.tail = [];
    let parent = new Group();
    parent.position.set(0, 0.030, -0.145);
    body.add(parent);
    this.tailBase = parent;
    for (let i = 0; i < 7; i++) {
      const seg = new Group();
      if (i > 0) seg.position.y = 0.030;
      parent.add(seg);
      const r0 = 0.015 - i * 0.0013;
      const m = new Mesh(new CylinderGeometry(r0 - 0.0013, r0, 0.032, 6), fur);
      m.position.y = 0.016;
      m.castShadow = !this.low;
      seg.add(m);
      this.tail.push(seg);
      parent = seg;
    }
    ball(sock, 0.011, 0.018, 0.011, 0, 0.028, 0, parent, false);   // the white tip
  }

  // --------------------------------------------------------------- behaviour

  /** The `rain` command reaches the cat too: hard weather is the best show in
   *  the room, and it takes the seats with a view. */
  setRain(intensity) {
    this.rain = intensity;
  }

  _setPose(name) {
    this.poseName = name;
    this.target = POSES[name];
    const [lo, hi] = HOLD[name] || [8, 20];
    this._hold = rand(lo, hi);
  }

  _pickPose() {
    const options = this.spot.poses.filter((p) => p !== this.poseName);
    return pick(options.length ? options : this.spot.poses);
  }

  _pickSpot() {
    const wet = Math.max(0, (this.rain - 0.35) / 0.65);
    let total = 0;
    const weights = SPOTS.map((s) => {
      const w = s === this.spot ? 0 : s.weight * lerp(1, s.wet, wet);
      total += w;
      return w;
    });
    let r = R() * total;
    for (let i = 0; i < SPOTS.length; i++) if ((r -= weights[i]) <= 0) return SPOTS[i];
    return SPOTS[0];
  }

  _idle(dt) {
    this._hold -= dt;
    if (this._hold > 0) return;
    // nothing gets up from a sleep in one move
    if (this.poseName === 'sleep' || this.poseName === 'loaf') {
      this._setPose('stretch');
      return;
    }
    if (R() < 0.45) this._goto(this._pickSpot());
    else this._setPose(this._pickPose());
  }

  /** True if the straight run from a to b clips any furniture. */
  _blocked(a, b) {
    const dx = b[0] - a[0], dz = b[1] - a[1];
    const len2 = dx * dx + dz * dz || 1e-6;
    for (const [ox, oz, r] of OBSTACLES) {
      let t = ((ox - a[0]) * dx + (oz - a[1]) * dz) / len2;
      t = Math.max(0, Math.min(1, t));
      const px = a[0] + dx * t - ox, pz = a[1] + dz * t - oz;
      if (px * px + pz * pz < r * r) return true;
    }
    return false;
  }

  _goto(spot) {
    const here = this.spot;
    const path = [];
    if (here.pos[1] > 0.05)
      path.push({ kind: 'arc', to: [here.approach[0], 0, here.approach[1]], dur: 0.5, up: 0.1 });
    // one lane through the open middle is enough cleverness: the room is 3.7 m
    // wide and everything in it is against a wall
    if (this._blocked(here.approach, spot.approach)) path.push({ kind: 'walk', to: LANE });
    path.push({ kind: 'walk', to: spot.approach });
    if (spot.pos[1] > 0.05) {
      path.push({
        kind: 'arc', to: spot.pos.slice(), dur: 0.55, up: 0.2 + spot.pos[1] * 0.3,
      });
    } else {
      path.push({ kind: 'walk', to: [spot.pos[0], spot.pos[2]] });
    }
    path.push({ kind: 'turn', heading: spot.heading });
    this.path = path;
    this.mode = 'travel';
    this.target = POSES.stand;
    this.poseName = 'stand';
    this.spot = spot;
  }

  _travel(dt) {
    const step = this.path[0];
    if (!step) {
      this.mode = 'idle';
      this._setPose(pick(this.spot.poses));
      return;
    }
    const p = this.root.position;

    if (step.kind === 'walk') {
      const dx = step.to[0] - p.x, dz = step.to[1] - p.z;
      const dist = Math.hypot(dx, dz);
      if (dist < 0.04) { this.path.shift(); return; }
      const want = Math.atan2(dx, dz);
      this.root.rotation.y = turn(this.root.rotation.y, want, 3.4 * dt);
      if (Math.abs(delta(this.root.rotation.y, want)) < 0.5) {
        const s = Math.min(0.44 * dt, dist);
        p.x += Math.sin(this.root.rotation.y) * s;
        p.z += Math.cos(this.root.rotation.y) * s;
        this._legPhase += dt * 9.5;
        this._walk = damp(this._walk, 1, 9, dt);
      } else {
        this._walk = damp(this._walk, 0, 9, dt);   // it stops to turn, then goes
      }
      this.target = POSES.stand;
      return;
    }

    if (step.kind === 'arc') {
      if (!step.from) step.from = [p.x, p.y, p.z];
      step.t = (step.t || 0) + dt / step.dur;
      const k = Math.min(1, step.t);
      p.x = lerp(step.from[0], step.to[0], k);
      p.y = lerp(step.from[1], step.to[1], k) + Math.sin(k * Math.PI) * step.up;
      p.z = lerp(step.from[2], step.to[2], k);
      const dx = step.to[0] - step.from[0], dz = step.to[2] - step.from[2];
      if (Math.hypot(dx, dz) > 0.02)
        this.root.rotation.y = turn(this.root.rotation.y, Math.atan2(dx, dz), 6 * dt);
      this.target = POSES.leap;
      this._walk = damp(this._walk, 0, 12, dt);
      if (k >= 1) this.path.shift();
      return;
    }

    // 'turn' — settle onto the heading the spot was chosen for
    this.root.rotation.y = turn(this.root.rotation.y, step.heading, 3.0 * dt);
    this.target = POSES.stand;
    this._walk = damp(this._walk, 0, 8, dt);
    if (Math.abs(delta(this.root.rotation.y, step.heading)) < 0.05) {
      this.root.rotation.y = step.heading;
      this.path.shift();
    }
  }

  // ------------------------------------------------------------------- frame

  /** Where the head points. Ahead most of the time; the window when the weather
   *  is doing something; you, now and then, for about as long as a cat does. */
  _aim(dt) {
    this._lookFor -= dt;
    if (this._lookFor <= 0) {
      const r = R();
      const wet = 0.2 + 0.35 * this.rain;
      this._lookAt = r < wet ? 'window' : r < wet + 0.22 ? 'camera' : 'ahead';
      this._lookFor = this._lookAt === 'ahead' ? rand(4, 9) : rand(2.5, 6);
    }
    let yaw = 0, pitch = 0;
    const at = this._lookAt === 'camera' ? (this.camera && this.camera.position)
      : this._lookAt === 'window' ? WINDOW : null;
    if (at && this.pose.eye > 0.4) {
      this.head.getWorldPosition(this._look);
      const dx = at.x - this._look.x, dy = at.y - this._look.y, dz = at.z - this._look.z;
      yaw = delta(this.root.rotation.y + this.fore.rotation.y, Math.atan2(dx, dz));
      pitch = -Math.atan2(dy, Math.hypot(dx, dz));
      // it cannot see through its own shoulder: what it can't turn to, it ignores
      if (Math.abs(yaw) > 1.15) { yaw = 0; pitch = 0; }
      pitch = Math.max(-0.45, Math.min(0.45, pitch));
    }
    this._lookYaw = damp(this._lookYaw, yaw, 2.6, dt);
    this._lookPitch = damp(this._lookPitch, pitch, 2.6, dt);
  }

  update(dt) {
    const t = (this._t += dt);
    if (this.mode === 'travel') this._travel(dt); else this._idle(dt);
    if (this.mode !== 'travel') this._walk = damp(this._walk, 0, 8, dt);
    this._aim(dt);

    // ease every joint toward the pose rather than snapping: the transition is
    // the animation, and it is never the same one twice
    const p = this.pose, q = this.target;
    for (const k of Object.keys(p)) p[k] = damp(p[k], q[k], 3.2, dt);

    const asleep = this.poseName === 'sleep';
    const bob = this._walk * Math.sin(this._legPhase * 2) * 0.008;
    this.body.position.y = p.y + bob;
    this.body.rotation.x = -p.pitch;
    this.fore.rotation.y = p.bend;
    this.body.rotation.z = this._walk * Math.sin(this._legPhase) * 0.03;

    // breathing, on the ribs only — the flank of a sleeping cat is the whole
    // reason to put one in a room
    const br = 1 + Math.sin(t * (0.9 + p.breath * 1.6)) * (0.006 + 0.012 * (1 - p.breath));
    this.torso.scale.set(0.062 * br, 0.072 * br, 0.135);

    for (const leg of this.legs) {
      const swing = this._walk * Math.sin(this._legPhase + leg.phase) * 0.5;
      leg.group.rotation.x = (leg.front ? p.front : p.rear) + p.pitch + swing;
      const len = leg.front ? p.flen : p.rlen;
      leg.bone.scale.y = len;
      leg.bone.position.y = -0.075 * len;
      leg.paw.position.y = -0.15 * len - 0.006;
    }

    // grooming is the sit, plus a head that keeps going back to one shoulder
    let headYaw = this._lookYaw, headPitch = this._lookPitch;
    if (this.poseName === 'groom') {
      const g = Math.sin(t * 3.4);
      headYaw = 0.55 * Math.sign(g);
      headPitch = 0.55 + 0.35 * Math.abs(g);
    } else if (asleep) {
      headYaw = 0.55;
      headPitch = 0.55;
    }
    this.head.rotation.y = damp(this.head.rotation.y, headYaw, 4, dt);
    this.head.rotation.x = damp(this.head.rotation.x, headPitch + p.pitch, 4, dt);

    // ears: alert by default, flattened toward sleep, flicked at nothing
    this._earFlick = Math.max(0, this._earFlick - dt * 3);
    if (R() < (asleep ? 0.15 : 0.6) * dt) this._earFlick = 1;
    this.ears.forEach((ear, i) => {
      const s = i === 0 ? -1 : 1;
      const flat = (1 - p.eye) * 0.5;
      ear.rotation.z = damp(ear.rotation.z, -0.30 * s - flat * s, 8, dt);
      ear.rotation.x = damp(ear.rotation.x, flat * 0.6 + this._earFlick * 0.5 * (i ? 1 : 0.4), 9, dt);
    });

    // a slow blink, which is the only thing a cat ever says out loud
    this._blinkAt -= dt;
    if (this._blinkAt <= 0) { this._blink = 0; this._blinkAt = rand(2.5, 8); }
    this._blink = damp(this._blink, 1, 7, dt);
    const open = Math.min(p.eye, 0.1 + 0.9 * this._blink);
    for (const eye of this.eyes) eye.scale.y = 0.009 * Math.max(0.05, open);

    // the tail, from the root out: lift and curl are the pose, the sway is the mood
    this.tailBase.rotation.x = -Math.PI / 2 + p.tail + p.pitch;
    const swayAmp = p.sway + this._walk * 0.06 + this._earFlick * 0.08;
    // Each segment grows along its own +y, so the bend is about z (the wrap,
    // sideways) and x (the droop). About y would only spin the tail around its
    // own axis, which from outside is nothing at all.
    this.tail.forEach((seg, i) => {
      seg.rotation.z = p.curl + swayAmp * Math.sin(t * 1.7 - i * 0.55) * (i / this.tail.length);
      seg.rotation.x = 0.06;
    });
  }
}
