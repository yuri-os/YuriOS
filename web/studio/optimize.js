/* "Optimize with AI" — the studio's half of `characters/optimize.py` (SPEC §30.6).
 *
 * The dialog is three panes and one rule. Setup: what you want changed, and which
 * model spends the tokens. Progress: which of the three passes is outstanding and
 * how long it has been, streamed from the route as it happens, because the run is
 * minutes long and a still dialog reads as a broken one. Review: the field-by-
 * field diff of what came back.
 * The rule is that nothing between them touches the draft — `open()` is handed
 * a getter for the current draft and a callback to apply an accepted one, and
 * the callback is only ever reached by pressing apply. A card is a file from the
 * internet; it may propose an edit to itself, and a person decides.
 *
 * The model picker is the settings panel's pattern (web/shared/settings.js), not
 * an import of it: that file is an IIFE bound to the `#settings` dialog and
 * loading it here would wire a second panel onto a page that has none. What is
 * genuinely shared is the endpoint behind it — one list of what a provider is
 * serving, so a model the settings panel offers is one the studio will run.
 */

const PROVIDERS = [
  { id: "lmstudio", label: "LM Studio", prefix: "lm_studio/" },
  { id: "ollama", label: "Ollama", prefix: "ollama/" },
  { id: "openrouter", label: "OpenRouter", prefix: "openrouter/" },
  { id: "custom", label: "Custom", prefix: "" },
];

/* The last model you optimised with, remembered per browser. Not a setting —
   it never reaches her registry record, because picking a big model to re-file
   one card says nothing about which model she should think with. */
const REMEMBERED = "yurios.studio.optimize.model";

const FIELD_LABELS = {
  name: "Name", nickname: "Nickname", creator: "Creator",
  character_version: "Version", tags: "Tags", identity: "Identity",
  history: "History", appearance: "Appearance", manner: "Manner",
  personality: "Personality line", scenario: "Scenario", first_mes: "Cold open",
  alternate_greetings: "Return greetings", examples: "Example exchanges",
  system_prompt: "Voice law", post_history_instructions: "Hard limits",
  creator_notes: "Creator notes", lorebook: "Lorebook",
};

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...kids) {
  const node = Object.assign(document.createElement(tag), props);
  for (const kid of kids) if (kid != null) node.append(kid);
  return node;
}

function splitModel(value) {
  for (const provider of PROVIDERS) {
    if (provider.prefix && value.startsWith(provider.prefix)) {
      return { provider: provider.id, model: value.slice(provider.prefix.length) };
    }
  }
  return { provider: value ? "custom" : "lmstudio", model: value };
}

function joinModel(providerId, model) {
  const name = model.trim();
  if (!name) return "";
  const provider = PROVIDERS.find((p) => p.id === providerId);
  if (!provider || !provider.prefix) return name;
  return name.startsWith(provider.prefix) ? name : provider.prefix + name;
}

/* Read an NDJSON body as it arrives. `response.json()` would wait for the last
   byte, which for this endpoint is the last pass — the whole reason the route
   streams. Lines can be split across chunks and a chunk can hold several, so the
   tail is carried forward until a newline completes it. */
async function* ndjson(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let cut;
    while ((cut = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 1);
      if (line) yield JSON.parse(line);
    }
    if (done) break;
  }
  if (buffer.trim()) yield JSON.parse(buffer.trim());
}

function clock(seconds) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/* A preview of a field's contents, whatever its shape. Lists and the lorebook
   arrive from the server already flattened to text (`optimize._render`), so the
   only thing left to decide is how much of a four-thousand-character section
   belongs in a dialog: enough to recognise what moved, and no more. */
function excerpt(text, limit = 420) {
  const value = String(text || "").replace(/\n{3,}/g, "\n\n").trim();
  if (!value) return "";
  return value.length > limit ? `${value.slice(0, limit).trimEnd()}…` : value;
}

export function createOptimizer({ getDraft, getCharacterId, onApply }) {
  const dialog = $("optimize-dialog");
  const setup = $("optimize-setup");
  const progress = $("optimize-progress");
  const stage = $("optimize-stage");
  const elapsed = $("optimize-elapsed");
  const stepList = $("optimize-steps");
  const result = $("optimize-result");
  const instructions = $("optimize-instructions");
  const providerSelect = $("optimize-provider");
  const modelInput = $("optimize-model");
  const browseButton = $("optimize-browse");
  const modelList = $("optimize-model-list");
  const modelStatus = $("optimize-model-status");
  const errorNote = $("optimize-error");
  const runButton = $("optimize-run");
  const applyButton = $("optimize-apply");
  const backButton = $("optimize-back");
  const notes = $("optimize-notes");
  const truncatedNote = $("optimize-truncated");
  const failedList = $("optimize-failed");
  const changeList = $("optimize-changes");
  if (!dialog) return { open: () => {} };

  let proposal = null;      // the last response, pending apply
  let running = false;
  let catalogue = [];       // last fetched model ids
  let catalogueFor = null;  // which provider they were fetched for
  let ticker = null;        // the 1s interval behind the elapsed clock
  let steps = new Map();    // pass index -> its <li>, built as passes announce
  let controller = null;    // aborts the run when the dialog is closed on it

  const remembered = splitModel(localStorage.getItem(REMEMBERED) || "");
  for (const provider of PROVIDERS) {
    providerSelect.append(el("option", {
      value: provider.id, textContent: provider.label,
      selected: provider.id === remembered.provider,
    }));
  }
  modelInput.value = remembered.model;

  function showError(message) {
    errorNote.hidden = !message;
    errorNote.textContent = message || "";
  }

  function hideList() { modelList.hidden = true; }

  function renderList(filter) {
    const query = filter.trim().toLowerCase();
    const shown = (query ? catalogue.filter((m) => m.toLowerCase().includes(query))
                         : catalogue).slice(0, 500);
    modelList.replaceChildren(...(shown.length
      ? shown.map((name) => {
          const option = el("button", { type: "button", className: "opt-model-opt",
                                        textContent: name });
          // mousedown, not click — the input's blur would otherwise win the race
          option.addEventListener("mousedown", (event) => {
            event.preventDefault();
            modelInput.value = name;
            hideList();
          });
          return option;
        })
      : [el("div", { className: "opt-model-empty",
                     textContent: catalogue.length ? "no match" : "nothing to show" })]));
  }

  async function browse() {
    const provider = providerSelect.value;
    if (provider === "custom") {
      modelStatus.textContent = "custom: type the full LiteLLM id";
      hideList();
      return;
    }
    if (catalogueFor === provider && catalogue.length) {
      renderList("");
      modelList.hidden = false;
      modelInput.focus();
      return;
    }
    modelStatus.textContent = "loading…";
    try {
      const query = new URLSearchParams({ provider });
      const character = getCharacterId();
      if (character) query.set("character", character);
      const response = await fetch(`/api/studio/models?${query}`);
      const data = await response.json();
      if (data.error) { modelStatus.textContent = data.error; return; }
      catalogue = data.models || [];
      catalogueFor = provider;
      modelStatus.textContent = catalogue.length
        ? `${catalogue.length} available` : "none loaded there";
      renderList("");
      if (catalogue.length) { modelList.hidden = false; modelInput.focus(); }
    } catch (error) {
      modelStatus.textContent = `couldn't load: ${error}`;
    }
  }

  function showSetup() {
    proposal = null;
    stopClock();
    setup.hidden = false;
    progress.hidden = true;
    result.hidden = true;
    runButton.hidden = false;
    applyButton.hidden = true;
    backButton.hidden = true;
  }

  function stopClock() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  /* The pane that exists so a three-call run does not look like a hung button.
     There is nothing to show a percentage of — a pass takes as long as the model
     takes — so what it shows instead is that time is passing and which of the
     three asks is outstanding, which is the honest version of the same thing. */
  function showProgress() {
    steps = new Map();
    stepList.replaceChildren();
    stage.textContent = "Sending the card…";
    setup.hidden = true;
    result.hidden = true;
    progress.hidden = false;
    runButton.hidden = true;
    applyButton.hidden = true;
    backButton.hidden = true;
    const started = Date.now();
    elapsed.textContent = "0:00";
    stopClock();
    ticker = setInterval(() => {
      elapsed.textContent = clock(Math.round((Date.now() - started) / 1000));
    }, 1000);
  }

  function stepRow(event) {
    let row = steps.get(event.index);
    if (!row) {
      row = el("li", { className: "opt-step" },
        el("span", { className: "opt-dot" }),
        el("span", { className: "opt-step-label" }),
        el("span", { className: "opt-step-note" }));
      steps.set(event.index, row);
      stepList.append(row);
    }
    row.querySelector(".opt-step-label").textContent =
      `Pass ${event.index} of ${event.total} — ${event.label}`;
    return row;
  }

  function onPass(event) {
    const row = stepRow(event);
    const note = row.querySelector(".opt-step-note");
    row.className = `opt-step is-${event.state}`;
    if (event.state === "start") {
      stage.textContent = `Working on ${event.label}…`;
      note.textContent = "thinking";
    } else if (event.state === "retry") {
      // Named rather than hidden: the retry roughly doubles this pass, and a
      // clock that runs twice as long with no explanation is the worry.
      note.textContent = "no room for an answer — asking again with more";
    } else if (event.state === "done") {
      const moved = event.fields || [];
      note.textContent = moved.length
        ? moved.map((f) => FIELD_LABELS[f] || f).join(", ")
        : "nothing to move";
    } else if (event.state === "failed") {
      note.textContent = event.message || "failed";
    }
  }

  function showResult(payload) {
    proposal = payload;
    stopClock();
    setup.hidden = true;
    progress.hidden = true;
    result.hidden = false;
    runButton.hidden = true;
    applyButton.hidden = false;
    backButton.hidden = false;
    const count = payload.changes.length;
    notes.textContent = payload.notes
      || `${count} field${count === 1 ? "" : "s"} changed.`;
    // A salvaged answer is still worth applying — the fields below are the ones
    // the model finished — but presenting it as a complete pass would have the
    // user wondering why half the card is untouched.
    truncatedNote.hidden = !payload.truncated;
    // Some passes failing while others worked is a real result, not an error:
    // the ones that worked are below and are worth applying. Saying which pass
    // fell over — and why — is what lets someone fix it and run it again.
    const failed = payload.failed || [];
    failedList.hidden = !failed.length;
    failedList.replaceChildren(...failed.map((reason) =>
      el("li", {}, el("span", { textContent: reason }))));
    changeList.replaceChildren(...payload.changes.map((change) => {
      const label = FIELD_LABELS[change.field] || change.field;
      const tag = change.filled ? "filled in" : change.emptied ? "emptied" : "rewritten";
      const item = el("li", { className: `opt-change opt-${tag.split(" ")[0]}` },
        el("header", {},
          el("strong", { textContent: label }),
          el("span", { className: "opt-tag", textContent: tag })));
      const before = excerpt(change.before);
      const after = excerpt(change.after);
      if (before) {
        item.append(el("div", { className: "opt-side opt-before" },
          el("span", { className: "opt-side-label", textContent: "was" }),
          el("p", { textContent: before })));
      }
      if (after) {
        item.append(el("div", { className: "opt-side opt-after" },
          el("span", { className: "opt-side-label", textContent: "now" }),
          el("p", { textContent: after })));
      }
      return item;
    }));
    result.scrollTop = 0;
  }

  async function run() {
    if (running) return;
    running = true;
    showError("");
    const model = joinModel(providerSelect.value, modelInput.value);
    if (model) localStorage.setItem(REMEMBERED, model);
    showProgress();
    controller = new AbortController();
    try {
      const response = await fetch("/api/studio/optimize", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        body: JSON.stringify({
          draft: getDraft(),
          instructions: instructions.value,
          model,
          character: getCharacterId() || "",
        }),
      });
      // The stream answers 200 before the first pass has run, so a failure comes
      // back as a final `error` line rather than a status. A non-2xx here is the
      // route itself refusing — a bad draft, no such character — and still JSON.
      if (!response.ok || !response.body) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail || "The model refused.");
      }
      let finished = null;
      for await (const event of ndjson(response)) {
        if (event.event === "pass") onPass(event);
        else if (event.event === "error") throw new Error(event.message);
        else if (event.event === "done") finished = event.result;
      }
      if (!finished) throw new Error("The run ended without an answer.");
      showResult(finished);
    } catch (error) {
      showSetup();
      // Walking away is not a failure to report back to — the dialog is already
      // shut and the server has dropped the run with it.
      if (error.name !== "AbortError") {
        showError(error.message || "Could not optimize this card.");
      }
    } finally {
      running = false;
      controller = null;
      stopClock();
    }
  }

  browseButton.addEventListener("click", browse);
  providerSelect.addEventListener("change", () => { hideList(); modelStatus.textContent = ""; });
  modelInput.addEventListener("input", () => { if (!modelList.hidden) renderList(modelInput.value); });
  modelInput.addEventListener("keydown", (event) => { if (event.key === "Escape") hideList(); });
  document.addEventListener("mousedown", (event) => {
    if (!modelList.parentElement.contains(event.target)) hideList();
  });
  runButton.addEventListener("click", run);
  backButton.addEventListener("click", showSetup);
  applyButton.addEventListener("click", () => {
    if (!proposal) return;
    const accepted = proposal;
    dialog.close();
    onApply(accepted);
  });
  dialog.addEventListener("click", (event) => {
    if (event.target.closest(".modal-close")) dialog.close();
  });
  // Closing the dialog — cancel, escape, apply — ends the run. Leaving a model
  // burning tokens for a pane nobody is looking at is the wrong default, and the
  // server drops its own task as soon as the body has no reader.
  dialog.addEventListener("close", () => {
    if (controller) controller.abort();
    stopClock();
  });

  return {
    open() {
      showSetup();
      showError("");
      hideList();
      dialog.showModal();
      instructions.focus();
    },
  };
}
