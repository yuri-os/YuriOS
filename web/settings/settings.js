/* House settings, as a page (SPEC §11.2) — the switchboard's House settings.
 *
 * The house `.env` is around two hundred and forty knobs. In a dialog that was a
 * single scroll with a filter box over it; here it is a rail of pages down the
 * left, the way the mind debug page is laid out, each page a few titled
 * sections in the order you would read them. The layout is the server's
 * (`envfile.PAGES`, delivered as `pages` on GET /api/settings), so the page, the
 * gear in every room and `yurios settings` cannot disagree about what a knob is
 * called or where it lives.
 *
 * Every page is drawn at once and only one is shown. That is not laziness: a
 * row's `relevant_if` names other rows by id (the SearXNG address only matters
 * under SEARCH_BACKEND=searxng), and a search has to be able to answer from
 * every page at once. The rows are /shared/settings.js's — `createForm` hands
 * back the same model picker, pairing panel and vocabulary boxes the dialog
 * draws, and the diff a save sends.
 *
 * One save for the whole house, in a bar that appears only when there is
 * something to save: the file is one file, and a restart applies all of it
 * together, so a save button per page would be promising a granularity the
 * server does not have. What a save is still waiting on survives a reload —
 * the server marks every field whose saved value is not what it booted with —
 * and the Restart button (SPEC §11.3) keeps the promise without a terminal.
 *
 * Below the house pages sit the files `.env` only names (addons.js) and one
 * page per character (character.js, SPEC §11.4): her overrides ride the same
 * save bar; her night jobs and scene library are files with their own buttons.
 */
import { renderMcp, renderOverlay } from "./addons.js";
import { characterSettings, renderJobs, renderScenes } from "./character.js";
import {
  ADDON_PAGES, CHARACTER_TABS, changedFromDefault, glance, indexFields, parseRoute,
  pendingRestart, shown,
} from "./model.js";
import { api, el, icon } from "./ui.js";

const S = window.YuriOSSettings;
const $ = (selector) => document.querySelector(selector);
const nodes = {
  rail: $("#rail"),
  railPages: $("#rail-pages"),
  railAddons: $("#rail-addons"),
  railCharacters: $("#rail-characters"),
  search: $("#search"),
  title: $("#page-title"),
  note: $("#page-note"),
  body: $("#stage-body"),
  extra: $("#stage-extra"),
  envPath: $("#env-path"),
  savebar: $("#savebar"),
  saveNote: $("#save-note"),
  save: $("#save"),
  discard: $("#discard"),
  restart: $("#restart"),
  restartNow: $("#savebar-restart"),
  restarting: $("#restarting"),
};

const OVERVIEW = {
  id: "overview", title: "Overview",
  note: "The house defaults every character inherits. Each character's own model and "
    + "switches sit on top of them — her page is under Characters, below.",
};

const state = {
  data: null,
  index: new Map(),
  form: null,
  route: { page: "overview", key: "" },
  restartSaid: "",      // what the last save in this visit said, kept until the next
  characters: [],
  house: { restartable: false, boot_id: "", why_not: "" },
  // id → { ready: Promise<{panel, save, dirtyKeys}> , value } — her settings
  // panel, drawn once a visit so an edit survives looking at somebody else
  panels: new Map(),
};

// ------------------------------------------------------------------ drawing

function rowsOf(fields, form) {
  return fields.map((field) => {
    const row = form.fieldRow(field);
    row.dataset.type = field.type;
    if (field.pending) {
      row.classList.add("is-pending");
      row.append(el("div", { className: "set-pending",
        textContent: "saved · waits for a restart" }));
    }
    return row;
  });
}

function sectionPanel(section, form) {
  const rows = rowsOf(section.fields, form);
  if (section.advanced) {
    // folded: these are the knobs for the case something is already wrong
    const fold = el("details", { className: "sec sec-advanced" },
      el("summary", { className: "sec-head" },
        el("h2", { textContent: section.title }),
        el("span", { className: "sec-count", textContent: `advanced · ${rows.length}` })),
      el("div", { className: "sec-body" }, ...rows));
    return fold;
  }
  return el("section", { className: "sec" },
    el("header", { className: "sec-head" }, el("h2", { textContent: section.title })),
    el("div", { className: "sec-body" }, ...rows));
}

function pagePanel(page, form) {
  const panel = el("section", { className: "page", hidden: true });
  panel.dataset.page = page.id;
  // only shown while searching, when every page is on screen at once
  panel.append(el("h2", { className: "page-label" },
    el("a", { href: `#/${page.id}`, textContent: page.title })));
  for (const section of page.sections) panel.append(sectionPanel(section, form));
  return panel;
}

function tile(t) {
  const link = el("a", { className: `tile ${t.tone ? "tone-" + t.tone : ""}`,
    href: `#/${t.page}/${t.key}` },
  el("span", { className: "tile-label", textContent: t.label }),
  el("span", { className: "tile-value", textContent: t.value, title: t.value }));
  if (t.sub) link.append(el("span", { className: "tile-sub", textContent: t.sub, title: t.sub }));
  return link;
}

function listTable(entries, columns) {
  return el("table", { className: "grid-table" },
    el("thead", {}, el("tr", {}, ...columns.map((c) => el("th", { textContent: c.head })))),
    el("tbody", {}, ...entries.map((entry) => el("tr", {},
      ...columns.map((c) => el("td", { className: c.className || "" }, c.cell(entry)))))));
}

const settingLink = ({ key, page, field }) => el("a", { className: "row-link",
  href: `#/${page}/${key}` },
el("span", { textContent: field.label || key }),
el("code", { textContent: key }));

const pageTitle = (id) => (state.data.pages.find((p) => p.id === id)?.title || "Overview");

function restartControl() {
  if (!state.house.restartable)
    return el("span", {}, "Run ", el("code", { textContent: "yurios restart" }),
      " in the installation folder to apply them.");
  const now = el("button", { type: "button", className: "button button-quiet",
    textContent: "Restart now" });
  now.addEventListener("click", () => restart());
  return now;
}

function overviewPanel(form) {
  const panel = el("section", { className: "page", hidden: true });
  panel.dataset.page = "overview";

  const pending = pendingRestart(state.index);
  if (pending.length) {
    panel.append(el("div", { className: "notice notice-row" },
      el("span", {},
        el("strong", { textContent: `${pending.length} saved setting${pending.length === 1 ? "" : "s"} `
          + "waiting for a restart. " }),
        "Everything in .env is read when YuriOS starts."),
      restartControl()));
  }

  if (state.data.overview.length) {
    panel.append(el("section", { className: "sec" },
      el("header", { className: "sec-head" }, el("h2", { textContent: "You" })),
      el("div", { className: "sec-body" }, ...rowsOf(state.data.overview, form))));
  }

  const tiles = glance(state.index);
  if (tiles.length) {
    panel.append(el("section", { className: "sec" },
      el("header", { className: "sec-head" }, el("h2", { textContent: "At a glance" })),
      el("div", { className: "tiles" }, ...tiles.map(tile))));
  }

  if (state.characters.length) {
    panel.append(el("section", { className: "sec" },
      el("header", { className: "sec-head" }, el("h2", { textContent: "Characters" }),
        el("span", { className: "sec-count", textContent: String(state.characters.length) })),
      listTable(state.characters, [
        { head: "Character", cell: (c) => el("a", { className: "row-link",
          href: `#/character/${encodeURIComponent(c.id)}` }, el("span", { textContent: c.name })) },
        { head: "State", cell: (c) => c.state, className: "mono muted" },
        { head: "Model", cell: (c) => c.model || "house", className: "mono" },
        { head: "Her files", cell: (c) => el("span", { className: "row-links" },
          ...CHARACTER_TABS.slice(1).map((t) => el("a", { className: "text-link",
            href: `#/character/${encodeURIComponent(c.id)}/${t.id}`, textContent: t.title }))) },
      ])));
  }

  const changed = changedFromDefault(state.index);
  panel.append(el("section", { className: "sec" },
    el("header", { className: "sec-head" },
      el("h2", { textContent: "Changed from the defaults" }),
      el("span", { className: "sec-count", textContent: String(changed.length) })),
    changed.length
      ? listTable(changed, [
        { head: "Setting", cell: settingLink },
        { head: "Page", cell: (e) => pageTitle(e.page), className: "muted" },
        { head: "Now", cell: (e) => shown(e.field.value), className: "mono" },
        { head: "Default", cell: (e) => shown(e.field.default), className: "mono muted" },
      ])
      : el("p", { className: "placeholder", textContent: "Everything is at its default." })));

  panel.append(el("section", { className: "sec" },
    el("header", { className: "sec-head" }, el("h2", { textContent: "How this works" })),
    el("ul", { className: "plain" },
      el("li", { textContent: "Every house setting is a line in the file named at the top. "
        + "Saving rewrites only the lines you changed; your comments in it survive." }),
      el("li", {}, "House changes apply the next time YuriOS starts — the Restart button "
        + "at the top, or ", el("code", { textContent: "yurios restart" }), "."),
      el("li", { textContent: "A character's own settings sit on top of these. Her model "
        + "applies at once; her night jobs and selfie scenes are files you can import and export." }),
      el("li", {}, "The same table from a terminal: ",
        el("code", { textContent: "yurios settings --all" }), "."))));
  return panel;
}

function railItem(href, iconName, title, dataset) {
  const item = el("a", { className: "rail-item", href },
    iconName, el("span", { textContent: title }),
    el("i", { className: "rail-mark", ariaHidden: "true" }));
  Object.assign(item.dataset, dataset);
  return item;
}

function drawRail() {
  nodes.railPages.replaceChildren(...state.data.pages.map((page) =>
    railItem(`#/${page.id}`, icon(page.icon || "dots"), page.title, { page: page.id })));
  nodes.railAddons.replaceChildren(...ADDON_PAGES.map((page) =>
    railItem(`#/${page.id}`, icon(page.icon), page.title, { page: page.id })));
  nodes.railCharacters.replaceChildren(...state.characters.map((c) => {
    const dot = el("i", { className: `char-dot state-${c.state}`, title: c.state });
    return railItem(`#/character/${encodeURIComponent(c.id)}`, dot, c.name,
      { page: "character", character: c.id });
  }));
  nodes.railCharacters.previousElementSibling.hidden = !state.characters.length;
}

async function load() {
  await S.ready;
  const [settings, characters, house] = await Promise.all([
    api("/api/settings"),
    api("/api/characters").catch(() => ({ characters: [] })),
    api("/api/house").catch(() => null),
  ]);
  if (!Array.isArray(settings.pages)) throw new Error("this server has no settings page layout");
  return { settings, characters: characters.characters || [], house };
}

function draw({ settings, characters, house }) {
  state.data = settings;
  state.characters = characters;
  if (house) state.house = house;
  state.index = indexFields(settings);
  state.form = S.createForm((text) => say(text, "error"));
  nodes.envPath.textContent = settings.env_path || ".env";
  nodes.restart.disabled = !state.house.restartable;
  nodes.restart.title = state.house.restartable
    ? "restart YuriOS so saved settings take effect" : state.house.why_not;
  drawRail();
  nodes.body.replaceChildren(overviewPanel(state.form),
    ...settings.pages.map((page) => pagePanel(page, state.form)));
  S.relevance(nodes.body);
  applyView();
  refreshDirty();
}

// ------------------------------------------------------------------ the view

const isHousePage = (page) => page === "overview" || state.data.pages.some((p) => p.id === page);

function applyView() {
  const q = nodes.search.value.trim().toLowerCase();
  const searching = Boolean(q);
  document.body.classList.toggle("searching", searching);
  const counts = new Map();
  const house = isHousePage(state.route.page);

  for (const panel of nodes.body.querySelectorAll(".page")) {
    const id = panel.dataset.page;
    let pageHits = 0;
    for (const sec of panel.querySelectorAll(".sec")) {
      const rows = sec.querySelectorAll(".set-row");
      let shownRows = 0;
      for (const row of rows) {
        const relevant = row.dataset.relevant !== "false";
        const hit = relevant && (!searching || (row.dataset.match || "").includes(q));
        row.hidden = !hit;
        shownRows += hit ? 1 : 0;
      }
      // a section of tiles or tables has no rows: it belongs to the overview,
      // and a search is not looking for it
      sec.hidden = rows.length ? !shownRows : searching;
      if (searching && sec.tagName === "DETAILS" && shownRows) sec.open = true;
      pageHits += shownRows;
    }
    counts.set(id, pageHits);
    panel.hidden = searching ? !pageHits : !(house && id === state.route.page);
  }
  nodes.extra.hidden = searching || house;

  for (const item of nodes.rail.querySelectorAll(".rail-item")) {
    const id = item.dataset.page;
    const here = id === "character"
      ? state.route.page === "character" && item.dataset.character === state.route.character
      : id === state.route.page;
    item.classList.toggle("on", !searching && here);
    const counted = counts.has(id) && id !== "character";
    item.dataset.hits = searching && counted ? String(counts.get(id) || 0) : "";
    item.classList.toggle("no-hits", searching && (!counted || !counts.get(id)));
  }

  if (searching) {
    const total = [...counts.values()].reduce((a, b) => a + b, 0);
    nodes.title.textContent = "Search";
    nodes.note.textContent = total
      ? `${total} house setting${total === 1 ? "" : "s"} match “${q}”.`
      : `No house setting matches “${q}”.`;
    return;
  }
  let page;
  if (state.route.page === "character") {
    const c = state.characters.find((x) => x.id === state.route.character);
    page = { title: c?.name || "Character", note: "Hers alone, on top of the house's: her model, "
      + "her profile and switches, her own channel, and the files she carries — her night "
      + "jobs and her camera's scenes." };
  } else {
    page = state.route.page === "overview" ? OVERVIEW
      : state.data.pages.find((p) => p.id === state.route.page)
        || ADDON_PAGES.find((p) => p.id === state.route.page);
  }
  nodes.title.textContent = page.title;
  nodes.note.textContent = page.note;
  document.title = `YuriOS / ${page === OVERVIEW ? "House settings" : page.title + " · House settings"}`;
}

const ctx = {
  needsRestart(what) {
    state.restartSaid = `${what} — restart YuriOS to apply`;
    refreshDirty();
  },
};

function characterPanel(character) {
  let entry = state.panels.get(character.id);
  if (!entry) {
    entry = { value: null };
    entry.ready = characterSettings(character, { houseKeys: new Set(state.index.keys()) })
      .then((value) => { entry.value = value; return value; });
    state.panels.set(character.id, entry);
  }
  return entry.ready;
}

async function renderCharacter() {
  const { character: id, tab } = state.route;
  const character = state.characters.find((c) => c.id === id);
  const enc = encodeURIComponent(id);
  const content = el("div", { className: "char-content" },
    el("p", { className: "placeholder", textContent: "loading…" }));
  nodes.extra.replaceChildren(
    el("div", { className: "char-head" },
      el("img", { className: "char-portrait", src: character.portrait_url, alt: "" }),
      el("div", { className: "char-who" },
        el("strong", { textContent: character.name }),
        el("span", { className: `char-state state-${character.state}`, textContent: character.state })),
      el("div", { className: "row-links" },
        el("a", { className: "text-link", href: `/characters/${enc}/sanctuary/`, textContent: "Her room" }),
        el("a", { className: "text-link", href: `/characters/${enc}/text/`, textContent: "Text" }),
        el("a", { className: "text-link", href: `/characters/${enc}/mind/`, textContent: "Debug mind" }),
        el("a", { className: "text-link", href: `/studio/?character=${enc}`, textContent: "Studio" }))),
    el("nav", { className: "char-tabs", role: "tablist" },
      ...CHARACTER_TABS.map((t) => el("a", { className: `char-tab${t.id === tab ? " on" : ""}`,
        href: `#/character/${enc}/${t.id}`, textContent: t.title, role: "tab" }))),
    content);
  try {
    if (tab === "jobs") await renderJobs(content, character, state.characters);
    else if (tab === "scenes") await renderScenes(content, character, state.characters);
    else {
      const panel = await characterPanel(character);
      // only if we are still on her settings when it arrives
      if (state.route.page === "character" && state.route.character === id
          && state.route.tab === "settings") content.replaceChildren(panel.panel);
    }
  } catch (error) {
    state.panels.delete(id);
    content.replaceChildren(el("p", { className: "placeholder error",
      textContent: `Couldn't load ${character.name}: ${error.message}` }));
  }
  refreshDirty();
}

function renderExtra() {
  const { page } = state.route;
  if (page === "mcp") return renderMcp(nodes.extra, ctx);
  if (page === "overlay") return renderOverlay(nodes.extra, ctx);
  if (page === "character") return renderCharacter();
  return null;
}

function route() {
  if (!state.data) return;
  state.route = parseRoute(location.hash, state.data.pages, state.characters);
  if (nodes.search.value) nodes.search.value = "";
  if (!isHousePage(state.route.page)) renderExtra();
  applyView();
  window.scrollTo({ top: 0 });
  const { key } = state.route;
  if (!key) return;
  const row = nodes.body.querySelector(`.page[data-page="${state.route.page}"] .set-row[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  const fold = row.closest("details");
  if (fold) fold.open = true;
  row.scrollIntoView({ block: "center" });
  row.classList.remove("is-flash");
  void row.offsetWidth;            // restart the animation on a second visit
  row.classList.add("is-flash");
}

// ------------------------------------------------------------------ saving

function say(text, tone = "") {
  nodes.saveNote.textContent = text;
  nodes.saveNote.className = `save-note ${tone}`;
  if (text) nodes.savebar.hidden = false;
}

const houseDirty = () => (state.form ? Object.keys(state.form.diffs().env) : []);
const characterDirty = () => [...state.panels]
  .map(([id, entry]) => [id, entry.value ? entry.value.dirtyKeys() : []])
  .filter(([, keys]) => keys.length);
const dirtyCount = () => houseDirty().length
  + characterDirty().reduce((n, [, keys]) => n + keys.length, 0);

function refreshDirty() {
  const keys = new Set(houseDirty());
  for (const row of nodes.body.querySelectorAll(".set-row[data-key]"))
    row.classList.toggle("is-dirty", keys.has(row.dataset.key));
  const pages = new Set([...keys].map((key) => state.index.get(key)?.page));
  const characters = new Map(characterDirty());
  for (const [id, entry] of state.panels) {
    if (!entry.value) continue;
    const mine = new Set(characters.get(id) || []);
    for (const row of entry.value.panel.querySelectorAll(".set-row[data-key]"))
      row.classList.toggle("is-dirty", mine.has(row.dataset.key));
  }
  for (const item of nodes.rail.querySelectorAll(".rail-item"))
    item.classList.toggle("is-dirty", item.dataset.page === "character"
      ? characters.has(item.dataset.character) : pages.has(item.dataset.page));

  const total = dirtyCount();
  nodes.save.disabled = !total;
  nodes.discard.disabled = !total;
  nodes.restartNow.hidden = !(state.restartSaid && state.house.restartable && !total);
  if (total) {
    say(`${total} unsaved change${total === 1 ? "" : "s"}`);
  } else if (state.restartSaid) {
    say(state.restartSaid + (state.house.restartable ? "" : " (yurios restart)"), "restart");
  } else {
    say("");
    nodes.savebar.hidden = true;
  }
}

async function save() {
  const diff = state.form.diffs().env;
  const characters = characterDirty();
  if (!Object.keys(diff).length && !characters.length) return;
  nodes.save.disabled = true;
  say("saving…");
  const said = [];
  try {
    if (Object.keys(diff).length) {
      const res = await S.saveEnv(diff);
      const n = (res.written || []).length;
      if (res.restart_required) state.restartSaid = `Saved ${n} house setting${n === 1 ? "" : "s"} — restart YuriOS to apply`;
    }
    for (const [id] of characters) {
      const entry = state.panels.get(id);
      const line = await entry.value.save();
      if (line) said.push(line);
      if (/restart/.test(line)) state.restartSaid ||= `${line}`;
      state.panels.delete(id);            // drawn again from what she now holds
    }
    // Redrawn from the server rather than patched in place: the defaults
    // note, the overview's lists and the "waits for a restart" marks are all
    // answers the server gives, and it has just changed its mind about them.
    const scroll = window.scrollY;
    draw(await load());
    if (!isHousePage(state.route.page)) await renderExtra();
    window.scrollTo({ top: scroll });
    if (said.length && !state.restartSaid) {
      say(said.join(" · "), "ok");
      setTimeout(refreshDirty, 5000);
    }
  } catch (error) {
    say(`Save failed: ${error.message || error}`, "error");
    nodes.save.disabled = false;
  }
}

async function discard() {
  const scroll = window.scrollY;
  state.panels.clear();
  draw(await load());
  if (!isHousePage(state.route.page)) await renderExtra();
  window.scrollTo({ top: scroll });
}

// ------------------------------------------------------------------ restart

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function restart() {
  const before = state.house.boot_id;
  try {
    await api("/api/house/restart", { method: "POST" });
  } catch (error) {
    say(`Restart refused: ${error.message}`, "error");
    return;
  }
  nodes.restarting.hidden = false;
  const started = Date.now();
  // A new boot id, not just an open port: the old server answers for the
  // moment it takes to stop, and reloading into it would show the old values.
  while (Date.now() - started < 180_000) {
    await pause(1000);
    try {
      const house = await api("/api/house");
      if (house.boot_id && house.boot_id !== before) {
        location.reload();
        return;
      }
    } catch (_) { /* down, as asked — keep waiting */ }
  }
  nodes.restarting.querySelector("p").textContent = "YuriOS has not come back after three "
    + "minutes. `yurios status` and `yurios log` say why.";
}

function armRestart() {
  let armed = null;
  nodes.restart.addEventListener("click", () => {
    const label = nodes.restart.querySelector("span");
    if (!armed) {
      label.textContent = dirtyCount() ? "Unsaved edits — restart?" : "Restart now?";
      nodes.restart.classList.add("is-armed");
      armed = setTimeout(() => {
        armed = null;
        label.textContent = "Restart";
        nodes.restart.classList.remove("is-armed");
      }, 4000);
      return;
    }
    clearTimeout(armed);
    armed = null;
    restart();
  });
  nodes.restartNow.addEventListener("click", restart);
}

// ------------------------------------------------------------------ boot

async function boot() {
  try {
    draw(await load());
  } catch (error) {
    nodes.body.replaceChildren(el("p", { className: "placeholder error",
      textContent: `Couldn't load the house settings: ${error.message || error}` }));
    return;
  }
  route();
}

if (S && nodes.body) {
  window.addEventListener("hashchange", route);
  nodes.search.addEventListener("input", applyView);
  for (const root of [nodes.body, nodes.extra]) {
    root.addEventListener("input", refreshDirty);
    root.addEventListener("change", () => {
      S.relevance(nodes.body);
      applyView();
      refreshDirty();
    });
    // a model picked from the browse list, or a password's "remove", changes a
    // value without an input event
    for (const type of ["mousedown", "click"])
      root.addEventListener(type, () => setTimeout(refreshDirty, 0));
  }
  // A rail item while searching is "take me there": the search goes, and a
  // click on the page already in the hash still has to land somewhere.
  nodes.rail.addEventListener("click", (event) => {
    const item = event.target.closest(".rail-item");
    if (!item || !nodes.search.value) return;
    nodes.search.value = "";
    if (item.getAttribute("href") === location.hash) route();
  });
  nodes.save.addEventListener("click", save);
  nodes.discard.addEventListener("click", discard);
  armRestart();
  document.addEventListener("keydown", (event) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (event.key === "/" && !typing) {
      event.preventDefault();
      nodes.search.focus();
    } else if (event.key === "Escape" && document.activeElement === nodes.search) {
      nodes.search.value = "";
      applyView();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirtyCount()) event.preventDefault();
  });
  boot();
}
