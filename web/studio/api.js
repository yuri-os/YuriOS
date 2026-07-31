const ROOT = "/api/characters";

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
    // The export refuses in two different voices — `leak` is "no", and
    // `review_required` is "not yet". The studio has to tell them apart, so the
    // exporter's structured detail is lifted onto the error rather than
    // flattened into a message string.
    const detail = payload && typeof payload === "object" ? payload.detail : null;
    const shaped = detail && typeof detail === "object" ? detail : null;
    this.code = shaped?.code || "";
    this.surface = shaped?.surface || "";
    this.field = shaped?.field || "";
    this.overlaps = shaped?.overlaps || [];
  }
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  if (response.ok && contentType.startsWith("image/")) {
    return { blob: await response.blob(), filename: filenameFrom(response) };
  }
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" ? payload.detail : payload;
    const message = typeof detail === "object" && detail !== null
      ? detail.detail || "The export was refused."
      : detail || `Request failed (${response.status})`;
    throw new ApiError(message, response.status, payload);
  }
  return payload;
}

function filenameFrom(response) {
  const header = response.headers.get("content-disposition") || "";
  return header.match(/filename="([^"]+)"/)?.[1] || "character.png";
}

const at = (id, suffix = "") => `${ROOT}/${encodeURIComponent(id)}${suffix}`;

export const studioApi = Object.freeze({
  template: () => request("/api/studio/template"),
  load: (id) => request(at(id, "/studio")),
  save: (id, draft) => request(at(id, "/studio"), {
    method: "PATCH", body: JSON.stringify({ draft }),
  }),
  preview: (id, options) => request(at(id, "/studio/preview"), {
    method: "POST", body: JSON.stringify(options),
  }),
  exportCard: (id, options) => request(at(id, "/export"), {
    method: "POST", body: JSON.stringify(options),
  }),
  create: (draft, portrait) => request(ROOT, {
    method: "POST", body: JSON.stringify({ draft, portrait }),
  }),
  selfies: (id) => request(at(id, "/selfies")),
  setPortrait: (id, body) => request(at(id, "/portrait"), {
    method: "POST", body: JSON.stringify(body),
  }),
});
