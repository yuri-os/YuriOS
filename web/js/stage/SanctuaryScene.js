/* The sanctuary (SPEC §6.1–§6.2) — a small room, procedural, canon:
 * low warm light from a lamp, a window with rain, a window seat, one plant.
 * No binary scene assets: every mesh here is generated geometry and every
 * effect a shader, so the whole room lives in git as readable code.
 *
 * Layout (world space; she stands at the origin facing the camera at −z):
 *   floor y=0 · ceiling y=2.6 · back wall z=+2.2 · side walls x=±1.6
 *   window: an opening in the LEFT wall (x=−1.6) centred at z=0.6, y=1.45 —
 *   the idle machine's WINDOW_TARGET (world/idle.py) is this, minus a step
 *   inside the glass. Move the window, move that constant.
 */
import {
  BoxGeometry, Color, CylinderGeometry, FogExp2, Group, HemisphereLight, Mesh,
  MeshBasicMaterial, MeshStandardMaterial, PlaneGeometry, PointLight,
  SphereGeometry,
} from 'three';

import { Rain } from './Rain.js';

const WALL_X = 1.6;
const BACK_Z = 2.2;
const FRONT_Z = -3.2;      // behind the camera — the room closes around you
const CEIL_Y = 2.6;
const WINDOW = { wallX: -WALL_X, y: 1.45, z: 0.6, width: 1.1, height: 1.2 };

export class SanctuaryScene {
  constructor(scene) {
    scene.background = new Color(0x050507);
    scene.fog = new FogExp2(0x06060a, 0.045);   // depth reads; edges dissolve

    const room = new Group();
    scene.add(room);

    // ---- surfaces ----
    const wallMat = new MeshStandardMaterial({ color: 0x14141d, roughness: 0.95 });
    const floorMat = new MeshStandardMaterial({ color: 0x1a1410, roughness: 0.85 });
    const depth = BACK_Z - FRONT_Z;

    const floor = new Mesh(new PlaneGeometry(WALL_X * 2, depth), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(0, 0, (BACK_Z + FRONT_Z) / 2);
    room.add(floor);

    const ceil = new Mesh(new PlaneGeometry(WALL_X * 2, depth),
      new MeshStandardMaterial({ color: 0x0d0d14, roughness: 1 }));
    ceil.rotation.x = Math.PI / 2;
    ceil.position.set(0, CEIL_Y, (BACK_Z + FRONT_Z) / 2);
    room.add(ceil);

    const back = new Mesh(new PlaneGeometry(WALL_X * 2, CEIL_Y), wallMat);
    back.rotation.y = Math.PI;
    back.position.set(0, CEIL_Y / 2, BACK_Z);
    room.add(back);

    const right = new Mesh(new PlaneGeometry(depth, CEIL_Y), wallMat);
    right.rotation.y = -Math.PI / 2;
    right.position.set(WALL_X, CEIL_Y / 2, (BACK_Z + FRONT_Z) / 2);
    room.add(right);

    // left wall: four slabs around the window opening, so the rain outside is
    // visible only through the hole (real occlusion, no masking tricks)
    const W = WINDOW;
    const t = 0.06;                              // wall slab thickness
    const zLo = W.z - W.width / 2, zHi = W.z + W.width / 2;
    const yLo = W.y - W.height / 2, yHi = W.y + W.height / 2;
    const slab = (d, h, zc, yc) => {
      const m = new Mesh(new BoxGeometry(t, h, d), wallMat);
      m.position.set(-WALL_X, yc, zc);
      room.add(m);
    };
    slab(zLo - FRONT_Z, CEIL_Y, (zLo + FRONT_Z) / 2, CEIL_Y / 2);        // front of window
    slab(BACK_Z - zHi, CEIL_Y, (zHi + BACK_Z) / 2, CEIL_Y / 2);          // behind window
    slab(W.width, yLo, W.z, yLo / 2);                                    // below
    slab(W.width, CEIL_Y - yHi, W.z, (yHi + CEIL_Y) / 2);                // above

    // window frame + sill
    const frameMat = new MeshStandardMaterial({ color: 0x241d16, roughness: 0.7 });
    const bar = (w, h, d, x, y, z) => {
      const m = new Mesh(new BoxGeometry(w, h, d), frameMat);
      m.position.set(x, y, z);
      room.add(m);
    };
    const fx = -WALL_X + 0.02;
    bar(0.1, 0.06, W.width + 0.12, fx, yHi + 0.03, W.z);                 // head
    bar(0.14, 0.08, W.width + 0.24, fx + 0.03, yLo - 0.04, W.z);         // sill
    bar(0.1, W.height + 0.12, 0.06, fx, W.y, zLo - 0.03);                // jambs
    bar(0.1, W.height + 0.12, 0.06, fx, W.y, zHi + 0.03);
    bar(0.08, W.height, 0.04, fx, W.y, W.z);                             // centre mullion

    // the night outside: a far dark backdrop so the glass has something behind it
    const night = new Mesh(new PlaneGeometry(14, 10),
      new MeshBasicMaterial({ color: 0x080c16, fog: false }));
    night.rotation.y = Math.PI / 2;
    night.position.set(-5.2, 2.2, W.z);
    room.add(night);

    // ---- the window seat (canon) ----
    const seatMat = new MeshStandardMaterial({ color: 0x24180f, roughness: 0.8 });
    const cushionMat = new MeshStandardMaterial({ color: 0x3a2436, roughness: 1 });
    const seat = new Mesh(new BoxGeometry(0.55, 0.42, 1.5), seatMat);
    seat.position.set(-WALL_X + 0.3, 0.21, W.z);
    room.add(seat);
    const cushion = new Mesh(new BoxGeometry(0.5, 0.1, 1.4), cushionMat);
    cushion.position.set(-WALL_X + 0.3, 0.47, W.z);
    room.add(cushion);

    // ---- the lamp (canon: the low warm light) ----
    const lampBase = new Mesh(new CylinderGeometry(0.12, 0.16, 0.04, 16),
      new MeshStandardMaterial({ color: 0x1c1c24, roughness: 0.6 }));
    lampBase.position.set(1.15, 0.02, 1.55);
    room.add(lampBase);
    const pole = new Mesh(new CylinderGeometry(0.015, 0.015, 1.15, 8),
      new MeshStandardMaterial({ color: 0x2a2a34, roughness: 0.5 }));
    pole.position.set(1.15, 0.6, 1.55);
    room.add(pole);
    const shade = new Mesh(new CylinderGeometry(0.13, 0.19, 0.24, 20, 1, true),
      new MeshStandardMaterial({
        color: 0xf5b462, emissive: 0xd98a3a, emissiveIntensity: 1.6,
        roughness: 1, side: 2,
      }));
    shade.position.set(1.15, 1.24, 1.55);
    room.add(shade);

    // ---- the plant (canon: exactly one) ----
    const pot = new Mesh(new CylinderGeometry(0.11, 0.09, 0.18, 12),
      new MeshStandardMaterial({ color: 0x2b2019, roughness: 0.9 }));
    pot.position.set(-1.15, 0.09, 1.85);
    room.add(pot);
    const leafMat = new MeshStandardMaterial({ color: 0x1d3a24, roughness: 0.9 });
    for (let i = 0; i < 6; i++) {
      const a = (i / 6) * Math.PI * 2;
      const leaf = new Mesh(new SphereGeometry(0.06, 8, 6), leafMat);
      leaf.scale.set(1, 2.6 + (i % 3) * 0.5, 0.5);
      leaf.position.set(-1.15 + Math.cos(a) * 0.07,
        0.32 + (i % 3) * 0.05, 1.85 + Math.sin(a) * 0.07);
      leaf.rotation.z = Math.cos(a) * 0.5;
      leaf.rotation.x = -Math.sin(a) * 0.5;
      room.add(leaf);
    }

    // ---- light (SPEC §6.1: LOW and WARM — the lamp carries the room) ----
    scene.add(new HemisphereLight(0x232838, 0x0a0a0f, 0.5));
    this.lamp = new PointLight(0xffb46a, 2.4, 8, 1.8);
    this.lamp.position.set(1.15, 1.3, 1.55);
    scene.add(this.lamp);
    this.lampBase = this.lamp.intensity;
    // cool spill from the window — the rain-light on her other side
    this.windowLight = new PointLight(0x7ea0c8, 0.9, 6, 1.8);
    this.windowLight.position.set(-1.25, 1.5, W.z);
    scene.add(this.windowLight);

    // ---- rain (SPEC §6.2) ----
    this.rain = new Rain(scene, WINDOW);
    this._t = 0;
  }

  setRain(intensity) {
    this.rain.setIntensity(intensity);
    // heavier rain: the window's cool light dims a touch
    this.windowLight.intensity = 1.0 - 0.35 * this.rain.intensity;
  }

  update(dt) {
    this._t += dt;
    this.rain.update(dt);
    // the lamp breathes — two slow sines, never a strobe
    this.lamp.intensity = this.lampBase
      * (1 + 0.03 * Math.sin(this._t * 1.7) + 0.02 * Math.sin(this._t * 4.3));
  }
}
