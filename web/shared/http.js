/* One fetch wrapper for every YuriOS page.
 *
 * FastAPI reports failures as `{"detail": ...}` (HTTPException) but the studio
 * and onboarding routes answer `{"error": ...}`, so unwrapping the message is a
 * rule about this server rather than a detail of any one page — which is why it
 * had already been copied into two api.js files before this existed.
 */

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export async function request(path, options = {}) {
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

/** Query string from an object, dropping anything the caller left unset — the
 *  debug endpoints all take optional filters and an empty one must not become
 *  `?kind=` (which FastAPI would read as the empty string, not as absent). */
export function query(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === "") continue;
    search.set(key, String(value));
  }
  const out = search.toString();
  return out ? `?${out}` : "";
}
