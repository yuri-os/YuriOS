import { STATE_META, canonicalState } from "../shared/activity-state.js";

export { STATE_META, canonicalState };

const ACCENTS = ["#88ad9b", "#a28dc0", "#ad8a74", "#7296a8", "#a2a970", "#a2768d"];

function text(value, fallback = "") {
  return value == null ? fallback : String(value);
}

function boolean(value) {
  if (typeof value === "string") return value.toLowerCase() === "true";
  return Boolean(value);
}

export function initials(name) {
  const words = text(name, "?").trim().split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words.at(-1)[0] : words[0]?.slice(0, 2) || "?").toUpperCase();
}

export function normalizeCharacter(raw, index = 0) {
  const source = raw && typeof raw === "object" ? raw : {};
  const runtime = source.runtime && typeof source.runtime === "object" ? source.runtime : {};
  const id = text(source.id ?? source.slug ?? source.character_id).trim();
  if (!id) return null;
  const name = text(source.name ?? source.display_name, id).trim() || id;
  const state = canonicalState(source.state ?? source.status ?? runtime.state);
  const loops = source.loops && typeof source.loops === "object" ? source.loops : {};
  return {
    id,
    name,
    state,
    stateMeta: STATE_META[state],
    loopEnabled: boolean(source.loop_enabled ?? source.loop?.enabled ?? runtime.loop_enabled),
    loops: {
      mind: boolean(loops.mind ?? source.loop_enabled ?? runtime.loop_enabled),
      utility: boolean(loops.utility ?? true),
      dream: boolean(loops.dream ?? true),
    },
    // A card this node did not write arrives parked until someone reads it
    // through (SPEC §28) — no runtime behind any of her rooms until then.
    reviewRequired: boolean(source.review_required),
    description: text(source.description ?? source.tagline ?? source.summary, "No profile note has been set."),
    avatarUrl: text(source.avatar_url ?? source.portrait_url ?? source.image_url),
    accent: text(source.accent, ACCENTS[index % ACCENTS.length]),
    model: text(source.model ?? source.model_id ?? source.settings?.model, "node default"),
    voice: text(source.voice ?? source.voice_id ?? source.settings?.voice, "default"),
    connectionProfile: text(source.connection_profile ?? source.settings?.connection_profile, "default"),
    updatedAt: source.updated_at ?? source.last_active_at ?? source.last_seen ?? null,
    raw: source,
  };
}

export function normalizeCharacters(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.characters;
  if (!Array.isArray(rows)) throw new TypeError("Character response must be an array or contain a characters array");
  return rows.map(normalizeCharacter).filter(Boolean).sort((a, b) =>
    a.stateMeta.rank - b.stateMeta.rank || a.name.localeCompare(b.name));
}

export function filterCharacters(characters, query) {
  const needle = text(query).trim().toLocaleLowerCase();
  if (!needle) return characters;
  return characters.filter((character) =>
    [character.name, character.id, character.state, character.description, character.model]
      .some((value) => text(value).toLocaleLowerCase().includes(needle)));
}

export function formatRelativeTime(value, now = Date.now()) {
  if (!value) return "no recent activity";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return text(value);
  const seconds = Math.round((date.getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (abs < 60) return formatter.format(seconds, "second");
  if (abs < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  if (abs < 604800) return formatter.format(Math.round(seconds / 86400), "day");
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: date.getFullYear() === new Date(now).getFullYear() ? undefined : "numeric" });
}

// The /log endpoint interleaves two raw row shapes with no shared vocabulary:
// tick traces ({ts: ISO string, activity_state, decided: {intention}, acted: {what, result}})
// and tool-call audits ({ts: epoch seconds, tool, verdict, result}). Neither has
// title/body/timestamp, so they need their own mapping instead of the generic one.
function logTimestamp(ts) {
  if (typeof ts === "number") return new Date(ts * 1000).toISOString();
  return text(ts);
}

// A tool result is a JSON-serialized dict (`brain.py`'s _execute), then cut
// off mid-string by the audit's 200-char cap — so it usually arrives as
// invalid, unterminated JSON. Render it as readable "key: value" lines
// instead of a raw brace-and-quote blob, tolerating the truncation.
function friendlyToolResult(raw) {
  const value = text(raw);
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return value;
  try {
    return formatResultValue(JSON.parse(trimmed));
  } catch {
    return formatTruncatedResultFields(trimmed);
  }
}

function formatResultValue(parsed) {
  if (Array.isArray(parsed)) return parsed.map((entry) => text(entry)).join(", ");
  if (!parsed || typeof parsed !== "object") return text(parsed);
  return Object.entries(parsed)
    .filter(([, fieldValue]) => fieldValue !== null && fieldValue !== undefined && fieldValue !== "")
    .map(([key, fieldValue]) => `${key}: ${typeof fieldValue === "object" ? JSON.stringify(fieldValue) : fieldValue}`)
    .join("\n");
}

function formatTruncatedResultFields(trimmed) {
  const lines = [];
  const pairPattern = /"([^"]+)":\s*"([^"]*)("|$)/g;
  let match;
  while ((match = pairPattern.exec(trimmed))) {
    const [, key, fieldValue, closed] = match;
    lines.push(closed ? `${key}: ${fieldValue}` : `${key}: ${fieldValue}…`);
  }
  return lines.length ? lines.join("\n") : trimmed.replace(/[{}[\]"]/g, "").trim();
}

function normalizeLogRow(row, index) {
  const isToolCall = "tool" in row;
  if (isToolCall) {
    return {
      id: text(row.id, `log-${index}`),
      title: row.verdict && row.verdict !== "ok" ? `${text(row.tool, "tool call")} — ${row.verdict}` : text(row.tool, "tool call"),
      body: friendlyToolResult(row.result),
      time: logTimestamp(row.ts),
      tone: text(row.verdict),
    };
  }
  return {
    id: text(row.id, `log-${index}`),
    title: text(row.decided?.intention, text(row.activity_state, "tick")),
    body: text(row.acted?.result),
    time: logTimestamp(row.ts),
    tone: row.acted?.what === "error" ? "error" : "",
  };
}

// A tick fires every few seconds, so an idle stretch is dozens of identical
// "REST / rest" rows in a row — fold consecutive rows with the same
// title+body into one line spanning the whole stretch, rather than
// spelling out every tick.
function collapseRepeatedLogRows(items) {
  const collapsed = [];
  for (const item of items) {
    const last = collapsed[collapsed.length - 1];
    if (last && last.title === item.title && last.body === item.body) {
      last.timeEnd = item.time;
      last.count += 1;
    } else {
      collapsed.push({ ...item, timeEnd: item.time, count: 1 });
    }
  }
  return collapsed;
}

export function normalizeDetailItems(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.entries ?? payload?.logs ?? payload?.events ?? payload?.items ?? [];
  if (!Array.isArray(rows)) return [];
  const items = rows.map((item, index) => {
    if (typeof item === "string") return { id: `log-${index}`, title: "Event", body: item, time: "" };
    const row = item && typeof item === "object" ? item : {};
    return normalizeLogRow(row, index);
  });
  return collapseRepeatedLogRows(items);
}

export function normalizeJournalDays(payload) {
  const days = Array.isArray(payload?.days) ? payload.days : [];
  return {
    days: days.map((row) => ({ day: text(row?.day), count: Number(row?.count) || 0 })),
    page: Number(payload?.page) || 0,
    hasMore: Boolean(payload?.has_more),
    total: Number(payload?.total) || days.length,
  };
}

export function normalizeJournalDay(payload) {
  const entries = Array.isArray(payload?.entries) ? payload.entries : [];
  return {
    day: text(payload?.day),
    entries: entries.map((row) => ({
      time: text(row?.time),
      hers: Boolean(row?.hers),
      text: text(row?.text),
    })),
  };
}

// `day` is a plain "YYYY-MM-DD" — parsed with explicit y/m/d args (not
// `new Date(day)`) so it lands on local midnight instead of UTC midnight,
// which can otherwise print as the wrong calendar day near a timezone edge.
export function formatDiaryDay(day, options) {
  const [y, m, d] = text(day).split("-").map(Number);
  if (!y || !m || !d) return text(day);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, options);
}

export function contextEntries(payload) {
  const source = payload?.context && typeof payload.context === "object" ? payload.context : payload;
  if (!source || typeof source !== "object" || Array.isArray(source)) return [];
  return Object.entries(source).map(([key, value]) => ({
    key: key.replaceAll("_", " "),
    value: typeof value === "object" ? JSON.stringify(value, null, 2) : text(value),
  }));
}
