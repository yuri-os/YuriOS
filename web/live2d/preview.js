/* The Live2D body preview's page script (SPEC §6.8).
 *
 * The studio frames /live2d/preview/ and talks to it by postMessage:
 *   in:  {type: "model", url}   show this rig — a path on this origin, nothing else
 *        {type: "pause"} / {type: "resume"}
 *   out: {type: "status", state: "loading" | "ready" | "error", text}
 * Both directions are checked against this page's own origin, so neither a
 * foreign frame nor a foreign parent can drive it or read from it.
 */
(function () {
  "use strict";
  const ORIGIN = location.origin;
  let app = null;
  let model = null;
  let token = 0;

  function tell(state, text) {
    if (window.parent !== window) window.parent.postMessage({ type: "status", state, text: text || "" }, ORIGIN);
  }

  function sameOriginPath(url) {
    if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) return null;
    try {
      const resolved = new URL(url, ORIGIN);
      return resolved.origin === ORIGIN ? resolved.pathname + resolved.search : null;
    } catch (_) {
      return null;
    }
  }

  function fit() {
    if (!app || !model) return;
    const { width, height } = app.renderer.screen;
    if (!width || !height) return;
    model.scale.set(1);
    const scale = Math.min(width / (model.width || 1), height / (model.height || 1)) * 0.94;
    model.scale.set(scale);
    model.anchor.set(0.5, 0.5);
    model.position.set(width / 2, height / 2);
  }

  function ensureApp() {
    if (app) return app;
    if (!(window.PIXI && window.PIXI.live2d)) throw new Error("the Live2D runtime is not installed");
    app = new PIXI.Application({ view: document.getElementById("live2d"),
      resizeTo: document.getElementById("stage"), backgroundAlpha: 0, antialias: true });
    addEventListener("resize", () => { app.resize(); fit(); });
    return app;
  }

  async function show(url) {
    const mine = ++token;
    tell("loading");
    try {
      const pixi = ensureApp();
      const next = await PIXI.live2d.Live2DModel.from(url, { autoInteract: false });
      if (mine !== token) { next.destroy(); return; }
      if (model) { pixi.stage.removeChild(model); model.destroy(); }
      model = next;
      pixi.stage.addChild(model);
      fit();
      tell("ready");
    } catch (error) {
      if (mine === token) tell("error", (error && error.message) || String(error));
    }
  }

  addEventListener("message", (event) => {
    if (event.origin !== ORIGIN || event.source !== window.parent) return;
    const data = event.data || {};
    if (data.type === "model") {
      const url = sameOriginPath(data.url);
      if (url) show(url); else tell("error", "a preview shows only this house's own bodies");
    } else if (data.type === "pause") {
      app?.stop();
    } else if (data.type === "resume") {
      app?.start();
    }
  });

  tell("idle");
})();
