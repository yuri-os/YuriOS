import { ApiError, studioApi } from "./api.js";
import { BUDGETS, SECTIONS, description, equal, normalise, problems, tokens } from "./draft.js";
import { createOptimizer } from "./optimize.js";
import { EVERY_MODEL, archTarget, assemble, cellOf, inherited, modelTarget,
         parseTarget, resolve, usedTargets, withField } from "./wording.js";

const $ = (selector, root = document) => root.querySelector(selector);

const params = new URLSearchParams(location.search);
const state = {
  id: params.get("character"),
  draft: null,
  saved: null,
  provenance: {},
  grown: [],
  images: { portrait: false, selfies: [] },
  // Her selfie library, loaded and saved on its own (the card draft is a SOUL;
  // this is the camera's vocabulary). `bookSource` is "character" once she has
  // a file of her own, "shipped" while she is still looking at ours.
  book: null,
  bookSource: "shipped",
  bookSlots: [],
  bookError: "",
  bookSaving: false,
  bookResetArmed: false,
  // How her camera words a picture (forge/prompts.py): every layer, as the
  // server sent it, edited in place. `wordingScope` is which editable layer the
  // fields write to; `wordingTarget` which cell — every model, one
  // architecture, one model. `wordingDirty` holds the scopes awaiting a save.
  wording: null,
  wordingError: "",
  wordingScope: "her",
  wordingTarget: null,
  wordingDirty: new Set(),
  wordingResetArmed: false,
  // Where she is — a Vault file like the library above, arriving with the card
  // draft and saved on its own. `derived` is whether it is still the machine's
  // reading of her card, or yours.
  setting: { setting: "", derived: false, exists: false },
  settingBusy: false,
  settingError: "",
  options: { spec: "v3", include_soul: true, image: "portrait", fit: "none",
             attribution: true, timestamps: true, acknowledged: false },
  preview: null,
  unlocked: new Set(),
  saving: false,
  previewing: false,
  // Bumped whenever `portrait.png` is rewritten — an upload or an adopted
  // selfie. `/portrait` is one fixed URL, so this is the only thing that tells
  // the browser the bytes behind it changed.
  portraitVersion: 0,
  adopting: false,
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
  const choose = (selfieName, label, url) => {
    const button = element("button", {
      className: `selfie${selfieName === null ? " chosen" : ""}`,
      attrs: { type: "button",
               title: selfieName === null ? label : `Make ${label} her face` },
    }, url ? element("img", { attrs: { src: url, alt: label, loading: "lazy" } })
           : element("span", { className: "selfie-empty", text: "no face yet" }));
    if (selfieName !== null) {
      button.addEventListener("click", () => adoptSelfie(selfieName));
    }
    return button;
  };
  strip.append(choose(null, "Her portrait",
    state.images.portrait ? `/api/characters/${state.id}/portrait?v=${state.portraitVersion || 0}` : null));
  for (const selfie of state.images.selfies) {
    strip.append(choose(selfie.name, selfie.name, selfie.url));
  }
  wrap.append(strip);
  if (state.images.selfies.length) {
    wrap.append(element("p", { className: "form-note",
      text: "The ringed one is her face. Click a selfie to make it hers instead — "
            + "the card, her tile and her settings all follow." }));
  }

  const drop = element("label", { className: "drop-zone" },
    element("span", { className: "drop-symbol" }, icon("upload")),
    element("strong", { text: "Upload a face" }),
    element("span", { text: "PNG or JPEG — it becomes her portrait" }));
  const file = element("input", { attrs: { type: "file", accept: "image/png,image/jpeg" } });
  file.addEventListener("change", () => file.files?.[0] && uploadPortrait(file.files[0]));
  drop.append(file);
  wrap.append(drop);

  const fits = element("div", { className: "seg" });
  for (const [value, label] of [["none", "Original"], ["contain", "Pad"], ["cover", "Crop"]]) {
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

/* How her camera words a picture (SPEC §7.6, forge/prompts.py) — prefix,
   postfix and negatives, for every model, one kind of model or one checkpoint,
   in her layer or the house's. Like the library below it this is a file the
   camera reads, not the card, and saves on its own debounce; unlike the
   library it is read per render, so a save reaches her next shot with no
   restart. It rebuilds only itself — never the whole form — because the form
   rebuild would tear out the box being typed into. */
let wordingNode = null;

const ARCH_SHORT = { sdxl: "SDXL", krea2: "Krea 2", anima: "Anima",
                     openrouter: "hosted", mock: "mock" };

function wordingLayer(scope) {
  return state.wording?.layers.find((layer) => layer.name === scope) || null;
}

function cameraLabel(camera) {
  if (!camera?.architecture) return "her camera is off";
  const kind = ARCH_SHORT[camera.architecture] || camera.architecture;
  return camera.model ? `${kind} · ${camera.model}` : kind;
}

function refreshWording() {
  const old = wordingNode;              // selfieWordingControl() reassigns it
  if (!old) return;
  old.replaceWith(selfieWordingControl());
}

function selfieWordingControl() {
  const wrap = element("div", { className: "field field-wording" });
  wordingNode = wrap;
  if (!state.id) {
    wrap.append(element("p", { className: "form-note",
      text: "Create her first — her camera's wording is a file that lives beside her." }));
    return wrap;
  }
  if (!state.wording) {
    wrap.append(element("p", { className: "form-note",
      text: state.wordingError || "Reading her camera's wording…" }));
    return wrap;
  }

  const w = state.wording;
  const scope = state.wordingScope;
  const layer = wordingLayer(scope);
  const target = state.wordingTarget || EVERY_MODEL;
  const used = usedTargets(w.layers);

  const head = element("div", { className: "field-head" },
    element("span", { className: "field-label", text: "Prompt wording" }));
  const scopes = element("div", { className: "seg" });
  for (const [value, label] of [["her", "Her"], ["house", "Every character"]]) {
    const button = element("button", { className: scope === value ? "on" : "",
      attrs: { type: "button" }, text: label });
    button.addEventListener("click", () => {
      state.wordingScope = value;
      state.wordingResetArmed = false;
      refreshWording();
    });
    scopes.append(button);
  }
  head.append(scopes);
  if (layer && !isEmptyProfile(layer.profile)) {
    const reset = element("button", {
      className: `button button-quiet book-reset${state.wordingResetArmed ? " armed" : ""}`,
      attrs: { type: "button" },
    }, icon("back"), element("span", { text: state.wordingResetArmed
      ? "Clear all of it — sure?"
      : scope === "her" ? "Clear her wording" : "Clear the house wording" }));
    reset.addEventListener("click", () => {
      if (!state.wordingResetArmed) {
        state.wordingResetArmed = true;
        refreshWording();
        return;
      }
      resetSelfieWording(scope);
    });
    head.append(reset);
  }
  wrap.append(head);
  wrap.append(element("small", { className: "field-hint", text: scope === "her"
    ? "How her camera asks for a picture. Set it for every model, one kind of model or one checkpoint — the narrowest setting wins, and anything here beats the house's. An empty field inherits."
    : `Every character inherits this, unless her own wording says otherwise. Saved to ${w.paths.house}.` }));

  wrap.append(targetPicker(target, used));

  const fields = element("div", { className: "wording-fields" });
  const from = inherited(w.layers, scope, target, w.models);
  const cell = cellOf(layer?.profile, target);
  for (const field of w.fields) {
    const own = (cell[field.key] || "").trim();
    const note = element("span", { className: `wording-source${own ? " own" : ""}`,
      text: own ? "set here" : from[field.key].source
        ? `inherits · ${from[field.key].source}` : "nothing set" });
    const box = element("textarea", { attrs: { rows: field.key.endsWith("negative") ? 3 : 2,
      placeholder: from[field.key].value || "(empty)" } });
    box.value = cell[field.key] || "";
    box.addEventListener("input", () => {
      const index = state.wording.layers.findIndex((l) => l.name === scope);
      if (index < 0) return;
      state.wording.layers[index].profile = withField(
        state.wording.layers[index].profile, target, field.key, box.value);
      const set = box.value.trim() !== "";
      note.textContent = set ? "set here" : from[field.key].source
        ? `inherits · ${from[field.key].source}` : "nothing set";
      note.classList.toggle("own", set);
      state.wordingResetArmed = false;
      state.wordingDirty.add(scope);
      renderWordingPreview();
      scheduleWordingSave();
    });
    fields.append(element("label", { className: "field field-textarea" },
      element("span", { className: "field-head" },
        element("span", { className: "field-label", text: field.label }), note),
      element("small", { className: "field-hint", text: field.hint }), box));
  }
  wrap.append(fields);
  wrap.append(element("div", { className: "wording-preview", attrs: { id: "wording-preview" } }));
  renderWordingPreview(wrap.querySelector(".wording-preview"));
  return wrap;
}

function isEmptyProfile(profile) {
  return !profile || (!Object.keys(profile.default || {}).length
    && !Object.keys(profile.architectures || {}).length
    && !Object.keys(profile.models || {}).length);
}

function targetPicker(target, used) {
  const w = state.wording;
  const mark = (value, text) => (used.has(value) ? `● ${text}` : text);
  const select = element("select", { attrs: { "aria-label": "Which models this wording is for" } });
  const option = (value, text) => {
    const node = element("option", { attrs: { value }, text });
    if (value === target) node.selected = true;
    return node;
  };
  select.append(option(EVERY_MODEL, mark(EVERY_MODEL, "Every model")));
  const kinds = element("optgroup", { attrs: { label: "One kind of model" } });
  for (const arch of w.architectures) {
    const value = archTarget(arch.key);
    const here = arch.key === w.camera.architecture ? " · her camera's kind" : "";
    kinds.append(option(value, mark(value, `${arch.label}${here}`)));
  }
  select.append(kinds);
  const models = element("optgroup", { attrs: { label: "One model" } });
  const listed = new Set();
  for (const model of w.models) {
    const value = modelTarget(model.key);
    listed.add(model.key);
    const kind = ARCH_SHORT[model.architecture] || "unknown kind";
    const here = model.key === w.camera.model ? " · her camera" : "";
    models.append(option(value, mark(value, `${model.key} (${kind})${here}`)));
  }
  // A model typed in below that nothing lists yet still needs to be selectable.
  const { kind, key } = parseTarget(target);
  if (kind === "model" && !listed.has(key)) {
    models.append(option(target, `${key} (new)`));
  }
  select.append(models);
  select.addEventListener("change", () => {
    state.wordingTarget = select.value;
    state.wordingResetArmed = false;
    refreshWording();
  });

  const other = element("input", { attrs: { type: "text",
    placeholder: "another model — a checkpoint file name or a hosted model id" } });
  other.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const name = other.value.trim();
    if (!name) return;
    state.wordingTarget = modelTarget(name);
    refreshWording();
  });
  return element("div", { className: "wording-picker" },
    element("span", { className: "field-label", text: "For" }), select, other);
}

/* What her camera sends right now, with whatever is in the boxes — the scene
   left as a marker, because the library below picks that per shot. */
function renderWordingPreview(node = document.getElementById("wording-preview")) {
  if (!node || !state.wording) return;
  const w = state.wording;
  const camera = w.camera;
  const resolved = resolve(w.layers, camera.architecture || "", camera.model || "");
  const prompt = assemble(resolved, w.identity, w.negative_extra);
  const sources = w.fields.filter((f) => resolved[f.key].source).map((f) =>
    element("li", {}, element("strong", { text: f.label }), ` ← ${resolved[f.key].source}`));
  node.replaceChildren(
    element("p", { className: "field-label", text: `What her camera sends now — ${cameraLabel(camera)}` }),
    element("p", { className: "wording-prompt", text: prompt.positive }),
    element("p", { className: "field-label", text: "Negative" }),
    element("p", { className: "wording-prompt negative", text: prompt.negative || "(none)" }),
    element("ul", { className: "wording-sources" }, ...sources));
}

const scheduleWordingSave = debounce(async () => {
  if (!state.id || !state.wording) return;
  const scopes = [...state.wordingDirty];
  state.wordingDirty.clear();
  setSaveState("saving her camera's wording", true);
  try {
    for (const scope of scopes) {
      const layer = wordingLayer(scope);
      if (!layer) continue;
      // The reply is the server's whole view; only what the page cannot know
      // is taken from it. The layers stay the page's own — a reply to an older
      // save must not overwrite what was typed while it was in flight.
      const payload = await studioApi.saveSelfiePrompts(state.id, scope, layer.profile);
      state.wording.models = payload.models;
      state.wording.camera = payload.camera;
    }
    setSaveState("saved");
    notice("");
  } catch (error) {
    for (const scope of scopes) state.wordingDirty.add(scope);
    setSaveState("wording not saved");
    notice(error.message || "Could not save her camera's wording.");
  }
}, 1200);

async function resetSelfieWording(scope) {
  try {
    state.wording = await studioApi.resetSelfiePrompts(state.id, scope);
    state.wordingDirty.delete(scope);
    state.wordingResetArmed = false;
    refreshWording();
    toast(scope === "her" ? "Her wording is gone — she inherits the house's."
                          : "The house wording is gone — back to the shipped register.");
  } catch (error) {
    toast(error.message || "Could not clear that wording.", "error");
  }
}

async function loadSelfieWording() {
  try {
    state.wording = await studioApi.selfiePrompts(state.id);
    const camera = state.wording.camera;
    // Open on her camera's own model: the setting someone came here to change.
    state.wordingTarget ||= camera.model ? modelTarget(camera.model)
      : camera.architecture ? archTarget(camera.architecture) : EVERY_MODEL;
  } catch (error) {
    state.wordingError = error.message || "Could not read her camera's wording.";
  }
  refreshWording();
}

/* Her selfie library (SPEC §7.6) — with the wording above, the part of this
   page that edits files the *camera* reads rather than the card. It saves on its own debounce, to its
   own endpoint, because writing it restarts her runtime: the forge builds the
   book once at start, and the `take_selfie` description is built from the same
   book, so a saved row nobody restarted for is a page describing scenes she
   cannot name. */
function selfieBookControl() {
  const wrap = element("div", { className: "field field-selfiebook" });
  if (!state.id) {
    wrap.append(element("p", { className: "form-note",
      text: "Create her first — a library is a file that lives beside her." }));
    return wrap;
  }
  if (!state.book) {
    wrap.append(element("p", { className: "form-note",
      text: state.bookError || "Reading her library…" }));
    return wrap;
  }

  const own = state.bookSource === "character";
  const head = element("div", { className: "book-head" },
    element("span", { className: `book-badge${own ? " own" : ""}`,
      text: own ? "her own library" : "the shipped library" }),
    element("small", { className: "field-hint", text: own
      ? "This file is hers and replaces ours completely. Delete a row and she stops being offered it."
      : "These are the house defaults, shown so you have somewhere to start. The first edit forks them into a library of her own." }));
  if (own) {
    const reset = element("button", {
      className: `button button-quiet book-reset${state.bookResetArmed ? " armed" : ""}`,
      attrs: { type: "button" },
    }, icon("back"), element("span", { text: state.bookResetArmed
      ? "Discard her library — sure?" : "Back to the shipped library" }));
    reset.addEventListener("click", () => {
      if (!state.bookResetArmed) {
        state.bookResetArmed = true;
        renderForm();
        return;
      }
      resetSelfieBook();
    });
    head.append(reset);
  }
  wrap.append(head);

  const hint = element("label", { className: "field field-textarea" },
    element("span", { className: "field-head" },
      element("span", { className: "field-label", text: "Tool hint" })),
    element("small", { className: "field-hint",
      text: "One line the take_selfie tool description carries verbatim — how this library explains its own register to her. Leave it empty and the description stands on the rows alone." }));
  const hintBox = element("textarea", { attrs: { rows: 2,
    placeholder: "Prefer the rooftop scenes in the evening." } });
  hintBox.value = state.book.tool_hint;
  hintBox.addEventListener("input", () =>
    updateBook((book) => { book.tool_hint = hintBox.value; }));
  hint.append(hintBox);
  wrap.append(hint);

  for (const slot of state.bookSlots) wrap.append(slotControl(slot));
  return wrap;
}

function slotControl(slot) {
  const rows = state.book.slots[slot.key] || [];
  const block = element("div", { className: "book-slot" });
  block.append(element("div", { className: "field-head" },
    element("span", { className: "field-label", text: slot.label }),
    element("span", { className: "book-count", text: `${rows.length}` })));
  block.append(element("small", { className: "field-hint", text: slot.hint }));

  rows.forEach((row, index) => {
    const card = element("div", { className: "lore-entry book-row" });
    const key = element("input", { className: "book-key",
      attrs: { type: "text", placeholder: "key — one word she can name" } });
    key.value = row.key;
    key.addEventListener("input", () => patchRow(slot.key, index, { key: key.value }));
    const remove = element("button", { className: "icon-button danger",
      attrs: { type: "button", "aria-label": `Remove ${slot.label} ${index + 1}` } }, icon("trash"));
    remove.addEventListener("click", () => updateBook((book) => {
      book.slots[slot.key] = book.slots[slot.key].filter((_r, i) => i !== index);
    }, { rerender: true }));
    const prompt = element("textarea", { attrs: { rows: 2,
      placeholder: "the fragment composed into the picture" } });
    prompt.value = row.prompt;
    prompt.addEventListener("input", () =>
      patchRow(slot.key, index, { prompt: prompt.value }));

    // The two mechanics that make a row real rather than decorative, kept on
    // one quiet line: most rows want neither, and a form that asks for three
    // fields per scene is a form nobody fills in.
    const negative = element("input", { className: "book-negative",
      attrs: { type: "text", placeholder: "negative — what must not appear (optional)" } });
    negative.value = row.negative;
    negative.addEventListener("input", () =>
      patchRow(slot.key, index, { negative: negative.value }));
    const pinned = element("input", { attrs: { type: "checkbox" } });
    pinned.checked = row.pinned;
    pinned.addEventListener("change", () =>
      patchRow(slot.key, index, { pinned: pinned.checked }));
    const pin = element("label", { className: "book-pin",
      attrs: { title: "Never rotated into an unprompted shot — a named ask only" } },
      pinned, element("span", { text: "named asks only" }));

    card.append(element("div", { className: "lore-head" }, key, remove), prompt,
      element("div", { className: "book-extra" }, negative, pin));
    block.append(card);
  });

  const add = element("button", { className: "button button-quiet add",
    attrs: { type: "button" } }, icon("plus"),
    element("span", { text: `Add ${slot.label.toLowerCase().replace(/s$/, "")}` }));
  add.addEventListener("click", () => updateBook((book) => {
    book.slots[slot.key] = [...book.slots[slot.key],
      { key: "", prompt: "", negative: "", pinned: false }];
  }, { rerender: true }));
  block.append(add);
  return block;
}

function patchRow(slot, index, patch) {
  updateBook((book) => {
    book.slots[slot] = book.slots[slot].map((row, i) =>
      i === index ? { ...row, ...patch } : row);
  });
}

/* Where she is (SPEC §2.5) — the other file on this page that is not the card.
   The scenario above ships inside the PNG and frames the meeting; this is the
   standing room her prompt puts her in every single turn, and it stays in the
   Vault. It is derived from that scenario at import, so the two agree until you
   make them disagree, and it saves to its own endpoint on its own debounce. */
function settingControl() {
  const wrap = element("div", { className: "field field-setting" });
  if (!state.id) {
    wrap.append(element("p", { className: "form-note",
      text: "Create her first — her room is a file in her Vault, derived from the scenario above." }));
    return wrap;
  }

  const head = element("div", { className: "field-head" },
    element("span", { className: "field-label", text: "Where she is" }),
    element("span", {
      className: `book-badge${state.setting.derived ? "" : " own"}`,
      text: state.setting.derived ? "derived from her card" : "yours",
    }));
  const improve = element("button", {
    className: "button button-quiet",
    attrs: { type: "button", disabled: state.settingBusy },
  }, icon("sparkle"), element("span", {
    text: state.settingBusy ? "Reading her card…" : "Improve with AI" }));
  improve.addEventListener("click", improveSetting);
  head.append(improve);
  wrap.append(head);
  wrap.append(element("small", { className: "field-hint",
    text: "One to three sentences, second person, present tense — it replaces the house room inside the embodiment truth she carries every turn. Leave out the hour, the weather and the music: the runtime injects the real ones. Write {user} for the person she talks to. Empty it and she is re-derived from her card at her next start." }));

  const box = element("textarea", { attrs: { rows: 3,
    placeholder: "You are in a narrow flat over the laundromat, the fire escape outside your one window." } });
  box.value = state.setting.setting;
  box.addEventListener("input", () => {
    state.setting.setting = box.value;
    state.setting.derived = false;
    scheduleSettingSave();
  });
  wrap.append(box);
  if (state.settingError) {
    wrap.append(element("p", { className: "form-note", text: state.settingError }));
  }
  return wrap;
}

const scheduleSettingSave = debounce(async () => {
  if (!state.id || state.settingBusy) return;
  state.settingBusy = true;
  setSaveState("saving her room", true);
  try {
    const payload = await studioApi.saveSetting(state.id, state.setting.setting);
    state.setting = payload;
    state.settingError = "";
    setSaveState("saved");
  } catch (error) {
    state.settingError = error.message || "Could not save where she is.";
    setSaveState("room not saved");
  } finally {
    state.settingBusy = false;
  }
}, 1500);

/* The optimiser's rule, at one field's scale: the model proposes, the page
   shows you the prose, and the ordinary save is what writes it. */
async function improveSetting() {
  if (!state.id || state.settingBusy) return;
  state.settingBusy = true;
  state.settingError = "";
  renderForm();
  try {
    const payload = await studioApi.deriveSetting(state.id);
    state.setting.setting = payload.setting;
    state.setting.derived = false;
    state.settingBusy = false;
    renderForm();
    scheduleSettingSave();
    toast("A better room — saving.");
  } catch (error) {
    state.settingBusy = false;
    state.settingError = error.message || "The model could not place her.";
    renderForm();
  }
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
      else if (field.type === "selfiebook") grid.append(selfieBookControl());
      else if (field.type === "selfiewording") grid.append(selfieWordingControl());
      else if (field.type === "setting") grid.append(settingControl());
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

/* The preview shows the face that will actually be embedded, which — since
   picking a selfie adopts it (`adoptSelfie`) — is always her portrait. The
   version stamp is what makes the swap visible: the URL never changes, so
   without it the browser keeps showing the face it already cached. */
function renderPortrait() {
  let src = BLANK_FACE;
  if (state.id && state.images.portrait) {
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

/* The library's own `update()`. It never touches the card draft — a scene row
   is not a card field and must not make the card look unsaved — and it keeps
   the same "only structural edits rerender" rule, for the same reason: a
   rebuild from an `input` handler tears out the box being typed into. */
function updateBook(mutate, { rerender = false } = {}) {
  if (!state.book) return;
  mutate(state.book);
  state.bookResetArmed = false;
  if (rerender) renderForm();
  scheduleBookSave();
}

const scheduleBookSave = debounce(async () => {
  if (!state.id || !state.book || state.bookSaving) return;
  if (!Object.values(state.book.slots).some((rows) =>
        rows.some((row) => row.key.trim() && row.prompt.trim()))) {
    // An empty library is not a library — the server refuses it, and saying so
    // here beats a red toast for a half-typed row on the way to a full one.
    setSaveState("library needs a row");
    return;
  }
  state.bookSaving = true;
  setSaveState("saving her library", true);
  try {
    const payload = await studioApi.saveSelfieBook(state.id, state.book);
    const forked = state.bookSource !== "character";
    state.bookSource = payload.source || "character";
    setSaveState("saved");
    notice("");
    if (forked) {
      renderForm();
      toast("She has her own selfie library now.");
    }
  } catch (error) {
    setSaveState("library not saved");
    notice(error.message || "Could not save her selfie library.");
  } finally {
    state.bookSaving = false;
  }
}, 1500);

async function resetSelfieBook() {
  try {
    const payload = await studioApi.resetSelfieBook(state.id);
    state.book = payload.book;
    state.bookSource = payload.source || "shipped";
    state.bookResetArmed = false;
    renderForm();
    toast("Her library is gone — she's back on the shipped one.");
  } catch (error) {
    toast(error.message || "Could not reset her library.", "error");
  }
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

/* Picking one of her own selfies is the same act as uploading a face, and has
   to leave the same mark: `POST /portrait` copies it over `portrait.png`, so the
   card, the dashboard tile and the settings modal all follow. The picker used to
   only set an export option, which lived in this tab and nowhere else — the card
   you exported wore the selfie, and everything else still wore the old face. */
async function adoptSelfie(name) {
  if (state.adopting) return;
  state.adopting = true;
  try {
    await studioApi.setPortrait(state.id, { selfie: name });
    state.images.portrait = true;
    state.portraitVersion = Date.now();
    renderForm();
    renderPortrait();
    schedulePreview();
    toast("That's her face now.");
  } catch (error) {
    toast(error.message || "Could not use that selfie.", "error");
  } finally {
    state.adopting = false;
  }
}

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

/* The gate in front of both irreversible buttons, in one place because it had
   already drifted apart in two: export took you to the field it was complaining
   about and create did not. Both buttons live in a sticky header, so on a form
   this tall a refusal that only writes the banner leaves its reason a couple of
   thousand pixels above the fold — the button looks like it did nothing at all.
   Focusing the field is what actually moves the viewport to the problem. */
function blockedByAProblem(draft) {
  const [first] = problems(draft);
  if (!first) return false;
  notice(first.message);
  document.querySelector(
    `[data-field="${first.field}"] textarea, [data-field="${first.field}"] input`)?.focus();
  return true;
}

async function doExport() {
  if (blockedByAProblem(state.draft)) return;
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
  if (blockedByAProblem(state.draft)) return;
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

/* Fetched after the page is already usable: the library is one section down and
   a slow read of it must not hold up the card. A failure leaves the section
   saying so rather than taking the studio down with it — you can still edit and
   export her without ever opening her camera. */
async function loadSelfieBook() {
  try {
    const payload = await studioApi.selfieBook(state.id);
    state.book = payload.book;
    state.bookSource = payload.source || "shipped";
    state.bookSlots = payload.slots || [];
  } catch (error) {
    state.bookError = error.message || "Could not read her selfie library.";
  }
  renderForm();
}

async function boot() {
  try {
    if (state.id) {
      const payload = await studioApi.load(state.id);
      state.draft = normalise(payload.draft);
      state.provenance = payload.provenance || {};
      state.grown = payload.grown || [];
      state.images = payload.images || state.images;
      state.setting = payload.setting || state.setting;
      elements.brandSub.textContent = `studio / ${payload.draft.name || state.id}`;
      elements.title.textContent = payload.draft.name || state.id;
      elements.eyebrow.textContent = "Editing a character";
      elements.primary.querySelector("span").textContent = "Export PNG";
      elements.primary.addEventListener("click", doExport);
      setSaveState("saved");
      loadSelfieBook();                  // off the critical path — the form
                                         // renders without it and fills in
      loadSelfieWording();
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
