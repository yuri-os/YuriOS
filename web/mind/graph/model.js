/* The joined graph, indexed (SPEC §24.4). Pure: no DOM, so it is testable.
 *
 * The server (world/debug_graph.py) has already done the joining — events,
 * links with their confidence and reason, stories. This module only indexes
 * what came back so the views can ask cheap questions of it: what is in this
 * range, what is joined to this record, which story is it part of.
 */

/* Record kinds, in the order the filters list them. Colour identifies the kind
 * on the joined views, and never alone: every mark also has a shape, a lane,
 * and a label beside it in the legend, the tooltip and the inspector. */
export const KINDS = Object.freeze([
  ["chat", "Chat"],
  ["goal", "Goals"],
  ["tick", "Decisions"],
  ["prompt", "Model calls"],
  ["utility", "Utility"],
  ["tool", "Hands"],
  ["signal", "Signals"],
  ["journal", "Journal"],
  ["selfie", "Camera"],
  ["activity", "State changes"],
  ["commit", "Vault commits"],
]);
export const KIND_LABEL = Object.freeze(Object.fromEntries(KINDS));

export const LANES = Object.freeze([
  ["activity", "State"],
  ["chat", "Chat"],
  ["signals", "Signals"],
  ["goals", "Goals"],
  ["ticks", "Decisions"],
  ["prompts", "Model"],
  ["utility", "Utility"],
  ["tools", "Hands"],
  ["selfies", "Camera"],
  ["journal", "Journal"],
  ["commits", "Vault"],
]);
export const LANE_INDEX = Object.freeze(Object.fromEntries(LANES.map(([id], i) => [id, i])));

export const REL_LABEL = Object.freeze({
  tick: "same tick", corr: "same corr_id", sense: "sensed", goal: "goal",
  turn: "wrote this reply", said: "led to this reach-out", wrote: "journal line",
  commit: "committed by",
});

const NOUN = {
  chat: "message", goal: "goal", tick: "decision", prompt: "model call", utility: "utility run",
  tool: "tool call", signal: "signal", journal: "journal line", selfie: "photo",
  activity: "state change", commit: "commit",
};
export const noun = (kind, n) => { const w = NOUN[kind] || kind; return n === 1 ? w : `${w}s`; };

/* The windows the server is asked for, in days (0: everything retained). The
 * range you pan and zoom to on the Timeline is always inside the loaded one. */
export const WINDOWS = Object.freeze([[1, "Day"], [7, "Week"], [30, "Month"], [0, "All"]]);

export function createModel(data) {
  const meta = data.meta || {};
  const events = (data.events || []).filter((e) => Number.isFinite(e.t))
    .sort((a, b) => a.t - b.t || (a.id < b.id ? -1 : 1));
  const byId = new Map(events.map((e) => [e.id, e]));
  const indexOf = new Map(events.map((e, i) => [e.id, i]));
  const goals = data.goals || [];
  const goalsById = new Map(goals.map((g) => [g.id, g]));
  const links = data.links || [];

  const range = meta.range || [];
  const coverage = [...events.map((e) => e.t), ...range,
    ...(data.rest_density || []).flatMap((b) => [b.first_t ?? b.t, b.last_t ?? b.t + 3599]),
    ...(data.context || []).map((r) => r.t)].filter(Number.isFinite);
  const tMin = coverage.reduce((a, t) => Math.min(a, t), coverage[0] || 0);
  const tMax = coverage.reduce((a, t) => Math.max(a, t), coverage[0] || 0);
  const PAD = Math.max(600, (tMax - tMin) * 0.01);

  const linksOf = new Map();
  // by unordered pair, because the Timeline asks "is this join inferred?" for
  // every member of a selection's cluster on every frame of a pan
  const pairKey = (a, b) => (a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`);
  const byPair = new Map();
  for (const L of links) {
    const key = pairKey(L.a, L.b);
    if (!byPair.has(key)) byPair.set(key, []);
    byPair.get(key).push(L);
    if (!byId.has(L.a) || !byId.has(L.b)) continue;
    if (!linksOf.has(L.a)) linksOf.set(L.a, []);
    if (!linksOf.has(L.b)) linksOf.set(L.b, []);
    linksOf.get(L.a).push([L.b, L.rel]);
    linksOf.get(L.b).push([L.a, L.rel]);
  }
  const byTick = groupBy(events, (e) => e.tick_id);
  const byCorr = groupBy(events, (e) => e.corr_id);
  const stories = data.stories || [];
  const storiesOf = new Map();
  for (const s of stories) {
    for (const id of s.nodes) {
      if (!storiesOf.has(id)) storiesOf.set(id, []);
      storiesOf.get(id).push(s);
    }
  }
  const storyById = new Map(stories.map((s) => [s.id, s]));
  for (const e of events) {
    const d = e.detail || {};
    e._blob = [
      e.title, e.summary, e.kind, e.id, e.tick_id, e.corr_id, e.tool, e.goal, e.state,
      d.text, d.completion, d.asked, d.result, d.intention, d.reason, d.subject,
      d.args && JSON.stringify(d.args), d.ops && d.ops.map((o) => o.text).join(" "),
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function lowerBound(t) {
    let lo = 0, hi = events.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (events[mid].t < t) lo = mid + 1; else hi = mid; }
    return lo;
  }

  function inRange(t0, t1) {
    const out = [];
    for (let i = lowerBound(t0); i < events.length && events[i].t <= t1; i++) out.push(events[i]);
    return out;
  }

  /* Everything joined to one record: its links, its tick, and everything that
   * shares its tick or its correlation id. Keyed by id, valued by relation. */
  function clusterOf(id) {
    const out = new Map([[id, "self"]]);
    const e = byId.get(id);
    if (!e) return out;
    const add = (x, rel) => { if (byId.has(x) && !out.has(x)) out.set(x, rel); };
    for (const [n, rel] of linksOf.get(id) || []) add(n, rel);
    if (e.tick_id) {
      add(e.tick_id, "tick");
      for (const o of byTick.get(e.tick_id) || []) add(o.id, "tick");
      for (const [n, rel] of linksOf.get(e.tick_id) || []) add(n, rel);
    }
    if (e.kind === "tick") for (const o of byTick.get(e.id) || []) add(o.id, "tick");
    if (e.corr_id) for (const o of byCorr.get(e.corr_id) || []) add(o.id, "corr");
    return out;
  }

  /* The story a record is read in: the one you opened it from if it is part
   * of that, else the one it anchors, else the first that holds it. */
  function pickStory(id, pinnedId = null) {
    const list = storiesOf.get(id) || [];
    const pinned = pinnedId && storyById.get(pinnedId);
    if (pinned && pinned.nodes.includes(id)) return pinned;
    return list.find((s) => s.anchor === id) || list[0] || null;
  }

  function linksBetween(a, b) {
    return byPair.get(pairKey(a, b)) || [];
  }

  return {
    data, meta, events, byId, indexOf, goals, goalsById, links, stories, storyById,
    byTick, byCorr, tMin, tMax, PAD, lowerBound, inRange, clusterOf, pickStory, linksBetween,
  };
}

// ------------------------------------------------------------------- formats

const F = {
  clock: new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
  clockS: new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }),
  day: new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" }),
  dayY: new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }),
  short: new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
  month: new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" }),
};
const D = (t) => new Date(t * 1000);
export const fmtClock = (t) => F.clock.format(D(t));
export const fmtDay = (t) => F.day.format(D(t));
export const fmtShort = (t) => F.short.format(D(t));
export const fmtMonth = (t) => F.month.format(D(t));
export const fmtFull = (t) => `${F.dayY.format(D(t))}, ${F.clockS.format(D(t))}`;

export function dayKey(t) {
  const d = D(t);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function startOfDay(t) {
  const d = D(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

export function dur(s) {
  s = Math.abs(s);
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 129600) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`;
  return `${(s / 86400).toFixed(s < 864000 ? 1 : 0)} days`;
}

/* Grid steps aligned to local midnight, so "06:00" means six in the morning in
 * the house's zone, which is the zone the traces were written in. */
export function niceTimeSteps(t0, t1, width) {
  const span = Math.max(1, t1 - t0);
  const target = Math.max(3, Math.floor(width / 110));
  const steps = [60, 300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
    2 * 86400, 7 * 86400, 14 * 86400];
  let step = steps[steps.length - 1];
  for (const s of steps) { if (span / s <= target) { step = s; break; } }
  const ticks = [];
  let t = startOfDay(t0);
  if (step >= 86400) {
    while (t < t0) t = startOfDay(t + 86400 + 7200);
    const every = step / 86400;
    let n = 0;
    while (t < t1 && ticks.length < 400) {
      if (n++ % every === 0) ticks.push(t);
      t = startOfDay(t + 86400 + 7200);
    }
  } else {
    t += Math.ceil((t0 - t) / step) * step;
    for (; t < t1 && ticks.length < 400; t += step) ticks.push(t);
  }
  const label = step >= 86400 ? fmtMonth : fmtClock;
  return { ticks, label, step };
}

// --------------------------------------------------------------------- utils

export function groupBy(list, key) {
  const m = new Map();
  for (const x of list) {
    const k = key(x);
    if (!k) continue;
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(x);
  }
  return m;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export function clip(s, n) {
  s = String(s ?? "");
  return s.length <= n ? s : `${s.slice(0, n - 1).trimEnd()}…`;
}

export const fmt = (n) => Number(n || 0).toLocaleString();

export function compact(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

export function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

/* A call's verdict, read the way the audit writes it: `ok`, or `ok:…`, is
 * fine; `denied`/`dropped` was stopped before it ran; anything else failed. */
export const failed = (v) => !!v && !/^ok/.test(v);
export const stopped = (v) => /^denied|^dropped/.test(v || "");
