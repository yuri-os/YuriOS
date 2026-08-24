/* The draft model — the page's half of `characters/studio.py`.
 *
 * Pure functions only: the shape of a draft, the section layout the form renders
 * from, the token estimate the budget panel shows, and validation. Kept apart
 * from studio.js so it can be reasoned about (and diffed against the server's
 * `to_card_data`) without a DOM in the way.
 */

export const EMPTY_DRAFT = Object.freeze({
  name: "", nickname: "", creator: "", character_version: "1.0.0", tags: [], drives: [],
  identity: "", history: "", appearance: "", manner: "", personality: "",
  scenario: "", first_mes: "", alternate_greetings: [], group_only_greetings: [],
  examples: [], system_prompt: "", post_history_instructions: "", creator_notes: "",
  lorebook: { scan_depth: 4, token_budget: 600, recursive_scanning: false, entries: [] },
});

/* The page, top to bottom. `constitution: true` marks the fields §23 locks
 * against *her* — the studio may edit them, but never by accident: they render
 * behind an explicit unlock so a stray keystroke cannot rewrite her limits. */
export const SECTIONS = Object.freeze([
  {
    id: "identity", label: "Identity", eyebrow: "Who she is",
    blurb: "The immutable half of the soul. She can read every limit she runs under; she can never hold the pen that rewrites them.",
    fields: [
      { key: "name", label: "Name", type: "text", required: true },
      { key: "nickname", label: "Nickname", type: "text", hint: "Replaces {{char}} when set" },
      { key: "character_version", label: "Version", type: "text" },
      { key: "creator", label: "Creator", type: "text" },
      { key: "tags", label: "Tags", type: "chips" },
      { key: "identity", label: "Identity", type: "textarea", constitution: true },
      { key: "history", label: "History", type: "textarea", constitution: true },
    ],
  },
  {
    id: "manner", label: "Manner", eyebrow: "How she comes across",
    blurb: "The editable half — appearance, manner, and the personality line the card carries verbatim.",
    fields: [
      { key: "appearance", label: "Appearance", type: "textarea" },
      { key: "manner", label: "Manner", type: "textarea" },
      { key: "personality", label: "Personality line", type: "text", hint: "A short comma-separated register" },
      { key: "drives", label: "Drives and values", type: "list", rows: 2,
        hint: "Durable motivations for private planning — not executable tasks" },
      { key: "system_prompt", label: "Voice law", type: "textarea", constitution: true },
      { key: "post_history_instructions", label: "Hard limits", type: "textarea", constitution: true,
        hint: "Sent after the history — the last thing read before she replies" },
    ],
  },
  {
    id: "scenario", label: "Scenario", eyebrow: "Where she is",
    fields: [
      { key: "scenario", label: "Scenario", type: "textarea" },
      { key: "__setting", type: "setting" },
      { key: "first_mes", label: "Cold open", type: "textarea",
        hint: "The first thing she ever says to someone who imports this card" },
      { key: "alternate_greetings", label: "Return greetings", type: "list",
        hint: "For someone she has met before" },
    ],
  },
  {
    id: "examples", label: "Examples", eyebrow: "Demonstrated voice",
    blurb: "Each block ships as one <START> exchange. This is the cheapest way to fix a voice — spend here before you spend on prose.",
    fields: [{ key: "examples", label: "Example exchanges", type: "list", rows: 5 }],
  },
  {
    id: "lore", label: "Lore", eyebrow: "Fires on keywords",
    fields: [{ key: "lorebook", label: "Lorebook", type: "lorebook" }],
  },
  {
    id: "notes", label: "Notes", eyebrow: "For whoever imports her",
    fields: [{ key: "creator_notes", label: "Creator notes", type: "textarea",
               hint: "Never sent to the model — this is the card's README" }],
  },
  { id: "image", label: "Face", eyebrow: "The card image", fields: [{ key: "__image", type: "image" }] },
  {
    id: "selfies", label: "Selfies", eyebrow: "Her camera's vocabulary",
    blurb: "A selfie is composed as scene + framing + wardrobe + lighting + mood, and these are the rows those slots can name. Hers replaces the shipped library outright — she is not our character, and the house book is full of our character's world.",
    fields: [{ key: "__selfiebook", type: "selfiebook" }],
  },
  { id: "export", label: "Export", eyebrow: "Take her out", fields: [{ key: "__export", type: "export" }] },
]);

export const BUDGETS = Object.freeze({
  description: [150, 300], personality: [40, 80], scenario: [30, 60], first_mes: [80, 200],
});

export function normalise(value) {
  const draft = structuredClone(EMPTY_DRAFT);
  if (!value || typeof value !== "object") return draft;
  for (const key of Object.keys(draft)) {
    if (!(key in value)) continue;
    const current = draft[key];
    if (typeof current === "string") draft[key] = String(value[key] ?? "");
    else if (Array.isArray(current)) draft[key] = (value[key] || []).map(String).filter((s) => s.trim());
    else if (current && typeof current === "object") draft[key] = normaliseLorebook(value[key]);
  }
  return draft;
}

function normaliseLorebook(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    scan_depth: Number(source.scan_depth) || 4,
    token_budget: Number(source.token_budget) || 600,
    recursive_scanning: Boolean(source.recursive_scanning),
    entries: (source.entries || []).map((entry) => ({
      name: String(entry?.name || ""),
      keys: Array.isArray(entry?.keys) ? entry.keys.map(String)
        : String(entry?.keys || "").split(",").map((k) => k.trim()).filter(Boolean),
      content: String(entry?.content || ""),
      constant: Boolean(entry?.constant),
      use_regex: Boolean(entry?.use_regex),
      case_sensitive: Boolean(entry?.case_sensitive),
    })).filter((entry) => entry.keys.length && entry.content.trim()),
  };
}

/* The backbone, concatenated the way `soul.yaml` maps it into `description`.
 * Mirrors `Draft.description` on the server; the parity test pins them together. */
export function description(draft) {
  return [draft.identity, draft.history, draft.appearance, draft.manner]
    .filter((part) => part && part.trim()).join("\n\n");
}

/* chars/4, halves up. `estimate_tokens` in characters/exporter.py has to agree
   with this exactly — Python's round() is banker's rounding and would report 28
   where Math.round reports 29 — or the budget panel and the shipped report
   disagree about the same text. Pinned by the parity test. */
export function tokens(text) {
  return text ? Math.max(1, Math.floor(text.length / 4 + 0.5)) : 0;
}

export function problems(draft) {
  const found = [];
  if (!draft.name.trim()) found.push({ field: "name", message: "She needs a name." });
  if (!draft.first_mes.trim() && !draft.alternate_greetings.length) {
    found.push({ field: "first_mes", message: "A card with no first message cannot be imported anywhere." });
  }
  if (!description(draft).trim()) found.push({ field: "identity", message: "Say something about who she is." });
  return found;
}

export function equal(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
