/* Pictures → Checkpoints: Civitai, for her local camera (SPEC §11.5).
 *
 * Her local camera reads one .safetensors file. This page is where that file
 * comes from: the most-downloaded checkpoints of every family the camera can
 * load, each saying whether this build will load it — and, where a version
 * comes as several files (fp16 and fp32, full and pruned), which one is the best
 * match, with the others a click away — a download that runs on
 * the server while you do something else, and the button that points
 * SELFIE_LOCAL_MODEL at the file once it has landed and its header checks out.
 *
 * The decisions are checkpoint-list.js's; this draws them. The list is the
 * server's cache — opening the page costs nothing, and a missing or day-old list
 * is refreshed once, in the background of the visit. Progress is polled while
 * something downloads, and only then. The search box filters that list, and
 * asks Civitai for what it doesn't carry — the answer joins the cache.
 */
import {
  STATUS_LABELS, ago, asVariant, bytes, cardState, chosenVariant, count, matchesQuery, needsLookUp,
  normalizeQuery, percent, previews, progressLine, supportIssueUrl, unlistedFiles, variantLabel,
  variantsOf, visibleCards,
} from "./checkpoint-list.js";
import { api, button, confirmButton, el, section, status } from "./ui.js";

const FILTER_KEY = "yurios.checkpoints.filters";
const POLL_MS = 1000;
const LOOKUP_DELAY_MS = 700;   // a pause in typing before an unmatched search asks Civitai
let visit = 0;                 // a newer visit retires the older one's poll

function loadFilters() {
  try {
    return { family: "all", status: "all", nsfw: false, downloaded: false,
      ...JSON.parse(localStorage.getItem(FILTER_KEY) || "{}") };
  } catch (_) {
    return { family: "all", status: "all", nsfw: false, downloaded: false };
  }
}

function saveFilters(filters) {
  try { localStorage.setItem(FILTER_KEY, JSON.stringify(filters)); } catch (_) { /* private mode */ }
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A row of buttons where exactly one is pressed: two choices side by side,
 *  never a checkbox somewhere else (the house rule for modes). */
function pills(choices, current, onPick) {
  return el("div", { className: "ck-pills", role: "group" }, ...choices.map(([value, label]) => {
    const b = el("button", { type: "button", className: "ck-pill", textContent: label });
    b.setAttribute("aria-pressed", String(value === current));
    b.addEventListener("click", () => onPick(value));
    return b;
  }));
}

function verdictChip(verdict) {
  if (!verdict) return null;
  const tone = { ok: "ok", warn: "warn", unsupported: "no" }[verdict.status] || "";
  const word = { ok: "Checked", warn: "Loads, with a caveat", unsupported: "Won't load" }[verdict.status];
  return el("span", { className: `ck-verdict ${tone}`, title: verdict.note,
    textContent: `${word} · ${verdict.note}` });
}

export async function renderCheckpoints(container, ctx) {
  const mine = ++visit;
  const here = () => mine === visit && container.isConnected;
  container.replaceChildren(el("p", { className: "placeholder", textContent: "loading…" }));

  let data;
  try {
    data = await api("/api/house/checkpoints");
  } catch (error) {
    container.replaceChildren(el("p", { className: "placeholder error", textContent: error.message }));
    return;
  }
  const filters = loadFilters();
  let refreshing = false;
  let refreshError = "";
  let polling = false;

  const local = () => ({ files: data.local.files, partial: data.local.partial,
    downloads: data.downloads });
  const fileVerdict = (name) => data.local.files.find((f) => f.file === name);

  async function reload() {
    const fresh = await api("/api/house/checkpoints");
    if (!here()) return;
    data = fresh;
    draw();
  }

  // -- downloads ------------------------------------------------------------

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      while (here() && data.downloads.some((d) => d.state === "queued" || d.state === "downloading")) {
        await pause(POLL_MS);
        if (!here()) return;
        const before = new Map(data.downloads.map((d) => [d.file, d.state]));
        const { downloads } = await api("/api/house/checkpoints/downloads");
        data.downloads = downloads;
        // a job that just finished has a file (and a verdict) the folder scan
        // should show: draw the whole page again; otherwise just the bars
        if (downloads.some((d) => before.get(d.file) !== d.state)) await reload();
        else for (const d of downloads) cardViews.get(d.version_id)?.refresh();
      }
    } catch (_) {
      /* the server went away (a restart) — the next visit starts over */
    } finally {
      polling = false;
    }
  }

  async function startDownload(view, note) {
    try {
      const job = await api("/api/house/checkpoints/downloads", { method: "POST",
        body: { version_id: view.version_id, file: view.file } });
      data.downloads = [...data.downloads.filter((d) => d.file !== job.file), job];
      cardViews.get(view.version_id)?.refresh();
      poll();
    } catch (error) {
      note.say(error.message, "error");
    }
  }

  async function cancelDownload(view, note) {
    try {
      await api(`/api/house/checkpoints/downloads/${encodeURIComponent(view.file)}`, { method: "DELETE" });
    } catch (error) {
      note.say(error.message, "error");
    }
  }

  async function useFile(file, note) {
    try {
      const answer = await api("/api/house/checkpoints/use", { method: "POST", body: { file } });
      ctx.needsRestart(`Her camera set to ${answer.file}`);
      await reload();
    } catch (error) {
      note.say(error.message, "error");
    }
  }

  // -- one card ----------------------------------------------------------------

  const cardViews = new Map();
  const picked = new Map();      // version id → the file you chose on its card, this visit

  /** What a card's foot offers for one of its files. `view` is the card as
   *  that file (asVariant). */
  function actions(card, view, note) {
    const state = cardState(view, local());
    const job = data.downloads.find((d) => d.file === view.file);
    if (card.status === "no") {
      return [el("p", { className: "ck-unsupported" }, "Not supported yet. ",
        el("a", { className: "text-link", href: supportIssueUrl(card), target: "_blank",
          rel: "noopener", textContent: "Request support on GitHub" }))];
    }
    if (state === "downloading") {
      const bar = el("div", { className: "ck-bar" }, el("i", { style: `width:${percent(job)}%` }));
      return [bar, el("div", { className: "ck-row" },
        el("span", { className: "ck-progress", textContent: job.state === "queued"
          ? "starting…" : progressLine(job) }),
        confirmButton("Cancel", "Cancel and delete?", () => cancelDownload(view, note)))];
    }
    if (state === "downloaded") {
      const found = fileVerdict(view.file);
      const using = data.current.file === view.file && data.current.in_folder;
      return [verdictChip(found), el("div", { className: "ck-row" },
        using ? el("span", { className: "ck-using", textContent: data.current.pending
          ? "Set for her camera · waits for a restart" : "Her camera uses this" })
          : button("Use for her camera", () => useFile(view.file, note), "settings-save"),
      )];
    }
    if (!view.variant.loads) {
      return [el("p", { className: "ck-unsupported", textContent: "This file won't load. "
        + "Pick another one above." })];
    }
    const noKey = !data.key_configured;
    const go = button(state === "paused" ? "Resume download" : state === "failed" ? "Try again"
      : "Download", () => { go.disabled = true; startDownload(view, note); }, "settings-save");
    go.disabled = noKey;
    if (noKey) go.title = "Add your Civitai API key above first";
    const said = state === "paused" ? `${bytes(data.local.partial[view.file])} already here`
      : state === "failed" ? job.error : bytes(view.size_bytes);
    return [el("div", { className: "ck-row" },
      el("span", { className: `ck-progress${state === "failed" ? " error" : ""}`, textContent: said }),
      go)];
  }

  /** The files of a version, side by side: the best match marked, each one
   *  saying whether it is here already. */
  function variantPills(card, chosen, onPick) {
    const variants = variantsOf(card);
    if (variants.length < 2) return null;
    return el("div", { className: "ck-pills ck-variants", role: "group", ariaLabel: "Files" },
      ...variants.map((v) => {
        const here = ["downloaded", "downloading", "paused"].includes(cardState(asVariant(card, v), local()));
        const b = el("button", { type: "button", className: `ck-pill${v.loads ? "" : " is-off"}`,
          title: [v.recommended ? "Best match" : "", v.note, v.file].filter(Boolean).join("\n") },
        variantLabel(v), v.recommended ? el("span", { className: "ck-best", textContent: " ★" }) : null,
        here ? el("span", { className: "ck-here", textContent: " ✓" }) : null);
        b.setAttribute("aria-pressed", String(v.file === chosen.file));
        b.addEventListener("click", () => onPick(v));
        return b;
      }));
  }

  function cardNode(card) {
    const shots = previews(card, filters.nsfw);
    const prints = el("div", { className: `ck-prints${shots.length === 1 ? " one" : ""}` },
      ...(shots.length ? shots.map((s) => el("img", { src: s.url, alt: "", loading: "lazy",
        referrerPolicy: "no-referrer", decoding: "async" }))
        // hidden, not missing: an SFW-flagged model's gallery can be all R and up
        : [el("span", { textContent: !filters.nsfw && card.images?.length ? "previews hidden"
          : "no preview" })]));
    const note = status("ck-note-line");
    const foot = el("div", { className: "ck-actions" });
    const several = variantsOf(card).length > 1;
    const refresh = () => {
      const chosen = chosenVariant(card, local(), picked.get(card.version_id));
      const view = asVariant(card, chosen);
      foot.replaceChildren(...[
        variantPills(card, chosen, (v) => { picked.set(card.version_id, v.file); note.say(""); refresh(); }),
        several && chosen.note ? el("p", { className: `ck-variant-note${chosen.loads ? "" : " error"}`,
          textContent: chosen.note }) : null,
        el("code", { className: "ck-file", textContent: view.file, title: view.file }),
        ...actions(card, view, note), note.node].filter(Boolean));
    };
    refresh();
    cardViews.set(card.version_id, { refresh });
    const tags = el("div", { className: "ck-tags" },
      el("span", { className: `ck-status s-${card.status}`, textContent: STATUS_LABELS[card.status] || card.status }),
      card.nsfw ? el("span", { className: "ck-tag nsfw", textContent: "NSFW" }) : null,
      card.source === "week" ? el("span", { className: "ck-tag", textContent: "this week" }) : null,
      card.source === "search" ? el("span", { className: "ck-tag", textContent: "from a search" }) : null,
      card.published ? el("span", { className: "ck-tag", textContent: card.published }) : null);
    return el("article", { className: `ck-card${card.status === "no" ? " is-unsupported" : ""}` },
      el("a", { href: card.url, target: "_blank", rel: "noopener", className: "ck-print-link",
        title: "Open on Civitai" }, prints),
      el("div", { className: "ck-meta" },
        el("h3", { className: "ck-name" }, el("a", { href: card.url, target: "_blank",
          rel: "noopener", textContent: card.name })),
        el("div", { className: "ck-sub", textContent: [card.version, card.creator].filter(Boolean).join(" · ") }),
        tags,
        // with several files, each pill carries its own size and precision
        el("div", { className: "ck-stats", textContent: [`↓ ${count(card.downloads)}`,
          ...(several ? [] : [bytes(card.size_bytes), card.precision])].filter(Boolean).join(" · ") }),
        el("p", { className: "ck-note", textContent: card.note }),
        foot));
  }

  // -- the page ----------------------------------------------------------------

  function cameraSection() {
    const c = data.current;
    const found = c.in_folder ? fileVerdict(c.file) : null;
    const body = c.file
      ? [el("p", { className: "sec-text" }, "Her camera reads ", el("code", { textContent: c.path }),
        c.pending ? " — saved, and waiting for a restart." : "."),
      found ? el("div", { className: "ck-inline" }, verdictChip(found)) : null]
      : [el("p", { className: "sec-text", textContent: "No checkpoint is set yet. Download "
        + "one below, then press Use for her camera." })];
    if (!["diffusers", "krea2", "anima"].includes(data.backend))
      body.push(el("p", { className: "sec-text warn", textContent: `Her camera is set to `
        + `${data.backend || "off"}. Choosing a checkpoint here switches it to the local renderer.` }));
    body.push(el("p", { className: "sec-text ck-folder" }, "Downloads go to ",
      el("code", { textContent: data.folder }), " — ",
      el("a", { className: "text-link", href: "#/pictures/SELFIE_CHECKPOINT_DIR",
        textContent: "change the folder" }), "."));
    return section("Her camera", [...body, el("div", { className: "ck-gap" })]);
  }

  function keySection() {
    const note = status();
    const box = el("input", { className: "set-input inline-input ck-key", type: "password",
      autocomplete: "off", spellcheck: false,
      placeholder: data.key_configured ? "paste a new key to replace it" : "paste your Civitai API key" });
    const save = button(data.key_configured ? "Replace key" : "Save key", async () => {
      save.disabled = true;
      try {
        await api("/api/house/civitai-key", { method: "PUT", body: { key: box.value } });
        await reload();
      } catch (error) {
        note.say(error.message, "error");
        save.disabled = false;
      }
    }, "settings-save");
    const remove = data.key_configured ? confirmButton("Remove", "Remove the key?", async () => {
      await api("/api/house/civitai-key", { method: "PUT", body: { key: null } });
      await reload();
    }) : null;
    return section("Civitai API key", [
      el("p", { className: "sec-text" }, data.key_configured
        ? "Saved in .env as CIVITAI_API_KEY. Downloads use it; nothing waits for a restart."
        : "Civitai wants a key to download most checkpoints. Make one under ",
      data.key_configured ? null : el("a", { className: "text-link", target: "_blank", rel: "noopener",
        href: "https://civitai.com/user/account", textContent: "Account settings → API keys" }),
      data.key_configured ? null : ", paste it here, and it is saved to .env as CIVITAI_API_KEY."),
      el("div", { className: "row-actions" }, box, save, remove),
      note.node,
    ], { count: data.key_configured ? "saved" : "not set" });
  }

  function localSection() {
    const cards = data.catalog?.models || [];
    const rows = [...data.local.files].sort((a, b) => (b.file === data.current.file) - (a.file === data.current.file));
    if (!rows.length) return null;
    const unlisted = new Set(unlistedFiles(rows, cards).map((f) => f.file));
    const note = status();
    const table = el("table", { className: "grid-table" },
      el("thead", {}, el("tr", {}, ...["File", "Checked", "Size", ""].map((h) => el("th", { textContent: h })))),
      el("tbody", {}, ...rows.map((f) => {
        const using = data.current.file === f.file && data.current.in_folder;
        return el("tr", {},
          el("td", { className: "mono" }, f.file, f.title || unlisted.has(f.file)
            ? el("div", { className: "muted", textContent: [f.title, unlisted.has(f.file)
              ? "not in the list below" : ""].filter(Boolean).join(" · ") }) : null),
          el("td", {}, verdictChip(f)),
          el("td", { className: "mono muted", textContent: bytes(f.size_bytes) }),
          el("td", {}, using ? el("span", { className: "ck-using", textContent: "in use" })
            : f.status === "unsupported" ? null
              : button("Use", () => useFile(f.file, note))));
      })));
    return section("On this machine", [table, note.node], { count: `${rows.length} in the folder` });
  }

  // -- search ------------------------------------------------------------------
  // The box filters the cached list as you type. When nothing in it answers,
  // a pause asks Civitai; Enter, or the link under the box, asks any time.
  // Whatever Civitai finds joins the cached list, so the next visit has it.

  let query = "";
  let searching = "";            // the search Civitai is being asked right now
  let searchNote = null;         // { query, text, tone }: how the last one went
  const asked = new Set();       // searches already sent this visit
  let lookTimer = 0;
  const searchBox = el("input", { className: "set-input ck-search", type: "search",
    autocomplete: "off", spellcheck: false,
    placeholder: "Search the list, or Civitai when it isn't here" });
  searchBox.setAttribute("aria-label", "Search checkpoints");
  searchBox.addEventListener("input", () => {
    query = searchBox.value;
    drawResults();
    clearTimeout(lookTimer);
    lookTimer = setTimeout(() => {
      if (here() && needsLookUp(data.catalog?.models || [], query, asked)) lookUp(query);
    }, LOOKUP_DELAY_MS);
  });
  searchBox.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    clearTimeout(lookTimer);
    lookUp(query);
  });

  async function lookUp(text) {
    const q = normalizeQuery(text);
    if (q.length < 2 || searching === q) return;
    asked.add(q);
    searching = q;
    drawResults();
    try {
      const answer = await api("/api/house/checkpoints/search", { method: "POST", body: { query: q } });
      if (!here()) return;
      data.catalog = answer.catalog;
      searchNote = { query: q, tone: "", text: !answer.found ? `Civitai has no checkpoint matching “${q}”.`
        : `Civitai found ${answer.found} for “${q}”: ${answer.added
          ? `${answer.added} new, now in the list` : "all of them already in the list"}.` };
    } catch (error) {
      searchNote = { query: q, tone: "error", text: `Couldn't search Civitai: ${error.message}` };
    } finally {
      if (searching === q) searching = "";
      // the whole page: a new family may have joined the pills, and a file in
      // the folder may now be listed
      if (here()) draw();
    }
  }

  // -- the list ----------------------------------------------------------------

  let listNode = null;
  let resultsNode = null;
  let countNode = null;

  function listSection() {
    const catalog = data.catalog;
    const cards = catalog?.models || [];
    const families = ["all", ...data.families.filter((f) => cards.some((c) => c.family === f))];
    const set = (patch) => { Object.assign(filters, patch); saveFilters(filters); drawList(); };
    const refreshButton = button(refreshing ? "Refreshing…" : "Refresh list", () => refresh());
    refreshButton.disabled = refreshing;
    const hiddenNsfw = filters.nsfw ? 0 : cards.filter((c) => c.nsfw).length;
    resultsNode = el("div", { className: "ck-results" });
    const node = section("Civitai", [
      el("div", { className: "ck-toolbar" },
        searchBox,
        pills(families.map((f) => [f, f === "all" ? "All families" : f.replace("SDXL · ", "")]),
          filters.family, (family) => set({ family })),
        el("div", { className: "ck-toolbar-row" },
          pills([["all", "Any status"], ["usable", "Ready + Loads"], ["best", "Ready"]],
            filters.status, (s) => set({ status: s })),
          pills([[false, "SFW only"], [true, "Include NSFW"]], filters.nsfw, (nsfw) => set({ nsfw })),
          pills([[false, "Everything"], [true, "Downloaded"]], filters.downloaded,
            (downloaded) => set({ downloaded })))),
      refreshError && cards.length ? el("p", { className: "sec-text warn", textContent: refreshError }) : null,
      catalog?.errors?.length ? el("p", { className: "sec-text warn", textContent:
        `Some lists didn't load: ${catalog.errors.join("; ")}` }) : null,
      resultsNode,
      el("p", { className: "sec-text ck-legal" }, `Per base model: the 16 most downloaded of all time `
        + `(60 for Krea 2, where most files are formats the camera can't read) and the 12 most `
        + `downloaded this week, plus whatever a search found.${hiddenNsfw ? ` ${hiddenNsfw} NSFW `
        + `models hidden.` : ""} Each model's own licence applies.`),
      el("div", { className: "ck-gap" }),
    ], { count: "…", actions: [refreshButton] });
    countNode = node.querySelector(".sec-count");
    drawResults();
    return node;
  }

  /** The cards, and the line under the search box: everything a keystroke
   *  changes, so typing never rebuilds the box it is typing into. */
  function drawResults() {
    if (!resultsNode) return;
    const catalog = data.catalog;
    const cards = catalog?.models || [];
    const q = normalizeQuery(query);
    const shown = visibleCards(cards, { ...filters, query: q }, local());
    const matched = q ? cards.filter((c) => matchesQuery(c, q)).length : 0;
    cardViews.clear();

    const groups = new Map();
    for (const card of shown) {
      if (!groups.has(card.family)) groups.set(card.family, []);
      groups.get(card.family).push(card);
    }
    const blocks = data.families.filter((f) => groups.has(f)).map((family) => el("div", { className: "ck-family" },
      el("h3", { className: "ck-family-head" }, family,
        el("span", { className: "sec-count", textContent: String(groups.get(family).length) })),
      el("div", { className: "ck-grid" }, ...groups.get(family).map(cardNode))));

    let line = null;
    if (q) {
      const said = searching === q ? { text: `Searching Civitai for “${q}”…`, tone: "" }
        : searchNote?.query === q ? searchNote : null;
      const hidden = matched - shown.length;
      const ask = searching === q ? null : button(`Search Civitai for “${q}”`, () => lookUp(q), "ck-ask");
      line = el("p", { className: "sec-text ck-search-line" },
        said ? el("span", { className: said.tone, textContent: said.text }) : null,
        hidden > 0 ? el("span", { className: "muted", textContent:
          `${hidden} more ${hidden === 1 ? "match is" : "match"} hidden by the filters.` }) : null,
        ask);
    }
    const empty = !cards.length
      ? el("p", { className: refreshError ? "placeholder error" : "placeholder",
        textContent: refreshError || (refreshing ? "Listing Civitai…" : "No list yet.") })
      : shown.length ? null
        : el("p", { className: "placeholder", textContent: q ? (matched ? "Every match is hidden by "
          + "the filters." : searching === q ? "Nothing in the list yet — asking Civitai…"
          : `Nothing in the list matches “${q}”.`)
          : filters.downloaded ? "None of the listed checkpoints is on this machine yet."
            : "Nothing matches these filters." });
    resultsNode.replaceChildren(...[line, empty, ...blocks].filter(Boolean));
    const meta = catalog?.fetched_at ? `fetched ${ago(catalog.fetched_at)}` : "not fetched yet";
    if (countNode) countNode.textContent = `${shown.length} of ${cards.length} · ${meta}`;
  }

  function drawList() {
    const typing = document.activeElement === searchBox;
    const next = listSection();
    if (listNode) listNode.replaceWith(next);
    listNode = next;
    if (typing) searchBox.focus({ preventScroll: true });
  }

  function draw() {
    const typing = document.activeElement === searchBox;
    listNode = listSection();
    container.replaceChildren(...[cameraSection(), keySection(), localSection(), listNode]
      .filter(Boolean));
    if (typing) searchBox.focus({ preventScroll: true });
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    refreshError = "";
    drawList();
    try {
      const catalog = await api("/api/house/checkpoints/refresh", { method: "POST" });
      if (!here()) return;
      data.catalog = catalog;
    } catch (error) {
      refreshError = `Couldn't list Civitai: ${error.message}`;
    } finally {
      refreshing = false;
      if (here()) draw();
    }
  }

  draw();
  if (!data.catalog?.fetched_at || data.catalog.stale) refresh();
  poll();
}
