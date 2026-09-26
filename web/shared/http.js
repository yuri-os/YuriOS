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

/** The words in an error body, whatever shape it came in. A request that fails
 *  FastAPI's own validation answers `{"detail": [{loc, msg, …}, …]}` — a list,
 *  which a template string renders as "[object Object]". */
export function detailMessage(payload, fallback = "") {
  if (payload == null) return fallback;
  if (typeof payload === "string") return payload || fallback;
  if (Array.isArray(payload)) {
    const lines = payload.map((item) => detailMessage(item)).filter(Boolean);
    return lines.length ? lines.join("; ") : fallback;
  }
  if (typeof payload === "object") {
    if (typeof payload.msg === "string") {
      const field = Array.isArray(payload.loc) ? payload.loc.filter((part) => part !== "body").join(".") : "";
      // Pydantic prefixes a validator's own message with "Value error, ".
      const msg = payload.msg.replace(/^Value error, /, "");
      // …and a message that already names its field needs no label in front.
      return field && !msg.toLowerCase().startsWith(field.toLowerCase()) ? `${field}: ${msg}` : msg;
    }
    return detailMessage(payload.detail ?? payload.error ?? payload.message, fallback);
  }
  return String(payload);
}

export async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new ApiError(detailMessage(payload, `Request failed (${response.status})`),
      response.status, payload);
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
