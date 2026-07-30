/* The look (SPEC §6.2–§6.3) — the post chain the sanctuary renders through.
 *
 * The room is lit almost entirely by things that emit: the lamp, the signs, the
 * terminal, the city. Without bloom those read as flat coloured rectangles, so
 * the neon never leaves the surface it is painted on. This chain is what makes
 * the room look like the room:
 *
 *   RenderPass → UnrealBloom → FXAA → Output(ACES+sRGB) → grade
 *
 * The grade is the last word: vignette, a filmic contrast curve with a soft toe,
 * a teal-shadow/amber-highlight split tone, and a whisper of grain. Post-process
 * AA rather than MSAA because the beauty pass lands in a composer target and
 * only a full-screen quad reaches the framebuffer — an MSAA backbuffer would be
 * antialiasing nothing.
 *
 * The tier (→ quality.js) buys the chain down rather than off, because a room
 * without bloom is not this room:
 *
 *   low     no FXAA, no chromatic aberration, bloom mips at 0.7 of the frame
 *   phone   bloom mips at half — a blur asked for a softer blur, which is the
 *           one effect that survives being cheap — and the tone-map folded into
 *           the grade, so a handset runs three full-screen passes instead of
 *           four. Every one of those is the whole screen at half-float.
 */
import { Vector2 } from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { FXAAShader } from 'three/addons/shaders/FXAAShader.js';

import { QUALITY } from './quality.js';

/* ACES + sRGB, lifted from three's own tone-mapping chunks. Only the merged pass
 * needs them: with an OutputPass in the chain the grade already receives
 * display-referred colour, and the maths below has happened. The scene renders
 * into a composer target, so no material tone-maps on the way in — whoever ends
 * the chain owns it. Keep these two in step with the renderer's own settings
 * (VrmStage: ACES filmic, sRGB out); an OutputPass reads them off the renderer,
 * this is the copy that has to be told. */
const TONEMAP = /* glsl */`
  uniform float exposure;
  vec3 acesFit(vec3 v) {
    vec3 a = v * (v + 0.0245786) - 0.000090537;
    vec3 b = v * (0.983729 * v + 0.4329510) + 0.238081;
    return a / b;
  }
  vec3 aces(vec3 c) {
    const mat3 IN = mat3(
      0.59719, 0.07600, 0.02840,
      0.35458, 0.90834, 0.13383,
      0.04823, 0.01566, 0.83777);
    const mat3 OUT = mat3(
       1.60475, -0.10208, -0.00327,
      -0.53108,  1.10813, -0.07276,
      -0.07367, -0.00605,  1.07602);
    c *= exposure / 0.6;
    return clamp(OUT * acesFit(IN * c), 0.0, 1.0);
  }
  vec3 encode(vec3 c) {
    return mix(pow(c, vec3(0.41666)) * 1.055 - 0.055, c * 12.92,
               vec3(lessThanEqual(c, vec3(0.0031308))));
  }
`;

const gradeShader = ({ low, merge, exposure }) => ({
  uniforms: {
    tDiffuse: { value: null },
    time: { value: 0 },
    ...(merge ? { exposure: { value: exposure } } : {}),
  },
  vertexShader: /* glsl */`
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: /* glsl */`
    uniform sampler2D tDiffuse;
    uniform float time;
    varying vec2 vUv;
    float rand(vec2 co){ return fract(sin(dot(co, vec2(12.9898, 78.233))) * 43758.5453); }
    ${merge ? TONEMAP : ''}
    void main() {
      vec2 d = vUv - 0.5;
      float r2 = dot(d, d);
      ${low ? `
      vec3 col = texture2D(tDiffuse, vUv).rgb;
      ` : `
      float ca = 0.0011 * (0.35 + r2 * 3.0);
      vec3 col;
      col.r = texture2D(tDiffuse, vUv + d * ca).r;
      col.g = texture2D(tDiffuse, vUv).g;
      col.b = texture2D(tDiffuse, vUv - d * ca).b;
      `}
      ${merge ? 'col = encode(aces(col));' : ''}
      float vig = smoothstep(0.90, 0.18, r2 * 1.7);
      col *= mix(0.62, 1.0, vig);
      col = (col - 0.5) * 1.07 + 0.5;
      float luma = dot(col, vec3(0.2126, 0.7152, 0.0722));
      col = mix(vec3(luma), col, 1.14);
      col = mix(vec3(luma), col, 1.0 + smoothstep(0.55, 1.0, luma) * 0.12);
      col += (1.0 - smoothstep(0.0, 0.45, luma)) * vec3(-0.010, 0.005, 0.026);
      col += smoothstep(0.6, 1.0, luma) * vec3(0.020, 0.007, -0.014);
      col += (1.0 - smoothstep(0.0, 0.12, luma)) * vec3(0.004, 0.008, 0.016);
      col += (rand(vUv * vec2(1920.0, 1080.0) + fract(time) * 13.7) - 0.5) * 0.016;
      gl_FragColor = vec4(col, 1.0);
    }`,
});

export class Post {
  constructor(renderer, scene, camera) {
    this.renderer = renderer;
    this.q = QUALITY;
    const low = this.q.low;
    this.t = 0;

    const size = renderer.getSize(new Vector2());
    this.composer = new EffectComposer(renderer);
    this.composer.addPass(new RenderPass(scene, camera));

    this.bloom = new UnrealBloomPass(size.clone(), low ? 0.35 : 0.5, 0.6, 0.85);
    // The pass halves the frame once on its own before it starts the mip chain;
    // this halves it again on a phone. Wrapped rather than passed in the
    // constructor because EffectComposer re-sizes every pass to the effective
    // framebuffer as it is added, and again on every resize — the constructor's
    // resolution never survives the first of those.
    if (this.q.bloomScale !== 1) {
      const proto = UnrealBloomPass.prototype.setSize.bind(this.bloom);
      const f = this.q.bloomScale;
      this.bloom.setSize = (w, h) => proto(Math.round(w * f), Math.round(h * f));
    }
    this.composer.addPass(this.bloom);

    if (!low) {
      this.fxaa = new ShaderPass(FXAAShader);
      this.composer.addPass(this.fxaa);
    }
    // The merged tier ends the chain with one pass instead of two: tone-map,
    // encode and grade in a single trip over the frame (→ TONEMAP).
    if (!this.q.mergeGrade) this.composer.addPass(new OutputPass());

    this.grade = new ShaderPass(gradeShader({
      low, merge: this.q.mergeGrade, exposure: renderer.toneMappingExposure,
    }));
    this.composer.addPass(this.grade);

    // Sized last, so the bloom wrapper and the composer's own targets are laid
    // out at the ratio the renderer actually ended up with.
    this.setSize(size.x, size.y);
  }

  _fxaaResolution() {
    if (!this.fxaa) return;
    const size = this.renderer.getSize(new Vector2());
    const pr = this.renderer.getPixelRatio();
    this.fxaa.material.uniforms.resolution.value.set(1 / (size.x * pr), 1 / (size.y * pr));
  }

  setSize(w, h) {
    this.composer.setPixelRatio(this.renderer.getPixelRatio());
    this.composer.setSize(w, h);
    this._fxaaResolution();
  }

  render(delta) {
    this.t += delta;
    this.grade.uniforms.time.value = this.t;
    this.composer.render(delta);
  }
}
