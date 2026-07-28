const ROOT = "/api/characters";

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" ? payload.detail || payload.error || payload.message : payload;
    throw new ApiError(detail || `Request failed (${response.status})`, response.status, payload);
  }
  return payload;
}

function characterPath(id, suffix = "") {
  return `${ROOT}/${encodeURIComponent(id)}${suffix}`;
}

export const charactersApi = Object.freeze({
  list: ({ signal } = {}) => request(ROOT, { signal }),
  detail: (id, section, { signal } = {}) => request(
    characterPath(id, section === "context" ? "/context-history" : `/${section}`), { signal }),
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
  archive: (id) => request(characterPath(id, "/archive"), { method: "POST" }),
  importPng: (file) => {
    const body = new FormData();
    body.append("file", file, file.name);
    return request(`${ROOT}/import`, { method: "POST", body });
  },
});
