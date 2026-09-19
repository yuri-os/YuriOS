/* The joined views (SPEC §24.4): Map, Timeline, Space, Stories, Goals, Ledger.
 *
 * Each is `draw(root, ws)` over the one workspace (graph/workspace.js): one
 * loaded window, one range inside it, one set of filters, one selection. A
 * view that animates or listens registers its cleanup with `ws.onTeardown`,
 * and one that can repaint for a new selection without a rebuild registers
 * that with `ws.setRedraw`.
 */
import {
  KINDS, KIND_LABEL, LANES, LANE_INDEX, clamp, clip, dayKey, dur, esc, fmt, fmtClock, fmtDay,
  fmtFull, fmtShort, hash, niceTimeSteps, noun, startOfDay,
} from "./model.js";

const PAGE = 60;

// ----------------------------------------------------------------------- map

export function drawMap(root, ws) {
  const s = ws.model.data.stats || {};
  const meta = ws.model.meta;
  const rel = (x) => (x == null ? "?" : fmt(x));
  const thr = meta.act_threshold;
  const user = meta.user || "you";
  const wrap = document.createElement("div");
  wrap.className = "gx-map";
  wrap.innerHTML = `
    <div class="gx-map-tools"><button type="button" data-map="out" aria-label="Zoom map out">−</button>
      <output aria-live="polite"></output><button type="button" data-map="in" aria-label="Zoom map in">+</button>
      <button type="button" data-map="fit">Fit</button><button type="button" data-map="read">Readable</button>
      <span class="muted">Counts cover the loaded window. Click a box for its records.</span></div>
    <div class="gx-map-viewport" tabindex="0" aria-label="Architecture diagram; scroll to explore">
    <svg class="gx-map-svg" viewBox="0 0 1100 610" role="img" aria-label="Architecture of ${esc(meta.name || "her")} mind">
      <defs>
        <marker id="gx-arrow" viewBox="0 0 8 8" markerWidth="7" markerHeight="7" refX="7" refY="4" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" class="arrow-tip"></path></marker>
        <marker id="gx-arrow-hot" viewBox="0 0 8 8" markerWidth="7" markerHeight="7" refX="7" refY="4" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" class="arrow-tip hot"></path></marker>
      </defs>
      ${route([[220, 75], [260, 75]])}
      ${route([[500, 75], [540, 75]])}
      ${route([[130, 110], [130, 170]])}
      ${route([[200, 170], [200, 140], [600, 140], [600, 110]], "", "user_message · turn_committed", [400, 134])}
      ${route([[240, 213], [270, 213]])}
      ${route([[180, 256], [180, 280], [770, 280], [770, 256]], "", "conversational hands", [470, 274])}
      ${route([[890, 213], [920, 213]])}
      ${route([[695, 110], [695, 296], [115, 296], [115, 322]], "hot", "the inbox", [300, 290])}
      ${route([[190, 367], [210, 367]], "hot")}
      ${route([[380, 367], [400, 367]], "hot")}
      ${route([[570, 367], [590, 367]], "hot")}
      ${route([[760, 367], [780, 367]], "hot")}
      ${route([[930, 367], [950, 367]], "hot")}
      ${route([[620, 322], [620, 256]])}
      ${route([[740, 322], [740, 256]])}
      ${route([[1005, 322], [1005, 304], [905, 304], [905, 140], [960, 140], [960, 110]], "", "cadence")}
      ${route([[820, 412], [820, 446], [170, 446], [170, 492]])}
      ${route([[440, 446], [440, 492]])}
      ${route([[880, 412], [880, 492]])}
      ${route([[640, 492], [640, 466], [295, 466], [295, 412]], "dash", "goals become candidates", [470, 462])}

      <text class="lane-title" x="40" y="28">World</text>
      <text class="lane-title" x="40" y="160">Reactive body — sub-second, on your turn</text>
      <text class="lane-title" x="40" y="314">The tick — between turns, on the cadence</text>
      <text class="lane-title" x="40" y="478">What persists</text>

      ${box(40, 40, 180, 70, "chat", user, "the one person in the room", "", ["chat"])}
      ${box(260, 40, 240, 70, "chat", "Sanctuary / browser", "thin client · one SSE bus", "", ["chat", "signal"])}
      ${box(540, 40, 240, 70, "signal", "SignalBus inbox", "presence, turns, timers, wakeups", `${rel(s.signals)} kept signals`, ["signal"])}
      ${box(820, 40, 240, 70, "activity", "Activity ladder", `${meta.current_state || "?"} now · ${meta.cadence_s || "?"}s`, `${rel(s.activity)} transitions`, ["activity"])}

      ${box(40, 170, 200, 86, "chat", "Chat / voice turn", "reactive pipeline, not the tick", `${rel(s.chat)} messages`, ["chat"])}
      ${box(270, 170, 200, 86, "utility", "Utility model", "extract / summarise after a turn", `${rel(s.utility)} runs`, ["utility"])}
      ${box(500, 170, 180, 86, "prompt", "Model calls", "every call, with tokens", `${rel(s.prompts)} calls`, ["prompt"])}
      ${box(710, 170, 180, 86, "tool", "Hands", "MCP tools behind a Guard", `${rel(s.tools)} calls`, ["tool"])}
      ${box(920, 170, 140, 86, "selfie", "Camera", "the forge", `${rel(s.selfies)} photos`, ["selfie"])}

      ${box(40, 322, 150, 90, "tick", "SENSE", "read the bus · no model", "empty most ticks", ["signal", "tick"])}
      ${box(210, 322, 170, 90, "tick", "APPRAISE", `heuristics · Gate 1 ≥ ${thr}`, "never calls a model", ["tick"])}
      ${box(400, 322, 170, 90, "tick", "DECIDE", "one intention, or REST", `${rel(s.rest)} rest · ${rel(s.acted)} acted`, ["tick"])}
      ${box(590, 322, 170, 90, "tick", "ACT", "the only phase with a model", "speak · goal · hands", ["tick", "prompt", "tool"])}
      ${box(780, 322, 150, 90, "journal", "REFLECT", "journal + goals", `${rel(s.journal)} [she] lines`, ["journal", "goal"])}
      ${box(950, 322, 110, 90, "activity", "REGULATE", "drift down", "budget", ["activity"])}

      ${box(40, 492, 260, 86, "tick", "Tick trace", "the why-record · traces/ticks.jsonl", `${rel(s.ticks)} heartbeats`, ["tick"])}
      ${box(330, 492, 240, 86, "commit", "Vault / journal", "the files are the database", `${rel(s.commits)} commits`, ["journal", "commit", "utility"])}
      ${box(600, 492, 300, 86, "goal", "Goals on her mind", "promises and strategy become work", `${rel(s.goals)} filed · ${rel(s.open_goals)} open`, null, "goals")}
      ${box(920, 492, 140, 86, "prompt", "Private surfaces", "never exported", "traces · corpus", null)}
    </svg>
    </div>
    <div class="gx-primer">
      <article><h3>The mind is a process</h3>
        <p>Between your turns she exists, ticks, and decides. Disable the mind and the reactive companion still answers — she just has no ambient life. A message from ${esc(user)} preempts any state straight to ENGAGED.</p></article>
      <article><h3>One intention per tick</h3>
        <p>DECIDE commits to exactly one thing, or to resting. Rest is the majority on purpose (${rel(s.rest)} of ${rel(s.ticks)} heartbeats in this window). Runners-up wait for the next heartbeat instead of piling into this one.</p></article>
      <article><h3>APPRAISE never calls a model</h3>
        <p>Gate 1 is cheap heuristics (hers is ${esc(thr)}). The model is invoked only inside ACT, for work the loop has already decided is worth it. That is what makes always-on affordable.</p></article>
      <article><h3>One corr_id per unit of work</h3>
        <p>The tick, the prompt, the tool call and the photo are four files. The shared correlation id makes them one story. Select anything to light up its chain.</p></article>
    </div>`;
  root.appendChild(wrap);
  const diagram = wrap.querySelector(".gx-map-svg");
  const viewport = wrap.querySelector(".gx-map-viewport");
  const zoomMap = () => {
    if (ws.state.mapZoom == null) {
      // the first look is the whole diagram; Readable is one click away
      const width = viewport.clientWidth || root.clientWidth;
      ws.state.mapZoom = width ? clamp(width / 1100, 0.25, 1) : 1;
    }
    diagram.style.width = `${1100 * ws.state.mapZoom}px`;
    wrap.querySelector(".gx-map-tools output").textContent = `${Math.round(ws.state.mapZoom * 100)}%`;
  };
  zoomMap();
  wrap.querySelector(".gx-map-tools").addEventListener("click", (ev) => {
    const action = ev.target.closest("button")?.dataset.map;
    if (!action) return;
    ws.state.mapZoom = action === "fit" ? viewport.clientWidth / 1100 : action === "read" ? 1
      : clamp(ws.state.mapZoom * (action === "in" ? 1.25 : 0.8), 0.25, 2.5);
    zoomMap();
  });
  wrap.querySelectorAll(".node.clickable").forEach((node) => {
    const go = () => {
      const kinds = (node.dataset.kinds || "").split(",").filter(Boolean);
      if (kinds.length) for (const [k] of KINDS) ws.state.kinds[k] = kinds.includes(k);
      ws.show(node.dataset.go || "timeline");
    };
    node.addEventListener("click", go);
    node.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
    });
  });
}

function box(x, y, w, h, kind, title, sub, count, kinds, go) {
  const clickable = kinds || go;
  return `<g class="node${clickable ? " clickable" : ""}" ${kinds ? `data-kinds="${kinds.join(",")}"` : ""} ${go ? `data-go="${go}"` : ""}
      transform="translate(${x},${y})" ${clickable ? `tabindex="0" role="button" aria-label="${esc(title)}: show records"` : ""}
      style="--swatch:var(--k-${kind})">
    <rect class="box" width="${w}" height="${h}" rx="10"></rect>
    <rect class="box-accent" width="3" height="${h - 20}" x="0" y="10" rx="1.5"></rect>
    <text class="label" x="14" y="23">${esc(title)}</text>
    <text class="sub" x="14" y="41">${esc(sub)}</text>
    <text class="count" x="14" y="${h - 13}">${esc(count)}</text>
  </g>`;
}

function route(points, cls = "", label = "", at = null) {
  // orthogonal path with softened corners
  const r = 8;
  let d = `M${points[0][0]} ${points[0][1]}`;
  for (let i = 1; i < points.length; i++) {
    const [x, y] = points[i];
    const next = points[i + 1];
    if (!next) { d += ` L${x} ${y}`; break; }
    const [px, py] = points[i - 1];
    const inX = Math.sign(x - px), inY = Math.sign(y - py);
    const outX = Math.sign(next[0] - x), outY = Math.sign(next[1] - y);
    d += ` L${x - inX * r} ${y - inY * r} Q${x} ${y} ${x + outX * r} ${y + outY * r}`;
  }
  const marker = cls.includes("hot") ? "url(#gx-arrow-hot)" : "url(#gx-arrow)";
  let text = "";
  if (label) {
    const [lx, ly] = at || [(points[0][0] + points[1][0]) / 2, Math.min(points[0][1], points[1][1]) - 6];
    text = `<text class="arrow-label" x="${lx}" y="${ly}" text-anchor="middle">${esc(label)}</text>`;
  }
  return `<path class="arrow ${cls}" d="${d}" marker-end="${marker}" />${text}`;
}

// ------------------------------------------------------------------ timeline

export function drawTimeline(root, ws) {
  const { model, state } = ws;
  const wrap = document.createElement("div");
  wrap.className = "gx-canvas";
  const canvas = document.createElement("canvas");
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-label", "Timeline of events; use arrow keys to step through them");
  const tools = document.createElement("div");
  tools.className = "gx-canvas-tools";
  tools.innerHTML = `
    <button type="button" data-z="in" title="Zoom in">+</button>
    <button type="button" data-z="out" title="Zoom out">−</button>
    <button type="button" data-z="sel" title="Center on the selection">◎</button>
    <button type="button" data-z="all" title="Show the whole loaded window">all</button>`;
  wrap.append(canvas, tools);
  root.appendChild(wrap);

  const ctx = canvas.getContext("2d");
  const colors = ws.kindColors();
  const stateColors = ws.stateColors();
  const ink = { rule: ws.cssVar("--rule"), grid: ws.cssVar("--grid"), dim: ws.cssVar("--dim"),
    soft: ws.cssVar("--soft"), text: ws.cssVar("--ink"), acid: ws.cssVar("--acid"),
    panel: ws.cssVar("--panel"), mono: ws.cssVar("--font-mono"), sans: ws.cssVar("--font-sans") };
  const L = { left: 118, top: 30, bottom: 26, right: 16 };
  let hits = [];
  let W = 0, H = 0;

  const geom = () => {
    const plotW = Math.max(1, W - L.left - L.right), plotH = Math.max(1, H - L.top - L.bottom);
    const laneH = plotH / LANES.length;
    const span = Math.max(1, state.t1 - state.t0);
    return { plotW, plotH, laneH, span,
      xOf: (t) => L.left + ((t - state.t0) / span) * plotW };
  };
  const yOf = (e, g) => {
    const lane = LANE_INDEX[e.lane] ?? 0;
    const jitter = ((hash(e.id) % 1000) / 1000 - 0.5) * g.laneH * 0.5;
    return L.top + lane * g.laneH + g.laneH / 2 + (g.laneH > 26 ? jitter : 0);
  };

  const paint = () => {
    const dpr = window.devicePixelRatio || 1;
    W = wrap.clientWidth; H = wrap.clientHeight;
    if (!W || !H) return;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    hits = [];
    const g = geom();
    const { plotW, plotH, laneH, span, xOf } = g;

    // days: alternating stripes, date labels on top
    ctx.font = `10px ${ink.mono}`;
    ctx.textBaseline = "alphabetic";
    let dayT = startOfDay(state.t0);
    let stripe = Math.round(dayT / 86400) % 2;
    while (dayT < state.t1) {
      const next = startOfDay(dayT + 86400 + 7200);
      const x0 = Math.max(L.left, xOf(dayT)), x1 = Math.min(W - L.right, xOf(next));
      if (stripe) { ctx.fillStyle = ink.grid; ctx.globalAlpha = 0.35; ctx.fillRect(x0, L.top, x1 - x0, plotH); ctx.globalAlpha = 1; }
      if (x1 - x0 > 46) { ctx.fillStyle = ink.soft; ctx.fillText(fmtDay(Math.max(dayT, state.t0)), x0 + 5, 18); }
      ctx.strokeStyle = ink.rule;
      if (xOf(dayT) > L.left) { ctx.beginPath(); ctx.moveTo(xOf(dayT), 6); ctx.lineTo(xOf(dayT), L.top + plotH); ctx.stroke(); }
      dayT = next; stripe = 1 - stripe;
    }

    ctx.save();
    ctx.beginPath(); ctx.rect(L.left, 0, plotW, H); ctx.clip();
    // activity state: solid in the State lane, a faint wash behind the rest
    const acts = model.data.activity || [];
    for (let i = 0; i < acts.length; i++) {
      const a = acts[i];
      const tEnd = acts[i + 1] ? acts[i + 1].t : Math.max(model.tMax, model.meta.generated_t || model.tMax);
      if (tEnd < state.t0 || a.t > state.t1) continue;
      const x = Math.max(L.left, xOf(a.t)), x2 = Math.min(W - L.right, xOf(tEnd));
      if (x2 <= x) continue;
      ctx.fillStyle = stateColors[a.to] || ink.dim;
      ctx.globalAlpha = 0.05;
      ctx.fillRect(x, L.top + laneH, x2 - x, plotH - laneH);
      ctx.globalAlpha = 0.55;
      ctx.fillRect(x, L.top + laneH * 0.3, x2 - x, laneH * 0.4);
      if (x2 - x > 60) {
        ctx.globalAlpha = 1; ctx.fillStyle = ink.panel;
        ctx.fillText(a.to || "", x + 4, L.top + laneH / 2 + 3.5);
      }
    }
    ctx.globalAlpha = 1;

    // rest density in the decision lane, scaled to the busiest hour in view
    const tickLane = LANE_INDEX.ticks;
    const buckets = (model.data.rest_density || []).filter((b) => b.t + 3600 >= state.t0 && b.t <= state.t1);
    const maxN = buckets.reduce((m, b) => Math.max(m, b.n), 1);
    ctx.fillStyle = colors.tick;
    ctx.globalAlpha = 0.16;
    for (const b of buckets) {
      const bw = Math.max(1, (3600 / span) * plotW - (span < 86400 * 3 ? 1 : 0));
      const bh = (b.n / maxN) * (laneH - 6);
      ctx.fillRect(xOf(b.t), L.top + (tickLane + 1) * laneH - 3 - bh, bw, bh);
    }
    ctx.globalAlpha = 1;
    ctx.restore();

    // time grid
    const steps = niceTimeSteps(state.t0, state.t1, plotW);
    ctx.fillStyle = ink.dim;
    for (const t of steps.ticks) {
      const x = xOf(t);
      ctx.strokeStyle = ink.grid;
      ctx.beginPath(); ctx.moveTo(x, L.top); ctx.lineTo(x, L.top + plotH); ctx.stroke();
      ctx.fillText(steps.label(t), x + 3, H - 9);
    }

    // lanes: rules, labels, counts in view
    const items = ws.visible();
    const perLane = {};
    for (const e of items) perLane[e.lane] = (perLane[e.lane] || 0) + 1;
    LANES.forEach(([id, label], i) => {
      const y = L.top + i * laneH;
      ctx.strokeStyle = ink.rule;
      ctx.beginPath(); ctx.moveTo(L.left, y); ctx.lineTo(W - L.right, y); ctx.stroke();
      ctx.fillStyle = ink.soft;
      ctx.font = `11px ${ink.mono}`;
      ctx.fillText(label, 12, y + laneH / 2 + 1);
      ctx.fillStyle = ink.dim;
      ctx.font = `10px ${ink.mono}`;
      if (laneH > 24) ctx.fillText(fmt(perLane[id] || 0), 12, y + laneH / 2 + 13);
    });
    ctx.strokeStyle = ink.rule;
    ctx.beginPath(); ctx.moveTo(L.left, L.top + plotH); ctx.lineTo(W - L.right, L.top + plotH); ctx.stroke();

    // selection cluster: dim the rest, draw the chain
    const cluster = state.selected ? model.clusterOf(state.selected) : null;
    const pos = new Map();
    ctx.save();
    ctx.beginPath(); ctx.rect(L.left - 8, L.top, plotW + 16, plotH); ctx.clip();
    for (const e of items) {
      const x = xOf(e.t);
      if (x < L.left - 8 || x > W - L.right + 8) continue;
      pos.set(e.id, [x, yOf(e, g)]);
    }
    if (cluster) {
      const sel = model.byId.get(state.selected);
      if (sel && !pos.has(sel.id)) pos.set(sel.id, [xOf(sel.t), yOf(sel, g)]);
      ctx.lineWidth = 1.2;
      for (const [id] of cluster) {
        if (id === state.selected) continue;
        const a = pos.get(state.selected), b = pos.get(id);
        if (!a || !b) continue;
        // an inferred join is drawn dashed and amber, never like a recorded one
        const inferred = model.linksBetween(state.selected, id).some((l) => l.confidence === "inferred");
        ctx.strokeStyle = inferred ? ws.cssVar("--amber") : ink.acid;
        ctx.setLineDash(inferred ? [4, 4] : []);
        ctx.globalAlpha = 0.55;
        ctx.beginPath();
        const mx = (a[0] + b[0]) / 2;
        ctx.moveTo(a[0], a[1]);
        ctx.bezierCurveTo(mx, a[1], mx, b[1], b[0], b[1]);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
      ctx.globalAlpha = 1;
    }
    for (const e of items) {
      const p = pos.get(e.id);
      if (!p) continue;
      const on = !cluster || cluster.has(e.id);
      const isSel = e.id === state.selected;
      const r = isSel ? 6.5 : on && cluster ? 4.6 : 3.6;
      ctx.globalAlpha = on ? 0.95 : 0.16;
      ctx.fillStyle = e.kind === "activity" ? (stateColors[e.state] || colors.activity) : (colors[e.kind] || ink.soft);
      shape(ctx, e, p[0], p[1], r);
      ctx.fill();
      if (isSel) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = ink.text;
        ctx.lineWidth = 1.6;
        ctx.stroke();
        ctx.lineWidth = 1;
      }
      hits.push({ x: p[0], y: p[1], id: e.id });
    }
    ctx.restore();
    ctx.globalAlpha = 1;

    const sel = state.selected && model.byId.get(state.selected);
    if (sel) {
      const x = xOf(sel.t);
      if (x >= L.left && x <= W - L.right) {
        ctx.strokeStyle = ink.text; ctx.globalAlpha = 0.4; ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.moveTo(x, L.top); ctx.lineTo(x, L.top + plotH); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
    }
    if (!items.length) {
      ctx.fillStyle = ink.dim; ctx.font = `13px ${ink.sans}`;
      ctx.fillText(state.query ? `Nothing matches “${state.query}” in this range.`
        : "Nothing in this range with the current filters.", L.left + 16, L.top + 22);
    }
  };

  const hit = (mx, my) => {
    let best = null, bestD = 10;
    for (const h of hits) {
      const d = Math.hypot(mx - h.x, my - h.y);
      if (d < bestD) { bestD = d; best = h.id; }
    }
    return best;
  };

  let chromeTimer = 0;
  const scheduleChrome = () => {
    clearTimeout(chromeTimer);
    chromeTimer = setTimeout(() => ws.renderChrome(), 160);
  };
  const zoom = (factor, anchor = 0.5) => {
    const span = state.t1 - state.t0;
    const next = clamp(span * factor, 600, model.tMax - model.tMin + model.PAD * 2);
    const center = state.t0 + anchor * span;
    ws.setRange(center - next * anchor, center - next * anchor + next, { redraw: false });
  };

  // a press that moves past a few pixels is a pan, not a click
  let press = null;
  canvas.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    canvas.setPointerCapture(ev.pointerId);
    press = { x: ev.clientX, t0: state.t0, t1: state.t1, moved: false };
  });
  canvas.addEventListener("pointermove", (ev) => {
    const r = canvas.getBoundingClientRect();
    if (press) {
      const dx = ev.clientX - press.x;
      if (Math.abs(dx) > 4) press.moved = true;
      if (press.moved) {
        const dt = -(dx / geom().plotW) * (press.t1 - press.t0);
        ws.setRange(press.t0 + dt, press.t1 + dt, { redraw: false });
        ws.hideTooltip();
        canvas.style.cursor = "grabbing";
        paint();
        return;
      }
    }
    const id = hit(ev.clientX - r.left, ev.clientY - r.top);
    canvas.style.cursor = id ? "pointer" : "grab";
    if (id) ws.tooltip(model.byId.get(id), ev.clientX, ev.clientY);
    else ws.hideTooltip();
  });
  canvas.addEventListener("pointerup", (ev) => {
    if (!press) return;
    const moved = press.moved;
    press = null;
    canvas.style.cursor = "grab";
    if (moved) { ws.renderChrome(); return; }
    const r = canvas.getBoundingClientRect();
    const id = hit(ev.clientX - r.left, ev.clientY - r.top);
    if (id) ws.select(id);
  });
  canvas.addEventListener("pointercancel", () => { press = null; });
  canvas.addEventListener("pointerleave", () => { if (!press) ws.hideTooltip(); });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect();
    const g = geom();
    if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY) || ev.shiftKey) {
      const d = (ev.shiftKey ? ev.deltaY : ev.deltaX) / g.plotW * g.span;
      ws.setRange(state.t0 + d, state.t1 + d, { redraw: false });
    } else {
      const anchor = clamp((ev.clientX - r.left - L.left) / g.plotW, 0, 1);
      zoom(Math.pow(1.0018, ev.deltaY * (ev.deltaMode === 1 ? 30 : 1)), anchor);
    }
    ws.hideTooltip();
    paint();
    scheduleChrome();
  }, { passive: false });
  tools.addEventListener("click", (ev) => {
    const z = ev.target.closest("button")?.dataset.z;
    if (!z) return;
    if (z === "in") zoom(0.6);
    else if (z === "out") zoom(1.6);
    else if (z === "all") ws.setRange(model.tMin - model.PAD, model.tMax + model.PAD, { redraw: false });
    else if (z === "sel" && state.selected) {
      const e = model.byId.get(state.selected);
      const span = Math.min(state.t1 - state.t0, 6 * 3600);
      ws.setRange(e.t - span / 2, e.t + span / 2, { redraw: false });
    }
    paint();
    scheduleChrome();
  });

  const ro = new ResizeObserver(() => paint());
  ro.observe(wrap);
  ws.onTeardown(() => ro.disconnect());
  ws.onTeardown(() => clearTimeout(chromeTimer));
  ws.setRedraw(() => {
    // a selection off-screen pulls the view to it
    const e = state.selected && model.byId.get(state.selected);
    if (e && (e.t < state.t0 || e.t > state.t1)) {
      const span = state.t1 - state.t0;
      ws.setRange(e.t - span / 2, e.t + span / 2, { redraw: false });
    }
    paint();
  });
  canvas.style.cursor = "grab";
  paint();
}

/* A shape per kind, so a mark reads without its colour. */
function shape(ctx, e, x, y, r) {
  ctx.beginPath();
  if (e.kind === "tick") {
    ctx.moveTo(x, y - r * 1.2); ctx.lineTo(x + r * 1.2, y); ctx.lineTo(x, y + r * 1.2); ctx.lineTo(x - r * 1.2, y); ctx.closePath();
  } else if (e.kind === "chat") {
    if (e.role === "user") ctx.rect(x - r, y - r, r * 2, r * 2);
    else { ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x - r, y + r); ctx.closePath(); }
  } else if (e.kind === "goal") {
    ctx.moveTo(x, y - r * 1.3); ctx.lineTo(x + r * 1.2, y + r); ctx.lineTo(x - r * 1.2, y + r); ctx.closePath();
  } else if (e.kind === "activity") {
    ctx.rect(x - 1.5, y - r * 1.6, 3, r * 3.2);
  } else if (e.kind === "commit") {
    ctx.rect(x - r * 0.8, y - r * 0.8, r * 1.6, r * 1.6);
  } else {
    ctx.arc(x, y, r, 0, Math.PI * 2);
  }
}

// --------------------------------------------------------------------- space

export async function drawSpace(root, ws) {
  const holder = document.createElement("div");
  holder.className = "gx-space";
  holder.innerHTML = `<p class="gx-empty">Loading the 3D renderer…</p>`;
  root.appendChild(holder);
  let Space;
  try {
    Space = await import("./space3d.js");
  } catch {
    holder.innerHTML = `<p class="gx-empty">The 3D view could not load. Reload to try again, or use the Timeline.</p>`;
    return;
  }
  if (!holder.isConnected) return;             // the view changed while it loaded
  holder.replaceChildren();
  const view = Space.mount(holder, {
    events: ws.visible(), links: ws.model.links, colors: ws.kindColors(), labels: KIND_LABEL,
    selected: () => ws.state.selected, cluster: ws.model.clusterOf, select: (id) => ws.select(id),
    time: fmtShort, escape: esc, tooltip: (e, x, y) => ws.tooltip(e, x, y),
    hideTooltip: () => ws.hideTooltip(),
    range: [ws.state.t0, ws.state.t1],
    timeGrid: (t0, t1, width) => {
      const steps = niceTimeSteps(t0, t1, width);
      return steps.ticks.map((t) => {
        const day = startOfDay(t) === t;
        return { t, day, label: day ? fmtDay(t) : steps.label(t) };
      });
    },
  });
  ws.setRedraw(view.update);
  ws.onTeardown(view.dispose);
}

// ------------------------------------------------------------------- stories

export function drawStories(root, ws) {
  const { model, state } = ws;
  const q = state.query;
  const list = document.createElement("div");
  list.className = "gx-list";
  const kindOn = { tick: state.kinds.tick, chat: state.kinds.chat, reach: state.kinds.chat };
  const stories = model.stories.filter((s) => {
    if (s.t < state.t0 || s.t > state.t1) return false;
    if (!kindOn[s.kind]) return false;
    if (!q) return true;
    return `${s.title} ${s.why}`.toLowerCase().includes(q)
      || s.nodes.some((id) => model.byId.get(id)?._blob.includes(q));
  });

  const selStory = state.selected ? model.pickStory(state.selected, state.pinnedStory) : null;
  if (selStory) list.appendChild(storyFlow(selStory, ws));

  if (!stories.length) {
    list.insertAdjacentHTML("beforeend", `<p class="gx-empty">No stories in this range${q ? ` match “${esc(q)}”` : ""}. Widen the range or turn on Chat / Decisions.</p>`);
    root.appendChild(list);
    return;
  }
  let lastDay = "";
  for (const s of stories.slice(0, state.storyLimit)) {
    const dk = dayKey(s.t);
    if (dk !== lastDay) { list.insertAdjacentHTML("beforeend", `<h3 class="gx-day">${esc(fmtDay(s.t))}</h3>`); lastDay = dk; }
    const card = document.createElement("article");
    card.className = `gx-card${selStory && selStory.id === s.id ? " on" : ""}`;
    card.dataset.id = s.anchor;
    card.tabIndex = 0;
    const tag = s.kind === "tick" ? `decision${s.state ? ` · ${s.state}` : ""}` : s.kind === "reach" ? "reach-out" : "exchange";
    card.innerHTML = `
      <time>${esc(fmtClock(s.t))}</time>
      <div>
        <div class="who">${esc(s.title)}</div>
        <div class="sum">${esc(clip(s.why, 220))}</div>
        <div class="gx-mini">${countKinds(s.nodes, model)}</div>
      </div>
      <span class="tag" style="--swatch:var(--k-${s.kind === "tick" ? "tick" : "chat"})">${esc(tag)}</span>`;
    const open = () => {
      state.pinnedStory = s.id;
      ws.select(s.anchor || s.nodes[0]);
      root.querySelector(".gx-list")?.scrollTo({ top: 0, behavior: "smooth" });
    };
    card.addEventListener("click", open);
    card.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
    list.appendChild(card);
  }
  appendMore(list, stories.length, state.storyLimit, "stories", () => {
    state.storyLimit += PAGE; ws.refresh({ preserve: true });
  });
  root.appendChild(list);
}

function countKinds(ids, model) {
  const c = {};
  for (const id of ids) { const e = model.byId.get(id); if (e) c[e.kind] = (c[e.kind] || 0) + 1; }
  return Object.entries(c)
    .map(([k, n]) => `<span style="--swatch:var(--k-${k})"><i></i>${n} ${esc(noun(k, n))}</span>`)
    .join("");
}

function storyFlow(story, ws) {
  const { model, state } = ws;
  const wrap = document.createElement("section");
  wrap.className = "gx-flow";
  wrap.innerHTML = `<div class="gx-flow-head"><b>The chain</b><span class="muted">${esc(fmtFull(story.t))}</span></div>`;
  const why = document.createElement("p");
  why.className = "gx-flow-why";
  why.textContent = story.why;
  wrap.appendChild(why);
  const row = document.createElement("div");
  row.className = "gx-flow-row";
  const nodes = story.nodes.map((id) => model.byId.get(id)).filter(Boolean);
  if (story.kind !== "tick") nodes.sort((a, b) => a.t - b.t);
  nodes.forEach((e, i) => {
    if (i) {
      const ar = document.createElement("div");
      ar.className = "gx-flow-arrow";
      const gap = e.t - nodes[i - 1].t;
      ar.innerHTML = `<span>→</span><small>${Math.abs(gap) >= 1 ? (gap > 0 ? "+" : "−") + esc(dur(gap)) : ""}</small>`;
      row.appendChild(ar);
    }
    const n = document.createElement("button");
    n.type = "button";
    n.className = `gx-flow-node${e.id === state.selected ? " on" : ""}`;
    n.style.setProperty("--swatch", `var(--k-${e.kind})`);
    n.innerHTML = `<div class="k">${esc(KIND_LABEL[e.kind] || e.kind)} <span>${esc(fmtClock(e.t))}</span></div>
      <div class="t">${esc(clip(e.title, 80))}</div>
      ${story.inferred_nodes?.[e.id] ? `<div class="gx-inference">Inferred · ${esc(story.inferred_nodes[e.id])}</div>` : ""}
      <div class="muted">${esc(clip(e.summary, 110))}</div>`;
    n.addEventListener("click", (ev) => { ev.stopPropagation(); state.pinnedStory = story.id; ws.select(e.id); });
    row.appendChild(n);
  });
  wrap.appendChild(row);
  return wrap;
}

// --------------------------------------------------------------------- goals

const GOAL_GROUPS = [
  ["open", "Open", (g) => ["pending", "active", "waiting"].includes(g.state)],
  ["done", "Done", (g) => g.state === "done"],
  ["abandoned", "Let go", (g) => g.state === "abandoned"],
  ["all", "All", () => true],
];

export function drawGoals(root, ws) {
  const { model, state } = ws;
  const list = document.createElement("div");
  list.className = "gx-list";
  // Goals created or worked on in the range; status is as of this load.
  const goals = state.kinds.goal ? model.goals.filter((g) =>
    (g.t != null && g.t >= state.t0 && g.t <= state.t1)
    || (g.ticks || []).some((id) => { const t = model.byId.get(id)?.t; return t >= state.t0 && t <= state.t1; })) : [];
  const q = state.query;
  const bar = document.createElement("div");
  bar.className = "gx-seg";
  for (const [key, label, pred] of GOAL_GROUPS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = state.goalFilter === key ? "on" : "";
    b.textContent = `${label} ${goals.filter(pred).length}`;
    b.addEventListener("click", () => { state.goalFilter = key; ws.refresh(); });
    bar.appendChild(b);
  }
  list.appendChild(bar);
  if (!state.kinds.goal) {
    list.insertAdjacentHTML("beforeend", `<p class="gx-empty">Goals are filtered out. Turn them back on above.</p>`);
  }
  const pred = (GOAL_GROUPS.find(([k]) => k === state.goalFilter) || GOAL_GROUPS[0])[2];
  const shown = goals.filter(pred)
    .filter((g) => !q || `${g.title} ${g.kind} ${g.from} ${JSON.stringify(g.meta || {})}`.toLowerCase().includes(q))
    .sort((a, b) => (b.t || 0) - (a.t || 0));
  if (!shown.length && state.kinds.goal) {
    list.insertAdjacentHTML("beforeend", `<p class="gx-empty">No goals here${q ? ` match “${esc(q)}”` : ""} in this range.</p>`);
  }
  for (const g of shown) {
    const m = g.meta || {};
    const card = document.createElement("article");
    card.className = `gx-goal${state.selected === g.id ? " on" : ""}`;
    card.dataset.id = g.id;
    card.tabIndex = 0;
    const ticks = g.ticks || [];
    const last = ticks.length ? model.byId.get(ticks[ticks.length - 1]) : null;
    card.innerHTML = `
      <div class="gx-goal-top">
        <span class="gx-pill state-${esc(g.state)}">${esc(g.state)}</span>
        <span class="gx-pill">${esc(g.kind)}</span>
        <span class="gx-prio" title="priority ${esc(g.priority)}"><span style="width:${clamp(g.priority, 0, 1) * 100}%"></span></span>
        <span class="muted gx-goal-when">${g.t ? esc(fmtShort(g.t)) : ""}</span>
      </div>
      <h3>${esc(g.title)}</h3>
      ${m.rationale || m.about ? `<p class="gx-goal-why">${esc(clip(m.rationale || m.about, 260))}</p>` : ""}
      ${m.success ? `<p class="gx-goal-done"><b>done looks like</b> ${esc(clip(m.success, 200))}</p>` : ""}
      <div class="gx-goal-foot muted">
        <span>from ${esc(g.from || "?")}</span>
        <span>${ticks.length} tick${ticks.length === 1 ? "" : "s"} worked on it</span>
        ${last ? `<span>last ${esc(fmtShort(last.t))}</span>` : ""}
        ${g.due ? `<span>due ${esc(g.due.replace("T", " ").slice(0, 16))}</span>` : ""}
      </div>`;
    const open = () => {
      if (model.byId.has(g.id)) ws.select(g.id, { reveal: false });
      else ws.toast("This goal was filed before the loaded window; widen it to All to select it.");
    };
    card.addEventListener("click", open);
    card.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
    list.appendChild(card);
  }
  root.appendChild(list);
}

// -------------------------------------------------------------------- ledger

export function drawLedger(root, ws) {
  const { state } = ws;
  const list = document.createElement("div");
  list.className = "gx-list";
  list.tabIndex = -1;
  const rows = ws.visible().reverse();
  if (!rows.length) {
    list.innerHTML = `<p class="gx-empty">Nothing in this range${state.query ? ` matches “${esc(state.query)}”` : ""}.</p>`;
    root.appendChild(list);
    return;
  }
  let lastDay = "";
  for (const e of rows.slice(0, state.ledgerLimit)) {
    const dk = dayKey(e.t);
    if (dk !== lastDay) { list.insertAdjacentHTML("beforeend", `<h3 class="gx-day">${esc(fmtDay(e.t))}</h3>`); lastDay = dk; }
    const card = document.createElement("article");
    card.className = `gx-card${e.id === state.selected ? " on" : ""}`;
    card.dataset.id = e.id;
    card.tabIndex = 0;
    card.innerHTML = `
      <time>${esc(fmtClock(e.t))}</time>
      <div>
        <div class="who">${esc(e.title)}</div>
        <div class="sum">${esc(clip(e.summary, 180))}</div>
      </div>
      <span class="tag" style="--swatch:var(--k-${e.kind})">${esc(KIND_LABEL[e.kind] || e.kind)}</span>`;
    card.addEventListener("click", () => ws.select(e.id));
    card.addEventListener("keydown", (ev) => { if (ev.key === "Enter") ws.select(e.id); });
    list.appendChild(card);
  }
  appendMore(list, rows.length, state.ledgerLimit, "events", () => {
    state.ledgerLimit += PAGE * 2; ws.refresh({ preserve: true });
  });
  root.appendChild(list);
}

function appendMore(list, total, limit, what, more) {
  if (total <= limit) {
    list.insertAdjacentHTML("beforeend", `<p class="gx-list-end">${fmt(total)} ${what} · that's all in this range</p>`);
    return;
  }
  const b = document.createElement("button");
  b.type = "button";
  b.className = "gx-more";
  b.textContent = `Show ${fmt(Math.min(total - limit, what === "events" ? PAGE * 2 : PAGE))} more · ${fmt(total - limit)} left`;
  b.addEventListener("click", more);
  list.appendChild(b);
}
