/* The why-record for one selection (SPEC §24.4), and the tick sections the
 * Tick detail page draws too — one renderer, so the inspector and the detail
 * page never describe the same decision two ways.
 *
 * Returns HTML strings. Everything dynamic goes through `esc`; clickable
 * records carry `data-jump="<id>"`, and the caller decides what a jump means
 * (select it here, open it in Stories from the detail page).
 */
import {
  KIND_LABEL, REL_LABEL, clamp, clip, dur, esc, failed, fmt, fmtFull, fmtShort, stopped,
} from "./model.js";

export function verdictHtml(v) {
  const cls = /^ok/.test(v) ? "ok" : stopped(v) ? "warn" : "bad";
  return `<span class="gx-verdict ${cls}">${esc(v)}</span>`;
}

const pill = (text, cls = "") => `<span class="gx-pill ${cls}">${esc(text)}</span>`;
const statePill = (state) => pill(state, `state-${esc(state)}`);

/**
 * The phases of one tick, in the order it lived them.
 * `opts`: { threshold, goal (the goal object or null), calls ([{id, tool,
 * verdict}] the tick's own hands), known (id → bool: can this be jumped to) }.
 */
export function tickSections(e, d, { threshold = 0.4, goal = null, calls = [], known = () => true } = {}) {
  let html = "";
  if (d.sensed && d.sensed.length) {
    html += `<h3 class="gx-h">SENSE</h3><div class="gx-chips">` + d.sensed.map((s) =>
      known(s.id)
        ? `<button type="button" class="gx-chip" data-jump="${esc(s.id)}">${esc(s.type)}</button>`
        : `<span class="gx-chip ghost" title="presence chatter is not kept as an event">${esc(s.type)}</span>`).join("") + `</div>`;
  } else {
    html += `<h3 class="gx-h">SENSE</h3><p class="muted gx-small">Nothing new on the bus.</p>`;
  }
  if (d.appraised && d.appraised.length) {
    html += `<h3 class="gx-h">APPRAISE <span class="muted">· Gate 1 at ${esc(threshold)}</span></h3>`;
    for (const a of d.appraised) {
      const score = Number(a.score);
      const pct = Number.isFinite(score) ? clamp(score, 0, 1) * 100 : 0;
      const won = a.what === d.intention;
      html += `<div class="gx-appraise${won ? " won" : ""}">
          <div class="lbl"><span>${won ? "▸ " : ""}${esc(clip(a.what, 70))}</span><span>${Number.isFinite(score) ? score.toFixed(2) : "—"}</span></div>
          <div class="gx-score" title="score ${Number.isFinite(score) ? score.toFixed(2) : "?"}; the line is Gate 1"><span style="width:${pct}%"></span><i style="left:${clamp(threshold, 0, 1) * 100}%"></i></div>
          ${a.why ? `<div class="muted gx-small">${esc(a.why)}</div>` : ""}</div>`;
    }
  }
  html += `<div class="gx-block why"><b>DECIDE</b> ${esc(d.intention)}
      ${d.runners_up?.length ? `<div class="muted">not: ${esc(d.runners_up.join(" · "))}</div>` : ""}</div>`;
  if (goal) {
    html += `<button type="button" class="gx-block" data-jump="${esc(goal.id)}"><b>for the goal</b> ${statePill(goal.state)}<div>${esc(goal.title)}</div></button>`;
  }
  const acted = d.acted || {};
  html += `<div class="gx-block"><b>ACT</b> ${esc(acted.what || "rest")} → ${esc(acted.result || "")}${acted.state ? `<div class="muted">goal now ${esc(acted.state)}</div>` : ""}</div>`;
  // The tick's own account of a step against the call log's: when they differ,
  // the call log is the call's own record, and the page says so rather than
  // quietly trusting whichever it read first.
  for (const c of calls) {
    if (c.tool !== acted.tool || !failed(c.verdict) || failed(acted.verdict || "ok")) continue;
    html += `<button type="button" class="gx-block warn" data-jump="${esc(c.id)}"><b>records disagree</b><div>This tick says the step was ok, but the call log says ${esc(c.tool)} → ${esc(c.verdict)}.</div></button>`;
  }
  const hands = d.hands || {};
  if (hands.blocked) html += `<div class="gx-block warn"><b>hands blocked</b> ${esc(hands.blocked)}</div>`;
  else if (hands.available && hands.available.length) {
    html += `<div class="muted gx-small gx-hands">hands on offer: ${esc(hands.available.join(", "))}${hands.more ? ` +${hands.more}` : ""}</div>`;
  }
  const it = d.interrupt || {};
  if (it.outcome) {
    html += `<h3 class="gx-h">Gate 2 · interrupt <span class="muted">${esc(it.score)} vs ${esc(it.threshold)} → ${esc(it.outcome)}</span></h3>`;
    if (it.goal) html += `<p class="muted gx-small">about: ${esc(it.goal)}</p>`;
    html += `<dl class="gx-kv factors">` + Object.entries(it.factors || {})
      .map(([k, v]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(v)}</dd>`).join("") + `</dl>`;
  }
  return html;
}

export function goalSections(g, model) {
  if (!g) return "";
  const m = g.meta || {};
  let html = `<div class="gx-chips">${statePill(g.state)}${pill(g.kind)}${pill(`priority ${g.priority}`)}${g.commit ? pill(g.commit) : ""}</div>`;
  html += `<dl class="gx-kv"><dt>from</dt><dd>${esc(g.from || "?")}</dd>`
    + (g.due ? `<dt>due</dt><dd>${esc(g.due.replace("T", " "))}</dd>` : "")
    + (m.steps != null ? `<dt>steps</dt><dd>${esc(m.steps)}${m.last_step ? ` · last ${esc(String(m.last_step).replace("T", " "))}` : ""}</dd>` : "")
    + (m.capability ? `<dt>capability</dt><dd>${esc(m.capability)}</dd>` : "") + `</dl>`;
  for (const [k, label] of [["about", "what prompted it"], ["source", "her words"], ["rationale", "why"],
    ["evidence", "evidence"], ["success", "done looks like"], ["first_action", "first action"],
    ["strategy_note", "strategy note"]]) {
    if (m[k]) html += `<div class="gx-block${k === "rationale" ? " why" : ""}"><b>${label}</b>\n${esc(m[k])}</div>`;
  }
  const desk = `workspace/goals/${g.id}.md`;
  html += `<div class="gx-actions"><button type="button" class="gx-quiet" data-desk="${esc(desk)}">read her desk file</button>`
    + `<button type="button" class="gx-quiet" data-go="#/vault/file/${esc(desk)}">open in vault</button></div><div class="gx-desk" hidden></div>`;
  const ticks = (g.ticks || []).map((id) => model.byId.get(id)).filter(Boolean);
  if (ticks.length) {
    html += `<h3 class="gx-h">Worked on in ${ticks.length} tick${ticks.length === 1 ? "" : "s"} in this window</h3><div class="gx-stack">`;
    html += ticks.map((t) => `<button type="button" class="gx-rowbtn" data-jump="${esc(t.id)}"><time>${esc(fmtShort(t.t))}</time><span>${esc(clip(t.summary || t.title, 90))}</span></button>`).join("");
    html += `</div>`;
  }
  return html;
}

/* What a call returned, or, for a failure, what went wrong. The audit's result
 * is the error text; a timeout's exception text is empty, so its absence is
 * explained rather than shown as a blank. */
function callOutcome(e, d, model) {
  let html = "";
  if (!failed(d.verdict)) {
    if (d.result) html += `<div class="gx-block"><b>result</b>\n${esc(d.result)}</div>`;
  } else if (d.result) {
    html += `<div class="gx-block ${stopped(d.verdict) ? "warn" : "bad"}"><b>${stopped(d.verdict) ? "why it was stopped" : "what went wrong"}</b>\n${esc(d.result)}</div>`;
  } else if (!stopped(d.verdict)) {
    const secs = (d.duration_ms || 0) / 1000;
    html += `<div class="gx-block bad"><b>what went wrong</b>\nThe log has no message for this failure: the exception's text was empty.`
      + (secs >= 10 ? ` It ran ${secs.toFixed(1)} s, and a timeout's text is empty, so it most likely timed out. <span class="muted">(Inferred)</span>` : "") + `</div>`;
  }
  const tick = e.tick_id && model.byId.get(e.tick_id);
  const acted = tick?.detail?.acted;
  if (acted && acted.tool === d.tool) {
    const said = acted.verdict || (/\((\w+)\)\s*$/.exec(acted.result || "") || [])[1];
    if (said && failed(said) !== failed(d.verdict)) {
      html += `<button type="button" class="gx-block warn" data-jump="${esc(tick.id)}"><b>records disagree</b><div>The tick recorded this step as “${esc(acted.result || said)}”; the call log says ${esc(d.verdict)}. The call log is the call's own record.</div></button>`;
    }
  }
  return html;
}

function relatedHtml(e, model) {
  const cluster = model.clusterOf(e.id);
  cluster.delete(e.id);
  if (!cluster.size) return "";
  const items = [...cluster].map(([id, rel]) => [model.byId.get(id), rel])
    .filter(([o]) => o).sort((a, b) => a[0].t - b[0].t);
  let html = `<h3 class="gx-h">Joined to this (${items.length})</h3><div class="gx-stack">`;
  for (const [o, rel] of items) {
    const dt = o.t - e.t;
    const reasons = model.linksBetween(e.id, o.id)
      .map((l) => `${l.confidence === "inferred" ? "Inferred" : "Recorded"}: ${l.reason || REL_LABEL[l.rel]}`)
      .join(" · ");
    html += `<button type="button" class="gx-rowbtn" data-jump="${esc(o.id)}" style="--swatch:var(--k-${o.kind})">
        <i></i><span class="rk">${esc(KIND_LABEL[o.kind] || o.kind)}</span>
        <span>${esc(clip(o.title, 60))}</span>
        <time title="${esc(REL_LABEL[rel] || rel)}">${Math.abs(dt) < 1 ? "same moment" : (dt > 0 ? "+" : "−") + esc(dur(dt))}</time>
        <small class="gx-evidence${reasons.startsWith("Inferred") ? " inferred" : ""}">${esc(reasons || `Associated through ${REL_LABEL[rel] || rel}`)}</small></button>`;
  }
  return `${html}</div>`;
}

/** The whole inspector body for one event. */
export function inspectHtml(e, model, { pinnedStory = null } = {}) {
  const d = e.detail || {};
  const story = model.pickStory(e.id, pinnedStory);
  const inference = story?.inferred_nodes?.[e.id];
  const why = story ? `${inference ? `Inferred association: ${inference}. ` : ""}${story.why}` : "";

  const rows = [
    ["state", e.state],
    ["tick", e.tick_id && e.tick_id !== e.id ? e.tick_id : null],
    ["corr", e.corr_id],
    ["origin", e.origin || d.origin],
    ["model", d.model],
    ["tier", d.tier],
    ["tokens", d.tokens_in != null ? `${fmt(d.tokens_in)} in · ${fmt(d.tokens_out || 0)} out` : null],
    ["messages", d.n_messages],
    ["took", d.duration_ms != null ? `${fmt(Math.round(d.duration_ms))} ms` : null],
    ["verdict", d.verdict],
    ["channel", d.channel],
    ["backend", d.backend],
    ["seed", d.seed],
    ["size", d.size],
    ["sha", d.sha ? d.sha.slice(0, 12) : null],
    ["id", e.id],
  ].filter(([, v]) => v != null && v !== "");
  let html = `<dl class="gx-kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${k === "verdict" ? verdictHtml(v) : esc(String(v))}</dd>`).join("")}</dl>`;
  if (e.ref) html += `<button type="button" class="gx-open" data-go="${esc(e.ref)}">Open the full record →</button>`;

  if (e.kind === "tick") {
    const calls = (model.byTick.get(e.id) || []).filter((c) => c.kind === "tool")
      .map((c) => ({ id: c.id, tool: c.tool, verdict: c.verdict }));
    html += tickSections(e, d, {
      threshold: model.meta.act_threshold ?? 0.4,
      goal: e.goal ? model.goalsById.get(e.goal) : null,
      calls, known: (id) => model.byId.has(id),
    });
  }
  if (e.kind === "goal") html += goalSections(model.goalsById.get(e.goal || e.id), model);
  if (e.kind === "activity") {
    html += `<div class="gx-block"><b>${esc(d.from || "·")} → ${esc(d.to)}</b><div>${esc(d.reason || "")}</div><div class="muted">cadence now ${esc(dur(d.cadence_s || 0))}</div></div>`;
  }
  if (e.kind === "signal") {
    if (e.goal && model.goalsById.has(e.goal)) {
      html += `<button type="button" class="gx-block" data-jump="${esc(e.goal)}"><b>goal</b> ${esc(model.goalsById.get(e.goal).title)}</button>`;
    }
    html += `<div class="gx-block mono"><b>payload</b>\n${esc(JSON.stringify(d.payload, null, 2))}</div>`;
  }
  if (e.kind === "commit" && d.files) {
    html += `<h3 class="gx-h">Files</h3><div class="gx-stack">` + d.files.map((f) =>
      `<button type="button" class="gx-rowbtn" data-go="#/vault/file/${esc(f.path)}"><span class="mono">${esc(f.path)}</span><time>+${esc(f.added ?? "?")} −${esc(f.deleted ?? "?")}</time></button>`).join("")
      + (d.more_files ? `<p class="muted gx-small">+${d.more_files} more in the commit</p>` : "") + `</div>`;
  }
  if (d.image) html += `<figure class="gx-photo"><img src="${esc(d.image)}" alt="${esc(clip(d.look || e.title, 120))}" loading="lazy" onerror="this.closest('figure').classList.add('missing')"><figcaption class="muted">${esc(d.file || d.selfie_id || "photo")}</figcaption></figure>`;
  if (d.look) html += `<div class="gx-block"><b>the look she asked for</b>\n${esc(d.look)}</div>`;
  if (d.ops && d.ops.length) {
    html += `<h3 class="gx-h">${d.applied ? "Applied" : "Proposed"} (${d.ops.length})</h3>`;
    html += d.ops.map((op) => `<div class="gx-block"><b>${esc(op.op || "op")}</b> <span class="muted">${esc(op.section || "")}${op.confidence != null ? ` · confidence ${esc(op.confidence)}` : ""}</span><div>${esc(op.text || "")}</div></div>`).join("");
  }
  if (d.quarantined && d.quarantined.length) html += `<div class="gx-block warn"><b>quarantined</b>\n${esc(d.quarantined.join("\n"))}</div>`;
  if (e.kind === "chat" && d.unwound) html += `<div class="gx-block warn"><b>unwound</b>\nThis line was rolled back out of her window (a barged-in turn); it stays on the page because it was said.</div>`;
  if (d.text) html += `<div class="gx-block prose">${esc(d.text)}</div>`;
  if (d.asked) html += `<div class="gx-block"><b>asked</b>\n${esc(d.asked)}</div>`;
  if (d.completion) html += `<div class="gx-block prose"><b>came back</b>\n${esc(d.completion)}</div>`;
  if (d.args && Object.keys(d.args).length) html += `<div class="gx-block mono"><b>args</b>\n${esc(JSON.stringify(d.args, null, 2))}</div>`;
  if (e.kind === "tool") html += callOutcome(e, d, model);
  else if (d.result) html += `<div class="gx-block"><b>result</b>\n${esc(d.result)}</div>`;
  if (d.prompt) html += `<details class="gx-block"><summary><b>image prompt</b></summary>${esc(d.prompt)}</details>`;
  if (d.exchange) html += `<details class="gx-block"><summary><b>the exchange it read</b></summary>${esc(d.exchange)}</details>`;

  html += relatedHtml(e, model);
  return { why, html };
}

/** The inspector with nothing selected: where she is, as of this load. */
export function snapshotHtml(model) {
  const meta = model.meta;
  const cur = meta.current_state || "—";
  const b = meta.budget || {};
  let html = `<dl class="gx-kv">
      <dt>state</dt><dd>${esc(cur)}</dd>
      <dt>cadence</dt><dd>${meta.cadence_s ? `${esc(dur(meta.cadence_s))} between heartbeats` : "—"}</dd>
      <dt>budget</dt><dd>${b.spent_tokens != null ? `${fmt(b.spent_tokens)} tokens · ${fmt(b.calls || 0)} calls on ${esc(b.date || "?")}` : "—"}</dd>
    </dl>`;
  html += contextSpark(model.data.context || []);
  const jump = (e, label, cls = "") => `<button type="button" class="gx-block ${cls}" data-jump="${esc(e.id)}"><b>${esc(label)}</b> <span class="muted">${esc(fmtShort(e.t))}</span>
      <div>${esc(e.title)}</div><div class="muted">${esc(clip(e.summary, 220))}</div></button>`;
  const la = meta.last_acted && model.byId.get(meta.last_acted);
  if (la) html += jump(la, "last decision", "why");
  const lc = meta.last_chat && model.byId.get(meta.last_chat);
  if (lc) html += jump(lc, "last in the room");
  const open = model.goals.filter((g) => ["pending", "active", "waiting"].includes(g.state));
  if (open.length) {
    html += `<h3 class="gx-h">Open goals</h3>`;
    html += open.map((g) => `<button type="button" class="gx-block" ${model.byId.has(g.id) ? `data-jump="${esc(g.id)}"` : `data-go="#/goals"`}>
        ${statePill(g.state)} ${esc(g.title)}
        <div class="muted">${esc(g.kind)} · p${esc(g.priority)} · from ${esc(g.from)} · ${(g.ticks || []).length} ticks in window</div></button>`).join("");
  }
  html += `<p class="gx-note">Private surface: traces, prompts, tool logs and the corpus never leave with an exported card.</p>`;
  return { why: meta.situation || "Select anything for its why-record.", html };
}

function contextSpark(rows) {
  // From the counts where they are there: `pct` is a percentage on disk, but
  // used/limit cannot be misread as one scale or the other.
  const pct = (r) => (r.limit > 0 && Number.isFinite(r.used) ? (r.used / r.limit) * 100 : r.pct);
  rows = rows.filter((r) => Number.isFinite(pct(r)));
  if (rows.length < 2) return "";
  const w = 300, h = 44;
  const t0 = rows[0].t, t1 = rows[rows.length - 1].t || t0 + 1;
  const pts = rows.map((r) => `${(((r.t - t0) / Math.max(1, t1 - t0)) * w).toFixed(1)},${(h - (clamp(pct(r), 0, 100) / 100) * h).toFixed(1)}`).join(" ");
  const last = rows[rows.length - 1];
  return `<div class="gx-spark"><div class="gx-spark-head"><b>context window</b><span class="muted">last ${esc(Math.round(pct(last)))}% · ${fmt(last.used)} / ${fmt(last.limit)}</span></div>
      <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="Context window use over time">
        <line x1="0" x2="${w}" y1="${h * 0.2}" y2="${h * 0.2}" class="gx-spark-guide"></line>
        <polyline points="${pts}"></polyline></svg>
      <div class="gx-spark-foot muted"><span>${esc(fmtShort(t0))}</span><span>${esc(fmtShort(t1))}</span></div></div>`;
}

/** The header lines above the why: the kind pill and the full time. */
export function metaHtml(e) {
  return `<span class="gx-pill" style="--swatch:var(--k-${e.kind})"><i></i>${esc(KIND_LABEL[e.kind] || e.kind)}</span>
      <span>${esc(fmtFull(e.t))}</span>`;
}
