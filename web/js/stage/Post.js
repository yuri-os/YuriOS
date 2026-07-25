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
 * `low` (see SanctuaryScene) drops FXAA and the chromatic aberration: this GPU
 * is usually also running her model (→ SPEC §3), so every full-screen pass is a
 * choice, not a freebie.
 */
import { Vector2 } from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { FXAAShader } from 'three/addons/shaders/FXAAShader.js';

const gradeShader = (low) => ({
  uniforms: { tDiffuse: { value: null }, time: { value: 0 } },
  vertexShader: /* glsl */`
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: /* glsl */`
    uniform sampler2D tDiffuse;
    uniform float time;
    varying vec2 vUv;
    float rand(vec2 co){ return fract(sin(dot(co, vec2(12.9898, 78.233))) * 43758.5453); }
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
  constructor(renderer, scene, camera, { low = false } = {}) {
    this.renderer = renderer;
    this.low = low;
    this.t = 0;

    const size = renderer.getSize(new Vector2());
    this.composer = new EffectComposer(renderer);
    this.composer.setPixelRatio(renderer.getPixelRatio());
    this.composer.setSize(size.x, size.y);
    this.composer.addPass(new RenderPass(scene, camera));

    this.bloom = new UnrealBloomPass(size.clone(), low ? 0.35 : 0.5, 0.6, 0.85);
    this.composer.addPass(this.bloom);

    if (!low) {
      this.fxaa = new ShaderPass(FXAAShader);
      this.composer.addPass(this.fxaa);
    }
    this.composer.addPass(new OutputPass());

    this.grade = new ShaderPass(gradeShader(low));
    this.composer.addPass(this.grade);

    this._fxaaResolution();
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
