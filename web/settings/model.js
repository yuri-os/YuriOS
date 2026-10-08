/* The House settings page's decisions, kept apart from the DOM (SPEC §11.2).
 *
 * The page is a layout over the server's table — /api/settings answers with the
 * fields already sorted into pages (yurios/envfile.py PAGES) — so what is left
 * to decide here is small and pure: where a URL points, what the overview says
 * about the house at a glance, and which knobs have moved off their defaults.
 * Those are the parts worth a test (web/tests/settings-page.test.js); the rows
 * themselves are /shared/settings.js, the same ones every room's gear draws.
 */

/** The pages that are not `.env` pages: the files `.env` only names. */
export const ADDON_PAGES = Object.freeze([
  { id: "mcp", title: "MCP servers", icon: "plug",
    note: "Other people's hands: third-party MCP servers whose tools she gets beside "
      + "her own. Each is spawned when a character starts; test one before you save it." },
  { id: "overlay", title: "Scene overlay", icon: "layers",
    note: "Scenes, moods and outfits of your own, merged over every character's selfie "
      + "library — the house-wide register. Kept exactly as you write it." },
]);

export const CHARACTER_TABS = Object.freeze([
  { id: "settings", title: "Settings" },
  { id: "jobs", title: "Night jobs" },
  { id: "scenes", title: "Selfie scenes" },
]);

/** `#/models`, `#/models/CHAT_MODEL`, `#/mcp`, `#/character/mia/jobs` → where
 *  to go and, optionally, a row to bring into view. Anything else is the
 *  overview. */
export function parseRoute(hash, pages = [], characters = []) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/");
  const [page = "", key = ""] = parts;
  if (page === "character") {
    const id = decodeURIComponent(parts[1] || "");
    if (!characters.some((c) => c.id === id)) return { page: "overview", key: "" };
    const tab = CHARACTER_TABS.some((t) => t.id === parts[2]) ? parts[2] : "settings";
    return { page: "character", character: id, tab, key: "" };
  }
  const known = page === "overview" || pages.some((p) => p.id === page)
    || ADDON_PAGES.some((p) => p.id === page);
  return { page: known ? page : "overview", key: known ? key : "" };
}

/* ---- selfie scene libraries as modules ------------------------------------- */

/** Fold one library (rows, as /api/selfie-templates/parse returns them) into
 *  another: a row named in both takes the incoming one, everything else from
 *  either is kept, in the order she had them then the new ones. */
export function mergeBooks(base, incoming) {
  const slots = {};
  const names = new Set([...Object.keys(base?.slots || {}), ...Object.keys(incoming?.slots || {})]);
  for (const name of names) {
    const rows = new Map((base?.slots?.[name] || []).map((row) => [row.key, row]));
    for (const row of incoming?.slots?.[name] || []) rows.set(row.key, row);
    slots[name] = [...rows.values()];
  }
  return { tool_hint: incoming?.tool_hint || base?.tool_hint || "", slots };
}

export function countRows(book) {
  return Object.values(book?.slots || {}).reduce((n, rows) => n + rows.length, 0);
}

/* ---- MCP servers in a form ----------------------------------------------------- */

/** One argument per line — the only spelling that survives an argument with a
 *  space in it, which a single command-line box would split. */
export const argsToText = (args) => (args || []).join("\n");
export const textToArgs = (text) => String(text || "").split("\n").map((a) => a.trim()).filter(Boolean);

export const envToText = (env) => Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join("\n");
export function textToEnv(text) {
  const env = {};
  for (const line of String(text || "").split("\n")) {
    const at = line.indexOf("=");
    if (at <= 0) continue;
    env[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return env;
}

/** A pasted config in any of the shapes people have one in: the whole
 *  `{"mcpServers": …}` file, or just its inside. */
export function serversFromJson(text) {
  const data = JSON.parse(text);
  const servers = data && typeof data === "object" && data.mcpServers ? data.mcpServers : data;
  if (!servers || typeof servers !== "object" || Array.isArray(servers))
    throw new Error('expected {"mcpServers": {"name": {"command": …}}}');
  return servers;
}

/** A night job's starter file: frontmatter the runner accepts, and a body. */
export function jobTemplate(name) {
  return `---\nname: ${name}\ntitle: ${name.replace(/[-_]/g, " ")}\nkind: prompt\nenabled: true\n---\n\n`
    + "You are {char}. Read back over today and …\n";
}

/** key → {field, page} across the overview and every page. */
export function indexFields(data) {
  const index = new Map();
  for (const field of data?.overview || []) index.set(field.key, { field, page: "overview" });
  for (const page of data?.pages || [])
    for (const section of page.sections || [])
      for (const field of section.fields || [])
        index.set(field.key, { field, page: page.id, section: section.title });
  return index;
}

export function isLoopback(host) {
  const h = String(host || "").trim().toLowerCase();
  return h === "" || h === "localhost" || h === "::1" || h.startsWith("127.");
}

const unset = (v) => v == null || String(v).trim() === "" || String(v).toUpperCase() === "NONE";
const onOff = (v) => (v ? "on" : "off");

/* The overview's tiles: one line each on the things that decide what she can
 * do at all — which model, which voice, whether the web and the camera are on,
 * who can reach her. Each one names the page it is changed on. A tile whose
 * knobs this build does not have is left out rather than drawn empty. */
export function glance(index) {
  const value = (key) => index.get(key)?.field.value;
  const has = (key) => index.has(key);
  const tiles = [];
  const add = (tile) => tiles.push(tile);

  if (has("CHAT_MODEL")) {
    const chat = value("CHAT_MODEL");
    add({ label: "Her model", page: "models", key: "CHAT_MODEL",
      value: unset(chat) ? "none chosen" : chat, tone: unset(chat) ? "warn" : "",
      sub: unset(chat) ? "she cannot talk until one is set" : "replies and her own thinking" });
  }
  if (has("UTILITY_MODEL")) {
    const off = has("UTILITY_ENABLED") && value("UTILITY_ENABLED") === false;
    const utility = value("UTILITY_MODEL");
    add({ label: "Utility model", page: "models", key: "UTILITY_MODEL",
      value: off ? "off" : (unset(utility) ? "none chosen" : utility),
      tone: off || unset(utility) ? "warn" : "",
      sub: "memory, summaries, night work" });
  }
  if (has("EMBED_BACKEND")) {
    add({ label: "Memory", page: "memory", key: "EMBED_BACKEND",
      value: value("EMBED_BACKEND"), sub: value("EMBED_MODEL") || "" });
  }
  if (has("MIND_ENABLED")) {
    const on = value("MIND_ENABLED") !== false;
    const night = has("MIND_DREAM_START_HOUR")
      ? `night ${value("MIND_DREAM_START_HOUR")}:00–${value("MIND_DREAM_END_HOUR")}:00` : "";
    add({ label: "Mind", page: "mind", key: "MIND_ENABLED", value: onOff(on),
      tone: on ? "" : "dim", sub: on ? night : "no life between conversations" });
  }
  if (has("MIND_TOOLS_ENABLED")) {
    const on = value("MIND_TOOLS_ENABLED") !== false && value("TOOLS_BACKEND") !== "off";
    add({ label: "Hands", page: "hands", key: "MIND_TOOLS_ENABLED", value: onOff(on),
      tone: on ? "" : "dim",
      sub: on ? (value("MIND_TOOL_ALLOWLIST") === "*" ? "every hand" : "a chosen few") : "no tools" });
  }
  if (has("SEARCH_BACKEND")) {
    const search = value("SEARCH_BACKEND");
    add({ label: "Web search", page: "web", key: "SEARCH_BACKEND", value: search,
      tone: search === "off" ? "dim" : "",
      sub: search === "searxng" ? value("SEARXNG_URL") || "" : "" });
  }
  if (has("SELFIE_BACKEND")) {
    const camera = value("SELFIE_BACKEND");
    add({ label: "Camera", page: "pictures", key: "SELFIE_BACKEND", value: camera,
      tone: camera === "off" ? "dim" : "",
      sub: camera === "openrouter" ? value("SELFIE_MODEL") || "" : "" });
  }
  if (has("TTS_BACKEND")) {
    add({ label: "Voice", page: "voice", key: "TTS_BACKEND", value: value("TTS_BACKEND"),
      sub: has("STT_BACKEND") ? `hears you with ${value("STT_BACKEND")}` : "" });
  }
  if (has("HOST")) {
    const local = isLoopback(value("HOST"));
    const token = index.get("OWNER_TOKEN")?.field.configured;
    add({ label: "Access", page: "access", key: "HOST",
      value: local ? "this machine only" : "your network",
      tone: !local && !token ? "warn" : "",
      sub: `${value("HOST") || "127.0.0.1"}:${value("PORT") ?? ""}${token ? " · owner token set" : ""}` });
  }
  if (has("TELEGRAM_BOT_TOKEN")) {
    const bot = index.get("TELEGRAM_BOT_TOKEN").field.configured;
    add({ label: "Telegram", page: "channels", key: "TELEGRAM_BOT_TOKEN",
      value: bot ? "connected" : "off", tone: bot ? "" : "dim",
      sub: bot && unset(value("TELEGRAM_CHAT_ID")) ? "waiting for its first message" : "" });
  }
  return tiles;
}

/** Fields whose value is not the one they would have with no line in `.env`. */
export function changedFromDefault(index) {
  const out = [];
  for (const [key, entry] of index) {
    const { field } = entry;
    if (field.default == null || field.type === "password") continue;
    if (String(field.value ?? "") !== String(field.default)) out.push({ key, ...entry });
  }
  return out;
}

/** Saved to `.env`, not yet what the server runs on (the server decides). */
export function pendingRestart(index) {
  return [...index].filter(([, entry]) => entry.field.pending)
    .map(([key, entry]) => ({ key, ...entry }));
}

/** How a value reads in a sentence rather than in a control. */
export function shown(value) {
  if (typeof value === "boolean") return value ? "on" : "off";
  const text = String(value ?? "");
  return text === "" ? "empty" : text;
}
