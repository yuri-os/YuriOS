import { ApiError, request } from "../shared/http.js";

export { ApiError };

const ROOT = "/api/characters";

function characterPath(id, suffix = "") {
  return `${ROOT}/${encodeURIComponent(id)}${suffix}`;
}

export const charactersApi = Object.freeze({
  list: ({ signal } = {}) => request(ROOT, { signal }),
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
  archive: (id) => request(characterPath(id, "/archive"), { method: "POST" }),
  importPng: (file) => {
    const body = new FormData();
    body.append("file", file, file.name);
    return request(`${ROOT}/import`, { method: "POST", body });
  },
});
