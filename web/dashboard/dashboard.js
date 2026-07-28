import { charactersApi } from "./api.js";
import {
  contextEntries,
  filterCharacters,
  formatRelativeTime,
  initials,
  normalizeCharacter,
  normalizeCharacters,
  normalizeDetailItems,
} from "./model.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const elements = {
  grid: $("#character-grid"),
  empty: $("#empty-state"),
  notice: $("#notice"),
  search: $("#character-search"),
  gridView: $("#view-grid"),
  listView: $("#view-list"),
  refresh: $("#refresh"),
  summary: $("#fleet-summary"),
  pulse: $("#fleet-pulse"),
  drawer: $("#detail-drawer"),
  shade: $("#drawer-shade"),
  drawerName: $("#drawer-name"),
  drawerIdentity: $("#drawer-identity"),
  drawerEnter: $("#drawer-enter"),
  detailContent: $("#detail-content"),
  importDialog: $("#import-dialog"),
  settingsDialog: $("#settings-dialog"),
  archiveDialog: $("#archive-dialog"),
  toastRegion: $("#toast-region"),
};

const state = {
  characters: [],
  selectedId: null,
  tab: "journal",
  detailCache: new Map(),
  detailRequest: null,
  listRequest: null,
  view: readView(),
};

function readView() {
  try { return localStorage.getItem("yurios.dashboard.view") === "list" ? "list" : "grid"; }
  catch { return "grid"; }
}

function element(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text != null) node.textContent = options.text;
  if (options.attrs) for (const [key, value] of Object.entries(options.attrs)) node.setAttribute(key, value);
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

function setBusy(button, busy, busyLabel) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? busyLabel : button.dataset.label;
}

function errorMessage(error) {
  if (error?.name === "AbortError") return "";
  if (error?.status === 404) return "This endpoint is not available on the current YuriOS node.";
  return error?.message || "The node did not complete the request.";
}

function toast(message, type = "success") {
  const node = element("div", { className: `toast ${type}`, text: message });
  elements.toastRegion.append(node);
  setTimeout(() => node.remove(), 4200);
}

function portrait(character, className = "portrait") {
  const node = element("span", { className, attrs: { "aria-hidden": "true" } });
  if (character.avatarUrl) {
    const image = element("img", { attrs: { src: character.avatarUrl, alt: "", loading: "lazy" } });
    image.addEventListener("error", () => node.replaceChildren(document.createTextNode(initials(character.name))), { once: true });
    node.append(image);
  } else {
    node.textContent = initials(character.name);
  }
  return node;
}

function statusChip(character) {
  const chip = element("span", { className: "status-chip" },
    element("i", { className: "status-dot", attrs: { "aria-hidden": "true" } }),
    document.createTextNode(character.stateMeta.label));
  chip.style.setProperty("--state-color", character.stateMeta.color);
  return chip;
}

function characterCard(character) {
  const card = element("article", { className: "character-card", attrs: { "data-character-id": character.id } });
  card.style.setProperty("--character-accent", character.accent);

  const top = element("div", { className: "card-top" }, portrait(character), statusChip(character));
  const body = element("div", { className: "card-body" },
    element("h2", { text: character.name }),
    element("span", { className: "card-id", text: `ID / ${character.id}` }),
    element("p", { className: "card-description", text: character.description }));

  const loopLabel = (key, label) => {
    const toggle = element("input", {
      attrs: { type: "checkbox", "data-action": "control", "data-control": key,
        "aria-label": `Toggle ${character.name}'s ${label}` },
    });
    toggle.checked = character.loops[key];
    return element("label", { className: "loop-control" }, toggle,
      element("span", { className: "switch", attrs: { "aria-hidden": "true" } }),
      element("span", { className: "loop-label" }, document.createTextNode(label)));
  };
  const details = element("button", {
    className: "open-detail",
    attrs: { type: "button", "data-action": "details", "aria-label": `Inspect ${character.name}`, title: "Inspect details" },
  }, icon("tune"));
  const enter = element("a", {
    className: "enter-character",
    text: "Enter",
    attrs: { href: `/characters/${encodeURIComponent(character.id)}/sanctuary/`, "aria-label": `Enter ${character.name}'s sanctuary` },
  });
  enter.append(icon("arrow"));
  const controls = element("div", { className: "loop-stack" },
    loopLabel("mind", "Mind"), loopLabel("utility", "Utility"), loopLabel("dream", "Dream"));
  const footer = element("div", { className: "card-footer" }, controls, enter, details);
  card.append(top, body, footer);
  return card;
}

function renderSkeletons() {
  elements.grid.replaceChildren(...Array.from({ length: 4 }, () =>
    element("div", { className: "skeleton", attrs: { "aria-hidden": "true" } },
      element("i"), element("i"), element("i"))));
}

function renderCharacters() {
  const shown = filterCharacters(state.characters, elements.search.value);
  elements.grid.classList.toggle("list", state.view === "list");
  elements.grid.replaceChildren(...shown.map(characterCard));
  elements.empty.hidden = state.characters.length > 0 || Boolean(elements.search.value.trim());

  if (!shown.length && state.characters.length) {
    elements.grid.append(element("div", { className: "detail-placeholder", text: "No characters match this filter." }));
  }
  const active = state.characters.filter((character) => character.loopEnabled).length;
  elements.summary.textContent = `${state.characters.length} registered / ${active} loops active`;
  elements.pulse.classList.toggle("live", state.characters.length > 0);
  elements.grid.setAttribute("aria-busy", "false");
}

async function loadCharacters({ quiet = false } = {}) {
  state.listRequest?.abort();
  const request = new AbortController();
  state.listRequest = request;
  if (!quiet) {
    elements.empty.hidden = true;
    renderSkeletons();
  }
  elements.refresh.classList.add("loading");
  elements.notice.hidden = true;
  elements.grid.setAttribute("aria-busy", "true");
  try {
    state.characters = normalizeCharacters(await charactersApi.list({ signal: request.signal }));
    renderCharacters();
    syncDrawer();
  } catch (error) {
    if (error.name === "AbortError") return;
    if (!quiet) elements.grid.replaceChildren();
    elements.notice.textContent = `Character registry unavailable: ${errorMessage(error)}`;
    elements.notice.hidden = false;
    elements.empty.hidden = true;
    elements.summary.textContent = "node unavailable";
    elements.pulse.classList.remove("live");
    elements.grid.setAttribute("aria-busy", "false");
  } finally {
    if (state.listRequest === request) elements.refresh.classList.remove("loading");
  }
}

async function setLoop(id, key, enabled, input) {
  const character = state.characters.find((item) => item.id === id);
  if (!character) return;
  input.disabled = true;
  character.loops[key] = enabled;
  if (key === "mind") character.loopEnabled = enabled;
  renderCharacters();
  const renderedInput = $(`[data-character-id="${CSS.escape(id)}"] [data-control="${key}"]`, elements.grid);
  if (renderedInput) renderedInput.disabled = true;
  try {
    const payload = key === "mind"
      ? await charactersApi.setLoop(id, enabled)
      : await charactersApi.setControls(id, { [key]: enabled });
    const updated = normalizeCharacter(payload?.character ?? payload);
    if (updated?.id === id) Object.assign(character, updated);
    toast(`${character.name}'s ${key} work ${enabled ? "enabled" : "disabled"}.`);
  } catch (error) {
    character.loops[key] = !enabled;
    if (key === "mind") character.loopEnabled = !enabled;
    toast(`Loop change failed: ${errorMessage(error)}`, "error");
  }
  renderCharacters();
  syncDrawer();
}

function setView(view) {
  state.view = view;
  elements.gridView.setAttribute("aria-pressed", String(view === "grid"));
  elements.listView.setAttribute("aria-pressed", String(view === "list"));
  try { localStorage.setItem("yurios.dashboard.view", view); } catch { /* storage may be disabled */ }
  renderCharacters();
}

function selectedCharacter() {
  return state.characters.find((character) => character.id === state.selectedId);
}

function syncDrawer() {
  const character = selectedCharacter();
  if (!character) {
    if (state.selectedId) closeDrawer();
    return;
  }
  elements.drawerName.textContent = character.name;
  elements.drawerEnter.href = `/characters/${encodeURIComponent(character.id)}/sanctuary/`;
  $("#drawer-export").href = `/api/characters/${encodeURIComponent(character.id)}/export`;
  elements.drawerIdentity.style.setProperty("--character-accent", character.accent);
  elements.drawerIdentity.replaceChildren(
    portrait(character),
    element("div", { className: "drawer-meta" },
      statusChip(character),
      element("small", { text: `updated ${formatRelativeTime(character.updatedAt)} / ${character.model}` })));
}

function openDrawer(id) {
  state.selectedId = id;
  syncDrawer();
  elements.drawer.classList.add("open");
  elements.drawer.setAttribute("aria-hidden", "false");
  elements.shade.hidden = false;
  document.body.style.overflow = "hidden";
  loadDetail(state.tab);
  setTimeout(() => $("#drawer-close").focus(), 50);
}

function closeDrawer() {
  state.detailRequest?.abort();
  elements.drawer.classList.remove("open");
  elements.drawer.setAttribute("aria-hidden", "true");
  elements.shade.hidden = true;
  document.body.style.overflow = "";
  state.selectedId = null;
}

function selectTab(tab) {
  state.tab = tab;
  $$('[role="tab"]', $("#detail-tabs")).forEach((button) =>
    button.setAttribute("aria-selected", String(button.dataset.tab === tab)));
  loadDetail(tab);
}

function detailCacheKey(id, tab) {
  return `${id}:${tab}`;
}

async function loadDetail(tab, force = false) {
  const character = selectedCharacter();
  if (!character) return;
  const id = character.id;
  const key = detailCacheKey(id, tab);
  if (!force && state.detailCache.has(key)) {
    renderDetail(tab, state.detailCache.get(key));
    return;
  }
  state.detailRequest?.abort();
  state.detailRequest = new AbortController();
  elements.detailContent.replaceChildren(element("div", { className: "detail-placeholder", text: `Loading ${tab}...` }));
  try {
    const payload = await charactersApi.detail(id, tab, { signal: state.detailRequest.signal });
    if (state.selectedId !== id || state.tab !== tab) return;
    state.detailCache.set(key, payload);
    renderDetail(tab, payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    const retry = element("button", { className: "detail-retry", text: "Try again", attrs: { type: "button" } });
    retry.addEventListener("click", () => loadDetail(tab, true));
    elements.detailContent.replaceChildren(element("div", { className: "detail-error" },
      document.createTextNode(errorMessage(error)), element("br"), retry));
  }
}

function renderDetail(tab, payload) {
  if (tab === "context") {
    const entries = contextEntries(payload);
    if (!entries.length) {
      elements.detailContent.replaceChildren(element("div", { className: "detail-placeholder", text: "No context snapshot has been published." }));
      return;
    }
    const list = element("dl", { className: "context-sheet" });
    for (const row of entries) list.append(element("div", { className: "context-row" },
      element("dt", { text: row.key }), element("dd", { text: row.value || "-" })));
    const history = Array.isArray(payload?.history) ? payload.history : [];
    elements.detailContent.replaceChildren(contextChart(history), list);
    return;
  }
  const items = normalizeDetailItems(tab, payload);
  if (!items.length) {
    elements.detailContent.replaceChildren(element("div", { className: "detail-placeholder", text: `No ${tab} entries yet.` }));
    return;
  }
  const list = element("ol", { className: "timeline" });
  for (const item of items) {
    const row = element("li", {},
      item.time ? element("time", { text: formatDetailTime(item.time) }) : null,
      element("strong", { text: item.title }),
      item.body ? element("p", { text: item.body }) : null);
    if (/error|failed|reject/i.test(item.tone)) row.style.setProperty("--event-color", "var(--red)");
    list.append(row);
  }
  elements.detailContent.replaceChildren(list);
}

function contextChart(history) {
  const wrap = element("section", { className: "context-chart" },
    element("strong", { text: "Context use over time" }));
  if (history.length < 2) {
    wrap.append(element("p", { text: "History appears after model calls." }));
    return wrap;
  }
  const values = history.map((row) =>
    Number(row.pct ?? (row.limit ? row.used * 100 / row.limit : 0)) || 0);
  const width = 560, height = 120;
  const points = values.map((value, index) =>
    `${index * width / Math.max(1, values.length - 1)},${height - Math.min(100, value) * height / 100}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Context usage history, latest ${values.at(-1).toFixed(1)} percent`);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points);
  svg.append(line);
  wrap.append(svg, element("small", {
    text: `${history.length} samples / latest ${values.at(-1).toFixed(1)}%`,
  }));
  return wrap;
}

function formatDetailTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function openModal(dialog) {
  dialog.showModal();
}

function closeModal(dialog) {
  dialog.close();
}

function validPng(file) {
  return file && (file.type === "image/png" || file.name.toLowerCase().endsWith(".png")) && file.size <= 25 * 1024 * 1024;
}

async function submitImport(event) {
  event.preventDefault();
  const file = $("#png-file").files[0];
  const error = $("#import-error");
  if (!validPng(file)) {
    error.textContent = "Choose a PNG file no larger than 25 MB.";
    return;
  }
  error.textContent = "";
  const button = $("#import-submit");
  setBusy(button, true, "Importing...");
  try {
    await charactersApi.importPng(file);
    closeModal(elements.importDialog);
    $("#import-form").reset();
    $("#file-name").textContent = "";
    toast(`${file.name} imported.`);
    await loadCharacters({ quiet: true });
  } catch (apiError) {
    error.textContent = errorMessage(apiError);
  } finally {
    setBusy(button, false, "Importing...");
  }
}

async function openSettings() {
  const character = selectedCharacter();
  if (!character) return;
  const form = $("#settings-form");
  form.elements.name.value = character.name;
  form.elements.voice.value = character.voice === "default" ? "" : character.voice;
  form.elements.model.value = character.model === "node default" ? "" : character.model;
  form.elements.description.value = character.description === "No profile note has been set." ? "" : character.description;
  form.elements.connection_profile.value = character.connectionProfile;
  form.elements.utility_model.value = "";
  form.elements.body_backend.value = character.raw?.body_backend || "";
  form.elements.body_model.value = character.raw?.body_model || "";
  for (const name of ["personality", "scenario", "first_mes"]) form.elements[name].value = "";
  for (const key of ["mind", "utility", "dream"]) form.elements[key].checked = character.loops[key];
  $("#settings-error").textContent = "";
  openModal(elements.settingsDialog);
  try {
    const payload = await charactersApi.settings(character.id);
    const settings = payload?.settings ?? payload;
    if (state.selectedId !== character.id || !settings || typeof settings !== "object") return;
    for (const name of ["name", "voice", "model", "utility_model", "body_backend",
      "body_model", "description", "connection_profile", "personality", "scenario", "first_mes"]) {
      if (settings[name] != null) form.elements[name].value = settings[name];
    }
    for (const key of ["mind", "utility", "dream"]) {
      if (settings[key] != null) form.elements[key].checked = Boolean(settings[key]);
    }
  } catch (error) {
    if (error.name !== "AbortError") $("#settings-error").textContent = `Using registry values. ${errorMessage(error)}`;
  }
}

async function submitSettings(event) {
  event.preventDefault();
  const character = selectedCharacter();
  if (!character) return;
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  for (const key of ["mind", "utility", "dream"]) payload[key] = form.elements[key].checked;
  const button = $("#settings-submit");
  const error = $("#settings-error");
  error.textContent = "";
  setBusy(button, true, "Saving...");
  try {
    const response = await charactersApi.saveSettings(character.id, payload);
    const updated = normalizeCharacter(response?.character ?? { ...character.raw, ...payload, id: character.id });
    if (updated) Object.assign(character, updated);
    closeModal(elements.settingsDialog);
    renderCharacters();
    syncDrawer();
    toast(`${character.name}'s profile saved.`);
  } catch (apiError) {
    error.textContent = errorMessage(apiError);
  } finally {
    setBusy(button, false, "Saving...");
  }
}

async function submitArchive(event) {
  event.preventDefault();
  const character = selectedCharacter();
  if (!character) return;
  const button = $("#archive-submit");
  const error = $("#archive-error");
  error.textContent = "";
  setBusy(button, true, "Archiving...");
  try {
    await charactersApi.archive(character.id);
    closeModal(elements.archiveDialog);
    closeDrawer();
    state.characters = state.characters.filter((item) => item.id !== character.id);
    renderCharacters();
    toast(`${character.name} moved to the archive.`);
  } catch (apiError) {
    error.textContent = errorMessage(apiError);
  } finally {
    setBusy(button, false, "Archiving...");
  }
}

function wireEvents() {
  elements.grid.addEventListener("change", (event) => {
    if (event.target.dataset.action !== "control") return;
    const id = event.target.closest("[data-character-id]")?.dataset.characterId;
    if (id) setLoop(id, event.target.dataset.control, event.target.checked, event.target);
  });
  elements.grid.addEventListener("click", (event) => {
    const button = event.target.closest('[data-action="details"]');
    const id = button?.closest("[data-character-id]")?.dataset.characterId;
    if (id) openDrawer(id);
  });
  elements.search.addEventListener("input", renderCharacters);
  elements.gridView.addEventListener("click", () => setView("grid"));
  elements.listView.addEventListener("click", () => setView("list"));
  elements.refresh.addEventListener("click", () => loadCharacters({ quiet: true }));
  $("#drawer-close").addEventListener("click", closeDrawer);
  elements.shade.addEventListener("click", closeDrawer);
  $("#detail-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]")?.dataset.tab;
    if (tab) selectTab(tab);
  });
  $$(".import-open").forEach((button) => button.addEventListener("click", () => {
    $("#import-error").textContent = "";
    openModal(elements.importDialog);
  }));
  $$(".modal-close").forEach((button) => button.addEventListener("click", () => closeModal(button.closest("dialog"))));
  $$('dialog').forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeModal(dialog);
  }));
  $("#import-form").addEventListener("submit", submitImport);
  $("#settings-open").addEventListener("click", openSettings);
  $("#settings-form").addEventListener("submit", submitSettings);
  $("#archive-open").addEventListener("click", () => {
    const character = selectedCharacter();
    if (!character) return;
    $("#archive-name").textContent = character.name;
    $("#archive-error").textContent = "";
    openModal(elements.archiveDialog);
  });
  $("#archive-form").addEventListener("submit", submitArchive);
  const fileInput = $("#png-file");
  fileInput.addEventListener("change", () => $("#file-name").textContent = fileInput.files[0]?.name || "");
  const dropZone = $("#drop-zone");
  for (const type of ["dragenter", "dragover"]) dropZone.addEventListener(type, (event) => {
    event.preventDefault(); dropZone.classList.add("dragging");
  });
  for (const type of ["dragleave", "drop"]) dropZone.addEventListener(type, () => dropZone.classList.remove("dragging"));
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    fileInput.files = transfer.files;
    $("#file-name").textContent = file.name;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
      event.preventDefault(); elements.search.focus();
    }
    if (event.key === "Escape" && elements.drawer.classList.contains("open") && !$('dialog[open]')) closeDrawer();
  });
}

wireEvents();
setView(state.view);
loadCharacters();
