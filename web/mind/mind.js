/* The mind debug page (SPEC §24.3).
 *
 * Nine sections over one read-only API. Three rules hold throughout:
 *
 *  - Hash routing, so every view is a link you can paste and reload into
 *    (`#/prompts/2026-08-06`, `#/vault/a1b2c3`). A debug page you cannot point
 *    at is half a debug page.
 *  - Every list pages explicitly, newest first, matching the API. No infinite
 *    scroll: the reverse reader makes deep offsets cheap, and you came here to
 *    find one specific moment, not to browse.
 *  - Live updates arrive on the existing SSE bus, never a poll (AGENTS.md), and
 *    they only ever refresh the section you are looking at, and only on its
 *    first page — a page you are reading must not jump under you.
 */
import { element, errorMessage, showToast } from "../shared/dom.js";
import { debugApi, dreamApi } from "./api.js";

const { characterId, apiPath } = window.YuriOSRuntime;
const $ = (selector) => document.querySelector(selector);

const nodes = {
  rail: $("#rail"),
  title: $("#section-title"),
  note: $("#section-note"),
  body: $("#stage-body"),
  refresh: $("#refresh"),
  stateChip: $("#state-chip"),
  stateLabel: $("#state-label"),
  streamChip: $("#stream-chip"),
  streamLabel: $("#stream-label"),
  brandSub: $("#brand-sub"),
  toasts: $("#toast-region"),
};

const toast = (message, type = "error") => showToast(nodes.toasts, message, type);

/* The activity ladder's colours. Warm as she is engaged, cool as she drifts down
 * — the same ordering the cost ladder has (SPEC §17.1), so the band reads as
 * "how awake" without a legend. Always shown with the state's name beside it. */
const STATE_COLOR = {
  ENGAGED: "var(--acid)", IDLE: "var(--mint)",
  DORMANT: "var(--dim)", DREAM: "var(--amber)",
};

const state = { route: null, request: null, cache: new Map() };

// ---------------------------------------------------------------- small pieces

function chip(text, tone = "") {
  return element("span", { className: `chip ${tone}`.trim(), text: String(text) });
}

function relative(value) {
  const ms = typeof value === "number" ? value * 1000 : Date.parse(value);
  if (!Number.isFinite(ms)) return String(value ?? "");
  const seconds = Math.round((ms - Date.now()) / 1000);
  const steps = [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.35, "week"]];
  let amount = seconds;
  for (const [span, unit] of steps) {
    if (Math.abs(amount) < span) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(Math.round(amount), unit);
    }
    amount /= span;
  }
  return new Date(ms).toLocaleDateString();
}

function clock(value) {
  const ms = typeof value === "number" ? value * 1000 : Date.parse(value);
  return Number.isFinite(ms) ? new Date(ms).toLocaleString() : String(value ?? "—");
}

/** "Aug 7, 4:42:25 AM – 4:43:21 AM" for a collapsed stretch. Drops the date on
 *  the far edge when both ends fall on the same day. */
function clockRange(from, to) {
  if (to == null || to === from) return clock(from);
  const at = (v) => new Date(typeof v === "number" ? v * 1000 : Date.parse(v));
  const [start, end] = [at(from), at(to)];
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return clock(from);
  const far = start.toDateString() === end.toDateString()
    ? end.toLocaleTimeString() : end.toLocaleString();
  return `${clock(from)} – ${far}`;
}

const bytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
};

const number = (n) => (n == null ? "—" : Number(n).toLocaleString());

function json(value) {
  return element("pre", { className: "json", text: JSON.stringify(value, null, 2) });
}

function fold(summary, ...children) {
  return element("details", { className: "fold" },
    element("summary", { text: summary }), ...children);
}

function panel(title, body, ...headExtras) {
  return element("section", { className: "panel" },
    element("div", { className: "panel-head" },
      element("h2", { text: title }), ...headExtras),
    body);
}

function placeholder(text) {
  return element("div", { className: "placeholder", text });
}

/** One pager, used identically by every list. `total` is null on filtered views
 *  — counting a filtered log means a full pass, so the count reads "25+". */
function pager({ page, has_more: hasMore, total, limit }, onPage) {
  const button = (label, target, disabled) => {
    const node = element("button", {
      className: "button button-quiet", text: label,
      attrs: { type: "button", ...(disabled ? { disabled: "disabled" } : {}) },
    });
    if (!disabled) node.addEventListener("click", () => onPage(target));
    return node;
  };
  const pages = total != null ? Math.max(1, Math.ceil(total / (limit || 25))) : null;
  const status = pages ? `Page ${page + 1} of ${pages} · ${number(total)} rows`
    : `Page ${page + 1}${hasMore ? " · more below" : ""}`;
  return element("div", { className: "pager" },
    button("Newer", page - 1, page === 0),
    element("span", { className: "pager-status", text: status }),
    button("Older", page + 1, !hasMore));
}

function rows(items, render) {
  if (!items.length) return placeholder("Nothing recorded here yet.");
  return element("div", { className: "rows" }, ...items.map(render));
}

/** A table from headers + prebuilt <tr>s. Saves the deep paren nesting that
 *  hand-building thead/tbody inline turns into. */
function table(headers, bodyRows) {
  return element("table", { className: "grid-table" },
    element("thead", {}, element("tr", {},
      ...headers.map((text) => element("th", { text })))),
    element("tbody", {}, ...bodyRows));
}

function tile(label, value, sub) {
  return element("div", { className: "tile" },
    element("div", { className: "tile-label", text: label }),
    element("div", { className: "tile-value", text: String(value) }),
    sub ? element("div", { className: "tile-sub", text: sub }) : null);
}

function verdictTone(verdict = "") {
  if (verdict === "ok") return "ok";
  if (verdict.startsWith("denied")) return "warn";
  return "bad";
}

// --------------------------------------------------------------------- charts

/* A single-series line: tokens in the context window over time, against the
 * model's limit. One series needs no legend — the panel title names it — and the
 * limit is a labelled reference line rather than a second series on a second
 * axis, which would be unreadable and is never right. */
function lineChart(points, { limit, label = "tokens" } = {}) {
  const W = 720, H = 180, PAD = { top: 12, right: 46, bottom: 20, left: 44 };
  if (points.length < 2) return placeholder("Not enough measurements to plot yet.");
  const top = Math.max(limit || 0, ...points) * 1.08 || 1;
  const x = (i) => PAD.left + (i / (points.length - 1)) * (W - PAD.left - PAD.right);
  const y = (v) => H - PAD.bottom - (v / top) * (H - PAD.top - PAD.bottom);

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    `${label} over the last ${points.length} measurements, ending at ${points.at(-1)}`);
  const add = (tag, attrs, text) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    svg.append(node);
    return node;
  };

  for (let i = 0; i <= 2; i += 1) {           // a recessive grid: three lines, no more
    const value = (top / 2) * i;
    add("line", { class: "grid", x1: PAD.left, x2: W - PAD.right, y1: y(value), y2: y(value) });
    add("text", { class: "axis", x: PAD.left - 6, y: y(value) + 3, "text-anchor": "end" },
      value >= 1000 ? `${Math.round(value / 1000)}k` : String(Math.round(value)));
  }
  const line = points.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  add("path", { class: "area", d: `${line}L${x(points.length - 1)},${y(0)}L${x(0)},${y(0)}Z` });
  add("path", { class: "line", d: line });
  if (limit) {
    add("line", { class: "limit", x1: PAD.left, x2: W - PAD.right, y1: y(limit), y2: y(limit) });
    add("text", { class: "limit-label", x: W - PAD.right + 4, y: y(limit) + 3 }, "limit");
  }
  add("text", { class: "axis", x: PAD.left, y: H - 5 }, "oldest");
  add("text", { class: "axis", x: W - PAD.right, y: H - 5, "text-anchor": "end" }, "now");
  return svg;
}

/** A magnitude bar with its number always printed beside it. */
function meter(value, max, tone = "") {
  const bar = element("span", { className: `meter ${tone}`.trim() }, element("i"));
  bar.firstChild.style.width = `${max ? Math.max(2, (value / max) * 100) : 0}%`;
  return bar;
}

// ------------------------------------------------------------------- sections
// Each returns a node (or a promise of one). `ctx` carries the parsed route.

const SECTIONS = {
  overview: { title: "Overview", note: "What is on disk for this character, read without starting her.", render: renderOverview },
  timeline: { title: "State timeline", note: "Her activity ladder over time — every transition she actually made, and why.", render: renderTimeline },
  ticks: { title: "Tick traces", note: "One record per tick: what she sensed, how she appraised it, what she chose, and what it did.", render: renderTicks },
  dreams: { title: "Dreams", note: "The jobs that run at night, what each still owes, and a way to try one now.", render: renderDreams },
  context: { title: "Context windows", note: "Every prompt she was given — conversation, self-talk, goal work, dreams. Pick a day, then a call.", render: renderContext },
  tools: { title: "Tool calls", note: "Every call her hands made, allowed or denied, with the photo it produced.", render: renderTools },
  vault: { title: "Vault", note: "The files that are her mind, and the commit history of how they changed.", render: renderVault },
  memory: { title: "Memory", note: "What she remembers, what she was told to forget, and what is in the recall index.", render: renderMemory },
  signals: { title: "Signals, goals & edits", note: "Her inbox, her intentions, and the edits waiting on your ruling.", render: renderSignals },
  economics: { title: "What it costs", note: "Context pressure, the daily budget, and what the small model did with its answers.", render: renderEconomics },
};

// --- overview

async function renderOverview() {
  const data = await debugApi.overview();
  const out = [];

  const activity = data.activity || {};
  const budget = data.budget || {};
  const used = data.live?.context?.used;
  out.push(element("div", { className: "tiles" },
    tile("State", activity.state || "—",
      activity.cadence_s ? `every ${activity.cadence_s}s` : "never run"),
    tile("Last heard from you", activity.last_user_msg ? relative(activity.last_user_msg) : "—"),
    tile("Context now", used != null ? number(used) : "—",
      data.live ? `of ${number(data.live.context.limit)} tokens` : "she is not running"),
    tile("Spent today", number(budget.spent_tokens ?? 0), `${number(budget.calls ?? 0)} calls`),
    tile("Vault commits", number(data.vault?.commits ?? 0),
      data.vault?.head ? data.vault.head.slice(0, 8) : "not a repo yet")));

  const counts = data.counts || {};
  const fileRow = (file) => element("tr", {},
    element("td", {}, element("span", { className: "mono", text: file.path })),
    element("td", { className: "num", text: number(counts[file.name] ?? 0) }),
    element("td", { className: "num", text: bytes(file.bytes) }),
    element("td", { text: file.mtime ? relative(file.mtime) : "—" }),
    element("td", {}, file.rotated
      ? chip("rolled over", "warn")
      : element("span", { className: "muted", text: "—" })));
  out.push(panel("Records on disk", element("div", { className: "panel-body" },
    table(["Log", "Rows", "Size", "Last written", "Older records"],
      (data.files || []).map(fileRow)))));

  if ((data.files || []).some((f) => f.rotated)) {
    out.push(element("div", { className: "notice", text:
      "Some logs have rolled over. This page reads only the live file, so records "
      + "older than the last rotation are on disk (as .1) but are not shown here." }));
  }
  return element("div", { className: "stage-body" }, ...out);
}

// --- timeline

async function renderTimeline(ctx) {
  const data = await debugApi.activity(ctx.page);
  const items = data.items || [];
  const out = [];

  /* The band reads left-to-right oldest-to-newest, so reverse the newest-first
   * page. Each segment is as wide as she spent in that state, which is what
   * makes a night of DORMANT look like a night. */
  if (items.length > 1) {
    const ordered = [...items].reverse();
    const now = Date.now() / 1000;
    const spans = ordered.map((row, i) => ({
      ...row, span: Math.max(1, (ordered[i + 1]?.at ?? now) - row.at),
    }));
    const total = spans.reduce((sum, s) => sum + s.span, 0) || 1;
    const band = element("div", { className: "band" });
    for (const seg of spans) {
      const node = element("div", {
        className: "band-seg",
        attrs: { title: `${seg.to} — ${clock(seg.at)} (${seg.reason})` },
      });
      node.style.setProperty("--state-color", STATE_COLOR[seg.to] || "var(--dim)");
      node.style.flex = `${(seg.span / total) * 100} 0 auto`;
      band.append(node);
    }
    const key = element("div", { className: "band-key" });
    for (const name of [...new Set(spans.map((s) => s.to))]) {
      const entry = element("span", {}, element("i"), document.createTextNode(name));
      entry.firstChild.style.setProperty("--state-color", STATE_COLOR[name] || "var(--dim)");
      key.append(entry);
    }
    out.push(panel(`This page · oldest to newest`,
      element("div", { className: "panel-body" }, band, key)));
  }

  const list = rows(items, (row) => {
    const node = element("div", { className: "row" },
      element("div", { className: "row-top" },
        element("span", { className: "row-title", text: `${row.from || "start"} → ${row.to}` }),
        chip(row.reason || "tick"),
        element("span", { className: "row-time", text: clock(row.at ?? row.ts) })),
      element("div", { className: "row-body", text:
        `cadence ${row.cadence_s ?? "?"}s${row.last_user_msg
          ? ` · last heard from you ${relative(row.last_user_msg)}` : ""}` }));
    node.style.setProperty("--event-color", STATE_COLOR[row.to] || "var(--dim)");
    return node;
  });
  out.push(panel("Transitions", element("div", {}, list,
    pager(data, (page) => go(`#/timeline/${page}`)))));
  return element("div", { className: "stage-body" }, ...out);
}

// --- dreams

/* The one section with buttons on it.
 *
 * A dream job is a prompt you wrote that runs at 3am against yesterday, and
 * whose only visible output is a file that appears tomorrow. Iterating on one
 * that way takes a day per attempt. So this section is built around the
 * shortest loop that answers "what does this prompt actually do": pick a job,
 * pick a day, run it dry, read the raw completion. Nothing is written, the
 * ledger doesn't move, and you can press it again.
 *
 * Two other rules, both learned from the sections above:
 *   - The results replace only the results panel, never the page. A run you
 *     just triggered must not scroll the job you triggered it from off-screen.
 *   - The dry-run toggle defaults to ON. The wet run is the one that costs
 *     tokens and writes to her vault, and it should take a deliberate click.
 */
const dreamState = { day: "", dryRun: true, running: false, report: null };

function dreamJobCard(job, onRun) {
  const backlog = job.backlog || [];
  const head = element("div", { className: "row-top" },
    element("span", { className: "row-title", text: job.title || job.name }),
    chip(job.name),
    job.enabled ? chip("on", "ok") : chip("off", "warn"),
    job.per_day ? chip("per day") : chip("once a night"),
    element("span", { className: "row-time", text:
      job.last_run ? `last run ${relative(job.last_run)}` : "never run" }));

  const body = element("div", { className: "row-body" },
    element("div", { text: job.description || "" }),
    element("div", { className: "muted", text:
      `${backlog.length} day(s) owed${backlog.length
        ? ` — oldest ${backlog[0]}` : ""}`
      + ` · ${number(job.days ?? 0)} done`
      + (job.last_result ? ` · last: ${job.last_result}` : "") }));

  const run = element("button", {
    className: "button", text: "Test this job",
    attrs: { type: "button", ...(job.enabled ? {} : { disabled: "disabled" }) },
  });
  run.addEventListener("click", () => onRun(job.name));

  const node = element("div", { className: "row" }, head, body,
    element("div", { className: "row-actions" }, run));
  node.style.setProperty("--event-color",
    job.enabled ? "var(--amber)" : "var(--dim)");
  return node;
}

/** One model call, exactly as it went out and came back. The whole reason the
 *  button exists, so it is not folded away — you came here to read this. */
function dreamExchange(exchange) {
  return element("div", { className: "row" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: exchange.job }),
      chip("utility")),
    fold("System prompt",
      element("pre", { className: "json", text: exchange.system })),
    fold("Input",
      element("pre", { className: "json", text: exchange.user })),
    element("div", { className: "row-body" },
      element("div", { className: "muted", text: "Raw completion" }),
      element("pre", { className: "json", text: exchange.completion || "(empty)" })));
}

function dreamResults(report) {
  if (!report) return placeholder("No run yet. Test a job, or run the night.");
  const out = [];
  out.push(element("div", { className: "tiles" },
    tile("Outcome", report.nothing_to_do ? "nothing to do" : "ran",
      report.dry_run ? "dry run — nothing written" : "written and committed"),
    tile("Jobs", String((report.jobs || []).length),
      report.exhausted_budget ? "budget spent — backlog remains" : "budget held"),
    tile("Model calls", String((report.exchanges || []).length)),
    tile("Files", String((report.writes || []).length),
      report.dry_run ? "would have been written" : "written to her desk")));

  out.push(panel("What it did", element("div", {},
    rows(report.jobs || [], (job) => element("div", { className: "row" },
      element("div", { className: "row-top" },
        element("span", { className: "row-title", text: job.name }),
        job.failed ? chip("failed", "bad")
          : job.changed ? chip("wrote something", "ok") : chip("no-op"),
        element("span", { className: "row-time", text: (job.days || []).join(", ") })),
      element("div", { className: "row-body", text: job.failed || job.result }))))));

  if ((report.writes || []).length) {
    out.push(panel(report.dry_run ? "Files it would write" : "Files it wrote",
      element("div", { className: "panel-body" },
        element("ul", { className: "plain" }, ...report.writes.map(
          (path) => element("li", {}, element("span",
            { className: "mono", text: `workspace/${path}` })))))));
  }
  out.push(panel("The prompts", element("div", {},
    rows(report.exchanges || [], dreamExchange))));
  return element("div", {}, ...out);
}

async function renderDreams() {
  const data = await dreamApi.status();
  const out = [];
  const results = element("div", {}, dreamResults(dreamState.report));

  /* Every button on the section, disabled together while one run is in flight.
   * A dream job is one or more model calls against a local 12B — seconds, not
   * milliseconds — and two overlapping runs would interleave their reports. */
  // `render()` unwraps the .stage-body this function returns and adopts its
  // children, so by the time a click lands the buttons are directly under
  // #stage-body — not under anything this closure still holds a handle to.
  const buttons = () => nodes.body.querySelectorAll("button");
  const setBusy = (busy) => {
    dreamState.running = busy;
    for (const button of buttons()) button.disabled = busy;
  };

  const runDream = async (job) => {
    if (dreamState.running) return;
    setBusy(true);
    results.replaceChildren(
      placeholder(job ? `Running ${job}…` : "Running the night…"));
    try {
      dreamState.report = await dreamApi.run({
        job: job || undefined,
        day: dreamState.day || undefined,
        dry_run: dreamState.dryRun,
      });
      toast(dreamState.report.summary || "done", "ok");
    } catch (error) {
      dreamState.report = null;
      toast(errorMessage(error));
    } finally {
      // Only this panel: re-rendering the section would rebuild the controls
      // under the cursor that just clicked one of them, and lose the day you
      // typed into the box.
      results.replaceChildren(dreamResults(dreamState.report));
      setBusy(false);
    }
  };

  out.push(element("div", { className: "tiles" },
    tile("State now", data.state || "—",
      data.enabled ? "DREAM is on" : "DREAM is off for this character"),
    tile("Night window", `${String(data.window?.[0]).padStart(2, "0")}:00–`
      + `${String(data.window?.[1]).padStart(2, "0")}:00`,
      "she may enter DREAM from DORMANT in here"),
    tile("Days owed", String((data.backlog || []).length),
      (data.backlog || []).length ? `oldest ${data.backlog[0]}` : "nothing pending"),
    tile("Budget per tick", number(data.tick_budget), "tokens, shared by all jobs")));

  // --- the controls
  const dayInput = element("input", {
    className: "input", attrs: {
      type: "text", id: "dream-day", placeholder: "leave blank for the backlog",
      value: dreamState.day, "aria-label": "Day to run against (YYYY-MM-DD)",
    },
  });
  dayInput.addEventListener("input", () => { dreamState.day = dayInput.value.trim(); });

  const dryBox = element("input", {
    attrs: { type: "checkbox", id: "dream-dry", ...(dreamState.dryRun ? { checked: "checked" } : {}) },
  });
  dryBox.addEventListener("change", () => { dreamState.dryRun = dryBox.checked; });

  const runNight = element("button", { className: "button", text: "Run tonight's dream", attrs: { type: "button" } });
  runNight.addEventListener("click", () => runDream(null));

  out.push(panel("Try it", element("div", { className: "panel-body" },
    element("div", { className: "field-row" },
      element("label", { text: "Day", attrs: { for: "dream-day" } }), dayInput),
    element("div", { className: "field-row" },
      dryBox, element("label", {
        text: "Dry run — do the thinking, write nothing",
        attrs: { for: "dream-dry" } })),
    element("div", { className: "row-actions" }, runNight),
    element("p", { className: "muted", text:
      "A dry run makes the same model calls and shows you the same output, but "
      + "writes no files, marks no days done and leaves no commit. Turn it off "
      + "to let a run count." }))));

  out.push(panel("Jobs", element("div", {},
    rows(data.jobs || [], (job) => dreamJobCard(job, runDream)))));
  out.push(panel("Last run", results));
  return element("div", { className: "stage-body" }, ...out);
}

// --- ticks

/** A tick fires every few seconds, so an idle stretch is dozens of identical
 *  "REST / ENGAGED / rest" lines. Fold consecutive ticks whose whole summary
 *  matches into one row spanning the stretch; it still opens the newest tick
 *  in its range, and anything that differed stays its own row. */
function summariseTick(tick) {
  const acted = tick.acted || {};
  return {
    tick_id: tick.tick_id,
    ts: tick.ts,
    until: tick.ts,
    count: 1,
    title: tick.decided?.intention || "REST",
    activity: tick.activity_state || "?",
    outcome: (tick.interrupt || {}).outcome || "",
    failed: acted.what === "error",
    body: [acted.result,
      (tick.sensed || []).length ? `${tick.sensed.length} sensed` : null,
      (tick.appraised || []).length ? `${tick.appraised.length} appraised` : null]
      .filter(Boolean).join(" · ") || "nothing to do",
  };
}

const TICK_SAME = ["title", "activity", "outcome", "failed", "body"];

function collapseTicks(items) {
  const collapsed = [];
  for (const tick of items) {
    const row = summariseTick(tick);
    const last = collapsed[collapsed.length - 1];
    if (last && TICK_SAME.every((key) => last[key] === row[key])) {
      last.count += 1;
      last.until = row.ts;   // items arrive newest-first, so this is the older edge
    } else {
      collapsed.push(row);
    }
  }
  return collapsed;
}

async function renderTicks(ctx) {
  const data = await debugApi.ticks(ctx.page, { state: ctx.state, q: ctx.q });
  const wrap = element("div", { className: "stage-body" });

  const stateSelect = element("select", { attrs: { "aria-label": "Filter by activity state" } },
    element("option", { text: "any state", attrs: { value: "" } }),
    ...Object.keys(STATE_COLOR).map((name) => element("option", { text: name, attrs: { value: name } })));
  stateSelect.value = ctx.state || "";
  stateSelect.addEventListener("change", () =>
    go(`#/ticks/0${queryTail({ state: stateSelect.value, q: ctx.q })}`));

  const search = element("input", {
    attrs: { type: "search", placeholder: "search within tick records", value: ctx.q || "" },
  });
  search.addEventListener("change", () =>
    go(`#/ticks/0${queryTail({ state: ctx.state, q: search.value })}`));

  wrap.append(element("div", { className: "filters" }, stateSelect, search));

  const list = rows(collapseTicks(data.items || []), (row) => {
    const node = element("div", { className: "row clickable" },
      element("div", { className: "row-top" },
        element("span", { className: "row-title", text: row.title }),
        chip(row.activity),
        row.count > 1 ? chip(`×${row.count}`) : null,
        row.outcome ? chip(row.outcome, "accent") : null,
        row.failed ? chip("error", "bad") : null,
        element("span", { className: "row-time", text: clockRange(row.until, row.ts) })),
      element("div", { className: "row-body", text: row.body }));
    node.addEventListener("click", () => go(`#/ticks/detail/${encodeURIComponent(row.tick_id)}`));
    return node;
  });

  wrap.append(panel("Ticks", element("div", {}, list,
    pager(data, (page) => go(`#/ticks/${page}${queryTail({ state: ctx.state, q: ctx.q })}`)))));
  return wrap;
}

async function renderTickDetail(ctx) {
  const data = await debugApi.tick(ctx.id);
  const tick = data.tick || {};
  const wrap = element("div", { className: "stage-body" });

  wrap.append(backLink("All ticks", "#/ticks"));
  wrap.append(element("div", { className: "tiles" },
    tile("State", tick.activity_state || "—"),
    tile("Chose", tick.decided?.intention || "REST",
      (tick.decided?.runners_up || []).length
        ? `over ${tick.decided.runners_up.join(", ")}` : "nothing else was close"),
    tile("Did", tick.acted?.what || "—", tick.acted?.result || ""),
    tile("When", clock(tick.ts))));

  const phase = (name, value) => panel(name, element("div", { className: "panel-body" },
    Array.isArray(value) && !value.length
      ? element("p", { className: "muted", text: "nothing" }) : json(value)));
  wrap.append(phase("Sensed", tick.sensed || []));
  wrap.append(phase("Appraised", tick.appraised || []));
  if (Object.keys(tick.interrupt || {}).length) {
    wrap.append(phase("Interrupt decision (gate 2)", tick.interrupt));
  }

  /* What the correlation id is for: before it, lining a tool call up with the
   * tick that decided on it meant comparing timestamps across two files that
   * stamped time in different units. */
  if ((data.signals || []).length) {
    wrap.append(panel("Signals it drained", rows(data.signals, signalRow)));
  }
  if ((data.calls || []).length) {
    wrap.append(panel("Tools it used", rows(data.calls, callRow)));
  }
  if ((data.prompts || []).length) {
    wrap.append(panel("Models it asked", rows(data.prompts, promptRow)));
  }
  return wrap;
}

// --- context windows

async function renderContext(ctx) {
  if (ctx.id) return renderPromptDetail(ctx);
  if (!ctx.day) return renderPromptDays(ctx);

  const data = await debugApi.prompts(ctx.page, { day: ctx.day, kind: ctx.kind });
  const wrap = element("div", { className: "stage-body" });
  wrap.append(backLink("All days", "#/context"));

  const kinds = element("select", { attrs: { "aria-label": "Filter by call kind" } },
    element("option", { text: "every kind", attrs: { value: "" } }),
    ...["chat_turn", "ambient", "greeting", "compose", "utility", "dream", "goal_work", "knowledge"]
      .map((k) => element("option", { text: k, attrs: { value: k } })));
  kinds.value = ctx.kind || "";
  kinds.addEventListener("change", () =>
    go(`#/context/${ctx.day}/0${queryTail({ kind: kinds.value })}`));
  wrap.append(element("div", { className: "filters" }, kinds));

  wrap.append(panel(ctx.day, element("div", {},
    rows(data.items || [], promptRow),
    pager(data, (page) => go(`#/context/${ctx.day}/${page}${queryTail({ kind: ctx.kind })}`)))));
  return wrap;
}

async function renderPromptDays(ctx) {
  const data = await debugApi.promptDays(ctx.page);
  const list = rows(data.items || [], (day) => {
    const node = element("div", { className: "row clickable" },
      element("div", { className: "row-top" },
        element("span", { className: "row-title", text: day.day }),
        element("span", { className: "row-time", text: `${number(day.count)} calls` })),
      element("div", { className: "row-body" },
        ...Object.entries(day.kinds || {}).flatMap(([kind, n]) =>
          [chip(`${kind} ${n}`), document.createTextNode(" ")])));
    node.addEventListener("click", () => go(`#/context/${day.day}/0`));
    return node;
  });
  return element("div", { className: "stage-body" },
    panel("Days", element("div", {}, list, pager(data, (page) => go(`#/context/page/${page}`)))));
}

function signalRow(signal) {
  return element("div", { className: "row" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: signal.type }),
      chip(signal.source || "host"),
      element("span", { className: "row-time", text: clock(signal.ts) })),
    element("div", { className: "row-body mono", text: JSON.stringify(signal.payload || {}) }));
}

function promptRow(row) {
  const node = element("div", { className: "row clickable" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: row.kind || "call" }),
      row.model ? chip(row.model) : null,
      row.truncated ? chip("truncated", "warn") : null,
      element("span", { className: "row-time", text: clock(row.at ?? row.ts) })),
    element("div", { className: "row-body", text: row.preview || "" }),
    element("div", { className: "row-body mono muted", text:
      [`${number(row.n_messages)} messages`,
        row.tokens_in != null ? `~${number(row.tokens_in)} in` : null,
        row.tokens_out != null ? `~${number(row.tokens_out)} out` : null].filter(Boolean).join(" · ") }));
  if (row.id) node.addEventListener("click", () => go(`#/context/prompt/${encodeURIComponent(row.id)}`));
  return node;
}

async function renderPromptDetail(ctx) {
  const row = await debugApi.prompt(ctx.id);
  const wrap = element("div", { className: "stage-body" });
  wrap.append(backLink("Back", row.ts ? `#/context/${String(row.ts).slice(0, 10)}/0` : "#/context"));

  wrap.append(element("div", { className: "tiles" },
    tile("Kind", row.kind || "—"),
    tile("Model", row.model || "—", row.template_version || ""),
    tile("Tokens in", row.tokens_in != null ? `~${number(row.tokens_in)}` : "—",
      row.tokens_out != null ? `~${number(row.tokens_out)} out` : ""),
    tile("When", clock(row.at ?? row.ts))));

  if (row.corr_id || row.tick_id) {
    const links = element("div", { className: "filters" });
    if (row.tick_id) {
      const tick = element("button", { className: "button button-quiet", text: `Tick ${row.tick_id}` });
      tick.addEventListener("click", () => go(`#/ticks/detail/${encodeURIComponent(row.tick_id)}`));
      links.append(tick);
    }
    if (row.corr_id) {
      const tools = element("button", { className: "button button-quiet", text: "Tools from this call" });
      tools.addEventListener("click", () => go(`#/tools/0${queryTail({ corr_id: row.corr_id })}`));
      links.append(tools);
    }
    wrap.append(links);
  }

  const messages = row.messages || [];
  if (!messages.length) {
    wrap.append(placeholder("This record carries no messages."));
  } else {
    const stack = element("div", {});
    messages.forEach((message, index) => {
      const content = message.content || "";
      // The system block is the bulk of every prompt — the whole persona, USER.md
      // and every recalled memory. Collapsed by default or you never see the
      // conversation underneath it.
      const open = !(message.role === "system" && index === 0);
      const node = element("details", {
        className: "msg", attrs: { "data-role": message.role, ...(open ? { open: "" } : {}) },
      },
        element("summary", { className: "msg-head" },
          element("span", { text: message.role }),
          element("span", { className: "grow", text: `${number(content.length)} chars` })),
        element("div", { className: "msg-body", text: content }));
      stack.append(node);
    });
    wrap.append(panel(row.resolved_from
      ? `Assembled context · resolved from ${row.resolved_from}`
      : "Assembled context", element("div", { className: "panel-body" }, stack)));
  }
  if (row.completion) {
    wrap.append(panel("What she said back",
      element("div", { className: "panel-body" },
        element("div", { className: "msg-body", text: row.completion }))));
  }
  wrap.append(fold("The whole record", json(row)));
  return wrap;
}

// --- tools

async function renderTools(ctx) {
  const data = await debugApi.calls(ctx.page, { corr_id: ctx.corr_id, verdict: ctx.verdict });
  const wrap = element("div", { className: "stage-body" });
  if (ctx.corr_id) {
    wrap.append(element("div", { className: "filters" },
      element("span", { className: "chip accent", text: `one turn · ${ctx.corr_id}` }),
      linkButton("Show all calls", "#/tools/0")));
  }
  wrap.append(panel("Calls", element("div", {},
    rows(data.items || [], callRow),
    pager(data, (page) => go(`#/tools/${page}${queryTail({ corr_id: ctx.corr_id })}`)))));
  return wrap;
}

function callRow(call) {
  const node = element("div", { className: "row" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: call.tool || "tool" }),
      chip(call.verdict || "?", verdictTone(call.verdict || "")),
      call.origin ? chip(call.origin) : null,
      element("span", { className: "row-time", text: `${clock(call.ts)} · ${Math.round(call.duration_ms || 0)}ms` })),
    element("div", { className: "row-body mono", text: JSON.stringify(call.args || {}) }),
    call.result ? element("div", { className: "row-body muted mono", text: call.result }) : null);
  if (call.selfie) {
    const image = element("img", {
      className: "thumb",
      attrs: { src: call.selfie.url, alt: call.selfie.prompt || "the photo this call produced", loading: "lazy" },
    });
    node.append(image, fold("How it was rendered", json(call.selfie)));
  }
  if (call.tick_id) {
    node.append(linkButton(`Tick ${call.tick_id}`, `#/ticks/detail/${encodeURIComponent(call.tick_id)}`));
  }
  if (call.verdict && call.verdict !== "ok") node.style.setProperty("--event-color", "var(--red)");
  return node;
}

// --- vault

async function renderVault(ctx) {
  if (ctx.sha) return renderCommit(ctx);
  if (ctx.file) return renderVaultFile(ctx);

  const [commits, tree] = await Promise.all([
    debugApi.commits(ctx.page), debugApi.tree(ctx.path || ""),
  ]);
  const wrap = element("div", { className: "stage-body" });
  const split = element("div", { className: "split" });

  const crumbs = element("div", { className: "crumbs" });
  const parts = (ctx.path || "").split("/").filter(Boolean);
  const root = element("button", { text: "vault", attrs: { type: "button" } });
  root.addEventListener("click", () => go("#/vault"));
  crumbs.append(root);
  parts.forEach((part, i) => {
    crumbs.append(document.createTextNode(" / "));
    const step = element("button", { text: part, attrs: { type: "button" } });
    step.addEventListener("click", () => go(`#/vault/tree/${parts.slice(0, i + 1).join("/")}`));
    crumbs.append(step);
  });

  const entries = (tree.entries || []).map((entry) => {
    // USER.md first: it is the file people come to this page to read.
    const pinned = entry.path === "soul/USER.md";
    const node = element("div", {
      className: `tree-row ${entry.dir ? "dir" : ""} ${pinned ? "pinned" : ""}`.trim(),
    },
      element("span", { text: entry.dir ? `${entry.name}/` : entry.name }),
      element("span", { className: "grow", text: entry.dir ? "" : bytes(entry.bytes) }));
    node.addEventListener("click", () => go(entry.dir
      ? `#/vault/tree/${entry.path}` : `#/vault/file/${entry.path}`));
    return { pinned, node };
  });
  entries.sort((a, b) => Number(b.pinned) - Number(a.pinned));

  split.append(
    panel("Files", element("div", {},
      element("div", { className: "panel-body" }, crumbs),
      entries.length ? element("div", { className: "rows" }, ...entries.map((e) => e.node))
        : placeholder("Nothing here."))),
    panel("Commits", element("div", {},
      rows(commits.items || [], commitRow),
      pager(commits, (page) => go(`#/vault/commits/${page}`)))));
  wrap.append(split);
  return wrap;
}

function commitRow(commit) {
  const node = element("div", { className: "row clickable" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: commit.subject }),
      element("span", { className: "row-time", text: relative(commit.at) })),
    element("div", { className: "row-body mono muted", text:
      `${commit.short} · ${commit.files.length} file(s) · +${commit.insertions} −${commit.deletions}` }));
  node.addEventListener("click", () => go(`#/vault/commit/${commit.sha}`));
  return node;
}

async function renderCommit(ctx) {
  const commit = await debugApi.commit(ctx.sha);
  const wrap = element("div", { className: "stage-body" });
  wrap.append(backLink("All commits", "#/vault"));
  wrap.append(element("div", { className: "tiles" },
    tile("Subject", commit.subject),
    tile("When", clock(commit.at), commit.author),
    tile("Changed", `${commit.files.length} file(s)`,
      `+${commit.insertions} −${commit.deletions}`)));
  wrap.append(panel("Files", rows(commit.files, (file) => {
    const node = element("div", { className: "row clickable" },
      element("div", { className: "row-top" },
        element("span", { className: "row-title mono", text: file.path }),
        element("span", { className: "row-time", text: file.binary
          ? "binary" : `+${file.insertions} −${file.deletions}` })));
    node.addEventListener("click", () => go(`#/vault/file/${file.path}`));
    return node;
  })));
  if (commit.truncated) {
    wrap.append(element("div", { className: "notice", text:
      "This patch is too large to show whole; what follows is the first part of it." }));
  }
  wrap.append(panel("Diff", diffView(commit.diff || "")));
  return wrap;
}

function diffView(text) {
  const pre = element("pre", { className: "diff" });
  for (const line of text.split("\n")) {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "meta"
      : line.startsWith("@@") ? "hunk"
        : line.startsWith("+") ? "add"
          : line.startsWith("-") ? "del"
            : line.startsWith("diff ") || line.startsWith("index ") ? "meta" : "";
    pre.append(element("span", { className: cls, text: `${line}\n` }));
  }
  return pre;
}

async function renderVaultFile(ctx) {
  const [file, history] = await Promise.all([
    debugApi.file(ctx.file, ctx.rev), debugApi.fileHistory(ctx.file),
  ]);
  const wrap = element("div", { className: "stage-body" });
  wrap.append(backLink("Back to the vault", "#/vault"));
  wrap.append(element("div", { className: "filters" },
    element("span", { className: "chip accent", text: ctx.file }),
    chip(ctx.rev ? `at ${ctx.rev.slice(0, 8)}` : "as it is now"),
    ctx.rev ? linkButton("Current version", `#/vault/file/${ctx.file}`) : null));
  /* History first, contents second. "What changed in USER.md, and when" is the
   * question this page exists to answer; the file itself is the follow-up. It
   * also keeps the history reachable — the contents pane scrolls internally, so
   * anything under it is easy to scroll straight past. */
  const historyRow = (commit) => {
    const asItWas = element("button", {
      className: "button button-quiet", text: "as it was then", attrs: { type: "button" },
    });
    asItWas.addEventListener("click", (event) => {
      event.stopPropagation();
      go(`#/vault/file/${ctx.file}?rev=${commit.sha}`);
    });
    const node = element("div", { className: "row clickable" },
      element("div", { className: "row-top" },
        element("span", { className: "row-title", text: commit.subject }),
        element("span", { className: "row-time", text: relative(commit.at) })),
      element("div", { className: "row-body mono muted", text:
        `${commit.short} · +${commit.insertions} −${commit.deletions}` }),
      element("div", { className: "filters" }, asItWas));
    // the row itself opens the diff: the edit, not the outcome
    node.addEventListener("click", () => go(`#/vault/commit/${commit.sha}`));
    return node;
  };
  wrap.append(panel("Every edit to this file", rows(history.items || [], historyRow)));
  wrap.append(panel(ctx.rev ? `Contents at ${ctx.rev.slice(0, 8)}` : "Contents as it is now",
    element("div", { className: "panel-body" },
      element("div", { className: "msg-body", text: file.text || "(empty)" }))));
  return wrap;
}

// --- memory

async function renderMemory(ctx) {
  const [memory, chunks] = await Promise.all([
    debugApi.memory(), debugApi.chunks(ctx.page, { q: ctx.q }),
  ]);
  const wrap = element("div", { className: "stage-body" });
  const index = memory.chunks || {};

  wrap.append(element("div", { className: "tiles" },
    tile("Indexed passages", index.available ? number(index.count) : "—",
      index.available ? (index.embedder_id || "") : "index not built"),
    tile("Journal days", number((memory.journal_days || []).length)),
    tile("Beliefs", number((memory.beliefs || []).length)),
    tile("On the shelf", number((memory.knowledge || []).length))));

  const search = element("input", {
    attrs: { type: "search", placeholder: "search remembered passages", value: ctx.q || "" },
  });
  search.addEventListener("change", () => go(`#/memory/0${queryTail({ q: search.value })}`));
  wrap.append(element("div", { className: "filters" }, search));

  wrap.append(panel("Recall index", element("div", {},
    chunks.available === false && !chunks.error
      ? placeholder("The recall index has not been built yet (it is derived; scripts/reindex.py rebuilds it).")
      : rows(chunks.items || [], (row) => element("div", { className: "row" },
        element("div", { className: "row-top" },
          element("span", { className: "row-title mono", text: row.id }),
          chip(row.kind || "?"),
          element("span", { className: "row-time", text: relative(row.created_at) })),
        element("div", { className: "row-body", text: row.text }),
        element("div", { className: "row-body mono muted", text:
          `${row.source_path || "?"} · ${row.source_span || "?"} · salience ${row.salience}` }))),
    pager(chunks, (page) => go(`#/memory/${page}${queryTail({ q: ctx.q })}`)))));

  const text = (title, body) => body
    ? panel(title, element("div", { className: "panel-body" },
      element("div", { className: "msg-body", text: body })))
    : null;
  wrap.append(text("The rolling summary", memory.summary));
  wrap.append(text("What she is sure of", memory.facts));
  wrap.append(text("What she was told to forget", memory.forgotten));
  wrap.append(text("The situation, as she sees it", memory.situation));
  const beliefRow = (b) => element("div", { className: "row" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: b.subject || b.belief || "belief" }),
      chip(b.kind || "belief"),
      element("span", { className: "row-time", text: relative(b.ts) })),
    element("div", { className: "row-body", text:
      `${b.belief || ""}${b.confidence != null ? ` (confidence ${b.confidence})` : ""}` }));
  if ((memory.beliefs || []).length) {
    wrap.append(panel("Beliefs and expectations", rows(memory.beliefs, beliefRow)));
  }
  return wrap;
}

// --- signals, goals, self-edits

async function renderSignals(ctx) {
  const [signals, goals, edits] = await Promise.all([
    debugApi.signals(ctx.page), debugApi.goals(), debugApi.selfEdits(),
  ]);
  const wrap = element("div", { className: "stage-body" });

  if ((edits.pending || []).length) {
    wrap.append(panel("Edits waiting on your ruling",
      rows(edits.pending, (edit) => element("div", { className: "row" },
        element("div", { className: "row-top" },
          element("span", { className: "row-title mono", text: edit.surface || edit.id }),
          chip("waiting", "warn"),
          element("span", { className: "row-time", text: relative(edit.proposed_at) })),
        element("div", { className: "row-body", text: edit.reason || "" }),
        fold("What she wants to write", json(edit.content))))));
    wrap.append(element("div", { className: "notice", text:
      "This page is read-only. Approve or reject these from the inner-life panel "
      + "in her room, where the decision goes through the mind as a signal." }));
  }

  const goalRow = (goal) => element("div", { className: "row" },
    element("div", { className: "row-top" },
      element("span", { className: "row-title", text: goal.text }),
      chip(goal.state || "pending", goal.state === "done" ? "ok"
        : goal.state === "abandoned" ? "bad" : ""),
      chip(goal.kind || "task"),
      element("span", { className: "row-time", text: goal.due ? `due ${relative(goal.due)}` : "" })),
    element("div", { className: "row-body mono muted", text:
      `priority ${goal.priority} · ${goal.commitment || "single-minded"} · from ${goal.provenance || "?"}` }));
  wrap.append(panel("Goals", rows(goals.items || [], goalRow)));

  wrap.append(panel("Signal inbox", element("div", {},
    rows(signals.items || [], signalRow),
    pager(signals, (page) => go(`#/signals/${page}`)))));

  if ((edits.history || []).length) {
    wrap.append(panel("Changes to who she is", rows(edits.history, commitRow)));
  }
  return wrap;
}

// --- economics

async function renderEconomics() {
  const data = await debugApi.economics();
  const wrap = element("div", { className: "stage-body" });
  const history = data.context || [];
  const budget = data.budget || {};
  const limit = history.at(-1)?.limit;

  wrap.append(element("div", { className: "tiles" },
    tile("Spent today", number(budget.spent_tokens ?? 0),
      budget.date ? `on ${budget.date}` : ""),
    tile("Calls today", number(budget.calls ?? 0)),
    tile("Last context", history.length ? number(history.at(-1).used) : "—",
      limit ? `of ${number(limit)}` : ""),
    tile("Applied", number(data.utility?.applied ?? 0), "memory writes that landed"),
    tile("Quarantined", number(data.utility?.quarantined ?? 0), "held back for review")));

  wrap.append(panel("Context window over time",
    element("div", { className: "panel-body" },
      lineChart(history.map((row) => row.used || 0), { limit, label: "context tokens" }))));

  /* By-kind spend as a table with a magnitude bar, not eight coloured series on
   * one axis: on a debug page you want the exact number, and a categorical
   * palette that large is unreadable for anyone. */
  const kinds = Object.entries(data.by_kind || {})
    .sort((a, b) => (b[1].tokens_in + b[1].tokens_out) - (a[1].tokens_in + a[1].tokens_out));
  const worst = Math.max(1, ...kinds.map(([, v]) => v.tokens_in + v.tokens_out));
  const spendRow = ([kind, value]) => element("tr", {},
    element("td", { text: kind }),
    element("td", { className: "num", text: number(value.calls) }),
    element("td", { className: "num", text: number(value.tokens_in) }),
    element("td", { className: "num", text: number(value.tokens_out) }),
    element("td", {}, meter(value.tokens_in + value.tokens_out, worst)));
  wrap.append(panel("Where the tokens went", element("div", { className: "panel-body" },
    kinds.length
      ? table(["Kind of call", "Calls", "Tokens in", "Tokens out", "Share"],
        kinds.map(spendRow))
      : placeholder("No model calls recorded yet."))));

  const byKind = Object.entries(data.utility?.by_kind || {});
  if (byKind.length) {
    const utilityRow = ([kind, value]) => element("tr", {},
      element("td", { text: kind }),
      element("td", { className: "num", text: number(value.total) }),
      element("td", { className: "num" }, chip(String(value.applied), "ok")),
      element("td", { className: "num" },
        chip(String(value.quarantined), value.quarantined ? "warn" : "")));
    wrap.append(panel("What the small model produced",
      element("div", { className: "panel-body" },
        table(["Kind", "Total", "Applied", "Quarantined"], byKind.map(utilityRow)))));
  }
  return wrap;
}

// ------------------------------------------------------------------- plumbing

function backLink(label, href) {
  return element("div", { className: "filters" }, linkButton(label, href, "back"));
}

function linkButton(label, href, iconName = "arrow") {
  const node = element("button", { className: "button button-quiet", attrs: { type: "button" } },
    iconName === "back" ? svgIcon("back") : null,
    element("span", { text: label }),
    iconName === "arrow" ? svgIcon("arrow") : null);
  node.addEventListener("click", () => go(href));
  return node;
}

function svgIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function queryTail(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) search.set(key, value);
  const out = search.toString();
  return out ? `?${out}` : "";
}

/* `#/section/rest?filters`. Kept deliberately dumb — the shapes are few and a
 * router would be more code than the routes. */
function parseRoute(hash) {
  const [pathPart, queryPart] = (hash || "").replace(/^#\/?/, "").split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const params = Object.fromEntries(new URLSearchParams(queryPart || ""));
  const section = parts[0] && SECTIONS[parts[0]] ? parts[0] : "overview";
  const ctx = { section, page: 0, ...params };

  const rest = parts.slice(1);
  if (section === "ticks") {
    if (rest[0] === "detail") ctx.id = rest[1];
    else ctx.page = Number(rest[0]) || 0;
  } else if (section === "context") {
    if (rest[0] === "prompt") ctx.id = rest[1];
    else if (rest[0] === "page") ctx.page = Number(rest[1]) || 0;
    else if (rest[0]) { ctx.day = rest[0]; ctx.page = Number(rest[1]) || 0; }
  } else if (section === "vault") {
    if (rest[0] === "commit") ctx.sha = rest[1];
    else if (rest[0] === "file") ctx.file = rest.slice(1).join("/");
    else if (rest[0] === "tree") ctx.path = rest.slice(1).join("/");
    else if (rest[0] === "commits") ctx.page = Number(rest[1]) || 0;
  } else {
    ctx.page = Number(rest[0]) || 0;
  }
  return ctx;
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function renderer(ctx) {
  if (ctx.section === "ticks" && ctx.id) return renderTickDetail;
  return SECTIONS[ctx.section].render;
}

async function render() {
  const ctx = parseRoute(location.hash);
  state.route = ctx;
  const meta = SECTIONS[ctx.section];
  nodes.title.textContent = meta.title;
  nodes.note.textContent = meta.note;
  for (const item of nodes.rail.querySelectorAll(".rail-item")) {
    item.classList.toggle("on", item.dataset.section === ctx.section);
  }
  nodes.body.replaceChildren(placeholder("Reading from disk…"));

  state.request?.abort();
  state.request = new AbortController();
  const mine = state.request;
  try {
    const node = await renderer(ctx)(ctx);
    if (mine.signal.aborted) return;
    nodes.body.replaceChildren(...(node.className === "stage-body" ? [...node.children] : [node]));
  } catch (error) {
    if (error?.name === "AbortError") return;
    const retry = element("button", { className: "button", text: "Try again", attrs: { type: "button" } });
    retry.addEventListener("click", () => render());
    nodes.body.replaceChildren(element("div", { className: "error" },
      document.createTextNode(errorMessage(error)), element("br"), retry));
  }
}

/** The header chip. Read from disk on load, then kept current by the bus. */
async function syncHeader() {
  try {
    const data = await debugApi.overview();
    setState(data.activity?.state);
  } catch { /* the chip is decoration; the page below it reports its own errors */ }
}

function setState(name) {
  if (!name) return;
  nodes.stateLabel.textContent = name;
  nodes.stateChip.style.setProperty("--state-color", STATE_COLOR[name] || "var(--dim)");
}

/* One subscription to the existing event bus (world/routes/events.py). No new
 * polling path and no new event type: `mind` already carries her state, so the
 * chip and the timeline follow her without this page asking repeatedly.
 *
 * A refresh only ever touches the section in view, and only on its first page —
 * otherwise a list you are halfway through reading would rearrange itself. */
function subscribe() {
  const stream = new EventSource(apiPath("/api/events"));
  const live = (on) => {
    nodes.streamChip.classList.toggle("live", on);
    nodes.streamLabel.textContent = on ? "live" : "offline";
  };
  stream.addEventListener("open", () => live(true));
  stream.addEventListener("error", () => live(false));
  stream.addEventListener("message", (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    // The hub flattens its payload onto the envelope (world/hub.py), so a `mind`
    // event is {type, state, tick, intention} — no nested `data`.
    if (payload?.type === "mind") {
      const next = payload.state;
      const changed = next && next !== nodes.stateLabel.textContent;
      setState(next);
      // Only when the ladder actually moved: `mind` fires every tick, and
      // re-rendering the page two seconds apart forever is exactly the polling
      // this is supposed to replace.
      if (changed && ["overview", "timeline"].includes(state.route?.section)
          && !state.route?.page) render();
    }
    if (payload?.type === "journal" && state.route?.section === "context"
        && !state.route?.page && !state.route?.id) render();
  });
}

function boot() {
  if (!characterId) {
    nodes.body.replaceChildren(element("div", { className: "error", text:
      "This page needs a character in its URL: /characters/<id>/mind" }));
    return;
  }
  nodes.brandSub.textContent = `mind · ${characterId}`;
  document.title = `YuriOS / ${characterId} / mind`;
  nodes.refresh.addEventListener("click", () => { syncHeader(); render(); });
  window.addEventListener("hashchange", render);
  if (!location.hash) location.hash = "#/overview";
  syncHeader();
  render();
  subscribe();
}

boot();
