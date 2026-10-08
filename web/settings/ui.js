/* What every part of House settings builds with (SPEC §11.2–§11.4): nodes,
 * sections, and the three ways the page talks to the server — JSON, a file up,
 * a file down. Kept beside the page rather than in /shared because nothing else
 * draws these sections. */

export function el(tag, props = {}, ...kids) {
  const node = Object.assign(document.createElement(tag), props);
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
}

export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

/** A titled panel, the page's one container shape. */
export function section(title, body = [], { count = "", actions = [] } = {}) {
  return el("section", { className: "sec" },
    el("header", { className: "sec-head" }, el("h2", { textContent: title }),
      count !== "" ? el("span", { className: "sec-count", textContent: String(count) }) : null,
      actions.length ? el("div", { className: "sec-actions" }, ...actions) : null),
    el("div", { className: "sec-body" }, ...[body].flat()));
}

export const button = (text, onClick, className = "button button-quiet") => {
  const node = el("button", { type: "button", className, textContent: text });
  if (onClick) node.addEventListener("click", onClick);
  return node;
};

/** A note line inside a section: what just happened, or why it didn't. */
export function status(className = "sec-status") {
  const node = el("p", { className, hidden: true });
  return {
    node,
    say(text, tone = "") {
      node.textContent = text;
      node.className = `${className} ${tone}`;
      node.hidden = !text;
    },
  };
}

const apiPath = (path) => window.YuriOSSettings?.apiPath(path) || path;

export async function api(path, { method = "GET", body, form } = {}) {
  const init = { method, headers: {} };
  if (form) init.body = form;
  else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(apiPath(path), init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return data;
}

/** Hand the viewer a file built here (a job's .md, an edited YAML). */
export function download(filename, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = el("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A hidden file input behind a button; resolves with the chosen files. */
export function pickFiles({ accept = "", multiple = false } = {}) {
  return new Promise((resolve) => {
    const input = el("input", { type: "file", accept, multiple, hidden: true });
    input.addEventListener("change", () => { resolve([...input.files]); input.remove(); });
    document.body.append(input);
    input.click();
  });
}

export const readText = (file) => file.text();

/** A two-press button for the things that cannot be taken back: the first
 *  press asks, a second within a few seconds does it. No browser dialog —
 *  those block the page and everything driving it. */
export function confirmButton(text, ask, onConfirm, className = "button button-quiet") {
  const node = button(text, null, className);
  let armed = null;
  node.addEventListener("click", async () => {
    if (!armed) {
      node.textContent = ask;
      node.classList.add("is-armed");
      armed = setTimeout(() => {
        armed = null;
        node.textContent = text;
        node.classList.remove("is-armed");
      }, 4000);
      return;
    }
    clearTimeout(armed);
    armed = null;
    node.textContent = text;
    node.classList.remove("is-armed");
    await onConfirm();
  });
  return node;
}
