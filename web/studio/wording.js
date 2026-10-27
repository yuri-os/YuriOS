/* Her camera's wording — the studio's half of forge/prompts.py (SPEC §7.6).

   The server sends every layer (shipped, house, her appearance, hers) and this
   resolves them in the page, so the preview follows each keystroke instead of a
   round trip. The rule is forge/prompts.py's, and must stay it: later layers
   win outright; within a layer the narrowest cell that sets a field wins; an
   empty field is never a value. tests/wording.test.js holds the two together. */

export const FIELD_KEYS = ["quality_preamble", "quality_postfix", "base_negative",
                           "character_negative"];

export const EVERY_MODEL = "default";

/* A target names one cell: "default", "arch:<key>" or "model:<key>". */
export const archTarget = (key) => `arch:${key}`;
export const modelTarget = (key) => `model:${key}`;

export function parseTarget(target) {
  if (target.startsWith("arch:")) return { kind: "arch", key: target.slice(5) };
  if (target.startsWith("model:")) return { kind: "model", key: target.slice(6) };
  return { kind: "default", key: "" };
}

export function emptyProfile() {
  return { default: {}, architectures: {}, models: {} };
}

export function cellOf(profile, target) {
  const { kind, key } = parseTarget(target);
  if (!profile) return {};
  if (kind === "arch") return profile.architectures?.[key] || {};
  if (kind === "model") return profile.models?.[key] || {};
  return profile.default || {};
}

/* A copy of `profile` with one field of one cell set. An emptied field is
   dropped, and an emptied cell with it, so the file never fills with blanks. */
export function withField(profile, target, field, value) {
  const next = structuredClone(profile || emptyProfile());
  next.architectures ||= {};
  next.models ||= {};
  next.default ||= {};
  const { kind, key } = parseTarget(target);
  const section = kind === "arch" ? next.architectures : kind === "model" ? next.models : null;
  const cell = section ? { ...(section[key] || {}) } : next.default;
  const text = (value || "").trim() ? value : "";
  if (text) cell[field] = text;
  else delete cell[field];
  if (section) {
    if (Object.keys(cell).length) section[key] = cell;
    else delete section[key];
  }
  return next;
}

const label = (kind, arch, model) =>
  kind === "default" ? "every model" : kind === "arch" ? `architecture ${arch}` : `model ${model}`;

/* Each field's value for (arch, model), and where it came from — the same
   answer `prompts.resolve` gives. `skip` = {layer, target} leaves one cell out,
   which is how a field shows what it would inherit if you emptied it. */
export function resolve(layers, arch, model, { skip = null } = {}) {
  const out = Object.fromEntries(FIELD_KEYS.map((key) => [key, { value: "", source: "" }]));
  for (const layer of layers) {
    const profile = layer.profile || emptyProfile();
    const cells = [
      ["default", EVERY_MODEL, profile.default],
      ["arch", archTarget(arch), arch ? profile.architectures?.[arch] : null],
      ["model", modelTarget(model), model ? profile.models?.[model] : null],
    ];
    for (const [kind, target, cell] of cells) {
      if (!cell) continue;
      if (skip && skip.layer === layer.name && skip.target === target) continue;
      for (const key of FIELD_KEYS) {
        if ((cell[key] || "").trim()) {
          out[key] = { value: cell[key], source: `${layer.name} · ${label(kind, arch, model)}` };
        }
      }
    }
  }
  return out;
}

/* The camera a target is about: an architecture alone, or a model and the
   architecture it was sniffed as. */
export function cameraFor(target, models) {
  const { kind, key } = parseTarget(target);
  if (kind === "arch") return { arch: key, model: "" };
  if (kind === "model") {
    const known = (models || []).find((m) => m.key === key);
    return { arch: known?.architecture || "", model: key };
  }
  return { arch: "", model: "" };
}

/* What a field in `scope` would be if this cell said nothing: the layers up to
   and including that scope, resolved for the target's camera, minus the cell. */
export function inherited(layers, scope, target, models) {
  const upto = layers.findIndex((layer) => layer.name === scope);
  const { arch, model } = cameraFor(target, models);
  return resolve(upto < 0 ? layers : layers.slice(0, upto + 1), arch, model,
                 { skip: { layer: scope, target } });
}

/* The prompt skeleton her camera sends right now: register around who she is,
   the scene left as a marker. Mirrors Character.assemble. */
export function assemble(resolved, identity, negativeExtra = "", sceneMarker = "‹scene›") {
  const join = (parts) => parts.map((p) => (p || "").trim()).filter(Boolean).join(" ");
  return {
    positive: join([resolved.quality_preamble.value, identity, sceneMarker,
                    resolved.quality_postfix.value]),
    negative: join([resolved.base_negative.value, resolved.character_negative.value,
                    negativeExtra]),
  };
}

/* Targets someone has already set something for, in either editable layer —
   badged in the picker so a setting doesn't hide behind a closed menu. */
export function usedTargets(layers) {
  const used = new Set();
  for (const layer of layers) {
    if (!layer.editable) continue;
    const profile = layer.profile || {};
    if (Object.keys(profile.default || {}).length) used.add(EVERY_MODEL);
    for (const key of Object.keys(profile.architectures || {})) used.add(archTarget(key));
    for (const key of Object.keys(profile.models || {})) used.add(modelTarget(key));
  }
  return used;
}
