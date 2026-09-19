/* One workspace under six sections (SPEC §24.4).
 *
 * Map, Timeline, Space, Stories, Goals and Ledger are six ways of looking at
 * one thing — the joined graph for a window — so they share everything that is
 * not the drawing: the loaded window, the range inside it, the kind filters,
 * the search, the selection and its inspector. Moving from the Timeline to the
 * Ledger keeps the evening you zoomed into and the record you picked; that is
 * the point of having them side by side.
 *
 * State lives here, in module scope, across section changes. The hash carries
 * it too (`#/timeline?w=7&r=t0-t1&sel=id&off=kinds&q=word`), written with
 * replaceState so it never re-triggers the router, which is what makes a view
 * a link you can paste. A pasted link's params win over the memory.
 *
 * Live updates follow the page's rule: nothing you are reading moves under
 * you. New records mark the workspace stale and say so; you reload when ready.
 */
import {
  KINDS, KIND_LABEL, WINDOWS, clamp, clip, compact, createModel, dur, esc, fmt, fmtShort,
} from "./model.js";
import { inspectHtml, metaHtml, snapshotHtml } from "./inspector.js";
import { drawGoals, drawLedger, drawMap, drawSpace, drawStories, drawTimeline } from "./views.js";

export const GRAPH_SECTIONS = Object.freeze({
  map: { title: "How she is built", key: "1", draw: drawMap,
    note: "The architecture, reactive body beside the tick, with counts for the loaded window. Click a box for its records." },
  timeline: { title: "What happened, in order", key: "2", draw: drawTimeline,
    note: "Swimlanes of every record in the range. Drag to pan, wheel to zoom, click a mark for its why-record. Rest ticks are the faint bars, not dots." },
  space: { title: "Inside the connections", key: "3", draw: drawSpace,
    note: "Time runs left to right; shared work clusters across it. Solid lines are recorded links, dashed amber ones inferred. Nearness is not a causal score." },
  stories: { title: "Stories and evidence", key: "4", draw: drawStories,
    note: "Decisions, exchanges and reach-outs, each with its why in plain words and its chain in order. Inferred associations say so, and why." },
  goals: { title: "What she is working toward", key: "5", draw: drawGoals,
    note: "Goals filed or worked on in the range, with where each came from, her rationale, what done looks like, and the ticks that served it." },
  ledger: { title: "The ledger", key: "6", draw: drawLedger,
    note: "Every record in the range, newest first, grouped by day. Filter by kind above; search with /." },
});

const PAGE = 60;
const STATE_COLOR = { ENGAGED: "--acid", IDLE: "--mint", DORMANT: "--dim", DREAM: "--amber" };

const ws = {
  section: "timeline",
  model: null,
  loadedDays: null,
  stale: false,
  deps: null,
  els: null,
  teardown: [],
  redraw: null,
  keyHandler: null,
  drawerReturn: null,
  state: {
    days: 7,
    t0: null, t1: null,
    kinds: Object.fromEntries(KINDS.map(([k]) => [k, true])),
    query: "",
    selected: null,
    pinnedStory: null,
    storyLimit: PAGE,
    ledgerLimit: PAGE * 2,
    goalFilter: "open",
    mapZoom: null,          // null: fit the width on first sight
  },
};

// ------------------------------------------------------------ the view's API

ws.visible = (t0 = ws.state.t0, t1 = ws.state.t1) =>
  ws.model.inRange(t0, t1).filter((e) => ws.state.kinds[e.kind] && matches(e));
ws.onTeardown = (fn) => ws.teardown.push(fn);
ws.setRedraw = (fn) => { ws.redraw = fn; };
ws.cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#9aa39b";
ws.kindColors = () => Object.fromEntries(KINDS.map(([k]) => [k, ws.cssVar(`--k-${k}`)]));
ws.stateColors = () => Object.fromEntries(Object.entries(STATE_COLOR).map(([k, v]) => [k, ws.cssVar(v)]));
ws.toast = (message) => ws.deps?.toast(message, "success");
ws.show = (section) => ws.deps.go(`#/${section}`);
ws.select = select;
ws.setRange = setRange;
ws.refresh = refresh;
ws.renderChrome = () => { renderKinds(); renderStats(); syncRange(); };
ws.tooltip = showTooltip;
ws.hideTooltip = hideTooltip;

function matches(e) {
  return !ws.state.query || e._blob.includes(ws.state.query);
}

// ------------------------------------------------------------- mount / leave

/**
 * Build the workspace for `section`. `deps`: { load(days) → graph payload,
 * go(hash), toast(message, type), readFile(path) → {text} }.
 * Returns the element; call `attached()` once it is in the document.
 */
export async function mountGraph(section, params, deps) {
  unmountGraph();
  ws.deps = deps;
  ws.section = GRAPH_SECTIONS[section] ? section : "timeline";
  const fromLink = applyParams(params);

  if (!ws.model || ws.loadedDays !== ws.state.days || ws.stale) {
    await load(ws.state.days, { keepRange: fromLink && params.r });
  }
  // A link to a record the window does not hold widens to everything once,
  // rather than opening on a selection that silently isn't there.
  if (ws.state.selected && !ws.model.byId.has(ws.state.selected) && ws.state.days !== 0) {
    const wanted = ws.state.selected;
    ws.state.days = 0;
    await load(0);
    ws.state.selected = wanted;
    if (ws.model.byId.has(wanted)) reveal(wanted);
  }
  if (ws.state.selected && !ws.model.byId.has(ws.state.selected)) {
    ws.toast("That record is no longer on disk — its log has rolled past it.");
    ws.state.selected = null;
  }
  ws.els = buildChrome();
  return ws.els.root;
}

/** Draw the view. Separate from mount because a canvas measures itself. */
export function graphAttached() {
  if (!ws.els) return;
  refresh();
  // Stories draws the selection's chain at the top of its list, so it stays
  // put; a list that only marks the selection brings it into view.
  if (ws.state.selected && ws.section !== "stories") {
    requestAnimationFrame(() => revealCard(ws.els?.view.querySelector(".gx-card.on, .gx-goal.on")));
  }
}

/* Scroll the list, never the page: `scrollIntoView` moves every scrolling
 * ancestor, and the page jumping under the reader is what this avoids. */
function revealCard(card) {
  const list = card?.closest(".gx-list");
  if (!list) return;
  const top = card.offsetTop, bottom = top + card.offsetHeight;
  if (top < list.scrollTop || bottom > list.scrollTop + list.clientHeight) {
    list.scrollTop = Math.max(0, top - list.clientHeight / 3);
  }
}

export function unmountGraph() {
  runTeardown();
  if (ws.keyHandler) window.removeEventListener("keydown", ws.keyHandler);
  ws.keyHandler = null;
  hideTooltip();
  document.body.classList.remove("gx-drawer-open");
  ws.els = null;
}

/** New records exist on disk. Say so; reload only when asked. */
export function markStale() {
  if (!ws.model || ws.stale) return;
  ws.stale = true;
  if (ws.els) ws.els.stale.hidden = false;
}

/** The page's Refresh button: the next mount re-reads. */
export function invalidateGraph() {
  ws.stale = true;
}

async function load(days, { keepRange = false } = {}) {
  const data = await ws.deps.load(days);
  ws.model = createModel(data);
  ws.loadedDays = days;
  ws.stale = false;
  const { tMin, tMax } = ws.model;
  if (!keepRange || ws.state.t0 == null) {
    ws.state.t0 = tMin;
    ws.state.t1 = tMax;
  }
  clampRange();
  if (ws.state.selected && !ws.model.byId.has(ws.state.selected) && days === 0) {
    ws.state.selected = null;
  }
}

function applyParams(params = {}) {
  const has = ["w", "r", "sel", "off", "q", "goals"].some((k) => params[k] != null);
  if (!has) return false;
  const st = ws.state;
  if (params.w != null && WINDOWS.some(([d]) => String(d) === params.w)) st.days = Number(params.w);
  const r = (params.r || "").split("-").map(Number);
  if (r.length === 2 && r.every(Number.isFinite) && r[1] > r[0]) { st.t0 = r[0]; st.t1 = r[1]; }
  const off = new Set((params.off || "").split(",").filter(Boolean));
  if (params.off != null) for (const [k] of KINDS) st.kinds[k] = !off.has(k);
  if (params.q != null) st.query = params.q.toLowerCase();
  if (params.goals && ["open", "done", "abandoned", "all"].includes(params.goals)) st.goalFilter = params.goals;
  if (params.sel != null) st.selected = params.sel || null;
  return true;
}

function writeHash() {
  const st = ws.state;
  const p = new URLSearchParams();
  p.set("w", String(st.days));
  p.set("r", `${Math.round(st.t0)}-${Math.round(st.t1)}`);
  const off = KINDS.filter(([k]) => !st.kinds[k]).map(([k]) => k);
  if (off.length) p.set("off", off.join(","));
  if (st.query) p.set("q", st.query);
  if (st.goalFilter !== "open") p.set("goals", st.goalFilter);
  if (st.selected) p.set("sel", st.selected);
  const next = `#/${ws.section}?${p}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}

// ---------------------------------------------------------------- the chrome

function buildChrome() {
  const root = document.createElement("div");
  root.className = "gx";
  root.innerHTML = `
    <div class="gx-bar">
      <div class="gx-windows" role="group" aria-label="Window to load">
        ${WINDOWS.map(([d, label]) => `<button type="button" data-days="${d}">${label}</button>`).join("")}
      </div>
      <span class="gx-range" aria-live="polite"></span>
      <span class="gx-stale" hidden>newer records on disk <button type="button" class="gx-quiet" data-reload>reload</button></span>
      <label class="gx-search"><span class="sr-only">Find</span>
        <input type="search" placeholder="find: goal, tool, word…  ( / )" autocomplete="off" spellcheck="false"></label>
    </div>
    <div class="gx-kinds" role="group" aria-label="Show record kinds"></div>
    <div class="gx-stats"></div>
    <div class="gx-main">
      <div class="gx-view"></div>
      <button type="button" class="gx-backdrop" aria-label="Close details" hidden></button>
      <aside class="gx-inspector" aria-live="polite" tabindex="-1">
        <div class="gx-ins-head">
          <h2></h2>
          <div class="gx-ins-nav">
            <button type="button" class="gx-quiet" data-step="-1" title="Previous record (←)">←</button>
            <button type="button" class="gx-quiet" data-step="1" title="Next record (→)">→</button>
            <button type="button" class="gx-quiet" data-clear title="Clear selection (Esc)">Clear</button>
          </div>
        </div>
        <div class="gx-ins-meta"></div>
        <p class="gx-ins-why"></p>
        <div class="gx-ins-body"></div>
      </aside>
    </div>
    <p class="gx-keys"><kbd>1</kbd>–<kbd>6</kbd> views · <kbd>←</kbd><kbd>→</kbd> step · <kbd>/</kbd> find · <kbd>Esc</kbd> clear</p>`;
  const $ = (s) => root.querySelector(s);
  const els = {
    root, view: $(".gx-view"), range: $(".gx-range"), stale: $(".gx-stale"),
    search: $(".gx-search input"), kinds: $(".gx-kinds"), stats: $(".gx-stats"),
    inspector: $(".gx-inspector"), insTitle: $(".gx-ins-head h2"), insMeta: $(".gx-ins-meta"),
    insWhy: $(".gx-ins-why"), insBody: $(".gx-ins-body"), clear: $("[data-clear]"),
    backdrop: $(".gx-backdrop"),
  };
  els.stale.hidden = !ws.stale;
  els.search.value = ws.state.query;

  root.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", async () => {
    const days = Number(b.dataset.days);
    ws.state.days = days;
    ws.state.storyLimit = PAGE;
    ws.state.ledgerLimit = PAGE * 2;
    await reload();
  }));
  $("[data-reload]").addEventListener("click", () => reload({ keepRange: true }));
  let debounce = 0;
  els.search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      ws.state.query = els.search.value.trim().toLowerCase();
      ws.state.storyLimit = PAGE;
      ws.state.ledgerLimit = PAGE * 2;
      refresh();
    }, 140);
  });
  ws.onTeardown(() => clearTimeout(debounce));
  els.clear.addEventListener("click", () => select(null));
  els.backdrop.addEventListener("click", () => select(null));
  root.querySelectorAll("[data-step]").forEach((b) =>
    b.addEventListener("click", () => step(Number(b.dataset.step))));
  // One delegate for every jump and link the inspector and views draw.
  root.addEventListener("click", (ev) => {
    const jump = ev.target.closest("[data-jump]");
    if (jump && root.contains(jump)) { select(jump.dataset.jump); return; }
    const go = ev.target.closest("[data-go]");
    if (go && root.contains(go)) { ws.deps.go(go.dataset.go); return; }
    const desk = ev.target.closest("[data-desk]");
    if (desk) openDesk(desk);
  });

  ws.keyHandler = onKey;
  window.addEventListener("keydown", ws.keyHandler);
  return els;
}

async function reload({ keepRange = false } = {}) {
  const els = ws.els;
  if (els) els.view.replaceChildren(Object.assign(document.createElement("p"),
    { className: "gx-empty", textContent: "Reading her traces…" }));
  try {
    await load(ws.state.days, { keepRange });
  } catch (error) {
    ws.deps.toast(error?.message || "Could not read her traces.");
  }
  if (ws.els === els) refresh();
}

function refresh({ preserve = false } = {}) {
  const els = ws.els;
  if (!els) return;
  const oldList = els.view.querySelector(".gx-list");
  const scroll = preserve && oldList ? oldList.scrollTop : 0;
  const active = document.activeElement;
  const focusId = preserve && els.view.contains(active) ? active.dataset.id : null;
  const moreFocused = preserve && active?.classList.contains("gx-more");
  runTeardown();
  hideTooltip();
  ws.renderChrome();
  els.root.querySelectorAll("[data-days]").forEach((b) =>
    b.classList.toggle("on", Number(b.dataset.days) === ws.loadedDays));
  els.root.dataset.view = ws.section;
  els.view.replaceChildren();
  GRAPH_SECTIONS[ws.section].draw(els.view, ws);
  const list = els.view.querySelector(".gx-list");
  if (preserve && list) list.scrollTop = scroll;
  if (focusId) [...els.view.querySelectorAll("[data-id]")].find((n) => n.dataset.id === focusId)?.focus({ preventScroll: true });
  if (moreFocused) (els.view.querySelector(".gx-more") || list)?.focus({ preventScroll: true });
  renderInspector();
  writeHash();
}

function runTeardown() {
  for (const fn of ws.teardown.splice(0)) {
    try { fn(); } catch { /* a view already gone */ }
  }
  ws.redraw = null;
}

function clampRange() {
  const { tMin, tMax, PAD } = ws.model;
  const lo = tMin - PAD, hi = tMax + PAD;
  const span = clamp(Math.max(600, ws.state.t1 - ws.state.t0), 600, hi - lo);
  ws.state.t0 = clamp(ws.state.t0, lo, hi - span);
  ws.state.t1 = ws.state.t0 + span;
}

function setRange(t0, t1, { redraw = true } = {}) {
  ws.state.t0 = t0;
  ws.state.t1 = t1;
  clampRange();
  if (redraw) refresh();
  else { syncRange(); writeHash(); }
}

function syncRange() {
  if (!ws.els) return;
  const { t0, t1 } = ws.state;
  ws.els.range.textContent = `${fmtShort(t0)} → ${fmtShort(t1)} (${dur(t1 - t0)})`;
}

function renderKinds() {
  const counts = {};
  for (const e of ws.model.inRange(ws.state.t0, ws.state.t1)) {
    if (matches(e)) counts[e.kind] = (counts[e.kind] || 0) + 1;
  }
  const box = ws.els.kinds;
  box.replaceChildren();
  for (const [kind, label] of KINDS) {
    const on = ws.state.kinds[kind];
    const chip = document.createElement("span");
    chip.className = `gx-kind${on ? "" : " off"}`;
    chip.style.setProperty("--swatch", `var(--k-${kind})`);
    chip.innerHTML = `<button type="button" aria-pressed="${on}" title="${on ? "Hide" : "Show"} ${esc(label)}"><i></i>${esc(label)} <em>${fmt(counts[kind] || 0)}</em></button>`
      + `<button type="button" class="only" title="Show only ${esc(label)}">only</button>`;
    chip.firstChild.addEventListener("click", () => { ws.state.kinds[kind] = !on; refresh(); });
    chip.lastChild.addEventListener("click", () => setKinds([kind]));
    box.append(chip);
  }
  const all = document.createElement("button");
  all.type = "button";
  all.className = "gx-link";
  all.textContent = "all";
  all.addEventListener("click", () => setKinds(KINDS.map(([k]) => k)));
  const none = document.createElement("button");
  none.type = "button";
  none.className = "gx-link";
  none.textContent = "none";
  none.addEventListener("click", () => setKinds([]));
  box.append(all, none);
}

function setKinds(list) {
  const on = new Set(list);
  for (const [k] of KINDS) ws.state.kinds[k] = on.has(k);
  refresh();
}

function renderStats() {
  const inR = ws.model.inRange(ws.state.t0, ws.state.t1);
  const count = (k) => inR.reduce((n, e) => n + (e.kind === k ? 1 : 0), 0);
  let rest = 0;
  for (const b of ws.model.data.rest_density || []) {
    if (b.t + 3600 > ws.state.t0 && b.t <= ws.state.t1) rest += b.n;
  }
  let tokIn = 0, tokOut = 0;
  for (const e of inR) if (e.kind === "prompt") { tokIn += e.detail?.tokens_in || 0; tokOut += e.detail?.tokens_out || 0; }
  const bits = [
    ["decisions", count("tick"), "Ticks that chose something other than REST"],
    ["rest ticks", rest, "Heartbeats that rested, bucketed by hour"],
    ["messages", count("chat"), "Chat messages in both directions"],
    ["model calls", count("prompt"), "Every call to a model"],
    ["tokens", `${compact(tokIn)} → ${compact(tokOut)}`, "Tokens in → out across those calls"],
    ["hands", count("tool"), "Tool calls"],
    ["goals filed", count("goal"), "Goals created in this range"],
    ["journal", count("journal"), "[she] lines in her diary"],
  ];
  ws.els.stats.innerHTML = bits.map(([k, n, tip]) =>
    `<span class="gx-stat" title="${esc(tip)}"><b>${typeof n === "number" ? fmt(n) : esc(n)}</b> ${k}</span>`).join("")
    + `<span class="gx-stat dashed" title="The range every view shows">in ${esc(dur(ws.state.t1 - ws.state.t0))}</span>`;
}

// ------------------------------------------------------------- the selection

function reveal(id) {
  const e = ws.model.byId.get(id);
  if (!e || (e.t >= ws.state.t0 && e.t <= ws.state.t1)) return false;
  const span = ws.state.t1 - ws.state.t0;
  setRange(e.t - span / 2, e.t + span / 2, { redraw: false });
  return true;
}

function select(id, { reveal: move = true } = {}) {
  hideTooltip();
  const previous = ws.state.selected;
  ws.state.selected = id && ws.model.byId.has(id) ? id : null;
  // keep the shared range honest: a selection outside it moves the range
  const moved = ws.state.selected && move ? reveal(ws.state.selected) : false;
  const els = ws.els;
  if (!els) return;
  if (["ledger", "goals"].includes(ws.section) && !moved) {
    els.view.querySelectorAll("[data-id]").forEach((n) => n.classList.toggle("on", n.dataset.id === ws.state.selected));
    if (ws.section === "ledger" && ws.state.selected
        && ![...els.view.querySelectorAll("[data-id]")].some((n) => n.dataset.id === ws.state.selected)) {
      const position = ws.visible().reverse().findIndex((e) => e.id === ws.state.selected);
      if (position >= 0) { ws.state.ledgerLimit = Math.max(ws.state.ledgerLimit, position + 1); refresh({ preserve: true }); }
    }
    renderInspector();
    writeHash();
  } else if (ws.section === "stories" || !ws.redraw || moved) {
    refresh({ preserve: !moved });
  } else {
    ws.redraw();
    renderInspector();
    renderStats();
    writeHash();
  }
  if (previous !== ws.state.selected && ws.section !== "stories"
      && !document.body.classList.contains("gx-drawer-open")) {
    revealCard([...els.view.querySelectorAll("[data-id]")].find((n) => n.dataset.id === ws.state.selected));
  }
}

function step(dir) {
  const { events, indexOf, lowerBound } = ws.model;
  let i;
  if (!ws.state.selected) i = dir > 0 ? lowerBound(ws.state.t0) : lowerBound(ws.state.t1 + 1) - 1;
  else i = indexOf.get(ws.state.selected) + dir;
  // walk to the next record that passes the filters
  while (i >= 0 && i < events.length) {
    const e = events[i];
    if (ws.state.kinds[e.kind] && matches(e)) { select(e.id); return; }
    i += dir;
  }
  ws.toast(dir > 0 ? "That was the last one in the window." : "That was the first one in the window.");
}

function renderInspector() {
  const els = ws.els;
  const e = ws.state.selected && ws.model.byId.get(ws.state.selected);
  els.clear.hidden = !e;
  if (!e) {
    els.insTitle.textContent = "Right now";
    const cur = ws.model.meta.current_state || "—";
    els.insMeta.innerHTML = `<span class="gx-pill">${esc(cur)}</span><span class="muted">as of ${esc(fmtShort(ws.model.meta.generated_t))}</span>`;
    const { why, html } = snapshotHtml(ws.model);
    els.insWhy.textContent = why;
    els.insWhy.hidden = false;
    els.insBody.innerHTML = html;
  } else {
    els.insTitle.textContent = clip(e.title, 140);
    els.insMeta.innerHTML = metaHtml(e);
    const { why, html } = inspectHtml(e, ws.model, { pinnedStory: ws.state.pinnedStory });
    els.insWhy.textContent = why;
    els.insWhy.hidden = !why;
    els.insBody.innerHTML = html;
    els.inspector.scrollTop = 0;
  }
  syncDrawer();
}

/* On a narrow screen the inspector is a sheet over the page while something
 * is selected: focus goes into it, and comes back to where it was on close. */
function syncDrawer() {
  const els = ws.els;
  const narrow = matchMedia("(max-width: 1100px)").matches;
  const open = narrow && !!ws.state.selected;
  const wasOpen = document.body.classList.contains("gx-drawer-open");
  document.body.classList.toggle("gx-drawer-open", open);
  els.backdrop.hidden = !open;
  if (open) {
    els.inspector.setAttribute("role", "dialog");
    els.inspector.setAttribute("aria-modal", "true");
    if (!wasOpen) ws.drawerReturn = document.activeElement;
    if (!els.inspector.contains(document.activeElement)) els.clear.focus({ preventScroll: true });
  } else {
    els.inspector.removeAttribute("role");
    els.inspector.removeAttribute("aria-modal");
    if (wasOpen && ws.drawerReturn?.isConnected) ws.drawerReturn.focus({ preventScroll: true });
  }
}

async function openDesk(button) {
  const holder = button.closest(".gx-actions")?.nextElementSibling;
  if (!holder) return;
  if (holder.dataset.loaded) {
    holder.hidden = !holder.hidden;
    button.textContent = holder.hidden ? "read her desk file" : "fold it away";
    return;
  }
  button.disabled = true;
  button.textContent = "opening…";
  try {
    const file = await ws.deps.readFile(button.dataset.desk);
    holder.innerHTML = `<pre class="json">${esc(file.text || "(it is empty)")}</pre>`;
  } catch (error) {
    holder.innerHTML = `<p class="muted gx-small">${error?.status === 400 || error?.status === 404
      ? "She hasn't written this one up yet." : esc(error?.message || "Could not open that file.")}</p>`;
  } finally {
    holder.dataset.loaded = "1";
    holder.hidden = false;
    button.disabled = false;
    button.textContent = "fold it away";
  }
}

// ------------------------------------------------------------------ the keys

function onKey(ev) {
  if (!ws.els) return;
  const typing = ev.target instanceof HTMLElement && ev.target.matches("input, textarea, select, [contenteditable]");
  if (ev.key === "Escape") {
    if (typing && ev.target === ws.els.search && ws.els.search.value) {
      ws.els.search.value = "";
      ws.els.search.dispatchEvent(new Event("input"));
      return;
    }
    if (typing) { ev.target.blur(); return; }
    if (ws.state.selected) select(null);
    return;
  }
  if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
  if (ev.key === "/") { ev.preventDefault(); ws.els.search.focus(); ws.els.search.select(); }
  else if (ev.key === "ArrowLeft") { ev.preventDefault(); step(-1); }
  else if (ev.key === "ArrowRight") { ev.preventDefault(); step(1); }
  else {
    const target = Object.entries(GRAPH_SECTIONS).find(([, s]) => s.key === ev.key);
    if (target) ws.show(target[0]);
  }
}

// ---------------------------------------------------------------- the tooltip

let tooltipNode = null;

function showTooltip(e, cx, cy) {
  if (!e) { hideTooltip(); return; }
  if (!tooltipNode) {
    tooltipNode = document.createElement("div");
    tooltipNode.className = "gx-tooltip";
    tooltipNode.hidden = true;
    document.body.append(tooltipNode);
  }
  const t = tooltipNode;
  t.innerHTML = `<div class="gx-tt-k" style="--swatch:var(--k-${e.kind})"><i></i>${esc(KIND_LABEL[e.kind] || e.kind)} · ${esc(fmtShort(e.t))}</div>
    <div class="gx-tt-t">${esc(clip(e.title, 90))}</div>
    ${e.summary ? `<div class="gx-tt-s">${esc(clip(e.summary, 180))}</div>` : ""}`;
  t.hidden = false;
  const r = t.getBoundingClientRect();
  let x = cx + 14, y = cy + 14;
  if (x + r.width > window.innerWidth - 8) x = cx - r.width - 14;
  if (y + r.height > window.innerHeight - 8) y = cy - r.height - 14;
  t.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
}

function hideTooltip() {
  if (tooltipNode) tooltipNode.hidden = true;
}
