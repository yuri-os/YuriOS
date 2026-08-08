/* The activity ladder, shown as itself everywhere.
 *
 * mind/policy.py's ladder — ENGAGED, IDLE, DORMANT, DREAM — used to reach the
 * switchboard and the inner-life panel through a second, coarser vocabulary
 * that bucketed IDLE and DORMANT into one "resting" label. That meant the same
 * fact about the same character read as "resting" on one screen and "DORMANT"
 * on another. There is no second vocabulary now: whatever word the mind loop
 * is in, every screen that reports it prints that word, in that word's own
 * colour — the same --acid/--mint/--amber/--dim the ladder already has on the
 * /mind debug page (mind/mind.js's STATE_COLOR) and in dashboard.css/mind.css.
 *
 * A character with no mind running (MIND_ENABLED=false, still starting, a
 * failed boot) never reaches the ladder at all — Host.summary() falls back to
 * its own process states for those (world/host.py). Those keep their own
 * words here too; they're a different fact (is the process up), not another
 * name for a rung on the ladder.
 */

const STATE_ALIASES = Object.freeze({
  active: "ready",
  running: "ready",
  online: "ready",
  stopped: "offline",
  disabled: "offline",
  error: "attention",
  failed: "attention",
  sleeping: "dream",
  paused: "dormant",
});

export const STATE_META = Object.freeze({
  engaged: { label: "engaged", color: "#d7ff58", rank: 0 },    // --acid
  idle: { label: "idle", color: "#76e8bd", rank: 1 },          // --mint
  dream: { label: "dream", color: "#f4bd62", rank: 2 },        // --amber
  dormant: { label: "dormant", color: "#69736d", rank: 3 },    // --dim
  starting: { label: "starting", color: "#a7ca32", rank: 0 },  // --acid-dark
  ready: { label: "ready", color: "#a7ca32", rank: 1 },        // --acid-dark
  attention: { label: "attention", color: "#ef786f", rank: 4 },
  offline: { label: "offline", color: "#69736d", rank: 5 },
  unknown: { label: "unknown", color: "#69736d", rank: 6 },
});

export function canonicalState(value) {
  const input = String(value ?? "unknown").trim().toLowerCase();
  const state = STATE_ALIASES[input] || input;
  return STATE_META[state] ? state : "unknown";
}
