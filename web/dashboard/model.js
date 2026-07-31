const STATE_ALIASES = Object.freeze({
  active: "awake",
  running: "awake",
  ready: "awake",
  online: "awake",
  idle: "resting",
  dormant: "resting",
  paused: "resting",
  dream: "dreaming",
  sleeping: "dreaming",
  stopped: "offline",
  disabled: "offline",
  error: "attention",
  failed: "attention",
});

export const STATE_META = Object.freeze({
  awake: { label: "awake", color: "#d7ff58", rank: 0 },
  engaged: { label: "engaged", color: "#76e8bd", rank: 1 },
  dreaming: { label: "dreaming", color: "#b39cf4", rank: 2 },
  resting: { label: "resting", color: "#f4bd62", rank: 3 },
  attention: { label: "attention", color: "#ef786f", rank: 4 },
  offline: { label: "offline", color: "#69736d", rank: 5 },
  unknown: { label: "unknown", color: "#69736d", rank: 6 },
});

const ACCENTS = ["#88ad9b", "#a28dc0", "#ad8a74", "#7296a8", "#a2a970", "#a2768d"];

function text(value, fallback = "") {
  return value == null ? fallback : String(value);
}

function boolean(value) {
  if (typeof value === "string") return value.toLowerCase() === "true";
  return Boolean(value);
}

export function canonicalState(value) {
  const input = text(value, "unknown").trim().toLowerCase();
  const state = STATE_ALIASES[input] || input;
  return STATE_META[state] ? state : "unknown";
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

function flattenJournal(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.entries)) return payload.entries;
  if (Array.isArray(payload?.days)) {
    return payload.days.flatMap((day) => (day.entries || []).map((entry) => ({ ...entry, day: entry.day || day.day })));
  }
  return [];
}

export function normalizeDetailItems(kind, payload) {
  const rows = kind === "journal"
    ? flattenJournal(payload)
    : (Array.isArray(payload) ? payload : payload?.entries ?? payload?.logs ?? payload?.events ?? payload?.items ?? []);
  if (!Array.isArray(rows)) return [];
  return rows.map((item, index) => {
    if (typeof item === "string") return { id: `${kind}-${index}`, title: kind === "journal" ? "Journal entry" : "Event", body: item, time: "" };
    const row = item && typeof item === "object" ? item : {};
    return {
      id: text(row.id, `${kind}-${index}`),
      title: text(row.title ?? row.event ?? row.type ?? row.role, kind === "journal" ? "Journal entry" : "Event"),
      body: text(row.body ?? row.content ?? row.message ?? row.text ?? row.detail),
      time: text(row.timestamp ?? row.created_at ?? row.time ?? row.day),
      tone: text(row.level ?? row.tone ?? row.role),
    };
  });
}

export function contextEntries(payload) {
  const source = payload?.context && typeof payload.context === "object" ? payload.context : payload;
  if (!source || typeof source !== "object" || Array.isArray(source)) return [];
  return Object.entries(source).map(([key, value]) => ({
    key: key.replaceAll("_", " "),
    value: typeof value === "object" ? JSON.stringify(value, null, 2) : text(value),
  }));
}
