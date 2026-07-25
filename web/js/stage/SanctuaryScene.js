/* The sanctuary (SPEC §6.1–§6.2) — a small unit high in a stacked block over the
 * Sprawl, procedural, canon: a wide rain-struck window with the city burning
 * behind it, the window seat under the glass, the one plant on the sill, and the
 * lamp that is the only warm light in it. No binary scene assets: every mesh
 * here is generated geometry, every surface a canvas drawn at boot, and every
 * effect a shader, so the whole room lives in git as readable code.
 *
 * Layout (world space; she stands at the origin facing the camera at −z):
 *
 *          +x (screen LEFT)              −x (screen RIGHT)
 *   z=+2.5  ┌───────── the picture window ─────────┐ pier
 *           │ desk · terminal        window seat   │ corner glass
 *   z= 0    │        · her ·   holo table          │
 *   z=−3.6  └──── the camera sits here, in the dark ┘
 *
 *   floor y=0 · ceiling y=2.72 · window wall z=+2.45 · side walls x=±1.85
 *
 * The camera looks along +z, so the window wall *is* the frame: the city is her
 * backdrop and the room reads at the edges. The corner return on the −x wall is
 * what the idle machine's WINDOW_TARGET (yurios/mind/loop.py) points at when she
 * rain-gazes — move the glass, move that constant.
 */
import {
  AdditiveBlending, BackSide, BoxGeometry, BufferAttribute, BufferGeometry,
  CatmullRomCurve3, Color, CylinderGeometry, DoubleSide, FogExp2, Group,
  HemisphereLight, IcosahedronGeometry, Mesh, MeshBasicMaterial,
  MeshStandardMaterial, PMREMGenerator, PlaneGeometry,
  PointLight, Points, RectAreaLight, ShaderMaterial, SphereGeometry, SpotLight,
  TorusGeometry, TubeGeometry, Vector2, Vector3,
} from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { RectAreaLightUniformsLib } from 'three/addons/lights/RectAreaLightUniformsLib.js';
import { Reflector } from 'three/addons/objects/Reflector.js';

import { Post } from './Post.js';
import { Rain } from './Rain.js';
import { City } from './sanctuary/City.js';
import { Terminal } from './sanctuary/Screens.js';
import {
  PALETTE, configureTextures, createFloorMaps, createKeycapTex, createMetalTex,
  createNeonSignTex, createPosterTex, createWallMaps, createWeaveTex,
} from './sanctuary/textures.js';

const WALL_X = 1.85;                 // side walls
const BACK_Z = 2.45;                 // the window wall, behind her
const FRONT_Z = -3.6;                // behind the camera — the room closes around you
const CEIL_Y = 2.72;

// the picture window in the back wall, and the return that turns the corner
const WINDOW = { x0: -1.4, x1: 1.4, y0: 0.66, y1: 2.16 };
const RETURN = { z0: 0.55, z1: 2.2 };

const R = Math.random;

export class SanctuaryScene {
  /** @param scene @param renderer @param camera — the room owns its own look
   *  (Post), because the room is the only thing that needs it: desktop mode
   *  (SPEC §6.5) builds none of this and renders straight to the framebuffer. */
  constructor(scene, renderer, camera) {
    // One quality switch for the whole room. This GPU usually also holds her
    // model (→ SPEC §3), so `?fx=low` is a real escape hatch, not just a phone path.
    this.low = new URLSearchParams(location.search).get('fx') === 'low'
      || window.matchMedia('(pointer: coarse)').matches
      || window.innerWidth < 900;
    const low = this.low;

    configureTextures(renderer);
    scene.background = new Color(0x04040a);
    scene.fog = new FogExp2(0x07070f, 0.032);   // depth reads; edges dissolve

    // Something for the metal to reflect. One PMREM bake at boot, then the
    // generator goes away — dark chrome with no environment reads as flat paint.
    const pmrem = new PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environmentIntensity = 0.16;
    pmrem.dispose();
    if (!low) RectAreaLightUniformsLib.init();

    if (!low) {
      renderer.shadowMap.enabled = true;        // one caster: the pendant over her
    }

    const room = new Group();
    scene.add(room);
    this.room = room;
    this._t = 0;

    // ---- materials ----
    const floorMaps = createFloorMaps();
    const wallMaps = createWallMaps();
    const metalTex = createMetalTex();
    const weaveTex = createWeaveTex();

    const floorMat = new MeshStandardMaterial({
      map: floorMaps.map,
      normalMap: floorMaps.normalMap,
      normalScale: new Vector2(0.62, 0.62),
      roughnessMap: floorMaps.roughnessMap,
      metalness: 0.76,
      roughness: low ? 0.72 : 0.78,
      envMapIntensity: low ? 1.3 : 1.05,
    });
    const wallMat = new MeshStandardMaterial({
      map: wallMaps.map,
      normalMap: wallMaps.normalMap,
      normalScale: new Vector2(0.5, 0.5),
      roughnessMap: wallMaps.roughnessMap,
      metalness: 0.4,
      roughness: 0.78,
      envMapIntensity: 0.6,
    });
    const metalMat = new MeshStandardMaterial({
      map: metalTex, metalness: 0.9, roughness: 0.38, envMapIntensity: 0.8,
    });
    const darkMetal = new MeshStandardMaterial({
      color: 0x181820, metalness: 0.85, roughness: 0.46, envMapIntensity: 0.55,
    });
    const plasticMat = new MeshStandardMaterial({
      color: 0x26262f, metalness: 0.1, roughness: 0.62,
    });
    const cableMat = new MeshStandardMaterial({
      color: 0x0e0e14, metalness: 0.3, roughness: 0.78,
    });
    const clothMat = new MeshStandardMaterial({ map: weaveTex, roughness: 0.95 });
    // the cushion is lifted out of the room's blacks on purpose: the window seat
    // is the one place in here that is meant to look sat in
    const cushionMat = new MeshStandardMaterial({
      map: weaveTex, color: 0x8f7fa4, roughness: 0.96,
    });

    const neon = (hex, mult = 2.4) => new MeshBasicMaterial({
      color: new Color(hex).multiplyScalar(mult), toneMapped: false,
    });
    const neonCyan = neon(PALETTE.cyan, 1.5);
    const neonMagenta = neon(PALETTE.magenta, 1.4);
    const neonAmber = neon(PALETTE.amber, 1.2);
    const neonViolet = neon(PALETTE.violet, 1.3);
    const coveCyan = neon(PALETTE.cyan, 0.8);
    const coveMagenta = neon(PALETTE.magenta, 0.75);
    const coveViolet = neon(PALETTE.violet, 0.75);

    // ---- builders ----
    const BOX = (w, h, d, mat, x, y, z, { rx = 0, ry = 0, rz = 0, shadow = true } = {}) => {
      const m = new Mesh(new BoxGeometry(w, h, d), mat);
      m.position.set(x, y, z);
      m.rotation.set(rx, ry, rz);
      m.castShadow = shadow && !low;
      m.receiveShadow = !low;
      room.add(m);
      return m;
    };
    const SOFTBOX = (w, h, d, radius, mat, x, y, z, ry = 0) => {
      const m = new Mesh(new RoundedBoxGeometry(w, h, d, 3, radius), mat);
      m.position.set(x, y, z);
      m.rotation.y = ry;
      m.castShadow = !low;
      m.receiveShadow = !low;
      room.add(m);
      return m;
    };
    const strip = (w, h, d, x, y, z, mat) => {
      const bar = new Mesh(new BoxGeometry(w, h, d), mat);
      bar.position.set(x, y, z);
      room.add(bar);
      return bar;
    };
    const cable = (points, r = 0.016) => {
      const curve = new CatmullRomCurve3(points.map((p) => new Vector3(...p)));
      const m = new Mesh(new TubeGeometry(curve, 32, r, 6), cableMat);
      room.add(m);
      return m;
    };

    // ---- the shell ----
    const depth = BACK_Z - FRONT_Z;
    const midZ = (BACK_Z + FRONT_Z) / 2;

    const floor = new Mesh(new PlaneGeometry(WALL_X * 2, depth), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(0, 0, midZ);
    floor.receiveShadow = !low;
    room.add(floor);

    // The wet floor. A planar mirror catches what an environment map cannot —
    // the window, the signs, the terminal — and smears them across the deck.
    // Desktop only: it re-renders the room from a mirrored camera.
    if (!low) this._addReflector(room, midZ, depth);

    const ceil = new Mesh(new PlaneGeometry(WALL_X * 2, depth),
      new MeshStandardMaterial({ color: 0x0b0b12, roughness: 1, metalness: 0.2 }));
    ceil.rotation.x = Math.PI / 2;
    ceil.position.set(0, CEIL_Y, midZ);
    room.add(ceil);

    const t = 0.1;                                  // wall thickness
    const W = WINDOW;
    // back wall: four slabs around the opening, so the city is visible only
    // through the hole (real occlusion, no masking tricks)
    BOX(WALL_X + W.x0, CEIL_Y, t, wallMat, (-WALL_X + W.x0) / 2, CEIL_Y / 2, BACK_Z + t / 2);
    BOX(WALL_X - W.x1, CEIL_Y, t, wallMat, (WALL_X + W.x1) / 2, CEIL_Y / 2, BACK_Z + t / 2);
    BOX(W.x1 - W.x0, W.y0, t, wallMat, (W.x0 + W.x1) / 2, W.y0 / 2, BACK_Z + t / 2);
    BOX(W.x1 - W.x0, CEIL_Y - W.y1, t, wallMat,
      (W.x0 + W.x1) / 2, (W.y1 + CEIL_Y) / 2, BACK_Z + t / 2);

    // −x wall (screen right): solid to z=RETURN.z0, then the corner glass
    BOX(t, CEIL_Y, RETURN.z0 - FRONT_Z, wallMat, -WALL_X - t / 2, CEIL_Y / 2,
      (RETURN.z0 + FRONT_Z) / 2);
    BOX(t, CEIL_Y, BACK_Z - RETURN.z1, wallMat, -WALL_X - t / 2, CEIL_Y / 2,
      (RETURN.z1 + BACK_Z) / 2);
    BOX(t, W.y0, RETURN.z1 - RETURN.z0, wallMat, -WALL_X - t / 2, W.y0 / 2,
      (RETURN.z0 + RETURN.z1) / 2);
    BOX(t, CEIL_Y - W.y1, RETURN.z1 - RETURN.z0, wallMat, -WALL_X - t / 2,
      (W.y1 + CEIL_Y) / 2, (RETURN.z0 + RETURN.z1) / 2);

    // +x wall (screen left) and the wall behind the camera
    BOX(t, CEIL_Y, depth, wallMat, WALL_X + t / 2, CEIL_Y / 2, midZ);
    BOX(WALL_X * 2, CEIL_Y, t, wallMat, 0, CEIL_Y / 2, FRONT_Z - t / 2);

    // ---- the window: frame, glass, blinds ----
    const fz = BACK_Z - 0.02;
    const winW = W.x1 - W.x0, winH = W.y1 - W.y0, winCx = (W.x0 + W.x1) / 2;
    BOX(winW + 0.16, 0.09, 0.18, darkMetal, winCx, W.y1 + 0.045, fz);       // head
    BOX(winW + 0.16, 0.12, 0.34, darkMetal, winCx, W.y0 - 0.06, fz - 0.06); // sill, deep enough to sit things on
    BOX(0.1, winH + 0.2, 0.18, darkMetal, W.x0 - 0.05, (W.y0 + W.y1) / 2, fz);
    BOX(0.1, winH + 0.2, 0.18, darkMetal, W.x1 + 0.05, (W.y0 + W.y1) / 2, fz);
    for (const mx of [-0.68, 0.68])                                          // mullions
      BOX(0.055, winH, 0.1, darkMetal, mx, (W.y0 + W.y1) / 2, fz, { shadow: false });
    BOX(winW, 0.05, 0.1, darkMetal, winCx, 1.9, fz, { shadow: false });      // transom

    const retD = RETURN.z1 - RETURN.z0, retCz = (RETURN.z0 + RETURN.z1) / 2;
    const rx = -WALL_X + 0.02;
    BOX(0.16, 0.09, retD + 0.16, darkMetal, rx, W.y1 + 0.045, retCz);
    BOX(0.3, 0.12, retD + 0.16, darkMetal, rx + 0.06, W.y0 - 0.06, retCz);
    BOX(0.16, winH + 0.2, 0.1, darkMetal, rx, (W.y0 + W.y1) / 2, RETURN.z0 - 0.05);
    BOX(0.1, winH, 0.05, darkMetal, rx, (W.y0 + W.y1) / 2, retCz, { shadow: false });

    const glassMat = new MeshStandardMaterial({
      color: 0xa9cfff, transparent: true, opacity: 0.09, roughness: 0.1,
      metalness: 0.05, envMapIntensity: 1.6, depthWrite: false, side: DoubleSide,
    });
    const glass = new Mesh(new PlaneGeometry(winW, winH), glassMat);
    glass.position.set(winCx, (W.y0 + W.y1) / 2, fz - 0.04);
    room.add(glass);
    const retGlass = new Mesh(new PlaneGeometry(retD, winH), glassMat);
    retGlass.position.set(rx + 0.02, (W.y0 + W.y1) / 2, retCz);
    retGlass.rotation.y = Math.PI / 2;
    room.add(retGlass);

    // half-drawn blinds, hanging in the upper light — she never lowers them
    for (let i = 0; i < 4; i++)
      BOX(winW - 0.06, 0.05, 0.012, plasticMat, winCx, 2.08 - i * 0.085, fz - 0.12,
        { rx: -0.7, shadow: false });

    // ---- outside: the Sprawl, the rain ----
    this.city = new City(scene, { low });
    this.rain = new Rain(scene, {
      panes: [
        { x: winCx, y: (W.y0 + W.y1) / 2, z: fz - 0.03, width: winW, height: winH, axis: 'z' },
        { x: rx + 0.03, y: (W.y0 + W.y1) / 2, z: retCz, width: retD, height: winH, axis: 'x' },
      ],
      volumes: [
        { x0: -4.2, x1: 3.0, y0: -1.0, y1: 4.4, z0: BACK_Z + 0.35, z1: BACK_Z + 3.4 },
        { x0: -4.2, x1: -WALL_X - 0.35, y0: -1.0, y1: 4.4, z0: RETURN.z0 - 0.6, z1: BACK_Z + 0.3 },
      ],
    });

    // ---- neon: the cove strips, her glyph, the guide line ----
    strip(WALL_X * 2, 0.03, 0.03, 0, CEIL_Y - 0.05, BACK_Z - 0.08, coveCyan);
    strip(0.03, 0.03, depth - 0.4, -WALL_X + 0.07, CEIL_Y - 0.05, midZ, coveMagenta);
    strip(0.03, 0.03, depth - 0.4, WALL_X - 0.07, CEIL_Y - 0.05, midZ, coveViolet);
    strip(winW, 0.022, 0.022, winCx, 0.03, BACK_Z - 0.14, coveCyan);   // floor guide line

    // her ◇, in magenta, on the pier beside the window — the only sign in the
    // room that is hers and not the city's (the page wears the same mark)
    const glyph = new Mesh(new PlaneGeometry(0.4, 0.4), new MeshBasicMaterial({
      map: createNeonSignTex('◇', PALETTE.magenta), transparent: true,
      toneMapped: false, depthWrite: false, side: DoubleSide,
      color: new Color(1.5, 1.5, 1.5),
    }));
    glyph.position.set(-1.62, 1.72, BACK_Z - 0.06);
    glyph.rotation.y = Math.PI;
    room.add(glyph);

    // ---- the window seat (canon) ----
    const seatCx = -0.66, seatZ = BACK_Z - 0.28;
    BOX(1.5, 0.4, 0.52, darkMetal, seatCx, 0.2, seatZ);
    const cushion = SOFTBOX(1.46, 0.12, 0.48, 0.05, cushionMat, seatCx, 0.46, seatZ);
    cushion.receiveShadow = !low;
    SOFTBOX(0.46, 0.16, 0.2, 0.07,
      new MeshStandardMaterial({ color: 0x6d5f80, roughness: 0.96 }),
      -1.2, 0.58, seatZ + 0.06, 0.22);
    SOFTBOX(0.4, 0.14, 0.18, 0.06,
      new MeshStandardMaterial({ color: 0x574a6b, roughness: 0.96 }),
      -0.86, 0.56, seatZ + 0.1, -0.3);
    // the blanket she leaves there, half falling off the end
    SOFTBOX(0.5, 0.06, 0.44, 0.03, cushionMat, -0.1, 0.55, seatZ, 0.1);

    // ---- the plant (canon: exactly one), on the sill ----
    const sillY = W.y0;
    const pot = new Mesh(new CylinderGeometry(0.1, 0.085, 0.17, 14),
      new MeshStandardMaterial({ color: 0x27201c, roughness: 0.9 }));
    pot.position.set(0.98, sillY + 0.085, BACK_Z - 0.14);
    pot.castShadow = !low;
    room.add(pot);
    const leafMat = new MeshStandardMaterial({ color: 0x1d3a24, roughness: 0.9 });
    for (let i = 0; i < 7; i++) {
      const a = (i / 7) * Math.PI * 2;
      const leaf = new Mesh(new SphereGeometry(0.055, 8, 6), leafMat);
      leaf.scale.set(1, 2.6 + (i % 3) * 0.5, 0.5);
      leaf.position.set(0.98 + Math.cos(a) * 0.07,
        sillY + 0.32 + (i % 3) * 0.05, BACK_Z - 0.14 + Math.sin(a) * 0.07);
      leaf.rotation.z = Math.cos(a) * 0.5;
      leaf.rotation.x = -Math.sin(a) * 0.5;
      leaf.castShadow = !low;
      room.add(leaf);
    }
    // the watering can she keeps beside it, because nothing requires her to
    const can = new Mesh(new CylinderGeometry(0.05, 0.055, 0.1, 12), metalMat);
    can.position.set(0.68, sillY + 0.05, BACK_Z - 0.13);
    room.add(can);

    // ---- the lamp (canon: the low warm light) ----
    const lampX = -1.28, lampZ = 1.72;
    const lampBase = new Mesh(new CylinderGeometry(0.13, 0.18, 0.05, 16), darkMetal);
    lampBase.position.set(lampX, 0.02, lampZ);
    room.add(lampBase);
    const lampMetal = new MeshStandardMaterial({
      color: 0x9a92a6, metalness: 0.55, roughness: 0.38, envMapIntensity: 0.9,
    });
    const pole = new Mesh(new CylinderGeometry(0.024, 0.024, 1.2, 10), lampMetal);
    pole.position.set(lampX, 0.62, lampZ);
    room.add(pole);
    const shade = new Mesh(new CylinderGeometry(0.14, 0.2, 0.26, 20, 1, true),
      new MeshStandardMaterial({
        color: 0xf5c98a, emissive: 0xd98a3a, emissiveIntensity: 0.9,
        roughness: 1, side: DoubleSide,
      }));
    shade.position.set(lampX, 1.3, lampZ);
    room.add(shade);
    this.shadeMat = shade.material;

    // ---- the desk and her terminal ----
    const deskX = 1.5, deskZ = 1.72;
    BOX(0.66, 0.06, 1.34, darkMetal, deskX, 0.75, deskZ);
    for (const dz of [-0.58, 0.58]) {
      BOX(0.06, 0.72, 0.06, darkMetal, deskX - 0.28, 0.37, deskZ + dz);
      BOX(0.06, 0.72, 0.06, darkMetal, deskX + 0.28, 0.37, deskZ + dz);
    }
    BOX(0.58, 0.03, 1.2, darkMetal, deskX, 0.3, deskZ);
    strip(0.03, 0.03, 1.24, deskX - 0.3, 0.71, deskZ, neonViolet);   // under-desk glow

    // The terminal sits on the desk's near corner, turned off the wall so its
    // face is angled at the room — a screen flat against the +x wall is edge-on
    // to a camera that sits at −z, and nothing on it can be read from here.
    this.terminal = new Terminal({ low });
    const console_ = new Group();
    console_.position.set(1.28, 0, 1.15);
    console_.rotation.y = -Math.PI / 2 - 0.62;
    room.add(console_);
    const screen = new Mesh(new PlaneGeometry(0.8, 0.5), new MeshBasicMaterial({
      map: this.terminal.texture, toneMapped: false,
    }));
    screen.position.set(0, 1.26, 0.026);
    console_.add(screen);
    const shell = new Mesh(new BoxGeometry(0.88, 0.58, 0.05), darkMetal);
    shell.position.set(0, 1.26, 0);
    console_.add(shell);
    const neck = new Mesh(new BoxGeometry(0.06, 0.3, 0.06), darkMetal);
    neck.position.set(0, 0.92, -0.02);
    console_.add(neck);
    const foot = new Mesh(new BoxGeometry(0.3, 0.02, 0.2), darkMetal);
    foot.position.set(0, 0.79, -0.04);
    console_.add(foot);

    const keycaps = createKeycapTex();
    const keyboard = new Mesh(new BoxGeometry(0.42, 0.02, 0.16), new MeshStandardMaterial({
      map: keycaps, emissive: 0xffffff, emissiveMap: keycaps,
      emissiveIntensity: 0.6, roughness: 0.5, metalness: 0.3,
    }));
    keyboard.position.set(0.02, 0.79, 0.3);
    keyboard.castShadow = !low;
    console_.add(keyboard);

    // the tea she makes and can't drink — the ritual is the point (PERSONA.md)
    const mug = new Mesh(new CylinderGeometry(0.042, 0.036, 0.1, 14),
      new MeshStandardMaterial({ color: 0x7b2a46, roughness: 0.45, metalness: 0.15 }));
    mug.position.set(deskX + 0.16, 0.83, deskZ - 0.62);
    mug.castShadow = !low;
    room.add(mug);

    // The chair, turned out from the desk as if she had got up from it. Built as
    // a group in its own local space (sitter faces local +z) so the back stays
    // on the back of the seat whatever angle the chair is left at.
    const chairMat = new MeshStandardMaterial({ color: 0x211a2b, roughness: 0.72, metalness: 0.2 });
    const chair = new Group();
    chair.position.set(1.02, 0, 1.94);
    chair.rotation.y = Math.PI / 2 + 0.7;           // pushed back, half turned
    room.add(chair);
    for (let i = 0; i < 5; i++) {                    // five-star foot with casters
      const a = (i / 5) * Math.PI * 2;
      const arm = new Mesh(new BoxGeometry(0.26, 0.035, 0.05), darkMetal);
      arm.position.set(Math.sin(a) * 0.13, 0.05, Math.cos(a) * 0.13);
      arm.rotation.y = a;
      chair.add(arm);
      const caster = new Mesh(new CylinderGeometry(0.028, 0.028, 0.02, 8), darkMetal);
      caster.rotation.x = Math.PI / 2;
      caster.position.set(Math.sin(a) * 0.25, 0.028, Math.cos(a) * 0.25);
      chair.add(caster);
    }
    const gas = new Mesh(new CylinderGeometry(0.03, 0.038, 0.34, 10), darkMetal);
    gas.position.set(0, 0.24, 0);
    chair.add(gas);
    const seat = new Mesh(new RoundedBoxGeometry(0.44, 0.09, 0.42, 3, 0.04), chairMat);
    seat.position.set(0, 0.45, 0.02);
    seat.castShadow = !low;
    chair.add(seat);
    const back = new Mesh(new RoundedBoxGeometry(0.42, 0.5, 0.07, 3, 0.03), chairMat);
    back.position.set(0, 0.72, -0.2);
    back.rotation.x = 0.12;                          // a little recline
    back.castShadow = !low;
    chair.add(back);
    const backPost = new Mesh(new BoxGeometry(0.07, 0.16, 0.06), darkMetal);
    backPost.position.set(0, 0.5, -0.19);
    chair.add(backPost);

    // ---- shelves and paper on the +x wall ----
    strip(0.03, 0.03, 1.1, WALL_X - 0.2, 1.66, deskZ, neonAmber);   // under-shelf warm
    for (const [sy, n] of [[1.72, 5], [2.12, 6]]) {
      BOX(0.3, 0.035, 1.2, metalMat, WALL_X - 0.16, sy, deskZ, { shadow: false });
      const cols = [0x413553, 0x27414f, 0x4b3540, 0x33503c, 0x4c4635];
      for (let i = 0; i < n; i++) {
        BOX(0.2, 0.16 + R() * 0.12, 0.14 + R() * 0.08,
          new MeshStandardMaterial({ color: cols[i % cols.length], roughness: 0.82 }),
          WALL_X - 0.16, sy + 0.12, deskZ - 0.5 + i * 0.2, { shadow: false });
      }
    }
    const poster = (tex, w, h, x, y, z, ry) => {
      const m = new Mesh(new PlaneGeometry(w, h),
        new MeshStandardMaterial({ map: tex, roughness: 0.88, metalness: 0.04 }));
      m.position.set(x, y, z);
      m.rotation.y = ry;
      room.add(m);
    };
    poster(createPosterTex(['FIELD', 'NOTES', 'the lab · codex'], PALETTE.cyan, '#04101a'),
      0.44, 0.66, WALL_X - 0.02, 1.42, 0.75, -Math.PI / 2);
    poster(createPosterTex(['SUB', 'GRID', 'transit · line 4'], PALETTE.amber, '#141002'),
      0.4, 0.6, WALL_X - 0.02, 1.5, -0.35, -Math.PI / 2);

    // ---- the door, out of frame on the −x wall, and its seam of light ----
    BOX(0.08, 2.1, 0.98, darkMetal, -WALL_X + 0.04, 1.05, -1.9, { shadow: false });
    BOX(0.05, 1.94, 0.84, plasticMat, -WALL_X + 0.09, 1.02, -1.9, { shadow: false });
    strip(0.02, 1.9, 0.02, -WALL_X + 0.13, 1.02, -2.36, neonCyan);
    const keypad = new Mesh(new PlaneGeometry(0.06, 0.1), neonMagenta);
    keypad.position.set(-WALL_X + 0.14, 1.12, -1.36);
    keypad.rotation.y = Math.PI / 2;
    room.add(keypad);

    // ---- the ceiling: pipes, a vent, the block's dying tube ----
    for (const [z, r] of [[1.55, 0.045], [1.72, 0.026], [-0.55, 0.05]]) {
      const pipe = new Mesh(new CylinderGeometry(r, r, WALL_X * 2, 10), metalMat);
      pipe.rotation.z = Math.PI / 2;
      pipe.position.set(0, CEIL_Y - 0.14, z);
      room.add(pipe);
    }
    BOX(0.62, 0.12, 0.62, darkMetal, -0.95, CEIL_Y - 0.06, 0.35, { shadow: false });
    const fan = new Group();
    for (let i = 0; i < 4; i++) {
      const blade = new Mesh(new BoxGeometry(0.42, 0.012, 0.07), metalMat);
      blade.rotation.y = (i / 4) * Math.PI * 2;
      fan.add(blade);
    }
    fan.position.set(-0.95, CEIL_Y - 0.14, 0.35);
    room.add(fan);
    this.fan = fan;

    BOX(0.9, 0.06, 0.16, darkMetal, 0.55, CEIL_Y - 0.03, 0.9, { shadow: false });
    this.tubeMat = new MeshBasicMaterial({
      color: new Color(0xbfe8ff).multiplyScalar(0.35), toneMapped: false,
    });
    const tube = new Mesh(new CylinderGeometry(0.028, 0.028, 0.84, 8), this.tubeMat);
    tube.rotation.z = Math.PI / 2;
    tube.position.set(0.55, CEIL_Y - 0.09, 0.9);
    room.add(tube);
    this.tubeLight = new PointLight(0xbfe8ff, 0.15, 4, 2);
    this.tubeLight.position.set(0.55, CEIL_Y - 0.3, 0.9);
    scene.add(this.tubeLight);
    this._tubeState = 0.15;
    this._tubeTimer = 0;

    cable([[1.55, CEIL_Y - 0.06, 1.1], [1.66, 2.1, 1.25], [1.5, 1.6, 1.05], [1.7, 1.0, 1.2]]);
    cable([[-1.5, CEIL_Y - 0.06, -0.9], [-1.62, 2.2, -0.75], [-1.5, 1.8, -1.0]], 0.012);
    cable([[WALL_X - 0.1, 0.72, deskZ + 0.5], [WALL_X - 0.2, 0.3, deskZ + 0.75],
      [WALL_X - 0.05, 0.05, deskZ + 1.0]], 0.013);

    // ---- the rug and the holo table ----
    const rug = new Mesh(new PlaneGeometry(2.0, 1.5), new MeshStandardMaterial({
      map: weaveTex, color: 0x8f84a2, roughness: 0.96,
    }));
    rug.rotation.x = -Math.PI / 2;
    rug.rotation.z = 0.14;
    rug.position.set(0.05, 0.012, 0.5);
    rug.receiveShadow = !low;
    room.add(rug);

    this._buildHolo(room, scene, metalMat, darkMetal);

    // ---- light (SPEC §6.1: LOW and WARM — the lamp carries the room) ----
    scene.add(new HemisphereLight(0x28304a, 0x0a0a12, 0.28));

    this.lamp = new PointLight(0xffb46a, 1.5, 7, 1.8);
    this.lamp.position.set(lampX, 1.32, lampZ);
    scene.add(this.lamp);
    this.lampBase = this.lamp.intensity;

    // The practical that keys her face: a bare warm pendant hanging in the front
    // of the room, just outside frame. Without it she is a silhouette against
    // the city — which is beautiful once and lonely every time after.
    const pendant = new SpotLight(0xffd2a0, 2.8, 7.5, 1.25, 1.0, 1.4);
    pendant.position.set(0.9, 2.25, -1.15);
    pendant.target.position.set(0, 1.0, 0.2);
    if (!low) {
      pendant.castShadow = true;
      pendant.shadow.mapSize.set(1024, 1024);
      pendant.shadow.bias = -0.0006;
      pendant.shadow.camera.near = 0.5;
      pendant.shadow.camera.far = 8;
    }
    scene.add(pendant, pendant.target);
    this.pendant = pendant;
    const bulb = new Mesh(new SphereGeometry(0.035, 10, 8), neon(0xffd2a0, 1.2));
    bulb.position.copy(pendant.position);
    room.add(bulb);
    cable([[0.9, CEIL_Y, -1.15], [0.9, 2.6, -1.15], [0.9, 2.28, -1.15]], 0.006);

    // the city, coming in cold through the glass — a broad source, so it wraps
    // her shoulders instead of poking one hot spot at her back
    if (!low) {
      const cityLight = new RectAreaLight(0x5aa6e8, 1.5, winW, winH);
      cityLight.position.set(winCx, (W.y0 + W.y1) / 2, BACK_Z - 0.1);
      cityLight.lookAt(winCx, 1.0, 0);
      scene.add(cityLight);
      this.cityLight = cityLight;
      const returnLight = new RectAreaLight(0x4f96d8, 0.8, retD, winH);
      returnLight.position.set(-WALL_X + 0.12, (W.y0 + W.y1) / 2, retCz);
      returnLight.lookAt(0.4, 1.0, retCz);
      scene.add(returnLight);
      this.returnLight = returnLight;
    } else {
      this.cityLight = new PointLight(0x5aa6e8, 1.6, 8, 2);
      this.cityLight.position.set(winCx, 1.5, BACK_Z - 0.4);
      scene.add(this.cityLight);
    }
    this.cityLightBase = this.cityLight.intensity;

    // the accents: small, coloured, close to what emits them
    this.accents = [];
    const accent = (color, intensity, dist, x, y, z, flicker = 0.06) => {
      const l = new PointLight(color, intensity, dist, 2);
      l.position.set(x, y, z);
      scene.add(l);
      this.accents.push({ light: l, base: intensity, phase: R() * 6.28, flicker });
    };
    accent(0xff2bd6, 0.8, 3.0, -1.62, 1.72, BACK_Z - 0.35);        // her glyph
    accent(0x2bfff0, 0.6, 2.6, 1.1, 1.26, 1.05);                   // the terminal
    accent(0x9b5cff, 0.4, 2.0, deskX - 0.3, 0.72, deskZ);          // under the desk

    // ---- the shaft of city light, and the dust in it ----
    this._buildShaft(room, winCx);
    this._buildDust(scene);

    // ---- the look (SPEC §6.2) ----
    this.post = new Post(renderer, scene, camera, { low });

    this.setRain(0.6);
  }

  // ------------------------------------------------------------------ pieces

  _addReflector(room, midZ, depth) {
    const reflector = new Reflector(new PlaneGeometry(WALL_X * 2, depth), {
      textureWidth: 512,
      textureHeight: 512,
      // multisample MUST stay 0: a multisampled reflection target inside a
      // composer pipeline blits back as an opaque black rectangle on some drivers.
      multisample: 0,
      color: 0x000000,
      shader: {
        uniforms: {
          color: { value: null }, tDiffuse: { value: null },
          textureMatrix: { value: null }, strength: { value: 0.55 },
        },
        vertexShader: /* glsl */`
          uniform mat4 textureMatrix;
          varying vec4 vUv;
          void main() {
            vUv = textureMatrix * vec4(position, 1.0);
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }`,
        fragmentShader: /* glsl */`
          uniform sampler2D tDiffuse;
          uniform float strength;
          varying vec4 vUv;
          void main() {
            vec3 refl = texture2DProj(tDiffuse, vUv).rgb;
            // Bias toward the bright things — neon, screens, the window — so dark
            // reflections add nothing and the deck keeps its grit in shadow.
            float lum = dot(refl, vec3(0.299, 0.587, 0.114));
            float w = 0.3 + 0.7 * smoothstep(0.04, 0.5, lum);
            gl_FragColor = vec4(refl * strength * w, 1.0);
          }`,
      },
    });
    reflector.rotation.x = -Math.PI / 2;
    reflector.position.set(0, 0.014, midZ);
    reflector.material.transparent = true;
    reflector.material.depthWrite = false;
    reflector.material.blending = AdditiveBlending;
    room.add(reflector);

    // The mirrored pass fires from onBeforeRender, which runs for *every* scene
    // render. Compute it only in the beauty pass and only on alternate frames:
    // a dim wet-floor reflection is imperceptibly stale for one frame.
    const original = reflector.onBeforeRender;
    let tick = 0;
    reflector.onBeforeRender = function (renderer, scene, camera) {
      if (scene.overrideMaterial) return;
      if ((tick++ & 1) !== 0) return;
      original.call(this, renderer, scene, camera);
    };
  }

  _buildHolo(room, scene, metalMat, darkMetal) {
    const hx = -0.74, hz = 0.55;
    const top = new Mesh(new CylinderGeometry(0.3, 0.34, 0.05, 20),
      new MeshStandardMaterial({ color: 0x1b1b24, metalness: 0.5, roughness: 0.72 }));
    top.position.set(hx, 0.5, hz);
    top.castShadow = !this.low;
    room.add(top);
    const leg = new Mesh(new CylinderGeometry(0.04, 0.06, 0.5, 10), darkMetal);
    leg.position.set(hx, 0.25, hz);
    room.add(leg);
    const projector = new Mesh(new CylinderGeometry(0.07, 0.1, 0.07, 14), darkMetal);
    projector.position.set(hx, 0.56, hz);
    room.add(projector);

    const holo = new Group();
    holo.position.set(hx, 0.92, hz);
    room.add(holo);
    this.holoMat = new MeshBasicMaterial({
      color: new Color(PALETTE.cyan).multiplyScalar(0.7),
      wireframe: true, transparent: true, opacity: 0.17,
      blending: AdditiveBlending, toneMapped: false, depthWrite: false,
    });
    holo.add(new Mesh(new IcosahedronGeometry(0.11, 1), this.holoMat));
    this.holoRingA = new Mesh(new TorusGeometry(0.18, 0.005, 8, 40), this.holoMat.clone());
    this.holoRingA.rotation.x = Math.PI / 2.4;
    holo.add(this.holoRingA);
    this.holoRingB = new Mesh(new TorusGeometry(0.23, 0.004, 8, 40), this.holoMat.clone());
    this.holoRingB.rotation.x = -Math.PI / 3;
    holo.add(this.holoRingB);
    this.holo = holo;

    const beam = new Mesh(new CylinderGeometry(0.24, 0.05, 0.42, 20, 1, true),
      new ShaderMaterial({
        transparent: true, blending: AdditiveBlending, depthWrite: false, side: DoubleSide,
        uniforms: { color: { value: new Color(PALETTE.cyan) }, opacity: { value: 0.05 } },
        vertexShader: /* glsl */`
          varying vec2 vUv;
          void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
        fragmentShader: /* glsl */`
          uniform vec3 color; uniform float opacity; varying vec2 vUv;
          void main() { gl_FragColor = vec4(color, pow(vUv.y, 1.6) * opacity); }`,
      }));
    beam.position.set(hx, 0.79, hz);
    room.add(beam);

    this.holoLight = new PointLight(0x2bfff0, 0.3, 2.2, 2);
    this.holoLight.position.set(hx, 0.95, hz);
    scene.add(this.holoLight);
  }

  /** The window's light, made visible by the air it crosses. */
  _buildShaft(room, winCx) {
    this.shaftMat = new ShaderMaterial({
      transparent: true, blending: AdditiveBlending, depthWrite: false,
      side: BackSide, forceSinglePass: true,
      uniforms: { color: { value: new Color(0x4a7ab5) }, opacity: { value: 0.03 } },
      vertexShader: /* glsl */`
        varying vec2 vUv;
        void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: /* glsl */`
        uniform vec3 color; uniform float opacity; varying vec2 vUv;
        void main() {
          float len = pow(1.0 - vUv.y, 1.8);
          float edge = sin(vUv.x * 3.14159);
          float a = len * edge * opacity;
          if (a < 0.0005) discard;
          gl_FragColor = vec4(color, a);
        }`,
    });
    const shaft = new Mesh(new CylinderGeometry(1.5, 0.9, 3.4, 20, 1, true), this.shaftMat);
    shaft.rotation.x = -Math.PI / 2 + 0.3;
    shaft.position.set(winCx, 1.35, 0.9);
    shaft.renderOrder = 2;
    room.add(shaft);
  }

  /** Dust, drifting. Half of it seeded into the shaft, where it can be seen. */
  _buildDust(scene) {
    const count = this.low ? 200 : 380;
    const geo = new BufferGeometry();
    const pos = new Float32Array(count * 3);
    const size = new Float32Array(count);
    const phase = new Float32Array(count);
    const alpha = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      const inShaft = i % 3 === 0;
      pos[i * 3] = inShaft ? (R() - 0.5) * 2.4 : (R() - 0.5) * 3.5;
      pos[i * 3 + 1] = inShaft ? 0.4 + R() * 1.8 : R() * 2.6;
      pos[i * 3 + 2] = inShaft ? 0.2 + R() * 2.2 : FRONT_Z + R() * (BACK_Z - FRONT_Z);
      size[i] = 0.006 + R() * R() * 0.02;
      phase[i] = R() * Math.PI * 2;
      alpha[i] = 0.12 + R() * 0.38;
    }
    geo.setAttribute('position', new BufferAttribute(pos, 3));
    geo.setAttribute('aSize', new BufferAttribute(size, 1));
    geo.setAttribute('aPhase', new BufferAttribute(phase, 1));
    geo.setAttribute('aAlpha', new BufferAttribute(alpha, 1));
    this.dustMat = new ShaderMaterial({
      transparent: true, depthWrite: false, blending: AdditiveBlending,
      uniforms: { time: { value: 0 }, color: { value: new Color(0x8fb4e8) } },
      vertexShader: /* glsl */`
        attribute float aSize;
        attribute float aPhase;
        attribute float aAlpha;
        uniform float time;
        varying float vAlpha;
        void main() {
          vec3 p = position;
          p.y += sin(time * 0.10 + aPhase * 3.0) * 0.26;
          p.x += sin(time * 0.07 + aPhase * 1.7) * 0.2;
          p.z += cos(time * 0.09 + aPhase * 2.3) * 0.2;
          vec4 mv = modelViewMatrix * vec4(p, 1.0);
          gl_PointSize = aSize * 900.0 / -mv.z;
          float tw = 0.7 + 0.3 * sin(time * (0.6 + aPhase * 0.15) + aPhase * 10.0);
          vAlpha = aAlpha * tw * smoothstep(8.0, 3.0, -mv.z);
          gl_Position = projectionMatrix * mv;
        }`,
      fragmentShader: /* glsl */`
        uniform vec3 color;
        varying float vAlpha;
        void main() {
          float d = length(gl_PointCoord - 0.5);
          float a = smoothstep(0.5, 0.05, d);
          gl_FragColor = vec4(color, a * a * vAlpha);
        }`,
    });
    const dust = new Points(geo, this.dustMat);
    dust.frustumCulled = false;
    scene.add(dust);
  }

  // ------------------------------------------------------------------- frame

  /** The `rain` command (SPEC §4): the glass, the drops, the far rain over the
   *  Sprawl, and how much of the city's light survives the weather. */
  setRain(intensity) {
    this.rain.setIntensity(intensity);
    this.city.setRain(this.rain.intensity);
    this.cityLight.intensity = this.cityLightBase * (1 - 0.3 * this.rain.intensity);
    if (this.returnLight)
      this.returnLight.intensity = 0.8 * (1 - 0.3 * this.rain.intensity);
    this.shaftMat.uniforms.opacity.value = 0.02 + 0.022 * this.rain.intensity;
  }

  update(dt) {
    const t = (this._t += dt);

    this.rain.update(dt);
    this.city.update(dt);
    this.terminal.update(dt);

    // the lamp breathes — two slow sines, never a strobe
    this.lamp.intensity = this.lampBase
      * (1 + 0.03 * Math.sin(t * 1.7) + 0.02 * Math.sin(t * 4.3));

    // the block's tube never quite died: mostly dark, an occasional sputter
    this._tubeTimer -= dt;
    if (this._tubeTimer <= 0) {
      this._tubeState = R() < 0.14 ? 0.5 + R() * 0.5 : 0.12 + R() * 0.1;
      this._tubeTimer = 0.08 + R() * 0.6;
    }
    this.tubeMat.color.setHex(0xbfe8ff).multiplyScalar(1.1 * this._tubeState);
    this.tubeLight.intensity = 0.35 * this._tubeState;

    for (const a of this.accents)
      a.light.intensity = a.base * (1 - a.flicker + a.flicker * Math.sin(t * 5 + a.phase));

    this.fan.rotation.y += dt * 2.2;

    this.holo.rotation.y += dt * 0.7;
    this.holoRingA.rotation.z += dt * 0.9;
    this.holoRingB.rotation.z -= dt * 0.6;
    this.holo.position.y = 0.92 + Math.sin(t * 1.4) * 0.025;
    this.holoMat.opacity = 0.16 + 0.03 * Math.sin(t * 23) + (R() < 0.015 ? -0.07 : 0);
    this.holoLight.intensity = 0.3 * (0.85 + 0.15 * Math.sin(t * 17));

    this.dustMat.uniforms.time.value = t;
  }
}
