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
 * The table is EVERY knob the running config declares (yurios/envfile.py), which
 * is around two hundred of them — so the header grows a filter box, built here
 * rather than added to each page's markup. It narrows by key and by help text,
 * and hides a group once nothing in it matches, which is what makes "where is
 * the setting for X" a question the panel can answer.
 *
 * The CHAT_MODEL / UTILITY_MODEL fields (type "model") get a richer control: a
 * provider dropdown (LM Studio · Ollama · OpenRouter · Custom) beside a model box
 * you can type into OR fill from a live "browse" of what that provider is serving
 * (GET /api/models?provider=…). The stored .env value is the LiteLLM id — the
 * provider prefix + the model — which we split apart on load and re-join on save.
 *
 * OWNER_TOKEN is the one field that is not really a text box. Its value has to
 * end up on a *different device* — the phone you want to read her from — so
 * beside it sit two buttons: "generate" mints one server-side and applies it to
 * the running boundary at once, and "pair a phone" opens a panel of QR codes,
 * one per address this machine might be reachable at. Scanning opens
 * /auth?token=… and the phone is in, with nothing typed. Both talk to
 * /api/pairing (SPEC §11.1); the panel says out loud when the bind is loopback
 * and no phone can reach her whatever it scans.
 *
 * Above all of that sits a second panel with a different owner: HER brain
 * (GET|PATCH /api/brain, world/host.py — SPEC §31.2, §31.4). Those fields belong to one
 * character's registry record rather than to the file, every one of them blank
 * by default meaning *inherit the .env below*, and — because the providers behind
 * her voice are two small objects, not a process — a save applies to the running
 * conversation at once. So one dialog carries two scopes with two honest
 * promises: hers now, the house's on restart. `rows` carries the scope, which is
 * what keeps the diff going to the right endpoint. The board opens the same
 * dialog with data-scope="house" on it, which drops the character panel: the
 * switchboard is not standing in anybody's room. */
(() => {
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
  const dlg = document.getElementById("settings");
  if (!dlg) return;
  const body = document.getElementById("settings-body");
  const note = document.getElementById("settings-note");
  const pathEl = document.getElementById("settings-path");
  let initial = {};        // scope:key → value as loaded (the diff baseline)
  let rows = [];           // {key, scope, read} — the source of truth for save()
  let loaded = false;
  let brain = null;        // the character's own brain panel, if this node has one
  let filterBox = null;    // the header's narrowing box, built on first load

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
      await runtimeReady;
      const p = getProviderId();
      if (hints && hints[p] != null) { status.textContent = hints[p]; hide(); return; }
      if (fetchedFor === p && all.length) {   // already have them — just reopen, full list
        renderList(""); show(); input.focus(); return;
      }
      status.textContent = "loading…";
      try {
        const r = await fetch(apiPath("/api/models?provider=" + encodeURIComponent(p)));
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
    const current = f.value == null ? "" : String(f.value);
    // An empty box is not "the bare provider prefix" — for a character override
    // it is the whole point: inherit whatever the .env says. So the provider
    // starts on the id being inherited, and read() answers "" until something
    // is actually typed.
    const { provider, model } = splitModel(current || (f.inherited || ""));
    const sel = el("select", { className: "set-input set-model-provider" });
    for (const p of PROVIDERS)
      sel.append(el("option", { value: p.id, textContent: p.label, selected: p.id === provider }));
    const input = el("input", {
      id, className: "set-input set-model-name", type: "text",
      value: current ? model : "", autocomplete: "off",
      placeholder: f.inherited ? `inherit — ${f.inherited}` : "model id (or click browse)",
    });
    const b = attachBrowse(input, () => sel.value, { custom: "custom: type the full id" });
    sel.addEventListener("change", () => b.hide());   // stale list is for the old provider
    const combo = el("div", { className: "set-model" },
      el("div", { className: "set-model-row" }, sel, input, b.browse), b.list);
    document.addEventListener("mousedown", (e) => { if (!combo.contains(e.target)) b.hide(); });
    return {
      node: combo, status: b.status,
      read: () => (input.value.trim() ? joinModel(sel.value, input.value) : ""),
    };
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

  // ---- OWNER_TOKEN: the field whose value has to reach another device ----
  //
  // Everything here is drawn in JS rather than added to each page's markup:
  // four pages carry the settings <dialog>, and a pairing panel that only one
  // of them had would be the kind of drift this file exists to avoid.
  function pairingPanel(input) {
    const panel = el("div", { className: "set-pair", hidden: true });
    const status = el("p", { className: "set-pair-note" });
    const frame = el("div", { className: "set-pair-frame" });
    const link = el("a", { className: "set-pair-link", target: "_blank",
      rel: "noreferrer" });
    // The token in the clear, HERE and not in the field above: that one stays
    // write-only ("blank keeps what is there"), and filling it would make
    // merely looking at this panel read as an unsaved change. This is the place
    // the secret is handed over, so this is the place it is legible — for the
    // device that cannot use a camera.
    const token = el("code", { className: "set-pair-token" });
    const others = el("div", { className: "set-pair-others" });
    panel.append(status, frame, link, token, others);

    function draw(data, minted) {
      status.classList.toggle("warn", !data.reachable);
      const lines = [];
      if (minted) lines.push("New token — every other session is signed out.");
      if (!data.configured) lines.push("No owner token yet. Generate one to pair.");
      if (!data.reachable)
        lines.push(`HOST=${data.host || "unset"} is loopback: nothing off this `
          + "machine can reach her. Set HOST to 0.0.0.0, save, and restart — "
          + "then this code works.");
      else if (!data.live)
        lines.push("Saved, but she is still running on the previous token — "
          + "restart before scanning.");
      const links = data.links || [];
      if (data.configured && !links.length)
        lines.push("No address on this machine looks reachable from a phone.");
      status.textContent = lines.join(" ") || "Scan this with the phone's camera "
        + "— it opens her already signed in.";
      frame.replaceChildren();
      link.textContent = "";
      link.removeAttribute("href");
      token.textContent = data.token || "";
      others.replaceChildren();
      if (data.configured) input.placeholder = "configured — leave blank to keep";
      if (!links.length) { panel.hidden = false; return; }
      const chosen = links[0];
      frame.innerHTML = chosen.qr;                 // server-built <svg>, no markup from us
      link.href = chosen.url;
      link.textContent = chosen.origin;
      if (links.length > 1) {
        others.append(el("span", { className: "set-pair-others-label",
          textContent: "other addresses:" }));
        for (const other of links.slice(1)) {
          const pick = el("button", { type: "button", className: "set-reveal",
            textContent: other.origin });
          pick.addEventListener("click", () => {
            links.splice(links.indexOf(other), 1);
            links.unshift(other);
            draw(data, false);
          });
          others.append(pick);
        }
      }
      panel.hidden = false;
    }

    async function show(minted, data) {
      await runtimeReady;
      status.textContent = "loading…";
      panel.hidden = false;
      try {
        const payload = data || await fetch(apiPath("/api/pairing"))
          .then((r) => r.json());
        draw(payload, minted);
      } catch (e) {
        status.textContent = "couldn't read the pairing state: " + e;
      }
    }

    return { node: panel, show,
      toggle: () => (panel.hidden ? show(false) : (panel.hidden = true)) };
  }

  function ownerTokenExtras(ctl, wrap) {
    const pair = pairingPanel(ctl.input);
    const generate = el("button", { type: "button", className: "set-reveal",
      textContent: "generate" });
    const open = el("button", { type: "button", className: "set-reveal",
      textContent: "pair a phone" });
    generate.addEventListener("click", async () => {
      await runtimeReady;
      generate.disabled = true;
      try {
        const r = await fetch(apiPath("/api/pairing/token"), { method: "POST" });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        // Written and applied by that one call, so there is nothing for save()
        // to send afterwards — which is why nothing here touches the field.
        await pair.show(true, data);
      } catch (e) {
        note.textContent = "could not generate a token: " + e;
      } finally {
        generate.disabled = false;
      }
    });
    open.addEventListener("click", () => pair.toggle());
    wrap.append(generate, open);
    return pair.node;
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
      value: f.type === "password" ? "" : (f.value == null ? "" : f.value),
    });
    if (f.type === "password") {
      input.autocomplete = "new-password";
      input.placeholder = f.configured
        ? "configured — leave blank to keep"
        : "not configured";
      let removeRequested = false;
      const remove = el("button", {
        type: "button", className: "set-reveal", textContent: "remove",
        disabled: !f.configured,
      });
      remove.addEventListener("click", () => {
        removeRequested = true;
        input.value = "";
        input.placeholder = "will be removed on save";
        remove.disabled = true;
      });
      input.addEventListener("input", () => {
        if (input.value) {
          removeRequested = false;
          remove.disabled = false;
          input.placeholder = "replacement owner secret";
        }
      });
      return { node: input, read: () => removeRequested ? null : input.value,
        input, remove };
    }
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

  // ---- her own brain: the same controls, tri-stated on "inherit" ----
  // Every control here has a third state the .env panel has no use for: empty,
  // meaning "whatever the house runs". A checkbox cannot say that, so booleans
  // become a three-option select; the numbers and the endpoint say it with a
  // placeholder naming the value they would inherit.
  function brainControl(f) {
    const id = "brain-" + f.key;
    const shown = f.value == null ? "" : String(f.value);
    if (f.type === "model") return modelField({ ...f, key: "brain-" + f.key });
    if (f.type === "bool") {
      const s = el("select", { id, className: "set-input" });
      const inherited = f.inherited ? "on" : "off";
      s.append(el("option", { value: "", textContent: `inherit (${inherited})`,
        selected: shown === "" }));
      s.append(el("option", { value: "true", textContent: "on", selected: shown === "true" }));
      s.append(el("option", { value: "false", textContent: "off", selected: shown === "false" }));
      return { node: s, read: () => s.value };
    }
    const input = el("input", {
      id, className: "set-input", type: f.type === "number" ? "number" : "text",
      value: shown, autocomplete: "off",
      placeholder: f.inherited === "" || f.inherited == null
        ? "inherit" : `inherit — ${f.inherited}`,
    });
    if (f.step) input.step = f.step;
    return { node: input, read: () => input.value.trim() };
  }

  function brainRow(f) {
    const label = el("label", { className: "set-row" });
    const ctl = brainControl(f);
    const wrap = el("div", { className: "set-ctl" }, ctl.node);
    if (f.type !== "model") label.htmlFor = "brain-" + f.key;
    rows.push({ key: f.key, scope: "brain", read: ctl.read });
    initial["brain:" + f.key] = ctl.read();
    label.dataset.match = (f.key + " " + (f.help || "")).toLowerCase();
    label.append(el("div", { className: "set-key", textContent: f.key.replace(/_/g, " ") }), wrap);
    if (f.help) label.append(el("div", { className: "set-help", textContent: f.help }));
    if (ctl.status) label.append(ctl.status);
    return label;
  }

  function brainSection(data) {
    const sec = el("section", { className: "set-group set-group-live" });
    sec.append(el("h3", { className: "set-group-title",
      textContent: `${data.name || "this character"} · her own brain` }));
    const running = data.running
      ? "Saved here, applied to this conversation at once — no restart."
      : "She is not running; these apply the moment she starts.";
    sec.append(el("p", { className: "set-scope-note",
      textContent: `Hers alone. Leave a field empty to inherit the .env below. ${running}` }));
    for (const f of data.fields) sec.append(brainRow(f));
    const key = data.effective?.api_key_env;
    if (key && !data.key_configured) {
      sec.append(el("p", { className: "set-scope-warn",
        textContent: `${key} is not set — a hosted model will refuse her calls until it is.` }));
    }
    return sec;
  }

  function fieldRow(f) {
    const label = el("label", { className: "set-row" });
    const head = el("div", { className: "set-key" }, f.key.toLowerCase());
    const wrap = el("div", { className: "set-ctl" });

    let ctl;
    let pairPanel = null;
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
        wrap.append(ctl.remove);
        if (f.key === "OWNER_TOKEN") pairPanel = ownerTokenExtras(ctl, wrap);
      }
    }

    rows.push({ key: f.key, scope: "env", read: ctl.read });
    initial["env:" + f.key] = ctl.read();
    label.dataset.match = (f.key + " " + (f.help || "")).toLowerCase();
    if (f.relevant_if) label.dataset.relevantIf = JSON.stringify(f.relevant_if);
    label.append(head, wrap);
    if (f.help) label.append(el("div", { className: "set-help", textContent: f.help }));
    if (ctl.status) label.append(ctl.status);
    if (pairPanel) label.append(pairPanel);      // under the field it belongs to
    return label;
  }

  // The character panel is optional: a build with no registry behind it (the
  // Build #2 desktop app) 404s here, and a node with nothing running answers
  // 503 — both mean "no character to configure", not an error to show.
  async function loadBrain() {
    // The board opens this dialog for the house, not for a character: there is
    // no "her" to show a brain panel for, and the primary character's would be
    // the wrong one to show anyway.
    if (dlg.dataset.scope === "house") return null;
    try {
      const r = await fetch(apiPath("/api/brain"));
      if (!r.ok) return null;
      const data = await r.json();
      return Array.isArray(data?.fields) ? data : null;
    } catch (e) {
      return null;
    }
  }

  // ---- the filter -----------------------------------------------------------
  // Matching is on the key and on the one-line help, because half of what you
  // are looking for you know by description rather than by name: "telegram"
  // finds the credentials, "context" finds CONTEXT_LENGTH and the budgets.
  function applyFilter() {
    const q = (filterBox?.value || "").trim().toLowerCase();
    for (const section of body.querySelectorAll(".set-group")) {
      let shown = 0;
      for (const row of section.querySelectorAll(".set-row")) {
        const active = row.dataset.relevant !== "false";
        const hit = active && (!q || (row.dataset.match || "").includes(q));
        row.hidden = !hit;
        shown += hit ? 1 : 0;
      }
      section.hidden = !shown;
    }
    // …only when there is something to have failed to match: with no groups at
    // all the panel is reporting an error, and "no setting matches that" on top
    // of it would be answering a question nobody asked.
    const groups = [...body.querySelectorAll(".set-group")];
    const empty = q && groups.length && !groups.some((section) => !section.hidden);
    body.querySelector(".set-nomatch")?.toggleAttribute("hidden", !empty);
    const advanced = body.querySelector(".set-advanced");
    if (advanced) {
      advanced.hidden = ![...advanced.querySelectorAll(".set-group")]
        .some((section) => !section.hidden);
      if (q && !advanced.hidden) advanced.open = true;
    }
  }

  function applyRelevance() {
    for (const row of body.querySelectorAll(".set-row[data-relevant-if]")) {
      let rules = {};
      try { rules = JSON.parse(row.dataset.relevantIf || "{}"); } catch (_) {}
      row.dataset.relevant = String(Object.entries(rules).every(([key, allowed]) => {
        const input = document.getElementById("set-" + key);
        const value = input?.type === "checkbox" ? String(input.checked) : String(input?.value ?? "");
        return (allowed || []).map(String).includes(value);
      }));
    }
    applyFilter();
  }

  function ensureFilter() {
    if (filterBox) return;
    filterBox = el("input", { type: "search", className: "settings-filter",
      placeholder: "filter settings", autocomplete: "off",
      "aria-label": "filter settings" });
    filterBox.addEventListener("input", applyFilter);
    pathEl.parentElement.insertBefore(filterBox, pathEl.nextSibling);
  }

  async function load() {
    await runtimeReady;
    // The two panels are fetched together and fail apart: the .env editor lives
    // in her runtime and answers nothing while she is parked, which must not
    // take the character panel — the very screen you would fix that from — down
    // with it.
    const [settings, brainData] = await Promise.all([
      fetch(apiPath("/api/settings")).then((r) => r.json()).catch((e) => ({ error: e })),
      loadBrain(),
    ]);
    pathEl.textContent = settings.env_path || "";
    body.textContent = "";
    initial = {}; rows = [];
    brain = brainData;
    if (brain) {
      body.append(brainSection(brain));
      body.append(el("p", { className: "set-scope-note set-scope-house",
        textContent: "Everything below is stored in the house's .env and applies "
          + "after restart. Character channel credentials use her own named key." }));
    }
    if (Array.isArray(settings.groups)) {
      const ordinary = settings.groups.filter((g) => !g.advanced);
      const advancedGroups = settings.groups.filter((g) => g.advanced);
      const appendGroup = (parent, g) => {
        const sec = el("section", { className: "set-group" });
        sec.append(el("h3", { className: "set-group-title", textContent: g.group }));
        for (const f of g.fields) sec.append(fieldRow(f));
        parent.append(sec);
      };
      for (const g of ordinary) appendGroup(body, g);
      if (advancedGroups.length) {
        const count = advancedGroups.reduce((n, g) => n + g.fields.length, 0);
        const advanced = el("details", { className: "set-advanced" });
        advanced.append(el("summary", { className: "set-advanced-title",
          textContent: `Advanced settings · ${count}` }));
        for (const g of advancedGroups) appendGroup(advanced, g);
        body.append(advanced);
      }
    } else {
      body.append(el("p", { className: "settings-loading",
        textContent: "couldn't load the .env panel: "
          + (settings.error || settings.detail || "unavailable") }));
    }
    body.append(el("p", { className: "settings-loading set-nomatch", hidden: true,
      textContent: "no setting matches that" }));
    ensureFilter();
    applyRelevance();       // also reapplies a filter left in the box
    loaded = true;
  }

  // Her fields go to her registry record and take effect on the running
  // conversation; the .env fields go to the file and wait for a restart. Both
  // are sent, and the note says which of the two things happened.
  async function saveBrain(diff) {
    const r = await fetch(apiPath("/api/brain"), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(diff),
    });
    const res = await r.json();
    if (!r.ok) throw new Error(res.detail || `HTTP ${r.status}`);
    for (const k of Object.keys(diff)) initial["brain:" + k] = diff[k];
    const applied = (res.applied || []).length;
    if (!applied) return `saved ${brainName()}'s settings`;
    return res.running
      ? `${brainName()} is now speaking through ${res.effective.chat_model}`
      : `saved — ${brainName()} starts on ${res.effective.chat_model}`;
  }

  const brainName = () => (brain && brain.name) || "she";

  async function saveEnv(diff) {
    const r = await fetch(apiPath("/api/settings"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(diff),
    });
    const res = await r.json();
    if (!r.ok) throw new Error(res.detail || `HTTP ${r.status}`);
    const n = (res.written || []).length;
    for (const k of res.written || []) initial["env:" + k] = diff[k];
    return { restart: !!res.restart_required,
             text: res.restart_required
               ? `${n} .env setting${n === 1 ? "" : "s"} saved — restart to apply`
               : "saved" };
  }

  async function save() {
    await runtimeReady;
    const diffs = { brain: {}, env: {} };
    for (const row of rows) {
      const now = row.read();
      if (String(now) !== String(initial[`${row.scope}:${row.key}`]))
        diffs[row.scope][row.key] = now;
    }
    const changedBrain = Object.keys(diffs.brain).length;
    const changedEnv = Object.keys(diffs.env).length;
    if (!changedBrain && !changedEnv) { note.textContent = "no changes"; return; }
    note.textContent = "saving…";
    note.classList.remove("restart");
    const said = [];
    let restart = false;
    try {
      if (changedBrain) said.push(await saveBrain(diffs.brain));
      if (changedEnv) {
        const res = await saveEnv(diffs.env);
        restart = res.restart;
        said.push(res.text);
      }
      note.textContent = said.join(" · ");
      note.classList.toggle("restart", restart);
    } catch (e) {
      note.textContent = "save failed: " + e;
    }
  }

  document.getElementById("settings-open").addEventListener("click", async () => {
    // Reloaded on every open once a character panel is in play: what it reports
    // — which model she is actually on, whether she is running — is live state,
    // and a stale panel about a running companion is worse than a lost draft.
    if (!loaded || brain) await load();
    note.textContent = "";
    note.classList.remove("restart");
    dlg.showModal();
  });
  document.getElementById("settings-close").addEventListener("click", () => dlg.close());
  document.getElementById("settings-save").addEventListener("click", save);
  body.addEventListener("change", applyRelevance);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // click backdrop
})();
