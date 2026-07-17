/* Emotion blendshape state machine (SPEC §3.4; CS §5.2) — eased blends, ported
 * from the vrm-viewer reference impl with two Build #4 changes:
 *
 *  1. The catalog covers Build #2's full 8-name palette (desktop/voice/emotion.py
 *     PALETTE) — the names the brain actually emits — each realised as a composite
 *     of the six VRM *preset* expressions, the only names guaranteed to exist on
 *     any conformant model (→ ch. 25). The six preset names also work directly
 *     (VrmController.set_expression's catalog). This file IS the name→parameter
 *     map B2 §6.1 requires to live in the frontend: swap the renderer, keep the
 *     names, and the brain never changes.
 *
 *  2. The blender never touches 'blink' or 'aa' — those channels are owned by the
 *     blink controller and the viseme driver (SPEC §3.4). The vrm-viewer original
 *     zeroed every expression on each blend, which froze blinking mid-blend and
 *     fought the mouth; the exclusion is the fix (first made in the YuriOS port).
 */

// Values are deliberately < 1.0 to avoid an over-expressive face (CS §5.2).
// Mouth-viseme accents (ee/oh) are allowed; 'aa' is not — the viseme driver owns it.
const EMOTIONS = new Map([
  // -- the six VRM presets (direct, for the puppet channel) --
  ['happy',     { expr: [['happy', 0.7]],                    blend: 0.4 }],
  ['sad',       { expr: [['sad', 0.7], ['oh', 0.15]],        blend: 0.4 }],
  ['angry',     { expr: [['angry', 0.7], ['ee', 0.3]],       blend: 0.3 }],
  ['surprised', { expr: [['surprised', 0.8], ['oh', 0.4]],   blend: 0.15 }],
  ['neutral',   { expr: [['neutral', 1.0]],                  blend: 0.6 }],
  ['relaxed',   { expr: [['relaxed', 0.75]],                 blend: 0.5 }],
  // -- the Build #2 palette names the brain emits (SPEC §3.4), as preset composites --
  // (neutral/happy/sad/surprised above are shared by both catalogs)
  ['shy',       { expr: [['sad', 0.25], ['happy', 0.3], ['relaxed', 0.35]], blend: 0.45 }],
  ['thinking',  { expr: [['relaxed', 0.45], ['ee', 0.12]],   blend: 0.5 }],
  ['playful',   { expr: [['happy', 0.6], ['surprised', 0.2]], blend: 0.25 }],
  ['tender',    { expr: [['relaxed', 0.6], ['happy', 0.25]], blend: 0.55 }],
]);

const lerp = (a, b, t) => a + (b - a) * t;
const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const clamp01 = (v) => Math.min(1, Math.max(0, v));

export class EmoteController {
  constructor(vrm) {
    this.vrm = vrm;
    this.current = null;
    this.transitioning = false;
    this.progress = 0;
    this.start = new Map();
    this.target = new Map();
    this.resetTimer = undefined;
  }

  /** Apply an emotion. When `resetAfterMs` is set, return to neutral afterwards. */
  setEmotion(name, intensity = 1, resetAfterMs) {
    if (this.resetTimer) {
      clearTimeout(this.resetTimer);
      this.resetTimer = undefined;
    }
    const state = EMOTIONS.get(name);
    if (!state) {
      console.warn(`[EmoteController] unknown emotion: ${name}`);
      return;
    }

    this.current = name;
    this.transitioning = true;
    this.progress = 0;
    this.start.clear();
    this.target.clear();

    const k = clamp01(intensity);
    const mgr = this.vrm.expressionManager;
    if (mgr) {
      // Start the lerp from the actual displayed values (avoids snap-to-zero) —
      // but never claim 'blink'/'aa': owned by the blink + viseme channels (§3.4).
      for (const exprName of Object.keys(mgr.expressionMap)) {
        if (exprName === 'blink' || exprName === 'aa') continue;
        this.start.set(exprName, mgr.getValue(exprName) ?? 0);
        this.target.set(exprName, 0);
      }
    }
    for (const [n, v] of state.expr) {
      if (n === 'aa') continue;                     // belt and suspenders (§3.4)
      this.target.set(n, v * k);
    }

    if (resetAfterMs && name !== 'neutral')
      this.resetTimer = setTimeout(() => this.setEmotion('neutral'), resetAfterMs);
  }

  /** Per-frame (CS §4 step 8). Only stages values; expressionManager.update() commits. */
  update(delta) {
    if (!this.transitioning || !this.current) return;
    const state = EMOTIONS.get(this.current);
    this.progress += delta / state.blend;
    if (this.progress >= 1) {
      this.progress = 1;
      this.transitioning = false;
    }
    const eased = easeInOutCubic(this.progress);
    const mgr = this.vrm.expressionManager;
    if (!mgr) return;
    for (const [name, tgt] of this.target)
      mgr.setValue(name, lerp(this.start.get(name) ?? 0, tgt, eased));
  }
}

export const KNOWN_EMOTIONS = [...EMOTIONS.keys()];
