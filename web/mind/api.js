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
