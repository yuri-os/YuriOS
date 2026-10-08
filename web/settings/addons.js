/* The files `.env` only names, edited as what they are (SPEC §11.3).
 *
 * MCP_SERVERS points at a JSON file of other people's MCP servers, and
 * SELFIE_TEMPLATES_EXTRA at a YAML of house scenes. The knobs were editable on
 * the System and Pictures pages all along; the files were not, which meant a
 * path to a file you then had to go and write by hand. Each page here reads the
 * file through the host (world/host/house.py), edits it, and writes it back
 * through the same check the boot applies — and both say, after a save, that a
 * restart is what makes it real.
 */
import {
  argsToText, envToText, serversFromJson, textToArgs, textToEnv,
} from "./model.js";
import {
  api, button, confirmButton, download, el, pickFiles, readText, section, status,
} from "./ui.js";

/* ---- MCP servers -------------------------------------------------------------- */

function serverCard(name, entry, { onRemove, onTest }) {
  const nameBox = el("input", { className: "set-input", value: name, placeholder: "name",
    spellcheck: false, autocomplete: "off" });
  const command = el("input", { className: "set-input", value: entry.command || "",
    placeholder: "uvx, npx, node, python …", spellcheck: false, autocomplete: "off" });
  const args = el("textarea", { className: "set-input code-box", rows: 3, spellcheck: false,
    value: argsToText(entry.args), placeholder: "mcp-server-fetch\n(one argument per line)" });
  const env = el("textarea", { className: "set-input code-box", rows: 2, spellcheck: false,
    value: envToText(entry.env), placeholder: "API_KEY=…\n(one KEY=value per line)" });
  const rate = el("input", { className: "set-input", type: "number", min: "1", step: "1",
    value: entry.rate ?? "", placeholder: "house default" });
  const enabled = el("input", { type: "checkbox", className: "set-check", checked: !entry.disabled });
  const result = status("mcp-result");

  const read = () => {
    const out = { command: command.value.trim(), args: textToArgs(args.value),
      env: textToEnv(env.value) };
    if (rate.value !== "") out.rate = Number(rate.value);
    if (!enabled.checked) out.disabled = true;
    return { name: nameBox.value.trim(), entry: out };
  };

  const test = button("Test", async () => {
    test.disabled = true;
    result.say("starting it and asking for its tools…");
    try {
      const answer = await onTest(read());
      if (!answer.ok) result.say(`didn't start: ${answer.error}`, "error");
      else if (!answer.tools.length) result.say("it started, and offers no tools", "warn");
      else result.say(`${answer.tools.length} tool${answer.tools.length === 1 ? "" : "s"}: `
        + answer.tools.map((t) => t.name).join(", "), "ok");
    } catch (error) {
      result.say(error.message, "error");
    } finally {
      test.disabled = false;
    }
  });
  const field = (label, control, help = "") => el("label", { className: "mini-field" },
    el("span", { className: "mini-label", textContent: label }), control,
    help ? el("span", { className: "mini-help", textContent: help }) : null);

  const card = el("div", { className: "mcp-card" },
    el("div", { className: "mcp-card-head" },
      el("label", { className: "switch-label" }, enabled, el("span", { textContent: "on" })),
      nameBox, test, button("Remove", () => onRemove(card))),
    el("div", { className: "mcp-grid" },
      field("Command", command, "the program that runs the server"),
      field("Rate (calls/minute)", rate, "blank = TOOL_RATE_EXTERNAL"),
      field("Arguments", args),
      field("Environment", env, "what the server needs to run — keys stay in this file")),
    result.node);
  card.read = read;
  return card;
}

export async function renderMcp(container, ctx) {
  container.replaceChildren(el("p", { className: "placeholder", textContent: "loading…" }));
  let data;
  try {
    data = await api("/api/house/mcp-servers");
  } catch (error) {
    container.replaceChildren(el("p", { className: "placeholder error", textContent: error.message }));
    return;
  }
  const list = el("div", { className: "mcp-list" });
  const note = status();
  const empty = el("p", { className: "placeholder",
    textContent: "No third-party servers. She has her own hands either way." });
  const refreshEmpty = () => { empty.hidden = list.children.length > 0; };
  const add = (name, entry) => {
    list.append(serverCard(name, entry, {
      onRemove: (card) => { card.remove(); refreshEmpty(); },
      onTest: ({ name: n, entry: e }) => api("/api/house/mcp-servers/test",
        { method: "POST", body: { name: n || "test", server: e } }),
    }));
    refreshEmpty();
  };
  for (const [name, entry] of Object.entries(data.servers || {})) add(name, entry);
  refreshEmpty();

  const collect = () => {
    const servers = {};
    for (const card of list.children) {
      const { name, entry } = card.read();
      if (!name) throw new Error("every server needs a name");
      if (servers[name]) throw new Error(`two servers are called ${name}`);
      servers[name] = entry;
    }
    return servers;
  };

  const save = button("Save servers", async () => {
    save.disabled = true;
    try {
      await api("/api/house/mcp-servers", { method: "PUT", body: { servers: collect() } });
      note.say(`Saved to ${data.path}. Characters pick it up when they start — restart to apply.`, "ok");
      ctx.needsRestart("MCP servers saved");
    } catch (error) {
      note.say(error.message, "error");
    } finally {
      save.disabled = false;
    }
  }, "settings-save");

  const importBox = el("textarea", { className: "set-input code-box", rows: 6, spellcheck: false,
    placeholder: '{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}' });
  const importJson = (text) => {
    try {
      const servers = serversFromJson(text);
      for (const [name, entry] of Object.entries(servers)) {
        const existing = [...list.children].find((card) => card.read().name === name);
        if (existing) existing.remove();
        add(name, entry || {});
      }
      note.say(`Added ${Object.keys(servers).length} — save to keep them.`, "ok");
      importBox.value = "";
    } catch (error) {
      note.say(`Couldn't read that: ${error.message}`, "error");
    }
  };

  container.replaceChildren(
    data.error ? el("div", { className: "notice", textContent:
      `The file on disk is refused at boot: ${data.error}. Saving here rewrites it.` }) : null,
    section("Servers", [list, empty], {
      count: `${data.path}${data.configured ? "" : " · created on first save"}`,
      actions: [button("Add server", () => add("", { command: "", args: [] }))],
    }),
    section("Import a config", [
      el("p", { className: "sec-text", textContent: "Paste the mcpServers block a server's "
        + "README gives you (Claude Desktop, Cursor and friends use the same shape), or "
        + "load a .json file. A server with the same name is replaced." }),
      importBox,
      el("div", { className: "row-actions" },
        button("Add from text", () => importJson(importBox.value)),
        button("Load a .json file", async () => {
          const [file] = await pickFiles({ accept: ".json,application/json" });
          if (file) importJson(await readText(file));
        }),
        button("Export all", () => {
          try {
            download("mcp-servers.json", JSON.stringify({ mcpServers: collect() }, null, 2) + "\n",
              "application/json");
          } catch (error) {
            note.say(error.message, "error");
          }
        })),
    ]),
    el("div", { className: "page-actions" }, note.node, save),
  );
}

/* ---- the house scene overlay ------------------------------------------------- */

const OVERLAY_EXAMPLE = `# Merged over every character's library — a row here with the same name
# as one of hers replaces it. Slots: scenes, framings, lighting, moods, wardrobe.
scenes:
  rooftop: on a rain-slick rooftop at night, city lights below
moods:
  sleepy: half-lidded eyes, a slow smile
`;

export async function renderOverlay(container, ctx) {
  container.replaceChildren(el("p", { className: "placeholder", textContent: "loading…" }));
  let data;
  try {
    data = await api("/api/house/selfie-overlay");
  } catch (error) {
    container.replaceChildren(el("p", { className: "placeholder error", textContent: error.message }));
    return;
  }
  const note = status();
  const box = el("textarea", { className: "set-input code-box yaml-box", rows: 22,
    spellcheck: false, value: data.text || "", placeholder: OVERLAY_EXAMPLE });
  const save = button("Save overlay", async () => {
    save.disabled = true;
    try {
      await api("/api/house/selfie-overlay", { method: "PUT", body: { text: box.value } });
      note.say(`Saved to ${data.path} — restart to apply.`, "ok");
      ctx.needsRestart("scene overlay saved");
    } catch (error) {
      note.say(error.message, "error");
    } finally {
      save.disabled = false;
    }
  }, "settings-save");
  const slots = (data.slots || []).map((s) => el("li", {},
    el("code", { textContent: s.key }), ` — ${s.hint}`));

  container.replaceChildren(
    section("Overlay file", [
      box,
      el("div", { className: "row-actions" },
        button("Load a .yaml file", async () => {
          const [file] = await pickFiles({ accept: ".yaml,.yml,text/yaml" });
          if (file) {
            box.value = await readText(file);
            note.say(`Loaded ${file.name} — save to keep it.`, "ok");
          }
        }),
        button("Download", () => download("selfie-extra.yaml", box.value, "application/x-yaml")),
        confirmButton("Clear", "Clear it?", () => { box.value = ""; })),
    ], { count: `${data.path}${data.configured ? "" : " · created on first save"}` }),
    section("What goes in it", el("ul", { className: "plain" }, ...slots,
      el("li", {}, el("code", { textContent: "tool_hint" }),
        " — a sentence telling her what this register is for"))),
    el("div", { className: "page-actions" }, note.node, save),
  );
}
