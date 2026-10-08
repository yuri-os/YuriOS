/* One character, from House settings (SPEC §11.4).
 *
 * The house page edits the file underneath every character; this edits what is
 * hers on top of it, in three tabs:
 *
 * - **Settings** — her own brain (blank = inherit the house, applied to her
 *   running conversation at once), her profile and background switches (the
 *   registry record the switchboard drawer edits), and her own channel
 *   credentials. These ride the page's one save bar with the house's edits, so
 *   "what have I not saved" has one answer.
 * - **Night jobs** — `vault/dreams/*.md`, edited, created, deleted, and moved in
 *   and out as files: one job's .md, all of them as a zip, or copied straight
 *   into another character. Works with her stopped.
 * - **Selfie scenes** — her camera's library as a unit: export it, import a YAML
 *   to merge into hers or replace it, copy another character's, or go back to
 *   the shipped one. The studio still edits it row by row.
 *
 * Her settings panel is drawn once per visit and kept (detached while another
 * character is shown) so an edit survives looking at somebody else.
 */
import { countRows, jobTemplate, mergeBooks } from "./model.js";
import {
  api, button, confirmButton, download, el, pickFiles, readText, section, status,
} from "./ui.js";

const S = window.YuriOSSettings;
const enc = encodeURIComponent;

const BRAIN_LABELS = {
  chat_model: "Chat model", utility_model: "Utility model",
  chat_thinking: "Think before replying", chat_reasoning_effort: "Reply reasoning effort",
  utility_thinking: "Think during utility work", temperature: "Temperature",
  max_reply_tokens: "Reply token ceiling", context_length: "Context window (tokens)",
};

/* ---- settings ------------------------------------------------------------------ */

function plainRow({ key, label, help, control, read, inert = "" }, form) {
  const row = el("label", { className: "set-row" },
    el("div", { className: "set-key" }, el("span", { className: "set-label", textContent: label })),
    el("div", { className: "set-ctl" }, control));
  row.dataset.key = key;
  row.dataset.type = control.type === "checkbox" ? "bool" : "text";
  if (help) row.append(el("div", { className: "set-help", textContent: help }));
  if (inert) {
    control.disabled = true;
    row.append(el("div", { className: "set-pending", textContent: inert }));
  } else if (form) {
    form.track("profile", key, read);
  }
  return row;
}

function textRow(key, label, value, help, form, extra = {}) {
  const input = el("input", { className: "set-input", type: "text", value: value ?? "",
    autocomplete: "off", spellcheck: false, ...extra });
  return plainRow({ key, label, help, control: input, read: () => input.value }, form);
}

function selectRow(key, label, value, options, help, form) {
  const select = el("select", { className: "set-input" },
    ...options.map(([v, text]) => el("option", { value: v, textContent: text,
      selected: String(value ?? "") === v })));
  return plainRow({ key, label, help, control: select, read: () => select.value }, form);
}

function switchRow(key, label, value, help, form, inert = "") {
  const box = el("input", { type: "checkbox", className: "set-check", checked: !!value });
  return plainRow({ key, label, help, control: box, read: () => box.checked, inert }, form);
}

/** Her settings tab, built once. `save()` sends whatever moved to the three
 *  places it lives; `dirty()` counts it for the page's save bar. */
export async function characterSettings(character, { houseKeys }) {
  const id = character.id;
  const say = status();
  const [profile, brain, connections, channels] = await Promise.all([
    api(`/api/characters/${enc(id)}/profile`).then((d) => d.settings ?? d),
    api(`/api/characters/${enc(id)}/brain`),
    api("/api/connections").catch(() => ({ profiles: [] })),
    // her own channel credentials live in her runtime's view of the house file
    // (TELEGRAM_BOT_TOKEN_<HER ID>), so they are only reachable while she runs
    api(`/api/characters/${enc(id)}/settings`).catch(() => null),
  ]);
  const form = S.createForm((text) => say.say(text, "error"));
  const parked = !!profile.review_required;
  const profileForm = parked ? null : form;

  const brainRows = brain.fields.map((f) => form.brainRow({ ...f, label: BRAIN_LABELS[f.key] || f.key }));
  const keyWarning = brain.effective?.api_key_env && !brain.key_configured
    ? el("p", { className: "sec-text warn", textContent:
      `${brain.effective.api_key_env} is not set — a hosted model will refuse her calls until it is.` })
    : null;

  const profiles = (connections.profiles || []).map((p) => [p.name, `${p.name} · ${p.backend}`]);
  if (!profiles.some(([name]) => name === profile.connection_profile))
    profiles.unshift([profile.connection_profile || "default", profile.connection_profile || "default"]);

  const who = [
    textRow("name", "Display name", profile.name, "what the switchboard and her rooms call her",
      profileForm, { maxLength: 80 }),
    textRow("voice", "Voice", profile.voice, "her TTS voice id — blank uses the house voice",
      profileForm, { placeholder: "house default" }),
    selectRow("connection_profile", "Connection profile", profile.connection_profile, profiles,
      "which host-owned endpoint and key her model calls go through", profileForm),
    selectRow("body_backend", "Body", profile.body_backend,
      [["", "house default"], ["vrm", "VRM (3D)"], ["live2d", "Live2D"]],
      "which body her rooms and the desktop window draw", profileForm),
    textRow("body_model", "Body model", profile.body_model, "her VRM or Live2D asset id",
      profileForm, { placeholder: "character asset id" }),
  ];
  const work = [
    switchRow("enabled", "Run her on this node", profile.enabled,
      "off stops her runtime; her files stay", profileForm),
    switchRow("mind", "Mind loop", profile.mind, "her life between conversations", profileForm),
    switchRow("utility", "Utility work", profile.utility,
      "summaries and memory after turns", profileForm),
    switchRow("dream", "Night consolidation", profile.dream, "her night jobs (DREAM)", profileForm),
    switchRow("hands", "Her hands", profile.hands, "her tools, in a reply and on her own",
      profileForm, profile.hands_available ? "" : "the house has hands off (Hands › Hands)"),
    switchRow("notify", "Notify me on the desktop", profile.notify,
      "a desktop notification when she reaches out", profileForm,
      profile.notify_available ? "" : "the house has notifications off (Channels)"),
  ];

  // only the keys that are hers by name; the shared ones are the house page's
  const own = (channels?.groups || []).flatMap((g) => g.fields)
    .filter((f) => /^TELEGRAM_/.test(f.key) && !houseKeys.has(f.key));
  const channelBody = own.length
    ? own.map((f) => form.fieldRow({ ...f, label: f.key.startsWith("TELEGRAM_BOT_TOKEN")
      ? "Her Telegram bot token" : "Her Telegram chat id" }, { scope: "henv" }))
    : [el("p", { className: "sec-text", textContent: channels
      ? "She uses the house's Telegram pair (Channels page), or none."
      : "Start her to edit her own Telegram bot — the key is named after her and read by her runtime." })];

  const panel = el("div", { className: "char-settings" },
    parked ? el("div", { className: "notice", textContent: "Imported and waiting for approval: "
      + "her profile is read-only here until you approve her on the switchboard. "
      + "Her model can still be set." }) : null,
    section("Her model", [
      el("p", { className: "sec-text", textContent: brain.running
        ? "Hers alone. Blank inherits the house value. Saved, it reaches her running conversation at once."
        : "Hers alone. Blank inherits the house value. She is not running; it applies when she starts." }),
      keyWarning, ...brainRows]),
    section("Who she is", who),
    section("Background work", work),
    section("Her channels", channelBody),
    section("Her card", el("p", { className: "sec-text" },
      "Her description, personality, scenario and first message are her card — ",
      el("a", { className: "text-link", href: `/studio/?character=${enc(id)}`,
        textContent: "edit them in the studio" }), ".")),
    say.node);

  async function save() {
    const diffs = form.diffs();
    const said = [];
    if (Object.keys(diffs.brain || {}).length) {
      const res = await api(`/api/characters/${enc(id)}/brain`, { method: "PATCH", body: diffs.brain });
      form.settle("brain", diffs.brain);
      said.push(res.applied?.length && res.running ? "her model applied" : "her model saved");
    }
    if (Object.keys(diffs.profile || {}).length) {
      await api(`/api/characters/${enc(id)}/profile`, { method: "PATCH", body: diffs.profile });
      form.settle("profile", diffs.profile);
      said.push("her profile saved");
    }
    if (Object.keys(diffs.henv || {}).length) {
      const res = await api(`/api/characters/${enc(id)}/settings`, { method: "POST", body: diffs.henv });
      form.settle("henv", diffs.henv, res.written || []);
      said.push("her channel saved — restart to apply");
    }
    return said.length ? `${character.name}: ${said.join(", ")}` : "";
  }

  const dirtyKeys = () => Object.values(form.diffs()).flatMap((d) => Object.keys(d || {}));
  return { panel, save, dirtyKeys };
}

/* ---- night jobs ------------------------------------------------------------------ */

export async function renderJobs(container, character, characters) {
  const id = character.id;
  const note = status();
  const list = el("div", { className: "job-list" });
  const editor = el("div", { className: "job-editor" });
  const replace = el("input", { type: "checkbox", className: "set-check" });
  let data = { jobs: [] };
  let current = "";

  async function load(select = current) {
    data = await api(`/api/characters/${enc(id)}/dream-jobs`);
    drawList();
    const job = data.jobs.find((j) => j.name === select) || data.jobs[0];
    if (job) open(job.name, job.text);
    else editor.replaceChildren(el("p", { className: "placeholder",
      textContent: "No job files — her night runs on the built-in jobs alone." }));
  }

  function drawList() {
    list.replaceChildren(...data.jobs.map((job) => {
      const item = el("button", { type: "button", className: "job-item" },
        el("span", { className: "job-name", textContent: job.name }),
        el("span", { className: "job-meta", textContent: [
          job.front?.kind || "prompt",
          job.front?.enabled === false ? "off" : "",
          job.front?.standing ? "standing" : "",
          job.builtin ? "built-in" : "",
        ].filter(Boolean).join(" · ") }));
      item.classList.toggle("on", job.name === current);
      item.addEventListener("click", () => open(job.name, job.text));
      return item;
    }));
  }

  function open(name, text, { fresh = false } = {}) {
    current = name;
    drawList();
    const job = data.jobs.find((j) => j.name === name);
    const box = el("textarea", { className: "set-input code-box job-box", rows: 24,
      spellcheck: false, value: text });
    const others = characters.filter((c) => c.id !== id);
    const target = el("select", { className: "set-input inline-select" },
      ...others.map((c) => el("option", { value: c.id, textContent: c.name })));
    const save = button(fresh ? "Create job" : "Save job", async () => {
      save.disabled = true;
      try {
        await api(`/api/characters/${enc(id)}/dream-jobs/${enc(name)}`,
          { method: "PUT", body: { text: box.value } });
        note.say(data.running ? `Saved ${name} — in tonight's roster now.`
          : `Saved ${name} — read when she next starts.`, "ok");
        await load(name);
      } catch (error) {
        note.say(error.message, "error");
      } finally {
        save.disabled = false;
      }
    }, "settings-save");
    const copy = button("Copy", async () => {
      try {
        await api(`/api/characters/${enc(target.value)}/dream-jobs/${enc(name)}`,
          { method: "PUT", body: { text: box.value } });
        note.say(`Copied ${name} to ${target.selectedOptions[0]?.textContent}.`, "ok");
      } catch (error) {
        note.say(error.message, "error");
      }
    });
    const remove = fresh ? null : confirmButton(job?.builtin ? "Revert to built-in" : "Delete",
      job?.builtin ? "Revert it?" : "Delete it?", async () => {
        try {
          await api(`/api/characters/${enc(id)}/dream-jobs/${enc(name)}`, { method: "DELETE" });
          note.say(job?.builtin ? `${name} is back to its built-in prompt.` : `Deleted ${name}.`, "ok");
          current = "";
          await load();
        } catch (error) {
          note.say(error.message, "error");
        }
      });
    editor.replaceChildren(
      el("div", { className: "job-editor-head" },
        el("h3", { textContent: `${name}.md` }),
        fresh ? el("span", { className: "sec-count", textContent: "not saved yet" }) : null),
      box,
      el("div", { className: "row-actions" }, save,
        button("Download .md", () => download(`${name}.md`, box.value, "text/markdown")),
        others.length ? el("span", { className: "inline-group" },
          el("span", { className: "mini-label", textContent: "copy to" }), target, copy) : null,
        remove));
  }

  const nameBox = el("input", { className: "set-input inline-input", placeholder: "new-job-name",
    spellcheck: false, autocomplete: "off" });
  const create = button("New job", () => {
    const name = nameBox.value.trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9_-]*$/.test(name)) {
      note.say("a job name is lowercase letters, digits, - and _", "error");
      return;
    }
    if (data.jobs.some((j) => j.name === name)) {
      note.say(`${name} already exists — open it from the list`, "error");
      return;
    }
    nameBox.value = "";
    open(name, jobTemplate(name), { fresh: true });
  });
  const importJobs = button("Import .md / .zip", async () => {
    const files = await pickFiles({ accept: ".md,.zip,text/markdown,application/zip", multiple: true });
    if (!files.length) return;
    const formData = new FormData();
    for (const file of files) formData.append("files", file);
    formData.append("overwrite", replace.checked ? "true" : "false");
    try {
      const res = await api(`/api/characters/${enc(id)}/dream-jobs/import`,
        { method: "POST", form: formData });
      const parts = [];
      if (res.imported.length) parts.push(`imported ${res.imported.join(", ")}`);
      if (res.skipped.length) parts.push(`skipped ${res.skipped.join(", ")} (already hers)`);
      for (const r of res.refused) parts.push(`${r.file}: ${r.reason}`);
      note.say(parts.join(" · ") || "nothing imported", res.refused.length ? "warn" : "ok");
      await load(res.imported[0] || current);
    } catch (error) {
      note.say(error.message, "error");
    }
  });

  container.replaceChildren(
    section("Her night jobs", [
      el("p", { className: "sec-text", textContent: "Each job is a file in her Vault: YAML "
        + "frontmatter (title, kind, enabled, standing…) over the prompt she is given at night. "
        + "Import and export them as files to move a job between characters or installations. "
        + "The mind debug page's Dreams section can run any of them dry against a day." }),
      el("div", { className: "row-actions toolbar" },
        nameBox, create, importJobs,
        el("label", { className: "switch-label" }, replace,
          el("span", { textContent: "imports replace jobs with the same name" })),
        el("a", { className: "button button-quiet", href: `/api/characters/${enc(id)}/dream-jobs/export`,
          download: `${id}-night-jobs.zip`, textContent: "Export all (.zip)" })),
      note.node,
      el("div", { className: "job-split" }, list, editor),
    ]));
  try {
    await load();
  } catch (error) {
    editor.replaceChildren(el("p", { className: "placeholder error", textContent: error.message }));
  }
}

/* ---- selfie scenes ------------------------------------------------------------------ */

export async function renderScenes(container, character, characters) {
  const id = character.id;
  const note = status();
  const summary = el("div", { className: "scene-summary" });
  const yamlBox = el("textarea", { className: "set-input code-box yaml-box", rows: 20, spellcheck: false });
  const pending = el("div", { className: "scene-pending", hidden: true });
  let data = null;

  async function load() {
    data = await api(`/api/characters/${enc(id)}/selfie-templates`);
    const response = await fetch(S.apiPath(`/api/characters/${enc(id)}/selfie-templates/export`));
    yamlBox.value = response.ok ? await response.text() : "";
    summary.replaceChildren(
      el("p", { className: "sec-text", textContent: data.source === "character"
        ? `${character.name} has her own library — ${countRows(data.book)} rows.`
        : `${character.name} uses the shipped library (${countRows(data.book)} rows). `
          + "Importing or saving gives her a copy of her own." }),
      el("div", { className: "tiles" }, ...(data.slots || []).map((slot) => el("div", { className: "tile" },
        el("span", { className: "tile-label", textContent: slot.label }),
        el("span", { className: "tile-value", textContent: String(data.book.slots[slot.key]?.length || 0) }),
        el("span", { className: "tile-sub", textContent: slot.hint, title: slot.hint })))));
  }

  async function put(book, done) {
    await api(`/api/characters/${enc(id)}/selfie-templates`, { method: "PUT", body: { book } });
    note.say(`${done}${character.state && character.state !== "offline"
      ? " Her camera restarted to pick it up." : ""}`, "ok");
    pending.hidden = true;
    await load();
  }

  /** Offer the two things an incoming library can do to hers. */
  function offer(book, from) {
    pending.hidden = false;
    pending.replaceChildren(
      el("span", { textContent: `${from}: ${countRows(book)} rows.` }),
      button("Merge into hers", () => put(mergeBooks(data.book, book), `Merged ${from} into her library.`)
        .catch((error) => note.say(error.message, "error"))),
      confirmButton("Replace hers", "Replace it?", () => put(book, `Her library is now ${from}.`)
        .catch((error) => note.say(error.message, "error"))),
      button("Cancel", () => { pending.hidden = true; }));
  }

  const parse = (text) => api("/api/selfie-templates/parse", { method: "POST", body: { text } })
    .then((d) => d.book);
  const others = characters.filter((c) => c.id !== id);
  const source = el("select", { className: "set-input inline-select" },
    ...others.map((c) => el("option", { value: c.id, textContent: c.name })));

  container.replaceChildren(
    section("Her camera's library", [
      summary,
      el("div", { className: "row-actions toolbar" },
        button("Import .yaml", async () => {
          const [file] = await pickFiles({ accept: ".yaml,.yml,text/yaml" });
          if (!file) return;
          try {
            offer(await parse(await readText(file)), file.name);
          } catch (error) {
            note.say(`${file.name}: ${error.message}`, "error");
          }
        }),
        el("a", { className: "button button-quiet",
          href: `/api/characters/${enc(id)}/selfie-templates/export`,
          download: `${id}-selfie.yaml`, textContent: "Export .yaml" }),
        others.length ? el("span", { className: "inline-group" },
          el("span", { className: "mini-label", textContent: "take from" }), source,
          button("Load", async () => {
            try {
              const theirs = await api(`/api/characters/${enc(source.value)}/selfie-templates`);
              offer(theirs.book, `${source.selectedOptions[0]?.textContent}'s library`);
            } catch (error) {
              note.say(error.message, "error");
            }
          })) : null,
        el("a", { className: "button button-quiet", href: `/studio/?character=${enc(id)}`,
          textContent: "Edit rows in the studio" }),
        confirmButton("Back to shipped", "Discard hers?", async () => {
          try {
            await api(`/api/characters/${enc(id)}/selfie-templates`, { method: "DELETE" });
            note.say("She is back on the shipped library.", "ok");
            await load();
          } catch (error) {
            note.say(error.message, "error");
          }
        })),
      pending,
      note.node,
    ]),
    section("As YAML", [
      el("p", { className: "sec-text", textContent: "The whole library as one file. Saving "
        + "rewrites it from its rows, so comments do not survive — keep notes in tool_hint, "
        + "which she reads." }),
      yamlBox,
      el("div", { className: "row-actions" },
        button("Save YAML", async () => {
          try {
            await put(await parse(yamlBox.value), "Saved her library.");
          } catch (error) {
            note.say(error.message, "error");
          }
        }, "settings-save"),
        button("Download", () => download(`${id}-selfie.yaml`, yamlBox.value, "application/x-yaml"))),
    ]));
  try {
    await load();
  } catch (error) {
    summary.replaceChildren(el("p", { className: "placeholder error", textContent: error.message }));
  }
}
