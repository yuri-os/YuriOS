/* The VRM stage (SPEC §3; CS §4, §8) — port of the vrm-viewer VrmStage.ts to
 * no-build ES modules, with Build #4's changes:
 *
 *   - No OrbitControls: the camera is fixed and cinematic with subtle mouse
 *     parallax (SPEC §6.3) — this is a place she lives in, not a model viewer.
 *   - Step 9 of the update loop is the viseme driver (SPEC §5): the mouth follows
 *     the RMS of the audio actually playing, with the `mouth` command as the
 *     scripted override (max of the two wins).
 *   - An environment hook: the sanctuary scene (SanctuaryScene.js) registers
 *     itself and gets update(dt) inside the same frame.
 *
 * The per-frame update order is CS §4, verbatim, and `vrm.update()` is never
 * called — the manual order is the whole point (spring bones LAST, or physics
 * and expressions fight; CS §8 is the checklist).
 */
import { createVRMAnimationClip } from '@pixiv/three-vrm-animation';
import {
  ACESFilmicToneMapping, AnimationMixer, Euler, LoopOnce, LoopRepeat,
  MathUtils, PerspectiveCamera, Quaternion, Scene, Vector3, VectorKeyframeTrack,
  WebGLRenderer,
} from 'three';

import { Blink } from './Blink.js';
import { EmoteController } from './EmoteController.js';
import { GazeController } from './GazeController.js';
import { getLoader, loadVrm } from './VrmLoader.js';

export class VrmStage {
  /** `transparent` + `frameBody` are desktop mode (SPEC §6.5): the renderer
   *  clears to alpha 0 so the OS desktop shows through the frameless window,
   *  and the camera frames the whole body instead of head-and-torso-in-a-room. */
  constructor(container, { transparent = false, frameBody = false } = {}) {
    this.scene = new Scene();
    this.container = container;
    this.frameBody = frameBody;

    this.renderer = new WebGLRenderer({ antialias: true, alpha: transparent });
    if (transparent) this.renderer.setClearColor(0x000000, 0);
    // pixelRatio capped: the LLM and this renderer share one GPU (→ ch. 24 VRAM math)
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    this.renderer.setSize(container.clientWidth, container.clientHeight);
    this.renderer.toneMapping = ACESFilmicToneMapping;
    container.appendChild(this.renderer.domElement);

    this.camera = new PerspectiveCamera(32, container.clientWidth / container.clientHeight, 0.1, 60);
    this.camera.position.set(0, 1.25, -2.2);
    this.cameraHome = this.camera.position.clone();   // parallax anchor
    this.cameraTarget = new Vector3(0, 1.1, 0);
    this.parallax = { x: 0, y: 0 };                   // -1..1 mouse position

    this.vrm = undefined;
    this.group = undefined;
    this.mixer = undefined;
    this.emote = undefined;
    this.gaze = undefined;
    this.blink = new Blink();
    this.environment = undefined;                     // SanctuaryScene (SPEC §6)

    // Channel: direct bone overrides (CS §5.6 seam — generic pose input).
    this.boneOverrides = new Map();
    // Channel: scripted mouth override; the viseme driver is the live source (§5.3).
    this.mouth = 0;
    this.visemeSource = undefined;                    // () => 0..1 from the analyser
    // Persistent appearance state, re-applied on every model load (SPEC §4).
    this.materialColors = new Map();

    this.lastTime = performance.now();
    this.running = false;

    window.addEventListener('resize', () => this.onResize());
    window.addEventListener('mousemove', (e) => {
      this.parallax.x = (e.clientX / window.innerWidth) * 2 - 1;
      this.parallax.y = (e.clientY / window.innerHeight) * 2 - 1;
    });
  }

  onResize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  async loadModel(url, onProgress) {
    const loaded = await loadVrm(url, onProgress);

    if (this.group) {                                 // tear down any previous model
      this.scene.remove(this.group);
      this.mixer?.stopAllAction();
    }

    this.vrm = loaded.vrm;
    this.group = loaded.group;
    this.scene.add(loaded.group);

    this.emote = new EmoteController(loaded.vrm);
    this.gaze = new GazeController(this.camera, loaded.eyeHeight);
    this.mixer = new AnimationMixer(loaded.vrm.scene);
    this.boneOverrides.clear();
    this.mouth = 0;

    // Re-apply persistent appearance requested before this model was ready (§4).
    for (const [name, hex] of this.materialColors)
      this.applyMaterialColor(name, hex);

    // Cinematic framing (SPEC §6.3): head-and-torso in frame, pulled back enough
    // that the room reads around her. Fixed; only parallax moves it.
    // Desktop mode (SPEC §6.5) has no room to read, so the whole body is the
    // frame — head to feet, centred, like a figure standing on the taskbar.
    const headY = loaded.eyeHeight || loaded.modelCenter.y;
    const hipsY = this.vrm.humanoid?.getNormalizedBoneNode('hips')
      ?.getWorldPosition(new Vector3()).y ?? headY - 0.55;
    const top = headY + (this.frameBody ? 0.16 : 0.22);
    const bottom = this.frameBody ? -0.02 : hipsY - 0.35;
    const mid = (top + bottom) / 2;
    const dist = ((top - bottom) / 2) / Math.tan(MathUtils.degToRad(this.camera.fov / 2)) + 0.25;
    if (this.frameBody) this.cameraHome.set(0, mid, -dist);
    else this.cameraHome.set(0.18, headY - 0.06, -dist);   // slightly off-axis: a room, not a lineup
    this.cameraTarget.set(0, mid, 0);
    this.camera.position.copy(this.cameraHome);
    this.camera.lookAt(this.cameraTarget);
  }

  // CS §5.1 — load a .vrma, retarget to the current VRM, re-anchor hips, play.
  async playAnimation(url, loop = true, fadeIn = 0.3) {
    if (!this.vrm || !this.mixer) throw new Error('No model loaded');

    const gltf = await getLoader().loadAsync(url);
    const vrmAnimation = gltf.userData.vrmAnimations?.[0];
    if (!vrmAnimation) throw new Error(`No VRM animation in ${url}`);

    const clip = createVRMAnimationClip(vrmAnimation, this.vrm);
    this.reAnchorHips(clip);

    const action = this.mixer.clipAction(clip);
    action.setLoop(loop ? LoopRepeat : LoopOnce, Infinity);
    action.clampWhenFinished = !loop;
    this.mixer.stopAllAction();
    action.reset().fadeIn(fadeIn).play();
  }

  // CS §5.1 step 3 — prevent the avatar teleporting to the animator's origin.
  reAnchorHips(clip) {
    const hips = this.vrm?.humanoid?.getNormalizedBoneNode('hips');
    if (!hips) return;
    hips.updateMatrixWorld(true);
    const restHip = hips.getWorldPosition(new Vector3());
    const track = clip.tracks.find(
      (t) => t instanceof VectorKeyframeTrack && t.name === `${hips.name}.position`,
    );
    if (!track) return;
    const d = new Vector3(track.values[0], track.values[1], track.values[2]).sub(restHip);
    for (const t of clip.tracks) {
      if (t.name.endsWith('.position') && t instanceof VectorKeyframeTrack) {
        for (let i = 0; i < t.values.length; i += 3) {
          t.values[i] -= d.x;
          t.values[i + 1] -= d.y;
          t.values[i + 2] -= d.z;
        }
      }
    }
  }

  // ---- Control surface (called by the bridge and the voice client) ----

  /** `resetAfterMs` 0/undefined = hold until the next change (voice turns);
   *  the puppet channel passes 3000 (vrm-viewer's auto-reset semantics). */
  setExpression(name, intensity = 1, resetAfterMs = 0) {
    this.emote?.setEmotion(name, intensity, resetAfterMs || undefined);
  }

  setExpressionRaw(values) {
    const mgr = this.vrm?.expressionManager;
    if (!mgr) return;
    for (const [k, v] of Object.entries(values)) mgr.setValue(k, v);
  }

  lookAtMode(mode) { this.gaze?.setMode(mode); }

  lookAtTarget(t) { this.gaze?.setFixedTarget(t); }

  setMouth(value) { this.mouth = Math.min(1, Math.max(0, value)); }

  /** Register the live viseme source (web/js/viseme.js): a fn returning 0..1. */
  setVisemeSource(fn) { this.visemeSource = fn; }

  setMaterialColor(name, hex) {
    this.materialColors.set(name, hex);
    if (!this.vrm) return;                            // applied when the model loads
    const found = this.applyMaterialColor(name, hex);
    if (!found)
      console.warn(`[VrmStage] material not found: ${name} (have: ${this.materialNames().join(', ')})`);
  }

  applyMaterialColor(name, hex) {
    const vrm = this.vrm;
    if (!vrm) return 0;
    let found = 0;
    const apply = (m) => {
      if (!m || m.name !== name) return;
      m.color?.set(hex);
      m.shadeColorFactor?.set(hex);
      found++;
    };
    vrm.materials?.forEach(apply);
    if (!found) {
      vrm.scene.traverse((o) => {
        const mm = o.material;
        if (Array.isArray(mm)) mm.forEach(apply);
        else apply(mm);
      });
    }
    return found;
  }

  materialNames() {
    return (this.vrm?.materials ?? []).map((m) => m.name ?? '').filter(Boolean);
  }

  // Set a humanoid bone's local rotation from Euler degrees (CS §5.6 pose seam).
  setBone(name, euler) {
    if (!this.vrm?.humanoid?.getNormalizedBoneNode(name)) {
      console.warn(`[VrmStage] unknown bone: ${name}`);
      return;
    }
    const q = new Quaternion().setFromEuler(new Euler(
      MathUtils.degToRad(euler.x),
      MathUtils.degToRad(euler.y),
      MathUtils.degToRad(euler.z),
      'XYZ',
    ));
    this.boneOverrides.set(name, q);
  }

  resetBone(name) {
    if (name) this.boneOverrides.delete(name);
    else this.boneOverrides.clear();
  }

  // ---- Render loop (CS §4 — manual order; vrm.update() is never called) ----

  start() {
    if (this.running) return;
    this.running = true;
    this.lastTime = performance.now();
    const tick = () => {
      if (!this.running) return;
      const now = performance.now();
      const delta = Math.min((now - this.lastTime) / 1000, 0.05);
      this.lastTime = now;
      this.update(delta);
      this.renderer.render(this.scene, this.camera);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  stop() { this.running = false; }

  update(delta) {
    // camera parallax (SPEC §6.3): drift a few cm toward the cursor, always
    // re-aiming at her — presence, not navigation.
    this.camera.position.x = this.camera.position.x
      + ((this.cameraHome.x + this.parallax.x * 0.07) - this.camera.position.x) * 0.05;
    this.camera.position.y = this.camera.position.y
      + ((this.cameraHome.y - this.parallax.y * 0.04) - this.camera.position.y) * 0.05;
    this.camera.position.z = this.cameraHome.z;
    this.camera.lookAt(this.cameraTarget);

    // the room lives too (rain, flicker) — same frame, outside the CS §4 order
    this.environment?.update(delta);

    const vrm = this.vrm;
    if (!vrm) return;

    // 1. body animation clip
    this.mixer?.update(delta);
    // 2. animated material uniforms (MToon/shader)
    vrm.materials?.forEach((m) => m.update?.(delta));
    // 4. external pose hook → direct bone overrides (after mixer, before humanoid.update)
    for (const [name, q] of this.boneOverrides) {
      const node = vrm.humanoid?.getNormalizedBoneNode(name);
      if (node) node.quaternion.copy(q);
    }
    // 5. flush normalized → raw skeleton
    vrm.humanoid?.update();
    // 6. gaze
    this.gaze?.update(vrm, delta);
    // 7. blink (stages 'blink')
    this.blink.update(vrm, delta);
    // 8. emotion (stages expression weights; never 'blink'/'aa')
    this.emote?.update(delta);
    // 9. viseme (stages 'aa'): the live audio drives the mouth (SPEC §5); the
    //    scripted `mouth` command is an override — the louder of the two wins.
    const live = this.visemeSource ? this.visemeSource() : 0;
    const m = Math.max(this.mouth, live);
    if (m > 0.001) vrm.expressionManager?.setValue('aa', m);
    // 10. commit all staged blendshape weights
    vrm.expressionManager?.update();
    // 11. constraints
    vrm.nodeConstraintManager?.update();
    // 12. spring-bone physics (LAST — CS §8)
    vrm.springBoneManager?.update(delta);
  }
}
