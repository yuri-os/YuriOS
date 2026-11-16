import { ApiError, detailMessage, request } from "../shared/http.js";

export { ApiError };

const ROOT = "/api/characters";

function characterPath(id, suffix = "") {
  return `${ROOT}/${encodeURIComponent(id)}${suffix}`;
}

export const charactersApi = Object.freeze({
  list: ({ signal } = {}) => request(ROOT, { signal }),
  connections: ({ signal } = {}) => request("/api/connections", { signal }),
  /** `api_key` is write-only: the host saves it to its .env and never returns it. */
  saveConnection: (name, profile) => request(`/api/connections/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify(profile),
  }),
  deleteConnection: (name) => request(`/api/connections/${encodeURIComponent(name)}`, {
    method: "DELETE",
  }),
  forgetKey: (keyEnv, profile) => request(
    `/api/connections/keys/${encodeURIComponent(keyEnv)}?profile=${encodeURIComponent(profile || "")}`,
    { method: "DELETE" }),
  checkConnection: (name, character) => request(
    `/api/connections/${encodeURIComponent(name)}/check?character=${encodeURIComponent(character)}`),
  detail: (id, section, { signal } = {}) => request(
    characterPath(id, section === "context" ? "/context-history" : `/${section}`), { signal }),
  journalDays: (id, page = 0, { signal } = {}) => request(
    characterPath(id, `/journal?page=${encodeURIComponent(page)}`), { signal }),
  journalDay: (id, day, { signal } = {}) => request(
    characterPath(id, `/journal?day=${encodeURIComponent(day)}`), { signal }),
  settings: (id, { signal } = {}) => request(characterPath(id, "/profile"), { signal }),
  setLoop: (id, enabled) => request(characterPath(id, "/loop"), {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  }),
  setControls: (id, controls) => request(characterPath(id, "/controls"), {
    method: "PATCH",
    body: JSON.stringify(controls),
  }),
  saveSettings: (id, settings) => request(characterPath(id, "/profile"), {
    method: "PATCH",
    body: JSON.stringify(settings),
  }),
  brain: (id, { signal } = {}) => request(characterPath(id, "/brain"), { signal }),
  saveBrain: (id, overrides) => request(characterPath(id, "/brain"), {
    method: "PATCH",
    body: JSON.stringify(overrides),
  }),
  approve: (id) => request(characterPath(id, "/approve"), { method: "POST" }),
  desktop: (id) => request(characterPath(id, "/desktop"), { method: "POST" }),
  body: (id, { signal } = {}) => request(characterPath(id, "/body"), { signal }),
  archive: (id) => request(characterPath(id, "/archive"), { method: "POST" }),
  // The whole companion, not her card (SPEC §29.8, §36.3): a copy under a new
  // id, a snapshot kept on this node, a backup handed to you.
  clone: (id, name) => request(characterPath(id, "/clone"), {
    method: "POST",
    body: JSON.stringify(name ? { name } : {}),
  }),
  snapshots: (id, { signal } = {}) => request(characterPath(id, "/snapshots"), { signal }),
  takeSnapshot: (id) => request(characterPath(id, "/snapshots"), { method: "POST" }),
  snapshotUrl: (id, name) => characterPath(id, `/snapshots/${encodeURIComponent(name)}`),
  deleteSnapshot: (id, name) => request(
    characterPath(id, `/snapshots/${encodeURIComponent(name)}`), { method: "DELETE" }),
  restoreSnapshot: (id, name, mode) => request(
    characterPath(id, `/snapshots/${encodeURIComponent(name)}/restore`), {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  prepareBackup: (id) => request(characterPath(id, "/backup"), { method: "POST" }),
  preparePurge: (id) => request(characterPath(id, "/purge/prepare"), { method: "POST" }),
  purge: (id, challenge) => request(characterPath(id, "/purge"), {
    method: "DELETE",
    body: JSON.stringify({ challenge }),
  }),
  importPng: (file) => {
    const body = new FormData();
    body.append("file", file, file.name);
    return request(`${ROOT}/import`, { method: "POST", body });
  },
});

/* Bringing a character back: an uploaded backup, an archived one, or a snapshot
 * of someone no longer on the board. */
export const backupsApi = Object.freeze({
  snapshots: ({ signal } = {}) => request("/api/snapshots", { signal }),
  archives: ({ signal } = {}) => request("/api/archives", { signal }),
  restoreArchive: (name) => request(`/api/archives/${encodeURIComponent(name)}/restore`, {
    method: "POST",
    body: JSON.stringify({}),
  }),
  /* XHR rather than fetch for the one thing fetch cannot do: say how far an
   * upload of a few hundred megabytes has got. The file is sent as its own
   * bytes, and the server streams it to disk. */
  upload: (file, { onProgress } = {}) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/backups/uploads");
    xhr.setRequestHeader("Content-Type", "application/zip");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payload = xhr.responseText;
      try { payload = JSON.parse(xhr.responseText); } catch { /* plain text */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ApiError(detailMessage(payload, `Upload failed (${xhr.status})`),
        xhr.status, payload));
    });
    xhr.addEventListener("error", () => reject(new ApiError("The upload did not reach the node.", 0, null)));
    xhr.addEventListener("abort", () => reject(new DOMException("Upload cancelled", "AbortError")));
    xhr.send(file);
  }),
  restoreUpload: (token, mode) => request(`/api/backups/uploads/${encodeURIComponent(token)}/restore`, {
    method: "POST",
    body: JSON.stringify({ mode }),
  }),
  cancelUpload: (token) => request(`/api/backups/uploads/${encodeURIComponent(token)}`, {
    method: "DELETE",
  }),
});
