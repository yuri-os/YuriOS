import { ApiError, studioApi } from "./api.js";
import { BUDGETS, SECTIONS, description, equal, normalise, problems, tokens } from "./draft.js";
import { createOptimizer } from "./optimize.js";

const $ = (selector, root = document) => root.querySelector(selector);

const params = new URLSearchParams(location.search);
const state = {
  id: params.get("character"),
  draft: null,
  saved: null,
  provenance: {},
  grown: [],
  images: { portrait: false, selfies: [] },
  options: { spec: "v3", include_soul: true, image: "portrait", fit: "contain",
             attribution: true, timestamps: true, acknowledged: false },
  preview: null,
  unlocked: new Set(),
  saving: false,
  previewing: false,
};

const elements = {
  form: $("#studio-form"),
  nav: $("#section-nav"),
  notice: $("#notice"),
  saveState: $("#save-state"),
  savePulse: $("#save-pulse"),
  primary: $("#primary-action"),
  optimize: $("#optimize-action"),
  brandSub: $("#brand-sub"),
  title: $("#page-title"),
  eyebrow: $("#page-eyebrow"),
  intro: $("#page-intro"),
  grownStrip: $("#grown-strip"),
  grownList: $("#grown-list"),
  grownSummary: $("#grown-summary"),
  previewImage: $("#preview-image"),
  previewName: $("#preview-name"),
  previewMeta: $("#preview-meta"),
  tokenReport: $("#token-report"),
  privacyLede: $("#privacy-lede"),
  privacyStays: $("#privacy-stays"),
  warnings: $("#warning-list"),
  reviewDialog: $("#review-dialog"),
  reviewCopy: $("#review-copy"),
  reviewList: $("#review-list"),
  toasts: $("#toast-region"),
};

// ---------------------------------------------------------------- utilities

function element(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text != null) node.textContent = options.text;
  if (options.html != null) node.innerHTML = options.html;
  if (options.attrs) for (const [key, value] of Object.entries(options.attrs)) {
    if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function toast(message, kind = "success") {
  const node = element("div", { className: `toast ${kind}`, text: message });
  elements.toasts.append(node);
  setTimeout(() => node.remove(), 4600);
}

function notice(message, kind = "error") {
  elements.notice.hidden = !message;
  elements.notice.className = `notice ${kind}`;
  elements.notice.textContent = message || "";
}

function setSaveState(label, live = false) {
  elements.saveState.textContent = label;
  elements.savePulse.classList.toggle("live", live);
}

function debounce(fn, delay) {
  let handle = 0;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), delay);
  };
}

// ------------------------------------------------------------------- render

function fieldControl(field) {
  const value = state.draft[field.key];
  const locked = field.constitution && !state.unlocked.has(field.key);
  const wrap = element("label", {
    className: `field field-${field.type}${field.constitution ? " field-constitution" : ""}`,
    attrs: { "data-field": field.key },
  });

  const head = element("span", { className: "field-head" },
    element("span", { className: "field-label", text: field.label }));
  const mark = provenanceMark(field.key);
  if (mark) head.append(mark);
  if (field.constitution) {
    const lock = element("button", {
      className: `lock${locked ? "" : " unlocked"}`,
      attrs: { type: "button", title: locked ? "Unlock to edit her constitution" : "Locked again on reload" },
    }, icon(locked ? "lock" : "eye"), document.createTextNode(locked ? "Constitution" : "Editing"));
    lock.addEventListener("click", () => {
      if (locked) state.unlocked.add(field.key); else state.unlocked.delete(field.key);
      renderForm();
    });
    head.append(lock);
  }
  wrap.append(head);
  if (field.hint) wrap.append(element("small", { className: "field-hint", text: field.hint }));

  let control;
  if (field.type === "textarea") {
    control = element("textarea", { attrs: { rows: field.rows || 4, disabled: locked } });
    control.value = value;
    control.addEventListener("input", () => update(field.key, control.value));
  } else if (field.type === "chips") {
    control = element("input", { attrs: { type: "text", disabled: locked,
      placeholder: "comma separated" } });
    control.value = value.join(", ");
    control.addEventListener("input", () =>
      update(field.key, control.value.split(",").map((v) => v.trim()).filter(Boolean)));
  } else {
    control = element("input", { attrs: { type: "text", disabled: locked,
      required: field.required || false } });
    control.value = value;
    control.addEventListener("input", () => update(field.key, control.value));
  }
  wrap.append(control);
  return wrap;
}

function provenanceMark(key) {
  const item = state.provenance[key];
  if (!item || item.origin === "seed" || item.origin === "unknown") return null;
  const when = item.last ? new Date(item.last).toLocaleDateString(undefined,
    { month: "short", day: "numeric" }) : "";
  const mine = item.origin === "her";
  return element("span", {
    className: `prov${mine ? " prov-her" : ""}`,
    attrs: { title: item.subject || "" },
    text: mine ? `she wrote this · ${when}` : `edited · ${when}`,
  });
}

function listControl(field) {
  const values = state.draft[field.key];
  const wrap = element("div", { className: "field field-list", attrs: { "data-field": field.key } });
  wrap.append(element("span", { className: "field-head" },
    element("span", { className: "field-label", text: field.label })));
  if (field.hint) wrap.append(element("small", { className: "field-hint", text: field.hint }));

  values.forEach((value, index) => {
    const row = element("div", { className: "list-row" });
    const area = element("textarea", { attrs: { rows: field.rows || 3 } });
    area.value = value;
    area.addEventListener("input", () => {
      const next = [...state.draft[field.key]];
      next[index] = area.value;
      update(field.key, next);
    });
    const remove = element("button", { className: "icon-button danger",
      attrs: { type: "button", "aria-label": `Remove ${field.label} ${index + 1}` } }, icon("trash"));
    remove.addEventListener("click", () =>
      update(field.key, state.draft[field.key].filter((_v, i) => i !== index),
             { rerender: true }));
    row.append(element("span", { className: "list-index", text: String(index + 1) }), area, remove);
    wrap.append(row);
  });

  const add = element("button", { className: "button button-quiet add",
    attrs: { type: "button" } }, icon("plus"), document.createTextNode(`Add ${field.label.toLowerCase()}`));
  add.addEventListener("click", () =>
    update(field.key, [...state.draft[field.key], ""], { rerender: true }));
  wrap.append(add);
  return wrap;
}

function lorebookControl() {
  const book = state.draft.lorebook;
  const wrap = element("div", { className: "field field-lorebook" });
  const meta = element("div", { className: "lore-meta" });
  for (const [key, label] of [["scan_depth", "Scan depth"], ["token_budget", "Token budget"]]) {
    const input = element("input", { attrs: { type: "number", min: "0" } });
    input.value = book[key];
    input.addEventListener("input", () =>
      update("lorebook", { ...state.draft.lorebook, [key]: Number(input.value) || 0 }, { rerender: false }));
    meta.append(element("label", { className: "mini" },
      element("span", { text: label }), input));
  }
  wrap.append(meta);

  book.entries.forEach((entry, index) => {
    const card = element("div", { className: "lore-entry" });
    const keys = element("input", { attrs: { type: "text", placeholder: "keys, comma separated" } });
    keys.value = entry.keys.join(", ");
    keys.addEventListener("input", () => patchEntry(index, {
      keys: keys.value.split(",").map((k) => k.trim()).filter(Boolean) }));
    const content = element("textarea", { attrs: { rows: 3, placeholder: "what fires when a key matches" } });
    content.value = entry.content;
    content.addEventListener("input", () => patchEntry(index, { content: content.value }));
    const remove = element("button", { className: "icon-button danger",
      attrs: { type: "button", "aria-label": `Remove entry ${index + 1}` } }, icon("trash"));
    remove.addEventListener("click", () => update("lorebook", {
      ...book, entries: book.entries.filter((_e, i) => i !== index) },
      { rerender: true }));
    card.append(element("div", { className: "lore-head" }, keys, remove), content);
    wrap.append(card);
  });

  const add = element("button", { className: "button button-quiet add", attrs: { type: "button" } },
    icon("plus"), document.createTextNode("Add lore entry"));
  add.addEventListener("click", () => update("lorebook", {
    ...book, entries: [...book.entries, { name: "", keys: [], content: "",
      constant: false, use_regex: false, case_sensitive: false }] },
    { rerender: true }));
  wrap.append(add);
  return wrap;
}

function patchEntry(index, patch) {
  const entries = state.draft.lorebook.entries.map((entry, i) =>
    i === index ? { ...entry, ...patch } : entry);
  update("lorebook", { ...state.draft.lorebook, entries });
}

function imageControl() {
  const wrap = element("div", { className: "field field-image" });
  if (!state.id) {
    wrap.append(element("p", { className: "form-note",
      text: "Create her first, then pick a face — the picker needs somewhere to put it." }));
    return wrap;
  }
  const strip = element("div", { className: "selfie-strip" });
  const choose = (value, label, url) => {
    const button = element("button", {
      className: `selfie${state.options.image === value ? " chosen" : ""}`,
      attrs: { type: "button", title: label },
    }, url ? element("img", { attrs: { src: url, alt: label, loading: "lazy" } })
           : element("span", { className: "selfie-empty", text: "no face yet" }));
    button.addEventListener("click", () => {
      state.options.image = value;
      renderForm();
      renderPortrait();
      schedulePreview();
    });
    return button;
  };
  strip.append(choose("portrait", "Her portrait",
    state.images.portrait ? `/api/characters/${state.id}/portrait?v=${Date.now()}` : null));
  for (const selfie of state.images.selfies) {
    strip.append(choose(`selfie:${selfie.name}`, selfie.name, selfie.url));
  }
  wrap.append(strip);

  const drop = element("label", { className: "drop-zone" },
    element("span", { className: "drop-symbol" }, icon("upload")),
    element("strong", { text: "Upload a face" }),
    element("span", { text: "PNG or JPEG — it becomes her portrait" }));
  const file = element("input", { attrs: { type: "file", accept: "image/png,image/jpeg" } });
  file.addEventListener("change", () => file.files?.[0] && uploadPortrait(file.files[0]));
  drop.append(file);
  wrap.append(drop);

  const fits = element("div", { className: "seg" });
  for (const [value, label] of [["contain", "Fit"], ["cover", "Crop"], ["none", "As-is"]]) {
    const button = element("button", {
      className: state.options.fit === value ? "on" : "",
      attrs: { type: "button" }, text: label,
    });
    button.addEventListener("click", () => {
      state.options.fit = value; renderForm(); renderPortrait(); schedulePreview();
    });
    fits.append(button);
  }
  wrap.append(element("div", { className: "seg-wrap" },
    element("span", { className: "field-label", text: "Framing" }), fits));
  return wrap;
}

function exportControl() {
  const wrap = element("div", { className: "field field-export" });
  const toggle = (key, label, hint) => {
    const input = element("input", { attrs: { type: "checkbox" } });
    input.checked = state.options[key];
    input.addEventListener("change", () => {
      state.options[key] = input.checked;
      schedulePreview();
    });
    return element("label", { className: "check" }, input,
      element("span", { className: "switch", attrs: { "aria-hidden": "true" } }),
      element("span", { className: "check-copy" },
        element("strong", { text: label }), element("small", { text: hint })));
  };

  const spec = element("div", { className: "seg" });
  for (const [value, label] of [["v3", "V3 + V2"], ["v2", "V2 only"]]) {
    const button = element("button", {
      className: state.options.spec === value ? "on" : "",
      attrs: { type: "button" }, text: label,
    });
    button.addEventListener("click", () => { state.options.spec = value; renderForm(); schedulePreview(); });
    spec.append(button);
  }
  wrap.append(element("div", { className: "seg-wrap" },
    element("span", { className: "field-label", text: "Card format" }), spec));

  wrap.append(
    toggle("include_soul", "Carry her soul files",
      "Re-imports into YuriOS exactly as authored, instead of flattened prose"),
    toggle("attribution", "Credit YuriOS in the notes",
      "Appended to creator notes and tags — never to her persona"),
    toggle("timestamps", "Include dates",
      "A modification date says when you were last active"),
  );
  return wrap;
}

function renderForm() {
  const fragment = document.createDocumentFragment();
  for (const section of SECTIONS) {
    const block = element("section", { className: "form-section", attrs: { id: `sec-${section.id}` } });
    block.append(element("header", { className: "section-head" },
      element("p", { className: "eyebrow", text: section.eyebrow }),
      element("h2", { text: section.label }),
      section.blurb ? element("p", { className: "section-blurb", text: section.blurb }) : null));
    const grid = element("div", { className: "field-grid" });
    for (const field of section.fields) {
      if (field.type === "list") grid.append(listControl(field));
      else if (field.type === "lorebook") grid.append(lorebookControl());
      else if (field.type === "image") grid.append(imageControl());
      else if (field.type === "export") grid.append(exportControl());
      else grid.append(fieldControl(field));
    }
    block.append(grid);
    fragment.append(block);
  }
  elements.form.replaceChildren(fragment);
  renderNav();
}

function renderNav() {
  elements.nav.replaceChildren(...SECTIONS.map((section) => {
    const link = element("a", { attrs: { href: `#sec-${section.id}` }, text: section.label });
    return element("li", {}, link);
  }));
  spy();
}

/* The rail follows the form. Rebuilt with the nav because renderForm() replaces
   every section node, so a long-lived observer would be watching orphans. */
let observer = null;
function spy() {
  observer?.disconnect();
  const links = new Map([...elements.nav.querySelectorAll("a")]
    .map((link) => [link.getAttribute("href").slice(1), link]));
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      for (const link of links.values()) link.classList.remove("current");
      links.get(entry.target.id)?.classList.add("current");
    }
  }, { rootMargin: "-20% 0px -70% 0px", threshold: 0 });
  for (const id of links.keys()) {
    const node = document.getElementById(id);
    if (node) observer.observe(node);
  }
}

function renderPreviewPanels() {
  const draft = state.draft;
  elements.previewName.textContent = draft.name || "Unnamed";
  const parts = [draft.character_version, state.options.spec.toUpperCase()];
  if (state.preview?.bytes) parts.push(`${Math.round(state.preview.bytes / 1024)} KB`);
  elements.previewMeta.textContent = parts.filter(Boolean).join(" · ");

  const rows = state.preview?.report || localReport(draft);
  elements.tokenReport.replaceChildren(...rows.flatMap((row) => [
    element("dt", { className: row.over ? "over" : "", text: row.field }),
    element("dd", { className: row.over ? "over" : "" },
      element("b", { text: String(row.tokens) }),
      element("small", { text: row.budget })),
  ]));

  const stays = state.preview?.privacy?.stays || [];
  const memory = stays.find((s) => s.surface === "vault/memory");
  const corpus = stays.find((s) => s.surface === "corpus");
  const counts = [];
  if (memory?.entries) counts.push(`${memory.entries} memories`);
  if (corpus?.turns) counts.push(`${corpus.turns} conversation turns`);
  elements.privacyLede.textContent = counts.length
    ? `${counts.join(", ")} and everything she knows about you stay on this machine.`
    : "Her memory of you, her journal and her goals stay on this machine.";
  elements.privacyStays.replaceChildren(...stays.slice(0, 8).map((item) =>
    element("li", {}, element("code", { text: item.surface }),
      element("span", { text: item.reason }))));

  const warnings = state.preview?.warnings || [];
  const local = problems(draft).map((p) => ({ code: "validation", message: p.message, field: p.field }));
  const all = [...local, ...warnings];
  elements.warnings.replaceChildren(...(all.length ? all.map((warning) =>
    element("li", { className: `warn warn-${warning.code}` },
      element("strong", { text: warning.field || warning.code }),
      element("span", { text: warning.message }))
  ) : [element("li", { className: "warn warn-ok", text: "Nothing flagged. She's ready to travel." })]));

  if (state.preview?.card) {
    const data = state.preview.card.data || {};
    elements.previewName.textContent = data.name || draft.name;
  }
}

function localReport(draft) {
  const values = { description: description(draft), personality: draft.personality,
    scenario: draft.scenario, first_mes: draft.first_mes,
    mes_example: draft.examples.join("\n"), system_prompt: draft.system_prompt,
    post_history_instructions: draft.post_history_instructions };
  return Object.entries(values).map(([field, text]) => {
    const count = tokens(text);
    const budget = BUDGETS[field];
    return { field, tokens: count,
      budget: budget ? `${budget[0]}–${budget[1]}` : field === "mes_example" ? "spend freely" : "minimal",
      over: Boolean(budget && count > budget[1]) };
  });
}

function renderGrown() {
  const grown = state.grown.filter((key) => key in state.provenance);
  elements.grownStrip.hidden = grown.length === 0;
  if (!grown.length) return;
  elements.grownSummary.textContent =
    `She has rewritten ${grown.length} part${grown.length === 1 ? "" : "s"} of herself since she arrived, and you approved ${grown.length === 1 ? "it" : "them"}.`;
  elements.grownList.replaceChildren(...grown.map((key) => {
    const item = state.provenance[key];
    const when = item.last ? new Date(item.last).toLocaleDateString(undefined,
      { month: "long", day: "numeric" }) : "";
    const link = element("a", { attrs: { href: `#sec-${sectionFor(key)}` }, text: key });
    return element("li", {}, link, element("small", { text: when }),
      item.subject ? element("em", { text: item.subject }) : null);
  }));
}

function sectionFor(key) {
  return SECTIONS.find((section) => section.fields.some((f) => f.key === key))?.id || "identity";
}

const BLANK_FACE = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 768'%3E%3Crect width='512' height='768' fill='%23111816'/%3E%3C/svg%3E";

/* The preview shows the face that will actually be embedded, not whichever one
   happens to be her portrait — picking a selfie has to change the card you are
   looking at, or the picker is lying about what it does. */
function renderPortrait() {
  const chosen = state.options.image;
  let src = BLANK_FACE;
  if (state.id && chosen.startsWith("selfie:")) {
    const name = chosen.slice("selfie:".length);
    src = state.images.selfies.find((s) => s.name === name)?.url || BLANK_FACE;
  } else if (state.id && state.images.portrait) {
    src = `/api/characters/${state.id}/portrait?v=${state.portraitVersion || 0}`;
  }
  elements.previewImage.src = src;
  elements.previewImage.alt = state.draft?.name ? `${state.draft.name}'s card image` : "";
  elements.previewImage.classList.toggle("blank", src === BLANK_FACE);
}

// -------------------------------------------------------------------- state

/* `rerender` defaults to *false*, and that is load-bearing rather than an
   optimisation: renderForm() replaces every node in the form, so re-rendering
   from an `input` handler tears out the element the user is typing into and the
   keystroke after the first one lands nowhere. Only structural edits — adding or
   removing a greeting, an example, a lore entry — ask for a rebuild. */
function update(key, value, { rerender = false } = {}) {
  state.draft[key] = value;
  if (rerender) renderForm();
  renderPreviewPanels();
  markDirty();
  scheduleSave();
  schedulePreview();
}

function markDirty() {
  if (!equal(state.draft, state.saved)) setSaveState("unsaved", false);
}

const scheduleSave = debounce(async () => {
  if (!state.id || state.saving || equal(state.draft, state.saved)) return;
  if (problems(state.draft).some((p) => p.field === "name")) {
    setSaveState("needs a name");
    return;
  }
  state.saving = true;
  setSaveState("saving", true);
  try {
    await studioApi.save(state.id, state.draft);
    state.saved = structuredClone(state.draft);
    setSaveState("saved");
    notice("");
  } catch (error) {
    setSaveState("not saved");
    notice(error.message || "Could not save.");
  } finally {
    state.saving = false;
  }
}, 1200);

const schedulePreview = debounce(async () => {
  if (!state.id || state.previewing) return;
  state.previewing = true;
  try {
    state.preview = await studioApi.preview(state.id, { ...state.options, acknowledged: true });
    notice("");
  } catch (error) {
    state.preview = null;
    if (error instanceof ApiError && error.code === "leak") {
      notice(error.message, "error");
    } else if (error.status !== 404) {
      notice(error.message || "Preview failed.");
    }
  } finally {
    state.previewing = false;
    renderPreviewPanels();
  }
}, 500);

// ------------------------------------------------------------------ actions

async function uploadPortrait(file) {
  const reader = new FileReader();
  reader.addEventListener("load", async () => {
    try {
      await studioApi.setPortrait(state.id, { image: String(reader.result) });
      state.images.portrait = true;
      state.portraitVersion = Date.now();
      state.options.image = "portrait";
      renderForm();
      renderPortrait();
      schedulePreview();
      toast("That's her face now.");
    } catch (error) {
      toast(error.message || "Could not use that image.", "error");
    }
  });
  reader.readAsDataURL(file);
}

async function doExport() {
  const blocking = problems(state.draft);
  if (blocking.length) {
    notice(blocking[0].message);
    document.querySelector(`[data-field="${blocking[0].field}"] textarea, [data-field="${blocking[0].field}"] input`)?.focus();
    return;
  }
  if (state.id && !equal(state.draft, state.saved)) {
    // Never ship a file that disagrees with the Vault it claims to come from.
    await studioApi.save(state.id, state.draft);
    state.saved = structuredClone(state.draft);
    setSaveState("saved");
  }
  elements.primary.disabled = true;
  try {
    const result = await studioApi.exportCard(state.id, state.options);
    download(result.blob, result.filename);
    toast(`${result.filename} — she's yours to share.`);
  } catch (error) {
    if (error instanceof ApiError && error.code === "review_required") {
      openReview(error);
    } else if (error instanceof ApiError && error.code === "leak") {
      notice(error.message, "error");
      toast("Export refused — the privacy scrub found something.", "error");
    } else {
      toast(error.message || "Export failed.", "error");
    }
  } finally {
    elements.primary.disabled = false;
  }
}

function openReview(error) {
  elements.reviewCopy.textContent = error.message;
  elements.reviewList.replaceChildren(...error.overlaps.map((overlap) =>
    element("li", {}, element("code", { text: overlap.surface }),
      element("p", { text: `…${overlap.excerpt}…` }))));
  elements.reviewDialog.showModal();
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = element("a", { attrs: { href: url, download: filename } });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function doCreate() {
  const blocking = problems(state.draft);
  if (blocking.length) {
    notice(blocking[0].message);
    return;
  }
  elements.primary.disabled = true;
  try {
    const payload = await studioApi.create(state.draft);
    const id = payload.character?.id;
    toast(`${state.draft.name} exists now.`);
    // She is on disk, so the draft is no longer unsaved. Clearing this before
    // the redirect is what stops `beforeunload` firing a "leave site?" prompt
    // at the one moment nothing is at risk.
    state.saved = structuredClone(state.draft);
    location.search = `?character=${encodeURIComponent(id)}`;
  } catch (error) {
    notice(error.message || "Could not create her.");
    elements.primary.disabled = false;
  }
}

/* An accepted optimisation lands like a very large edit: straight into the
   draft, then down the ordinary save path. It deliberately does NOT go through
   `update()` — that takes one key, and this moves most of them — but it owes the
   page everything `update()` would have done, including the rerender, since the
   form's textareas hold the old text until they are rebuilt. */
function applyOptimization(payload) {
  state.draft = normalise(payload.draft);
  renderForm();
  renderPreviewPanels();
  markDirty();
  scheduleSave();
  schedulePreview();
  const count = payload.changes?.length || 0;
  toast(`${count} field${count === 1 ? "" : "s"} re-filed${state.id ? " — saving" : ""}.`);
}

const optimizer = createOptimizer({
  getDraft: () => state.draft,
  getCharacterId: () => state.id,
  onApply: applyOptimization,
});

// --------------------------------------------------------------------- boot

async function boot() {
  try {
    if (state.id) {
      const payload = await studioApi.load(state.id);
      state.draft = normalise(payload.draft);
      state.provenance = payload.provenance || {};
      state.grown = payload.grown || [];
      state.images = payload.images || state.images;
      elements.brandSub.textContent = `studio / ${payload.draft.name || state.id}`;
      elements.title.textContent = payload.draft.name || state.id;
      elements.eyebrow.textContent = "Editing a character";
      elements.primary.querySelector("span").textContent = "Export PNG";
      elements.primary.addEventListener("click", doExport);
      setSaveState("saved");
    } else {
      const payload = await studioApi.template();
      state.draft = normalise(payload.draft);
      state.draft.name = "";
      elements.brandSub.textContent = "studio / new";
      elements.title.textContent = "A new character";
      elements.eyebrow.textContent = "Creating a character";
      elements.intro.textContent =
        "Start from the shape of one that works, then make her yours. Nothing exists on disk until you press create.";
      elements.primary.replaceChildren(icon("plus"), element("span", { text: "Create character" }));
      elements.primary.addEventListener("click", doCreate);
      setSaveState("not created yet");
    }
    elements.optimize.addEventListener("click", () => optimizer.open());
    state.saved = structuredClone(state.draft);
    renderForm();
    renderGrown();
    renderPortrait();
    renderPreviewPanels();
    schedulePreview();
  } catch (error) {
    notice(error.message || "Could not open the studio.");
    setSaveState("error");
  }
}

elements.reviewDialog.addEventListener("click", (event) => {
  if (event.target.closest(".modal-close")) elements.reviewDialog.close();
});
$("#review-confirm").addEventListener("click", async () => {
  elements.reviewDialog.close();
  state.options.acknowledged = true;
  await doExport();
  state.options.acknowledged = false;
});

window.addEventListener("beforeunload", (event) => {
  if (state.draft && state.saved && !equal(state.draft, state.saved)) {
    event.preventDefault();
    event.returnValue = "";
  }
});

boot();
