/* The body (SPEC §6, §8.2, → ch. 25) — a Live2D avatar driven by the loop.
 *
 * This is the "expression mapping" half of Build #2's new work. The brain emits
 * abstract expression *names* ([happy], [tender] …, → desktop/voice/emotion.py);
 * this file owns the *realisation* — the map from a name to concrete Live2D
 * parameters on the Hiyori rig. Keeping the map here (not in the brain) is what
 * lets Build #4 swap this whole file for a VRM renderer that consumes the exact
 * same names (SPEC §6.1).
 *
 * Two inputs drive her face:
 *   - expression events  → a parameter preset (mouth shape, eye-smile, brows)
 *   - live audio RMS      → ParamMouthOpenY, so her mouth moves with what she says
 *
 * Model: Hiyori Free (Live2D sample). Its rig exposes ParamMouthOpenY (the
 * LipSync group), ParamMouthForm, ParamEyeL/RSmile, ParamBrowL/RY, ParamEyeBallX/Y
 * — enough for a small, legible expression table. Auto-blink + idle sway come
 * from pixi-live2d-display's built-in motion manager.
 */
(() => {
  // Which rig she wears is set in .env (AVATAR_MODEL) and resolved by the server
  // (/api/config, → desktop/routes/avatar.py). We fetch that at init; if the call
  // fails (server not up, offline) we fall back to Hiyori so the body still loads.
  const DEFAULT_MODEL_URL = "vendor/hiyori/runtime/hiyori_free_t08.model3.json";

  async function resolveModelUrl() {
    try {
      const r = await fetch("/api/config");
      if (r.ok) {
        const j = await r.json();
        if (j && j.avatar_model_url) return j.avatar_model_url;
      }
    } catch (_) { /* server not ready / offline — fall back below */ }
    return DEFAULT_MODEL_URL;
  }

  // name → target parameter values (SPEC §6.1). Absent params fall back to rest.
  // Values are the "intent"; applyExpression() eases toward them so faces glide.
  const PRESETS = {
    neutral:   { ParamMouthForm: 0.3, ParamEyeLSmile: 0, ParamEyeRSmile: 0, ParamBrowLY: 0,   ParamBrowRY: 0 },
    happy:     { ParamMouthForm: 1.0, ParamEyeLSmile: 0.6, ParamEyeRSmile: 0.6, ParamBrowLY: 0.3, ParamBrowRY: 0.3 },
    tender:    { ParamMouthForm: 0.6, ParamEyeLSmile: 0.4, ParamEyeRSmile: 0.4, ParamBrowLY: -0.1, ParamBrowRY: -0.1 },
    sad:       { ParamMouthForm: -0.8, ParamEyeLSmile: 0, ParamEyeRSmile: 0, ParamBrowLY: -0.6, ParamBrowRY: -0.6 },
    surprised: { ParamMouthForm: 0.0, ParamEyeLSmile: 0, ParamEyeRSmile: 0, ParamBrowLY: 1.0, ParamBrowRY: 1.0 },
    shy:       { ParamMouthForm: 0.2, ParamEyeLSmile: 0.3, ParamEyeRSmile: 0.3, ParamBrowLY: -0.3, ParamBrowRY: -0.3 },
    thinking:  { ParamMouthForm: -0.2, ParamEyeLSmile: 0, ParamEyeRSmile: 0, ParamBrowLY: 0.2, ParamBrowRY: -0.2 },
    playful:   { ParamMouthForm: 0.9, ParamEyeLSmile: 0.5, ParamEyeRSmile: 0.2, ParamBrowLY: 0.4, ParamBrowRY: 0.1 },
  };

  let model = null;
  let target = { ...PRESETS.neutral };   // eased toward each frame
  let current = { ...PRESETS.neutral };
  let mouthOpen = 0;                      // set by voice.js from playback RMS

  const lerp = (a, b, t) => a + (b - a) * t;

  // The Live2D runtime (PIXI + the cubism4 plugin) comes from the vendor/ scripts
  // in index.html. Those are synchronous, but some engines (notably WebKitGTK, as
  // used by the desktop-window mode) haven't finished registering PIXI.live2d at
  // the instant voice.js calls init() — so we wait briefly for it rather than
  // concluding vendor/ is empty on the first check (which stranded her body in
  // desktop mode). If it never appears, vendor/ really is empty → voice-only.
  const runtimeReady = () => !!(window.PIXI && window.PIXI.live2d);
  function waitForRuntime(timeoutMs = 5000, stepMs = 50) {
    return new Promise((resolve) => {
      if (runtimeReady()) return resolve(true);
      const t0 = Date.now();
      const iv = setInterval(() => {
        if (runtimeReady()) { clearInterval(iv); resolve(true); }
        else if (Date.now() - t0 > timeoutMs) { clearInterval(iv); resolve(false); }
      }, stepMs);
    });
  }

  async function init() {
    if (!(await waitForRuntime())) {
      console.info("[avatar] Live2D runtime not present (vendor/ empty) — " +
                   "running voiceonly. `python scripts/fetch_avatar.py` to add her body.");
      return false;
    }
    const app = new PIXI.Application({
      view: document.getElementById("live2d"),
      resizeTo: document.getElementById("scene"),
      backgroundAlpha: 0, antialias: true,
    });
    const url = await resolveModelUrl();
    try {
      model = await PIXI.live2d.Live2DModel.from(url, { autoInteract: false });
    } catch (e) {
      console.warn(`[avatar] model failed to load (${url}):`, e);
      return false;
    }
    app.stage.addChild(model);
    fit(app, model);
    window.addEventListener("resize", () => fit(app, model));
    window.__avatar = { app, model };   // dev inspection hook (console only)

    // drive parameters every tick: ease the expression, apply live mouth (§6)
    app.ticker.add(() => {
      const core = model.internalModel.coreModel;
      for (const k of Object.keys(target)) {
        current[k] = lerp(current[k] ?? 0, target[k], 0.15);
        setParam(core, k, current[k]);
      }
      setParam(core, "ParamMouthOpenY", mouthOpen);   // lipsync, overrides idle
    });
    return true;
  }

  function fit(app, m) {
    const { width, height } = app.renderer.screen;
    if (!width || !height) return;
    m.scale.set(1);                                 // measure native size first
    const nw = m.width || 1, nh = m.height || 1;    // Hiyori is ~2978×4177
    // fit her whole body to ~96% of the canvas, then bias slightly upward so the
    // face sits in the upper third (portrait framing, not dead-centre feet)
    const s = Math.min(width / nw, height / nh) * 0.96;
    m.scale.set(s);
    m.anchor.set(0.5, 0.5);
    m.x = width / 2;
    m.y = height / 2;
  }

  function setParam(core, id, value) {
    try { core.setParameterValueById(id, value); } catch (_) { /* param absent on this rig */ }
  }

  // --- the public surface voice.js calls ---
  window.Avatar = {
    init,
    setExpression(name) {                 // an [emotion] tag closed → change face
      if (PRESETS[name]) target = { ...PRESETS[name] };
    },
    setMouth(v) { mouthOpen = Math.max(0, Math.min(1, v)); },   // per audio frame
    ready() { return model !== null; },
  };
})();
