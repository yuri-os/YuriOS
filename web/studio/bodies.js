/* The studio's Body page (SPEC §6.8): the house's library of bodies, and hers.
 *
 * Two shelves — VRM models and Live2D rigs — each listing what ships and what
 * was uploaded, who wears what, and an upload at the foot. "Wear" puts a body
 * on *this* character and makes its kind the one her desktop window floats;
 * the two-button switch at the top says which that is, and changes it alone.
 * Removing a body from the library takes two clicks (no browser dialog — it
 * would freeze the page's automation and reads as an error), and anyone who
 * wore it goes back to the default.
 *
 * Clicking a body shows it in the preview (`bodypreview.js`) without putting it
 * on her; the preview starts on the body she floats.
 *
 * Nothing here is part of the card draft: every change is its own request and
 * is saved the moment it is made.
 */
import { bodiesApi } from "./api.js";

const KINDS = [
  { kind: "vrm", label: "VRM", accept: ".vrm",
    blurb: "3D models. Her room and her VRM desktop window wear the one chosen here.",
    add: "Add a VRM", hint: "A .vrm from VRoid Studio or UniVRM" },
  { kind: "live2d", label: "Live2D", accept: ".zip,application/zip",
    blurb: "2D rigs. Her Live2D room and her Live2D desktop window wear the one chosen here.",
    add: "Add a Live2D rig", hint: "The rig's folder, zipped — its .model3.json, .moc3 and textures" },
];

const megabytes = (bytes) => `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;

export function createBodyPage({ characterId, element, icon, toast, preview = null }) {
  const page = {
    library: null,         // GET /api/bodies
    worn: null,            // GET /api/characters/<id>/body
    loadError: "",
    errors: { vrm: "", live2d: "" },
    busy: "",              // "<kind>:<id>" or "upload:<kind>" or "backend"
    armed: "",             // "<kind>:<id>" awaiting its second click to remove
    loading: false,
    previewing: "",        // "<kind>:<id>" clicked to look at; "" = the one she floats
  };
  let root = null;

  async function load() {
    page.loading = true;
    try {
      const [library, worn] = await Promise.all([bodiesApi.list(), bodiesApi.worn(characterId)]);
      page.library = library;
      page.worn = worn;
      page.loadError = "";
    } catch (error) {
      page.loadError = error.message || "Could not read the body library.";
    } finally {
      page.loading = false;
    }
    rerender();
  }

  function rerender() {
    if (root?.isConnected) root.replaceWith(render());
  }

  async function act(key, work) {
    if (page.busy) return;
    page.busy = key;
    rerender();
    try {
      await work();
    } finally {
      page.busy = "";
      rerender();
    }
  }

  function wear(kind, entry) {
    act(`${kind}:${entry.id}`, async () => {
      try {
        page.worn = await bodiesApi.choose(characterId, { [kind]: entry.id, backend: kind });
        page.library = await bodiesApi.list();
        page.errors[kind] = "";
        page.previewing = `${kind}:${entry.id}`;
        toast(`She wears ${entry.title} now, and floats as ${kind === "vrm" ? "VRM" : "Live2D"}.`);
      } catch (error) {
        page.errors[kind] = error.message || "Could not put that body on her.";
      }
    });
  }

  function float(kind) {
    act("backend", async () => {
      try {
        page.worn = await bodiesApi.choose(characterId, { backend: kind });
      } catch (error) {
        toast(error.message || "Could not change which body she floats.", "error");
      }
    });
  }

  function remove(kind, entry) {
    const key = `${kind}:${entry.id}`;
    if (page.armed !== key) {
      page.armed = key;
      rerender();
      return;
    }
    page.armed = "";
    act(key, async () => {
      try {
        page.library = await bodiesApi.remove(kind, entry.id);
        page.worn = await bodiesApi.worn(characterId);
        page.errors[kind] = "";
        toast(`${entry.title} is out of the library.`);
      } catch (error) {
        page.errors[kind] = error.message || "Could not remove that body.";
      }
    });
  }

  function upload(kind, file) {
    if (!file) return;
    act(`upload:${kind}`, async () => {
      page.errors[kind] = "";
      try {
        const { body } = await bodiesApi.add(kind, file);
        page.worn = await bodiesApi.choose(characterId, { [kind]: body.id, backend: kind });
        page.previewing = `${kind}:${body.id}`;
        page.library = await bodiesApi.list();
        toast(`${body.title} is in the library, and she is wearing it.`);
      } catch (error) {
        // Beside the upload that failed, not at the top of a long page.
        page.errors[kind] = error.message || "That file was refused.";
      }
    });
  }

  function entryRow(kind, entry) {
    const key = `${kind}:${entry.id}`;
    const hers = page.worn?.[kind]?.id === entry.id;
    const wearers = entry.worn_by.map((w) => w.name);
    const meta = [entry.detail, entry.bytes ? megabytes(entry.bytes) : "",
      entry.builtin ? "ships with YuriOS" : ""].filter(Boolean).join(" · ");
    const wearButton = element("button", {
      className: `button ${hers ? "button-quiet" : "button-primary"}`,
      attrs: { type: "button", disabled: hers || Boolean(page.busy) },
      text: hers ? "Wearing" : page.busy === key ? "Putting on…" : "Wear",
    });
    if (!hers) wearButton.addEventListener("click", () => wear(kind, entry));
    let removeButton = null;
    if (!entry.builtin) {
      const armed = page.armed === key;
      removeButton = element("button", {
        className: `button button-quiet body-remove${armed ? " armed" : ""}`,
        attrs: { type: "button", disabled: Boolean(page.busy),
                 title: "Take it out of the library" },
      }, icon("trash"), element("span", {
        text: armed ? (wearers.length ? `Remove — ${wearers.join(", ")} go back to the default`
                                      : "Click again to remove")
                    : "Remove" }));
      removeButton.addEventListener("click", () => remove(kind, entry));
    }
    const looking = previewed()?.kind === kind && previewed()?.id === entry.id;
    const look = element("button", {
      className: "body-copy",
      attrs: { type: "button", title: "Show it in the preview", "aria-pressed": String(looking) },
    },
      element("strong", { text: entry.title }),
      element("small", { text: meta }),
      element("small", { className: "body-worn",
        text: wearers.length ? `Worn by ${wearers.join(", ")}` : "Nobody wears it" }));
    look.addEventListener("click", () => { page.previewing = key; rerender(); });
    return element("li", { className: `body-row${hers ? " hers" : ""}${looking ? " previewing" : ""}` },
      look, element("div", { className: "body-actions" }, wearButton, removeButton));
  }

  function shelf({ kind, label, accept, blurb, add, hint }) {
    const entries = page.library?.[kind] || [];
    const limit = page.library?.limits?.[kind];
    const uploading = page.busy === `upload:${kind}`;
    const file = element("input", { attrs: { type: "file", accept, disabled: Boolean(page.busy) } });
    file.addEventListener("change", () => upload(kind, file.files?.[0]));
    const drop = element("label", { className: `drop-zone${uploading ? " busy" : ""}` },
      element("span", { className: "drop-symbol" }, icon("upload")),
      element("strong", { text: uploading ? "Uploading and checking…" : add }),
      element("span", { text: limit ? `${hint}, up to ${megabytes(limit)}` : hint }),
      file);
    return element("section", { className: "body-shelf", attrs: { "data-kind": kind } },
      element("header", { className: "body-shelf-head" },
        element("h3", { text: label }),
        element("p", { className: "form-note", text: blurb })),
      element("ul", { className: "body-list" }, ...entries.map((entry) => entryRow(kind, entry))),
      drop,
      element("p", { className: "form-error", attrs: { role: "alert" }, text: page.errors[kind] }));
  }

  /** The body in the preview: the one clicked, else the one she floats. */
  function previewed() {
    if (!page.library || !page.worn) return null;
    const [kind, id] = page.previewing.split(":");
    const clicked = kind && (page.library[kind] || []).find((entry) => entry.id === id);
    if (clicked) return clicked;
    const floats = page.worn.desktop_body === "live2d" ? "live2d" : "vrm";
    return page.worn[floats] || null;
  }

  function previewPanel() {
    const entry = previewed();
    const hers = entry && page.worn?.[entry.kind]?.id === entry.id;
    const caption = element("div", { className: "body-preview-caption" },
      element("strong", { text: entry?.title || "—" }),
      element("small", { text: entry ? `${entry.kind === "vrm" ? "VRM" : "Live2D"} · ${
        hers ? "she wears this" : "previewing — not worn"}` : "" }));
    if (entry) void preview.show(entry);
    return element("figure", { className: "body-preview" }, preview.node, caption);
  }

  function floatSwitch() {
    const floats = page.worn?.desktop_body;
    const chosen = page.worn?.backend;
    const seg = element("div", { className: "seg" });
    for (const { kind, label } of KINDS) {
      const button = element("button", {
        className: floats === kind ? "on" : "",
        attrs: { type: "button", disabled: Boolean(page.busy), "aria-pressed": String(floats === kind) },
        text: label,
      });
      button.addEventListener("click", () => floats !== kind || !chosen ? float(kind) : null);
      seg.append(button);
    }
    const base = `/characters/${encodeURIComponent(characterId)}`;
    return element("div", { className: "body-float" },
      element("div", { className: "seg-wrap" },
        element("span", { className: "field-label", text: "Her desktop window floats" }), seg),
      element("p", { className: "form-note", text: chosen
        ? "Saved at once. An open window keeps its body until you float her again."
        : "She has not chosen, so this is the house's DESKTOP_BODY. Pick one to make it hers." }),
      element("p", { className: "body-rooms" },
        element("a", { attrs: { href: `${base}/sanctuary/`, target: "_blank", rel: "noopener" },
          text: "Open her VRM room" }),
        element("a", { attrs: { href: `${base}/live2d`, target: "_blank", rel: "noopener" },
          text: "Open her Live2D room" })));
  }

  function render() {
    let node;
    if (page.loadError) {
      node = element("p", { className: "notice error", text: page.loadError });
    } else if (!page.library || !page.worn) {
      node = element("p", { className: "form-note", text: "Reading the body library…" });
    } else {
      node = element("div", { className: "body-shelves" }, ...KINDS.map(shelf));
    }
    const top = page.worn
      ? element("div", { className: "body-top" },
        preview && page.library ? previewPanel() : null, floatSwitch())
      : null;
    root = element("div", { className: "field field-wide field-body" }, top, node);
    return root;
  }

  return {
    /** The page's node. Every visit re-reads the library — another tab, or the
     *  switchboard, may have changed it — and the node fills in when it lands. */
    control() {
      if (!page.loading && !page.busy) void load();
      return render();
    },
  };
}
