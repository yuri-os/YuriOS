import { $, $$, element, errorMessage, setBusy, showToast } from "../shared/dom.js";
import { charactersApi } from "./api.js";
import {
  contextEntries,
  filterCharacters,
  formatDiaryDay,
  formatRelativeTime,
  initials,
  normalizeCharacter,
  normalizeCharacters,
  normalizeDetailItems,
  normalizeJournalDay,
  normalizeJournalDays,
} from "./model.js";

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
  review: $("#drawer-review"),
  approve: $("#approve-open"),
  approveError: $("#approve-error"),
  reviewDialog: $("#review-dialog"),
  reviewDialogName: $("#review-dialog-name"),
  reviewDialogApprove: $("#review-dialog-approve"),
  reviewDialogStudio: $("#review-dialog-studio"),
  reviewDialogError: $("#review-dialog-error"),
  importDialog: $("#import-dialog"),
  settingsDialog: $("#settings-dialog"),
  brainDialog: $("#brain-dialog"),
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
  // the parked character the approval dialog is asking about, and the room the
  // user was trying to open when it got in the way
  reviewId: null,
  pendingRoom: null,
  // the diary's own place in the book: which day list page, or which day's
  // page is open — reset whenever a different character's drawer opens
  journal: { view: "days", page: 0, day: null },
};

function readView() {
  try { return localStorage.getItem("yurios.dashboard.view") === "list" ? "list" : "grid"; }
  catch { return "grid"; }
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// this page's toast region, bound once so the ~30 call sites below stay as they were
const toast = (message, type = "success") => showToast(elements.toastRegion, message, type);

function portrait(character, className = "portrait") {
  const node = element("span", { className, attrs: { "aria-hidden": "true" } });
  if (character.avatarUrl) {
    const image = element("img", { attrs: { src: character.avatarUrl, alt: "", loading: "lazy" } });
    /* Grid view shows the card art whole rather than cropped to a circle, so
     * whatever the artwork's own shape leaves over is filled with a blurred
     * copy of itself instead of a bar of background. encodeURI keeps the
     * registry's URL from ending the url() string it lands in. */
    node.classList.add("has-art");
    node.style.setProperty("--portrait-art", `url("${encodeURI(character.avatarUrl)}")`);
    image.addEventListener("error", () => {
      node.classList.remove("has-art");
      node.style.removeProperty("--portrait-art");
      node.replaceChildren(document.createTextNode(initials(character.name)));
    }, { once: true });
    node.append(image);
  } else {
    node.textContent = initials(character.name);
  }
  return node;
}

/* Where a character can be opened (world/host.py). Three clients, one runtime:
 * the 3D sanctuary, the Live2D body (SPEC §6.6) and the bodyless text room
 * (SPEC §6.7). Every one of them is character-scoped by its path, which is what
 * shared/runtime.js reads to aim the API and socket calls. */
function rooms(character) {
  const base = `/characters/${encodeURIComponent(character.id)}`;
  return {
    sanctuary: `${base}/sanctuary/`, live2d: `${base}/live2d`, text: `${base}/text/`,
    // …and the one way in that is not a room: the debug page, which reads her
    // files rather than talking to her (SPEC §24.3).
    mind: `${base}/mind`,
  };
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
    attrs: { href: rooms(character).sanctuary, "aria-label": `Enter ${character.name}'s sanctuary` },
  });
  enter.append(icon("arrow"));
  // …and the two other ways into the same runtime (SPEC §6.6, §6.7): the Live2D
  // body, and the text room for when a GPU is not what you have.
  const way = (name, label, href) => element("a", {
    className: "card-way",
    attrs: { href, title: label, "aria-label": `${label} — ${character.name}` },
  }, icon(name));
  // The mind page is not a room and carries `data-no-gate`: it reads her files
  // rather than talking to her, so it works on a parked character — who is
  // precisely the one you want to look inside before deciding about her.
  const debug = way("mind", "Debug mind", rooms(character).mind);
  debug.setAttribute("data-no-gate", "");
  const ways = element("div", { className: "card-ways" }, enter,
    way("live2d", "Live2D body", rooms(character).live2d),
    way("text", "Text only", rooms(character).text),
    debug);
  const controls = element("div", { className: "loop-stack" },
    loopLabel("mind", "Mind"), loopLabel("utility", "Utility"), loopLabel("dream", "Dream"));
  const footer = element("div", { className: "card-footer" }, controls, ways, details);
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
  const where = rooms(character);
  const studio = `/studio/?character=${encodeURIComponent(character.id)}`;
  elements.drawerEnter.href = where.sanctuary;
  $("#drawer-live2d").href = where.live2d;
  $("#drawer-text").href = where.text;
  $("#drawer-mind").href = where.mind;
  $("#drawer-export").href = `/api/characters/${encodeURIComponent(character.id)}/export`;
  $("#drawer-studio").href = studio;
  // Her rooms open either way — they are worth a look before you decide — but
  // while she is parked they open onto nothing, and this says why (SPEC §28).
  elements.review.hidden = !character.reviewRequired;
  $("#review-studio").href = studio;
  elements.drawerIdentity.style.setProperty("--character-accent", character.accent);
  elements.drawerIdentity.replaceChildren(
    portrait(character),
    element("div", { className: "drawer-meta" },
      statusChip(character),
      element("small", { text: `updated ${formatRelativeTime(character.updatedAt)} / ${character.model}` })));
}

function openDrawer(id) {
  if (state.selectedId !== id) state.journal = { view: "days", page: 0, day: null };
  state.selectedId = id;
  elements.approveError.textContent = "";      // last character's start failure
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
  if (tab === "journal") return loadJournal(force);
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

const JOURNAL_ENTRY_CLAMP_CHARS = 260;

function journalCacheKey(id) {
  const { view, page, day } = state.journal;
  return view === "day" ? `${id}:journal:day:${day}` : `${id}:journal:days:${page}`;
}

async function loadJournal(force = false) {
  const character = selectedCharacter();
  if (!character) return;
  const id = character.id;
  const key = journalCacheKey(id);
  if (!force && state.detailCache.has(key)) {
    renderJournal(state.detailCache.get(key));
    return;
  }
  state.detailRequest?.abort();
  state.detailRequest = new AbortController();
  elements.detailContent.replaceChildren(element("div", { className: "detail-placeholder", text: "Loading journal..." }));
  try {
    const { view, page, day } = state.journal;
    const payload = view === "day"
      ? await charactersApi.journalDay(id, day, { signal: state.detailRequest.signal })
      : await charactersApi.journalDays(id, page, { signal: state.detailRequest.signal });
    if (state.selectedId !== id || state.tab !== "journal") return;
    state.detailCache.set(key, payload);
    renderJournal(payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    const retry = element("button", { className: "detail-retry", text: "Try again", attrs: { type: "button" } });
    retry.addEventListener("click", () => loadJournal(true));
    elements.detailContent.replaceChildren(element("div", { className: "detail-error" },
      document.createTextNode(errorMessage(error)), element("br"), retry));
  }
}

function renderJournal(payload) {
  if (state.journal.view === "day") renderJournalDay(payload);
  else renderJournalDays(payload);
}

function renderJournalDays(payload) {
  const { days, page, hasMore, total } = normalizeJournalDays(payload);
  if (!days.length) {
    elements.detailContent.replaceChildren(element("div", { className: "detail-placeholder", text: "No journal entries yet." }));
    return;
  }
  const list = element("ol", { className: "diary-days" }, ...days.map((entry) =>
    element("li", {},
      element("button", { className: "diary-day-row", attrs: { type: "button", "data-open-day": entry.day } },
        element("span", { className: "diary-day-date", text: formatDiaryDay(entry.day, { weekday: "short", day: "numeric", month: "short", year: "numeric" }) }),
        element("span", { className: "diary-day-count", text: `${entry.count} ${entry.count === 1 ? "entry" : "entries"}` })))));
  const totalPages = Math.max(1, Math.ceil(total / 20));
  const pagerButton = (label, targetPage, disabled) => element("button", {
    className: "button button-quiet", text: label,
    attrs: { type: "button", "data-journal-page": String(targetPage), ...(disabled ? { disabled: "disabled" } : {}) },
  });
  const pager = element("div", { className: "diary-pager" },
    pagerButton("Newer", page - 1, page === 0),
    element("span", { className: "diary-pager-status", text: `Page ${page + 1} of ${totalPages}` }),
    pagerButton("Older", page + 1, !hasMore));
  elements.detailContent.replaceChildren(list, pager);
}

function diaryEntryRow(entry) {
  const clamp = entry.text.length > JOURNAL_ENTRY_CLAMP_CHARS;
  const textNode = element("p", { className: `diary-entry-text${clamp ? " clamped" : ""}`, text: entry.text });
  const children = [element("time", { text: entry.time }), textNode];
  if (clamp) children.push(element("button", {
    className: "diary-more", text: "Show more", attrs: { type: "button", "data-toggle-entry": "" },
  }));
  return element("li", { className: `diary-entry${entry.hers ? " diary-entry-hers" : ""}` }, ...children);
}

function renderJournalDay(payload) {
  const { day, entries } = normalizeJournalDay(payload);
  const header = element("div", { className: "diary-day-head" },
    element("button", { className: "button button-quiet", text: "← All days", attrs: { type: "button", "data-journal-back": "" } }),
    element("h3", { className: "diary-day-title", text: formatDiaryDay(day, { weekday: "long", day: "numeric", month: "long", year: "numeric" }) }));
  if (!entries.length) {
    elements.detailContent.replaceChildren(header, element("div", { className: "detail-placeholder", text: "No entries this day." }));
    return;
  }
  const list = element("ol", { className: "diary-entries" }, ...entries.map(diaryEntryRow));
  elements.detailContent.replaceChildren(header, list);
}

function toggleDiaryEntry(button) {
  const paragraph = button.previousElementSibling;
  const expanded = paragraph.classList.toggle("expanded");
  button.textContent = expanded ? "Show less" : "Show more";
}

function openJournalDay(day) {
  state.journal = { view: "day", page: state.journal.page, day };
  loadJournal();
}

function backToJournalDays() {
  state.journal = { view: "days", page: state.journal.page, day: null };
  loadJournal();
}

function changeJournalPage(page) {
  if (page < 0) return;
  state.journal = { view: "days", page, day: null };
  loadJournal();
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
  const items = normalizeDetailItems(payload);
  if (!items.length) {
    elements.detailContent.replaceChildren(element("div", { className: "detail-placeholder", text: `No ${tab} entries yet.` }));
    return;
  }
  const list = element("ol", { className: "timeline" });
  for (const item of items) {
    const row = element("li", {},
      item.time ? element("time", { text: formatDetailTimeRange(item.time, item.timeEnd) }) : null,
      element("strong", { text: item.count > 1 ? `${item.title} ×${item.count}` : item.title }),
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

function formatDetailTimeRange(start, end) {
  if (!end || end === start) return formatDetailTime(start);
  const startDate = new Date(start);
  const endDate = new Date(end);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) return formatDetailTime(start);
  const sameDay = startDate.toDateString() === endDate.toDateString();
  const endLabel = sameDay
    ? endDate.toLocaleTimeString(undefined, { timeStyle: "short" })
    : formatDetailTime(end);
  return `${formatDetailTime(start)} – ${endLabel}`;
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

// What the profile form held once it finished loading — the baseline that says
// whether leaving it for the brain panel would throw anything away.
let settingsBaseline = {};

function settingsValues() {
  const form = $("#settings-form");
  // The checkboxes have to be read off the elements: FormData drops an
  // unchecked box entirely, so unticking one would look like no change at all.
  return {
    ...Object.fromEntries(new FormData(form).entries()),
    ...Object.fromEntries(["mind", "utility", "dream"].map(
      (key) => [key, form.elements[key].checked])),
  };
}

function snapshotSettings() {
  settingsBaseline = settingsValues();
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
  brainState.confirmSwitch = false;
  snapshotSettings();
  openModal(elements.settingsDialog);
  try {
    const [payload, connections] = await Promise.all([
      charactersApi.settings(character.id), charactersApi.connections(),
    ]);
    const settings = payload?.settings ?? payload;
    if (state.selectedId !== character.id || !settings || typeof settings !== "object") return;
    const profileSelect = form.elements.connection_profile;
    const profiles = Array.isArray(connections?.profiles) ? connections.profiles : [];
    profileSelect.replaceChildren(...profiles.map((profile) => element("option", {
      text: profile.name, attrs: { value: profile.name },
    })));
    for (const name of ["name", "voice", "model", "utility_model",
      "body_backend", "body_model", "description",
      "connection_profile", "personality", "scenario", "first_mes"]) {
      if (settings[name] != null) form.elements[name].value = settings[name];
    }
    for (const key of ["mind", "utility", "dream"]) {
      if (settings[key] != null) form.elements[key].checked = Boolean(settings[key]);
    }
    snapshotSettings();          // the loaded record, not the registry summary
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
    // A model or connection change lands on the live runtime (SPEC §31.4) —
    // say so, because "saved" and "she is already using it" are different news.
    const applied = response?.applied || [];
    const model = updated?.model && updated.model !== "node default" ? updated.model : null;
    toast(applied.includes("chat_model") && model
      ? `${character.name} is now on ${model} — no restart.`
      : applied.length
        ? `${character.name}'s profile saved and applied live.`
        : `${character.name}'s profile saved.`);
  } catch (apiError) {
    error.textContent = errorMessage(apiError);
  } finally {
    setBusy(button, false, "Saving...");
  }
}

/* ---- her brain -------------------------------------------------------------
 *
 * A second scope on the same registry record. The profile form above writes
 * her model settings too, so these two panels are deliberately never
 * open together: "Her brain" closes the profile and "Back to profile" reopens
 * it, which re-reads /profile and so can never save a stale copy over a brain
 * change. What this panel adds is the thing a plain text box cannot say —
 * every field is tri-state, and an empty one names the .env value it inherits
 * — plus the four knobs (thinking, temperature, reply ceiling, context length)
 * the profile form has no row for at all. */

const brainState = { fields: [], initial: {}, read: {}, confirmSwitch: false, saved: false };

function brainControl(field) {
  const shown = field.value == null ? "" : String(field.value);
  const inherits = field.inherited === "" || field.inherited == null
    ? "inherit the node default" : `inherit — ${field.inherited}`;
  if (field.type === "bool") {
    // A checkbox has two states and this has three; "inherit" is not "off".
    const select = element("select", { attrs: { name: field.key } },
      element("option", { text: `inherit (${field.inherited ? "on" : "off"})`, attrs: { value: "" } }),
      element("option", { text: "on", attrs: { value: "true" } }),
      element("option", { text: "off", attrs: { value: "false" } }));
    select.value = shown;
    return { node: select, read: () => select.value };
  }
  const input = element("input", { attrs: {
    name: field.key, type: field.type === "number" ? "number" : "text",
    value: shown, placeholder: inherits, autocomplete: "off",
    ...(field.step ? { step: field.step } : {}),
    ...(field.type === "text" ? { maxlength: "240" } : {}),
  } });
  return { node: input, read: () => input.value.trim() };
}

function brainRow(field) {
  const control = brainControl(field);
  brainState.read[field.key] = control.read;
  brainState.initial[field.key] = control.read();
  const label = element("label", { className: field.type === "model" ? "field-wide" : "" },
    element("span", { text: field.key.replace(/_/g, " ") }), control.node);
  if (field.help) label.append(element("small", { className: "field-help", text: field.help }));
  return label;
}

function renderBrain(data) {
  brainState.fields = data.fields || [];
  brainState.read = {};
  brainState.initial = {};
  $("#brain-name").textContent = data.name ? `${data.name}'s` : "Her";
  $("#brain-scope").textContent = data.running
    ? "She is running — a save here reaches this conversation at once, no restart."
    : "She is not running — these apply the moment she starts.";
  const body = $("#brain-body");
  body.replaceChildren(...brainState.fields.map(brainRow));
  const key = data.effective?.api_key_env;
  if (key && !data.key_configured) {
    body.append(element("p", { className: "field-wide form-warn",
      text: `${key} is not set on this node — a hosted model will refuse her calls until it is.` }));
  }
}

async function openBrain() {
  const character = selectedCharacter();
  if (!character) return;
  closeModal(elements.settingsDialog);
  $("#brain-error").textContent = "";
  $("#brain-body").replaceChildren(element("p", { className: "form-note", text: "Reading her record..." }));
  openModal(elements.brainDialog);
  try {
    renderBrain(await charactersApi.brain(character.id));
  } catch (error) {
    $("#brain-body").replaceChildren();
    $("#brain-error").textContent = errorMessage(error);
  }
}

async function submitBrain(event) {
  event.preventDefault();
  const character = selectedCharacter();
  if (!character) return;
  // Only what moved: an untouched field must not be written back, or "inherit"
  // would be saved as the value it was merely displaying.
  const diff = {};
  for (const [key, read] of Object.entries(brainState.read)) {
    const value = read();
    if (value !== brainState.initial[key]) diff[key] = value;
  }
  const button = $("#brain-submit");
  const error = $("#brain-error");
  error.textContent = "";
  if (!Object.keys(diff).length) {
    closeModal(elements.brainDialog);
    openSettings();
    return;
  }
  setBusy(button, true, "Saving...");
  try {
    const response = await charactersApi.saveBrain(character.id, diff);
    brainState.saved = true;
    closeModal(elements.brainDialog);
    const applied = response?.applied || [];
    const chat = response?.effective?.chat_model;
    toast(applied.includes("chat_model") && chat
      ? `${character.name} is now speaking through ${chat} — no restart.`
      : applied.length
        ? `${character.name}'s brain saved and applied live.`
        : `${character.name}'s brain saved.`);
    await loadCharacters({ quiet: true });
  } catch (apiError) {
    error.textContent = errorMessage(apiError);
  } finally {
    setBusy(button, false, "Saving...");
  }
}

/** Leaving the profile panel discards whatever is typed in it, so say so once
 *  and let the second press mean it. */
function leaveForBrain() {
  const now = settingsValues();
  const dirty = Object.keys(now).some(
    (name) => String(now[name]) !== String(settingsBaseline[name] ?? ""));
  if (dirty && !brainState.confirmSwitch) {
    brainState.confirmSwitch = true;
    $("#settings-error").textContent =
      "Unsaved profile edits — save them first, or press Her brain again to discard them.";
    return;
  }
  brainState.confirmSwitch = false;
  openBrain();
}

async function approveCharacter(id, { button, errorSlot, goTo = null } = {}) {
  const character = state.characters.find((item) => item.id === id);
  if (!character) return;
  errorSlot.textContent = "";
  setBusy(button, true, "Starting...");
  try {
    const response = await charactersApi.approve(character.id);
    const updated = normalizeCharacter(response?.character ?? response);
    if (updated?.id === character.id) Object.assign(character, updated);
    renderCharacters();
    syncDrawer();
    // Approved and running are two different facts: she is out of review either
    // way, and a failed start is hers to report, not a reason to hide the change.
    if (response?.started === false) {
      errorSlot.textContent = `Approved, but she did not start: ${response.error || "unknown error"}`;
      toast(`${character.name} approved — start failed.`, "error");
      return;
    }
    toast(`${character.name} approved and running.`);
    if (state.selectedId === character.id) loadDetail(state.tab, true);
    // She is up, and the click that opened this was on her way in — finish it.
    if (goTo) {
      closeModal(elements.reviewDialog);
      location.href = goTo;
    }
  } catch (error) {
    errorSlot.textContent = errorMessage(error);
  } finally {
    setBusy(button, false, "Starting...");
  }
}

/* Every route into a character's rooms — the three links on her card, the three
 * in her drawer — runs through here first. A parked character's rooms are served
 * but empty (SPEC §28), so walking in gives a dead page and a socket that closes
 * with 4404; the door asks instead. Returns whether the click was swallowed. */
function guardRoom(event, character, href) {
  if (!character?.reviewRequired) return false;
  // …except the debug page, which is not a room (see characterCard).
  if (event.target.closest("[data-no-gate]")) return false;
  event.preventDefault();
  state.reviewId = character.id;
  state.pendingRoom = href;
  elements.reviewDialogName.textContent = character.name;
  elements.reviewDialogStudio.href = `/studio/?character=${encodeURIComponent(character.id)}`;
  elements.reviewDialogError.textContent = "";
  openModal(elements.reviewDialog);
  return true;
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
    const card = event.target.closest("[data-character-id]");
    const id = card?.dataset.characterId;
    if (!id) return;
    if (event.target.closest('[data-action="details"]')) return openDrawer(id);
    // Enter / Live2D / Text on the card itself — the same three doors as the
    // drawer's. The mind link rides along but opts out of the gate.
    const link = event.target.closest("a[href]");
    if (link) guardRoom(event, state.characters.find((item) => item.id === id), link.href);
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
  elements.detailContent.addEventListener("click", (event) => {
    const dayButton = event.target.closest("[data-open-day]");
    if (dayButton) return openJournalDay(dayButton.dataset.openDay);
    const pageButton = event.target.closest("[data-journal-page]");
    if (pageButton) return changeJournalPage(Number(pageButton.dataset.journalPage));
    if (event.target.closest("[data-journal-back]")) return backToJournalDays();
    const moreButton = event.target.closest("[data-toggle-entry]");
    if (moreButton) toggleDiaryEntry(moreButton);
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
  // The drawer's three doors, guarded the same way the card's are. `#drawer-mind`
  // is deliberately not among them — it is a window, not a door.
  for (const link of [elements.drawerEnter, $("#drawer-live2d"), $("#drawer-text")]) {
    link.addEventListener("click", (event) => guardRoom(event, selectedCharacter(), link.href));
  }
  elements.approve.addEventListener("click", () => approveCharacter(state.selectedId, {
    button: elements.approve, errorSlot: elements.approveError }));
  elements.reviewDialogApprove.addEventListener("click", () => approveCharacter(state.reviewId, {
    button: elements.reviewDialogApprove, errorSlot: elements.reviewDialogError,
    goTo: state.pendingRoom }));
  $("#settings-open").addEventListener("click", openSettings);
  $("#settings-form").addEventListener("submit", submitSettings);
  $("#brain-open").addEventListener("click", leaveForBrain);
  $("#brain-form").addEventListener("submit", submitBrain);
  // Every way out of the brain panel goes back where it came from — including
  // Esc, which fires `close` without passing through either button.
  $("#brain-cancel").addEventListener("click", () => closeModal(elements.brainDialog));
  elements.brainDialog.addEventListener("close", () => {
    // Backing out returns you where you started; a completed save doesn't —
    // reopening the panel you just finished with reads as a failed save.
    if (brainState.saved) brainState.saved = false;
    else if (!elements.settingsDialog.open) openSettings();
  });
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
