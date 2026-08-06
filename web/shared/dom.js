/* The handful of DOM helpers every YuriOS page ends up writing.
 *
 * There is no component library here on purpose — the pages are vanilla ES
 * modules that build nodes by hand. But `element()` and its neighbours had been
 * copied verbatim into the dashboard and the studio, and the mind debug page
 * would have been a third copy. Three is where duplication starts costing more
 * than the indirection.
 *
 * Deliberately NOT here: `icon()`. It resolves `#i-<name>` against each page's
 * own inline <svg> symbol bank, so sharing the function without sharing the
 * bank buys nothing.
 *
 * A module, not a classic script: shared/runtime.js has to be loadable raw by
 * the un-bundled Live2D client, this does not.
 */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export function element(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text != null) node.textContent = options.text;
  if (options.attrs) for (const [key, value] of Object.entries(options.attrs)) node.setAttribute(key, value);
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

export function setBusy(button, busy, busyLabel) {
  // Swap the label, not the button: one with an icon keeps it (textContent on
  // the button itself would take the <svg> out with the words and never put it back).
  const slot = $("span", button) || button;
  if (!slot.dataset.label) slot.dataset.label = slot.textContent;
  button.disabled = busy;
  slot.textContent = busy ? busyLabel : slot.dataset.label;
}

export function errorMessage(error) {
  if (error?.name === "AbortError") return "";
  if (error?.status === 404) return "This endpoint is not available on the current YuriOS node.";
  return error?.message || "The node did not complete the request.";
}

export function showToast(region, message, type = "success") {
  if (!region) return;
  const node = element("div", { className: `toast ${type}`, text: message });
  region.append(node);
  setTimeout(() => node.remove(), 4200);
}
