/* Rain (SPEC §6.2) — two layers, all procedural, no assets:
 *
 *   1. the glass: a streak-and-bead shader on every pane of the corner window —
 *      drips run down columns at pseudo-random speeds over a faint wet sheen,
 *      with fat beads clinging where the drips have not found them yet;
 *   2. outside: a Points cloud of falling drops in the volumes beyond the glass,
 *      visible only through the openings (the walls occlude the rest).
 *
 * `setIntensity(0..1)` follows the `rain` command (SPEC §4): drives streak
 * density and speed, drop count and opacity. The audible layer (the filtered
 * noise bed) lives in web/js/music.js and follows the same command, and the far
 * rain over the Sprawl (stage/sanctuary/City.js) follows it too.
 */
import {
  AdditiveBlending, BufferAttribute, BufferGeometry, Color, DoubleSide, Mesh,
  PlaneGeometry, Points, PointsMaterial, ShaderMaterial, Vector2,
} from 'three';

const PANE_VERT = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const PANE_FRAG = /* glsl */ `
  uniform float uTime;
  uniform float uIntensity;
  uniform vec3 uTint;
  uniform vec2 uCells;                  // bead grid, sized per pane so beads stay round
  varying vec2 vUv;

  float hash(float n) { return fract(sin(n * 12.9898) * 43758.5453); }
  float hash2(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

  // one layer of drips: columns, each with its own speed/phase; a column is
  // active only if its hash beats the density threshold
  float streaks(vec2 uv, float t, float cols, float density) {
    float x = uv.x * cols;
    float col = floor(x);
    float r = hash(col + cols);           // per-column randomness
    float on = step(1.0 - density, r);
    float speed = 0.10 + hash(col) * 0.30;
    float y = fract(uv.y + t * speed + r * 7.0);
    float head = smoothstep(0.0, 0.02, y) * smoothstep(0.13, 0.02, y);
    float line = smoothstep(0.35, 0.0, abs(fract(x) - 0.5));
    return head * line * on;
  }

  void main() {
    float t = uTime;
    float d = clamp(uIntensity, 0.0, 1.0);
    // faint wet sheen so the glass reads as glass even between drips
    float sheen = 0.045 + 0.03 * d;
    float drips = streaks(vUv, t, 58.0, 0.22 + 0.5 * d)
                + streaks(vUv, t * 1.6, 91.0, 0.18 + 0.45 * d) * 0.7;
    // beads: a jittered grid of droplets that just sit there, catching the city
    vec2 id = floor(vUv * uCells);
    vec2 cell = fract(vUv * uCells) - 0.5;
    cell += (vec2(hash2(id), hash2(id + 4.2)) - 0.5) * 0.55;
    float bead = smoothstep(0.16, 0.04, length(cell)) * step(0.68, hash2(id + 9.1));
    float a = sheen + drips * (0.18 + 0.3 * d) + bead * (0.05 + 0.13 * d);
    gl_FragColor = vec4(uTint, a);
  }
`;

export class Rain {
  /**
   * @param scene    the three.js scene
   * @param opening  the window, world space:
   *                 { panes: [{ x, y, z, width, height, axis }], volumes: [box] }
   *                 — a pane lies in the plane of its `axis` ('z' = the picture
   *                 window, 'x' = the corner return); each volume is a slab of
   *                 falling drops outside, given as { x0,x1, y0,y1, z0,z1 }.
   */
  constructor(scene, opening) {
    this.intensity = 0.6;

    // --- the glass ---
    this.paneMats = [];
    this.panes = [];
    for (const p of opening.panes) {
      const mat = new ShaderMaterial({
        vertexShader: PANE_VERT,
        fragmentShader: PANE_FRAG,
        uniforms: {
          uTime: { value: 0 },
          uIntensity: { value: this.intensity },
          uTint: { value: new Color(0.55, 0.72, 0.85) },
          // square-ish cells whatever the pane's proportions
          uCells: { value: new Vector2(Math.max(6, Math.round(p.width * 13)),
            Math.max(6, Math.round(p.height * 13))) },
        },
        transparent: true,
        depthWrite: false,
        side: DoubleSide,
      });
      const pane = new Mesh(new PlaneGeometry(p.width, p.height), mat);
      pane.position.set(p.x, p.y, p.z);
      if (p.axis === 'x') pane.rotation.y = Math.PI / 2;   // glass in the x = const plane
      pane.renderOrder = 3;
      scene.add(pane);
      this.paneMats.push(mat);
      this.panes.push(pane);
    }

    // --- the drops outside ---
    this.dropCount = 1100;
    const volumes = opening.volumes;
    const pos = new Float32Array(this.dropCount * 3);
    this.speeds = new Float32Array(this.dropCount);
    this.homes = new Array(this.dropCount);
    for (let i = 0; i < this.dropCount; i++) {
      const box = volumes[i % volumes.length];
      this.homes[i] = box;
      this._seed(pos, i, box, box.y0 + Math.random() * (box.y1 - box.y0));
      this.speeds[i] = 4.5 + Math.random() * 3.5;
    }
    this.geo = new BufferGeometry();
    this.geo.setAttribute('position', new BufferAttribute(pos, 3));
    this.dropMat = new PointsMaterial({
      color: new Color(0.6, 0.75, 0.9),
      size: 0.014,
      transparent: true,
      opacity: 0.5,
      blending: AdditiveBlending,
      depthWrite: false,
    });
    this.drops = new Points(this.geo, this.dropMat);
    this.drops.frustumCulled = false;
    scene.add(this.drops);

    this.setIntensity(this.intensity);
  }

  _seed(pos, i, box, y) {
    pos[i * 3] = box.x0 + Math.random() * (box.x1 - box.x0);
    pos[i * 3 + 1] = y;
    pos[i * 3 + 2] = box.z0 + Math.random() * (box.z1 - box.z0);
  }

  setIntensity(i) {
    this.intensity = Math.min(1, Math.max(0, i ?? 0));
    for (const m of this.paneMats) m.uniforms.uIntensity.value = this.intensity;
    this.dropMat.opacity = 0.12 + 0.33 * this.intensity;
    // fewer drops in a drizzle: draw a prefix of the cloud. The volumes are
    // interleaved (i % volumes.length), so a prefix thins them all evenly.
    this.geo.setDrawRange(0, Math.floor(this.dropCount * (0.15 + 0.85 * this.intensity)));
    this.drops.visible = this.intensity > 0.01;
    for (const p of this.panes) p.visible = true;   // the sheen stays at 0 — wet glass
  }

  update(dt) {
    for (const m of this.paneMats) m.uniforms.uTime.value += dt;
    if (!this.drops.visible) return;
    const pos = this.geo.attributes.position;
    const fall = 0.6 + 0.6 * this.intensity;        // heavier rain falls faster
    const arr = pos.array;
    for (let i = 0; i < this.dropCount; i++) {
      const box = this.homes[i];
      const y = arr[i * 3 + 1] - this.speeds[i] * fall * dt;
      if (y < box.y0) this._seed(arr, i, box, box.y1);
      else arr[i * 3 + 1] = y;
    }
    pos.needsUpdate = true;
  }
}
