/* The settings panel (SPEC §11, → desktop/routes/settings.py).
 *
 * The gear in the masthead opens a <dialog>. We fetch the schema + current
 * values from /api/settings, render one control per field (a <select> when the
 * value is an enum, otherwise text/number/password with an optional datalist),
 * and POST back only the fields the user actually changed — so the hand-written
 * comments in .env survive. Everything here is read at server boot, so a save
 * asks for a restart rather than pretending to hot-apply. */
(() => {
  const dlg = document.getElementById("settings");
  if (!dlg) return;
  const body = document.getElementById("settings-body");
  const note = document.getElementById("settings-note");
  const pathEl = document.getElementById("settings-path");
  let initial = {};        // key → value as loaded (to compute the diff on save)
  let loaded = false;

  const el = (tag, props = {}, ...kids) => {
    const n = Object.assign(document.createElement(tag), props);
    for (const k of kids) n.append(k);
    return n;
  };

  function control(f) {
    const id = "set-" + f.key;
    if (f.type === "select") {
      const s = el("select", { id, className: "set-input" });
      for (const opt of f.options || [])
        s.append(el("option", { value: opt, textContent: opt, selected: String(f.value) === opt }));
      // keep an unknown current value selectable rather than silently losing it
      if (f.value && !(f.options || []).includes(String(f.value)))
        s.append(el("option", { value: f.value, textContent: f.value + " (current)", selected: true }));
      return s;
    }
    if (f.type === "bool") {
      const c = el("input", { id, type: "checkbox", className: "set-check", checked: !!f.value });
      return c;
    }
    const input = el("input", {
      id, className: "set-input",
      type: f.type === "password" ? "password" : (f.type === "number" ? "number" : "text"),
      value: f.value == null ? "" : f.value,
    });
    if (f.step) input.step = f.step;
    if (f.min != null) input.min = f.min;
    if (f.max != null) input.max = f.max;
    if (f.suggest && f.suggest.length) {
      const listId = id + "-list";
      input.setAttribute("list", listId);
      const dl = el("datalist", { id: listId });
      for (const s of f.suggest) dl.append(el("option", { value: s }));
      input._datalist = dl;   // fieldRow() appends it next to the input
    }
    return input;
  }

  function fieldRow(f) {
    const input = control(f);
    initial[f.key] = readValue(f, input);
    const label = el("label", { className: "set-row", htmlFor: "set-" + f.key });
    const head = el("div", { className: "set-key" }, f.key.toLowerCase());
    const wrap = el("div", { className: "set-ctl" }, input);
    if (input._datalist) wrap.append(input._datalist);
    label.append(head, wrap);
    if (f.help) label.append(el("div", { className: "set-help", textContent: f.help }));
    if (f.type === "password") {
      const reveal = el("button", { type: "button", className: "set-reveal", textContent: "show" });
      reveal.addEventListener("click", () => {
        const hidden = input.type === "password";
        input.type = hidden ? "text" : "password";
        reveal.textContent = hidden ? "hide" : "show";
      });
      wrap.append(reveal);
    }
    return label;
  }

  function readValue(f, input) {
    if (f.type === "bool") return input.checked;
    if (f.type === "number") return input.value === "" ? "" : Number(input.value);
    return input.value;
  }

  async function load() {
    try {
      const r = await fetch("/api/settings");
      const data = await r.json();
      pathEl.textContent = data.env_path || "";
      body.textContent = "";
      initial = {};
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
    // collect only the fields whose value changed from load
    const diff = {};
    for (const g of document.querySelectorAll(".set-group")) {
      for (const label of g.querySelectorAll(".set-row")) {
        const key = label.htmlFor.replace(/^set-/, "");
        const input = document.getElementById(label.htmlFor);
        const f = { key, type: input.type === "checkbox" ? "bool" : (input.type === "number" ? "number" : "text") };
        const now = readValue(f, input);
        if (String(now) !== String(initial[key])) diff[key] = now;
      }
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
      // the saved values are now the baseline
      for (const k of res.written || []) initial[k] = diff[k];
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
