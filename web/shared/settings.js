/* The settings panel (SPEC §11, → desktop/routes/settings.py).
 *
 * ONE file, served raw at /shared/settings.js and loaded by both frontends — the
 * VRM sanctuary (web/index.html, where Vite folds it into the bundle) and the
 * vendored Live2D client (web/live2d/index.html, a classic script). It is a
 * framework-free IIFE on purpose so the same source runs in both without a build
 * step in the middle. Both pages carry the identical <dialog id="settings"> markup;
 * everything here is driven by element ids.
 *
 * The gear in the masthead opens the dialog. We fetch the schema + current values
 * from /api/settings, render one control per field, and POST back only the fields
 * the user actually changed — so the hand-written comments in .env survive.
 * Everything is read at server boot, so a save asks for a restart rather than
 * pretending to hot-apply.
 *
 * The CHAT_MODEL / UTILITY_MODEL fields (type "model") get a richer control: a
 * provider dropdown (LM Studio · Ollama · OpenRouter · Custom) beside a model box
 * you can type into OR fill from a live "browse" of what that provider is serving
 * (GET /api/models?provider=…). The stored .env value is the LiteLLM id — the
 * provider prefix + the model — which we split apart on load and re-join on save. */
(() => {
  const dlg = document.getElementById("settings");
  if (!dlg) return;
  const body = document.getElementById("settings-body");
  const note = document.getElementById("settings-note");
  const pathEl = document.getElementById("settings-path");
  let initial = {};        // key → value as loaded (to compute the diff on save)
  let rows = [];           // {key, read} — the single source of truth for save()
  let loaded = false;

  const el = (tag, props = {}, ...kids) => {
    const n = Object.assign(document.createElement(tag), props);
    for (const k of kids) if (k != null) n.append(k);
    return n;
  };

  // provider id ⇄ the LiteLLM prefix its model ids carry (see providers/openrouter.py
  // _route + app/config.py). "custom" is the escape hatch: the value is written
  // verbatim, prefix and all, so any route LiteLLM understands stays reachable.
  const PROVIDERS = [
    { id: "lmstudio", label: "LM Studio", prefix: "lm_studio/" },
    { id: "ollama", label: "Ollama", prefix: "ollama/" },
    { id: "openrouter", label: "OpenRouter", prefix: "openrouter/" },
    { id: "custom", label: "Custom", prefix: "" },
  ];

  function splitModel(value) {
    const v = value || "";
    for (const p of PROVIDERS) {
      if (p.prefix && v.startsWith(p.prefix))
        return { provider: p.id, model: v.slice(p.prefix.length) };
    }
    return { provider: "custom", model: v };
  }

  function joinModel(providerId, model) {
    const m = (model || "").trim();
    const p = PROVIDERS.find((x) => x.id === providerId) || PROVIDERS[3];
    if (p.id === "custom" || !p.prefix) return m;
    return m.startsWith(p.prefix) ? m : p.prefix + m;   // don't double a pasted prefix
  }

  // ---- the model field: provider dropdown + browsable model combobox ----
  function modelField(f) {
    const id = "set-" + f.key;
    const { provider, model } = splitModel(f.value == null ? "" : String(f.value));

    const sel = el("select", { className: "set-input set-model-provider" });
    for (const p of PROVIDERS)
      sel.append(el("option", { value: p.id, textContent: p.label, selected: p.id === provider }));

    const listId = id + "-list";
    const input = el("input", {
      id, className: "set-input set-model-name", type: "text",
      value: model, placeholder: "model id (or click browse)", autocomplete: "off",
    });
    input.setAttribute("list", listId);
    const datalist = el("datalist", { id: listId });

    const browse = el("button", {
      type: "button", className: "set-browse", textContent: "browse",
    });
    const status = el("span", { className: "set-model-status" });

    async function loadList() {
      const p = sel.value;
      if (p === "custom") {
        status.textContent = "custom: type the full id";
        datalist.replaceChildren();
        return;
      }
      status.textContent = "loading…";
      datalist.replaceChildren();
      try {
        const r = await fetch("/api/models?provider=" + encodeURIComponent(p));
        const data = await r.json();
        if (data.error) { status.textContent = data.error; return; }
        const models = data.models || [];
        for (const m of models) datalist.append(el("option", { value: m }));
        status.textContent = models.length
          ? `${models.length} available — click the box`
          : "none loaded there";
        if (models.length) input.focus();      // pop the datalist for the user
      } catch (e) {
        status.textContent = "couldn't load: " + e;
      }
    }

    browse.addEventListener("click", loadList);
    // switching provider invalidates the last browse and clears stale hints
    sel.addEventListener("change", () => { datalist.replaceChildren(); status.textContent = ""; });

    const combo = el("div", { className: "set-model" }, sel, input, browse, datalist);
    const read = () => joinModel(sel.value, input.value);
    return { node: combo, read, status };
  }

  function control(f) {
    const id = "set-" + f.key;
    if (f.type === "select") {
      const s = el("select", { id, className: "set-input" });
      for (const opt of f.options || [])
        s.append(el("option", { value: opt, textContent: opt, selected: String(f.value) === opt }));
      // keep an unknown current value selectable rather than silently losing it
      if (f.value && !(f.options || []).includes(String(f.value)))
        s.append(el("option", { value: f.value, textContent: f.value + " (current)", selected: true }));
      return { node: s, read: () => s.value };
    }
    if (f.type === "bool") {
      const c = el("input", { id, type: "checkbox", className: "set-check", checked: !!f.value });
      return { node: c, read: () => c.checked };
    }
    const input = el("input", {
      id, className: "set-input",
      type: f.type === "password" ? "password" : (f.type === "number" ? "number" : "text"),
      value: f.value == null ? "" : f.value,
    });
    if (f.step) input.step = f.step;
    if (f.min != null) input.min = f.min;
    if (f.max != null) input.max = f.max;
    let datalist = null;
    if (f.suggest && f.suggest.length) {
      const listId = id + "-list";
      input.setAttribute("list", listId);
      datalist = el("datalist", { id: listId });
      for (const s of f.suggest) datalist.append(el("option", { value: s }));
    }
    const read = () => (f.type === "number"
      ? (input.value === "" ? "" : Number(input.value))
      : input.value);
    return { node: input, datalist, read, input };
  }

  function fieldRow(f) {
    const label = el("label", { className: "set-row" });
    const head = el("div", { className: "set-key" }, f.key.toLowerCase());
    const wrap = el("div", { className: "set-ctl" });

    let ctl;
    if (f.type === "model") {
      ctl = modelField(f);
      wrap.append(ctl.node);
    } else {
      ctl = control(f);
      label.htmlFor = "set-" + f.key;         // click-label focuses simple inputs
      wrap.append(ctl.node);
      if (ctl.datalist) wrap.append(ctl.datalist);
      if (f.type === "password" && ctl.input) {
        const reveal = el("button", { type: "button", className: "set-reveal", textContent: "show" });
        reveal.addEventListener("click", () => {
          const hidden = ctl.input.type === "password";
          ctl.input.type = hidden ? "text" : "password";
          reveal.textContent = hidden ? "hide" : "show";
        });
        wrap.append(reveal);
      }
    }

    rows.push({ key: f.key, read: ctl.read });
    initial[f.key] = ctl.read();
    label.append(head, wrap);
    if (f.help) label.append(el("div", { className: "set-help", textContent: f.help }));
    if (ctl.status) label.append(ctl.status);
    return label;
  }

  async function load() {
    try {
      const r = await fetch("/api/settings");
      const data = await r.json();
      pathEl.textContent = data.env_path || "";
      body.textContent = "";
      initial = {}; rows = [];
      for (const g of data.groups) {
        const sec = el("section", { className: "set-group" });
        sec.append(el("h3", { className: "set-group-title", textContent: g.group }));
        for (const f of g.fields) sec.append(fieldRow(f));
        body.append(sec);
      }
      loaded = true;
    } catch (e) {
      body.textContent = "";
      body.append(el("p", { className: "settings-loading", textContent: "couldn't load settings: " + e }));
    }
  }

  async function save() {
    const diff = {};
    for (const row of rows) {
      const now = row.read();
      if (String(now) !== String(initial[row.key])) diff[row.key] = now;
    }
    if (!Object.keys(diff).length) { note.textContent = "no changes"; return; }
    note.textContent = "saving…";
    try {
      const r = await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(diff),
      });
      const res = await r.json();
      const n = (res.written || []).length;
      note.textContent = res.restart_required
        ? `saved ${n} setting${n === 1 ? "" : "s"} — restart the app to apply`
        : "saved";
      note.classList.toggle("restart", !!res.restart_required);
      for (const k of res.written || []) initial[k] = diff[k];   // the new baseline
    } catch (e) {
      note.textContent = "save failed: " + e;
    }
  }

  document.getElementById("settings-open").addEventListener("click", async () => {
    if (!loaded) await load();
    note.textContent = "";
    note.classList.remove("restart");
    dlg.showModal();
  });
  document.getElementById("settings-close").addEventListener("click", () => dlg.close());
  document.getElementById("settings-save").addEventListener("click", save);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // click backdrop
})();
