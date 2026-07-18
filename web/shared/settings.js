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

  // ---- the browsable model combobox, shared by chat + embedding fields ----
  // The "browse" list is a real dropdown we render ourselves, NOT a native
  // <datalist> — a datalist silently filters its options against whatever is
  // already typed in the box, so a manually-entered model would hide the very
  // list you clicked browse to see. Ours shows every model on browse and only
  // narrows as you type into it.
  //
  // `getProviderId()` is read live at browse time — for chat it's the provider
  // dropdown beside the box; for embeddings it's the EMBED_BACKEND select above.
  // `hints[provider]` short-circuits the fetch with a message for providers that
  // have nothing to list (custom ids; sentence_tf, which is an in-process HF repo).
  function attachBrowse(input, getProviderId, hints) {
    const browse = el("button", { type: "button", className: "set-browse", textContent: "browse" });
    const status = el("span", { className: "set-model-status" });
    const list = el("div", { className: "set-model-list", hidden: true });
    let all = [];              // last fetched models
    let fetchedFor = null;     // which provider `all` was fetched for

    function renderList(filter) {
      list.replaceChildren();
      const q = (filter || "").trim().toLowerCase();
      const shown = (q ? all.filter((m) => m.toLowerCase().includes(q)) : all).slice(0, 500);
      if (!shown.length) {
        list.append(el("div", { className: "set-model-empty",
          textContent: all.length ? "no match" : "nothing to show" }));
        return;
      }
      for (const m of shown) {
        const opt = el("button", { type: "button", className: "set-model-opt", textContent: m });
        // mousedown (not click) so we win the race against the input's blur
        opt.addEventListener("mousedown", (e) => { e.preventDefault(); input.value = m; hide(); });
        list.append(opt);
      }
    }
    const show = () => { list.hidden = false; };
    const hide = () => { list.hidden = true; };

    async function browseModels() {
      const p = getProviderId();
      if (hints && hints[p] != null) { status.textContent = hints[p]; hide(); return; }
      if (fetchedFor === p && all.length) {   // already have them — just reopen, full list
        renderList(""); show(); input.focus(); return;
      }
      status.textContent = "loading…";
      try {
        const r = await fetch("/api/models?provider=" + encodeURIComponent(p));
        const data = await r.json();
        if (data.error) { status.textContent = data.error; return; }
        all = data.models || []; fetchedFor = p;
        status.textContent = all.length ? `${all.length} available` : "none loaded there";
        renderList("");                       // browse shows ALL, ignoring the typed value
        if (all.length) { show(); input.focus(); }
      } catch (e) {
        status.textContent = "couldn't load: " + e;
      }
    }

    browse.addEventListener("click", browseModels);
    input.addEventListener("input", () => { if (!list.hidden) renderList(input.value); });
    input.addEventListener("focus", () => {
      if (all.length && fetchedFor === getProviderId()) { renderList(input.value); show(); }
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });

    return { browse, list, status, hide };
  }

  // the chat/utility model field: a provider dropdown + the browsable box. The
  // provider is a prefix baked into the stored id, so we split it off / rejoin it.
  function modelField(f) {
    const id = "set-" + f.key;
    const { provider, model } = splitModel(f.value == null ? "" : String(f.value));
    const sel = el("select", { className: "set-input set-model-provider" });
    for (const p of PROVIDERS)
      sel.append(el("option", { value: p.id, textContent: p.label, selected: p.id === provider }));
    const input = el("input", {
      id, className: "set-input set-model-name", type: "text",
      value: model, placeholder: "model id (or click browse)", autocomplete: "off",
    });
    const b = attachBrowse(input, () => sel.value, { custom: "custom: type the full id" });
    sel.addEventListener("change", () => b.hide());   // stale list is for the old provider
    const combo = el("div", { className: "set-model" },
      el("div", { className: "set-model-row" }, sel, input, b.browse), b.list);
    document.addEventListener("mousedown", (e) => { if (!combo.contains(e.target)) b.hide(); });
    return { node: combo, read: () => joinModel(sel.value, input.value), status: b.status };
  }

  // the embedding model field: no provider dropdown of its own — the "provider" IS
  // the EMBED_BACKEND select rendered just above it (read live). The id is bare, so
  // read() returns it verbatim. sentence_tf has no server to list, so it hints HF.
  function embedModelField(f) {
    const id = "set-" + f.key;
    const backendId = "set-" + (f.backend_key || "EMBED_BACKEND");
    const input = el("input", {
      id, className: "set-input set-model-name", type: "text",
      value: f.value == null ? "" : String(f.value),
      placeholder: "embedding model", autocomplete: "off",
    });
    const getProvider = () => (document.getElementById(backendId)?.value || "");
    const b = attachBrowse(input, getProvider, {
      sentence_tf: "sentence_tf runs in-process — type a HuggingFace repo id (e.g. BAAI/bge-small-en-v1.5)",
    });
    const combo = el("div", { className: "set-model" },
      el("div", { className: "set-model-row" }, input, b.browse), b.list);
    document.addEventListener("mousedown", (e) => { if (!combo.contains(e.target)) b.hide(); });
    return { node: combo, read: () => input.value, status: b.status };
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
    if (f.type === "model" || f.type === "embed_model") {
      ctl = f.type === "embed_model" ? embedModelField(f) : modelField(f);
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
