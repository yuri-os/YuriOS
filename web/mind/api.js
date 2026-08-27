/* The debug API (yurios/world/debug.py), one method per view.
 *
 * Every path goes through `apiPath` from shared/runtime.js, which rewrites
 * `/api/x` into `/api/characters/{id}/x` from the page's own URL. Never
 * hand-build that prefix: the shim owns the encoding, and it is also what makes
 * the same code work on a single-character node where there is no prefix.
 *
 * Note the namespace is `/api/debug/*`, not `/api/mind/*` — the latter already
 * belongs to the sanctuary's inner-life panel, and the host would shadow it.
 */
import { ApiError, query, request } from "../shared/http.js";

export { ApiError };

const { apiPath } = window.YuriOSRuntime;

const get = (path, params, { signal } = {}) =>
  request(apiPath(`/api/debug${path}`) + query(params), { signal });

export const debugApi = Object.freeze({
  overview: (opts) => get("/overview", {}, opts),
  activity: (page, opts) => get("/activity", { page, limit: 100 }, opts),
  events: ({ hours, kinds, limit } = {}, opts) =>
    get("/events", { hours, kinds: (kinds || []).join(","), limit }, opts),

  ticks: (page, { state, q } = {}, opts) => get("/ticks", { page, limit: 25, state, q }, opts),
  tick: (id, opts) => get(`/ticks/${encodeURIComponent(id)}`, {}, opts),

  signals: (page, { type } = {}, opts) => get("/signals", { page, limit: 50, type }, opts),
  goals: (opts) => get("/goals", {}, opts),
  selfEdits: (opts) => get("/self-edits", {}, opts),

  calls: (page, { tool, verdict, corr_id } = {}, opts) =>
    get("/calls", { page, limit: 25, tool, verdict, corr_id }, opts),
  selfies: (page, opts) => get("/selfies", { page, limit: 24 }, opts),

  promptDays: (page, opts) => get("/prompts/days", { page, limit: 20 }, opts),
  prompts: (page, { day, kind } = {}, opts) =>
    get("/prompts", { page, limit: 25, day, kind }, opts),
  prompt: (id, opts) => get(`/prompts/${encodeURIComponent(id)}`, {}, opts),

  commits: (page, { path } = {}, opts) => get("/vault/commits", { page, limit: 25, path }, opts),
  commit: (sha, opts) => get(`/vault/commits/${encodeURIComponent(sha)}`, {}, opts),
  tree: (path, opts) => get("/vault/tree", { path }, opts),
  file: (path, rev, opts) => get("/vault/file", { path, rev }, opts),
  fileHistory: (path, opts) => get("/vault/history", { path, limit: 25 }, opts),

  memory: (opts) => get("/memory", {}, opts),
  chunks: (page, { kind, q } = {}, opts) => get("/memory/chunks", { page, limit: 25, kind, q }, opts),
  chunk: (id, opts) => get(`/memory/chunks/${encodeURIComponent(id)}`, {}, opts),

  economics: (opts) => get("/economics", {}, opts),
  utility: (page, { kind } = {}, opts) => get("/utility", { page, limit: 25, kind }, opts),
});

/* The two exceptions to the note above, and to the "read-only" one in mind.js.
 *
 * DREAM's roster is not on disk in any readable form — it is the runner's list
 * of job objects, assembled from code and two ledgers — and running a job is by
 * definition not a read. Both therefore live on `/api/mind/*`, the runtime's
 * own surface, which answers 503 when the loop is off. Everything else on this
 * page works with her stopped; this section is the one that needs her awake,
 * and says so.
 *
 * The job *files* are the exception inside the exception: `vault/dreams/` is on
 * disk and readable, and a file edited with her stopped simply lands the next
 * time a runner is built. They ride here anyway rather than on the vault debug
 * surface, because the thing you do after editing a job is run it, and a roster
 * split across two clients is a roster that disagrees with itself.
 */
const mind = (path, options) => request(apiPath(`/api/mind${path}`), options);

export const dreamApi = Object.freeze({
  status: ({ signal } = {}) => mind("/dream", { signal }),
  run: (body, { signal } = {}) =>
    mind("/dream/run", { method: "POST", body: JSON.stringify(body || {}), signal }),
  jobs: ({ signal } = {}) => mind("/dream/jobs", { signal }),
  job: (name, { signal } = {}) =>
    mind(`/dream/jobs/${encodeURIComponent(name)}`, { signal }),
  saveJob: (name, text, { signal } = {}) =>
    mind(`/dream/jobs/${encodeURIComponent(name)}`,
         { method: "PUT", body: JSON.stringify({ text }), signal }),
  deleteJob: (name, { signal } = {}) =>
    mind(`/dream/jobs/${encodeURIComponent(name)}`, { method: "DELETE", signal }),
});
