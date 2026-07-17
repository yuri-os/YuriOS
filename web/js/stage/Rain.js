/* Rain (SPEC §6.2) — two layers, all procedural, no assets:
 *
 *   1. the pane: a streak shader on the window glass — drips run down columns
 *      at pseudo-random speeds over a faint wet sheen;
 *   2. outside: a Points cloud of falling drops beyond the window wall, visible
 *      only through the opening (the wall occludes the rest).
 *
 * `setIntensity(0..1)` follows the `rain` command (SPEC §4): drives streak
 * density and speed, drop count and opacity. The audible layer (the filtered
 * noise bed) lives in web/js/music.js and follows the same command.
 */
import {
  AdditiveBlending, BufferAttribute, BufferGeometry, Color, DoubleSide, Mesh,
  PlaneGeometry, Points, PointsMaterial, ShaderMaterial,
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
  varying vec2 vUv;

  float hash(float n) { return fract(sin(n * 12.9898) * 43758.5453); }

  // one layer of drips: columns, each with its own speed/phase; a column is
  // active only if its hash beats the density threshold
  float streaks(vec2 uv, float t, float cols, float density) {
    float x = uv.x * cols;
    float col = floor(x);
    float r = hash(col + cols);           // per-column randomness
    float on = step(1.0 - density, r);
    float speed = 0.10 + hash(col) * 0.30;
    float y = fract(uv.y + t * speed + r * 7.0);
    float head = smoothstep(0.0, 0.05, y) * smoothstep(0.30, 0.05, y);
    float line = smoothstep(0.35, 0.0, abs(fract(x) - 0.5));
    return head * line * on;
  }

  void main() {
    float t = uTime;
    float d = clamp(uIntensity, 0.0, 1.0);
    // faint wet sheen so the glass reads as glass even between drips
    float sheen = 0.045 + 0.03 * d;
    float drips = streaks(vUv, t, 26.0, 0.25 + 0.55 * d)
                + streaks(vUv, t * 1.6, 41.0, 0.20 + 0.50 * d) * 0.7;
    float a = sheen + drips * (0.25 + 0.45 * d);
    gl_FragColor = vec4(uTint, a);
  }
`;

export class Rain {
  /**
   * @param scene    the three.js scene
   * @param opening  the window opening, world space:
   *                 { wallX, y, z, width, height } — pane fills it; drops fall
   *                 in a slab just beyond wallX.
   */
  constructor(scene, opening) {
    this.intensity = 0.6;

    // --- the pane ---
    this.paneMat = new ShaderMaterial({
      vertexShader: PANE_VERT,
      fragmentShader: PANE_FRAG,
      uniforms: {
        uTime: { value: 0 },
        uIntensity: { value: this.intensity },
        uTint: { value: new Color(0.55, 0.72, 0.85) },
      },
      transparent: true,
      depthWrite: false,
      side: DoubleSide,
    });
    const pane = new Mesh(
      new PlaneGeometry(opening.width, opening.height), this.paneMat);
    pane.position.set(opening.wallX, opening.y, opening.z);
    pane.rotation.y = Math.PI / 2;           // glass lies in the x = wallX plane
    scene.add(pane);
    this.pane = pane;

    // --- the drops outside ---
    this.dropCount = 900;
    const pos = new Float32Array(this.dropCount * 3);
    this.speeds = new Float32Array(this.dropCount);
    // a slab beyond the wall, wider than the opening so drops enter the view
    this.box = {
      x0: opening.wallX - 3.0, x1: opening.wallX - 0.25,
      y0: 0.0, y1: opening.y + opening.height / 2 + 1.6,
      z0: opening.z - opening.width, z1: opening.z + opening.width,
    };
    for (let i = 0; i < this.dropCount; i++) {
      pos[i * 3] = this.box.x0 + Math.random() * (this.box.x1 - this.box.x0);
      pos[i * 3 + 1] = this.box.y0 + Math.random() * (this.box.y1 - this.box.y0);
      pos[i * 3 + 2] = this.box.z0 + Math.random() * (this.box.z1 - this.box.z0);
      this.speeds[i] = 4.5 + Math.random() * 3.5;
    }
    this.geo = new BufferGeometry();
    this.geo.setAttribute('position', new BufferAttribute(pos, 3));
    this.dropMat = new PointsMaterial({
      color: new Color(0.6, 0.75, 0.9),
      size: 0.02,
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

  setIntensity(i) {
    this.intensity = Math.min(1, Math.max(0, i ?? 0));
    this.paneMat.uniforms.uIntensity.value = this.intensity;
    this.dropMat.opacity = 0.15 + 0.45 * this.intensity;
    // fewer drops in a drizzle: draw a prefix of the cloud
    this.geo.setDrawRange(0, Math.floor(this.dropCount * (0.15 + 0.85 * this.intensity)));
    this.drops.visible = this.intensity > 0.01;
    this.pane.visible = true;                 // the sheen stays even at 0 — wet glass
  }

  update(dt) {
    this.paneMat.uniforms.uTime.value += dt;
    if (!this.drops.visible) return;
    const pos = this.geo.attributes.position;
    const fall = 0.6 + 0.6 * this.intensity;  // heavier rain falls faster
    for (let i = 0; i < this.dropCount; i++) {
      let y = pos.getY(i) - this.speeds[i] * fall * dt;
      if (y < this.box.y0) {
        y = this.box.y1;
        pos.setX(i, this.box.x0 + Math.random() * (this.box.x1 - this.box.x0));
        pos.setZ(i, this.box.z0 + Math.random() * (this.box.z1 - this.box.z0));
      }
      pos.setY(i, y);
    }
    pos.needsUpdate = true;
  }
}
