/* Where each record sits in Space. Pure, so the rules are testable without a
 * WebGL context (web/tests/mind-graph.test.js).
 *
 * X is time — the shared range, left to right. Y and Z carry only
 * relationship: records that share recorded work identity (a tick, a corr_id,
 * a turn) form one group; groups that overlap in time are pushed apart, and
 * groups joined by any other link (a goal, a signal) are drawn closer. An
 * inferred link never merges two groups: nearness in time is not identity.
 * Unlinked records form one thread per kind, and no edge is invented for them.
 */
const hash = (text) => {
  let n = 2166136261;
  for (const c of text) n = Math.imul(n ^ c.charCodeAt(0), 16777619);
  return n >>> 0;
};

/** World units from the start of the range to its end. */
export const SPAN = 2400;

export function layout(events, links, range) {
  const sorted = [...events].sort((a, b) => a.id.localeCompare(b.id));
  const index = new Map(sorted.map((e, i) => [e.id, i]));
  const parent = sorted.map((_, i) => i);
  const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
  const join = (a, b) => { a = find(a); b = find(b); if (a !== b) parent[Math.max(a, b)] = Math.min(a, b); };
  const edges = links.filter((l) => index.has(l.a) && index.has(l.b) && l.a !== l.b);
  const degree = new Map();
  for (const edge of edges) {
    degree.set(edge.a, (degree.get(edge.a) || 0) + 1);
    degree.set(edge.b, (degree.get(edge.b) || 0) + 1);
    if (["tick", "corr", "turn"].includes(edge.rel) && edge.confidence !== "inferred") {
      join(index.get(edge.a), index.get(edge.b));
    }
  }
  for (const field of ["tick_id", "corr_id"]) {
    const first = new Map();
    sorted.forEach((e, i) => {
      if (!e[field]) return;
      if (first.has(e[field])) join(i, first.get(e[field])); else first.set(e[field], i);
    });
  }
  const components = new Map();
  sorted.forEach((e, i) => {
    const key = find(i);
    if (!components.has(key)) components.set(key, []);
    components.get(key).push(e);
  });
  const batches = new Map();
  for (const members of components.values()) {
    const unlinked = members.length === 1 && !degree.has(members[0].id);
    const key = unlinked ? `unlinked:${members[0].kind}` : members[0].id;
    if (!batches.has(key)) batches.set(key, { key, events: [], unlinked });
    batches.get(key).events.push(...members);
  }

  // The time axis: the shared range when given, else the records' own span.
  let t0 = Infinity, t1 = -Infinity;
  for (const e of sorted) if (Number.isFinite(e.t)) { t0 = Math.min(t0, e.t); t1 = Math.max(t1, e.t); }
  if (range && Number.isFinite(range[0]) && Number.isFinite(range[1]) && range[1] > range[0]) [t0, t1] = range;
  if (!Number.isFinite(t0)) t0 = t1 = 0;
  const span = Math.max(1, t1 - t0);
  const xOf = (t) => ((Number.isFinite(t) ? t : t0) - t0) / span * SPAN - SPAN / 2;

  const groups = [...batches.values()];
  const groupOf = new Map();
  groups.forEach((g, i) => {
    g.events.forEach((e) => groupOf.set(e.id, i));
    g.anchor = g.events.find((e) => e.kind === "goal") || g.events.find((e) => e.kind === "tick")
      || g.events.find((e) => e.kind === "chat") || g.events[0];
    const xs = g.events.map((e) => xOf(e.t));
    g.lo = Math.min(...xs); g.hi = Math.max(...xs);
    // Radius across time, not along it: records of one unit of work share
    // (nearly) one moment, so they spread in Y/Z around it.
    g.radius = Math.min(46, 5 + Math.sqrt(g.events.length) * 4);
    const seed = hash(g.key), angle = (seed % 10000) / 10000 * Math.PI * 2;
    const r = 40 + ((seed >>> 12) % 1000) / 1000 * 120;
    g.p = [g.unlinked ? g.lo : xOf(g.anchor.t), Math.sin(angle) * r, Math.cos(angle) * r];
  });
  const bridges = new Map();
  for (const edge of edges) {
    const a = groupOf.get(edge.a), b = groupOf.get(edge.b);
    if (a === b) continue;
    const key = `${Math.min(a, b)}:${Math.max(a, b)}`;
    if (!bridges.has(key)) bridges.set(key, [a, b]);
  }
  // Groups only crowd each other when they share a stretch of time, so the
  // sweep compares a group with those whose time extent overlaps its own.
  const PAD = 24;
  const order = groups.map((_, i) => i).sort((a, b) => groups[a].lo - groups[b].lo || a - b);
  const pairs = [];
  for (let i = 0; i < order.length; i++) {
    const a = groups[order[i]];
    for (let j = i + 1; j < order.length && groups[order[j]].lo <= a.hi + PAD; j++) pairs.push([order[i], order[j]]);
  }
  const iterations = Math.max(8, Math.min(120, Math.floor(1500000 / Math.max(1, pairs.length + bridges.size))));
  for (let step = 0; step < iterations; step++) {
    const force = groups.map((g) => [0, -g.p[1] * .01, -g.p[2] * .01]);
    for (const [a, b] of pairs) {
      const ga = groups[a], gb = groups[b];
      let dy = ga.p[1] - gb.p[1], dz = ga.p[2] - gb.p[2];
      let distance = Math.hypot(dy, dz);
      if (distance < .1) {
        const turn = (hash(ga.key + gb.key) % 628) / 100;
        dy = Math.cos(turn); dz = Math.sin(turn); distance = 1;
      }
      const separation = ga.radius + gb.radius + 12;
      const strength = Math.min(12, 300 / (distance * distance) + Math.max(0, separation - distance) * .2);
      force[a][1] += dy / distance * strength; force[a][2] += dz / distance * strength;
      force[b][1] -= dy / distance * strength; force[b][2] -= dz / distance * strength;
    }
    for (const [a, b] of bridges.values()) {
      const ga = groups[a], gb = groups[b];
      const dy = gb.p[1] - ga.p[1], dz = gb.p[2] - ga.p[2];
      const distance = Math.max(1, Math.hypot(dy, dz));
      const strength = (distance - ga.radius - gb.radius - 20) * .02;
      force[a][1] += dy / distance * strength; force[a][2] += dz / distance * strength;
      force[b][1] -= dy / distance * strength; force[b][2] -= dz / distance * strength;
    }
    const cool = 1 - step / (iterations * 1.4);
    groups.forEach((g, i) => { g.p[1] += force[i][1] * cool; g.p[2] += force[i][2] * cool; });
  }
  const nodes = [];
  groups.forEach((g, group) => {
    g.events.forEach((event, i) => {
      // X is the record's own moment. Y/Z scatter it inside its group's disc
      // (a golden-angle spiral), so records of one unit never stack.
      const seed = hash(event.id);
      const angle = i * 2.399963229728653;
      const radius = g.events.length === 1 ? 0 : g.radius * (.3 + .7 * Math.sqrt((seed % 997) / 997));
      const p = [xOf(event.t), g.p[1] + Math.sin(angle) * radius, g.p[2] + Math.cos(angle) * radius];
      nodes.push({ event, group, p, size: 5 + Math.min(4, Math.log2(1 + (degree.get(event.id) || 0)) * 1.1) });
    });
  });
  return { nodes, edges, groups, degree, t0, t1, xOf };
}
