/* The activity ladder, in the app's one shared vocabulary.
 *
 * The mind loop's own states (mind/policy.py) are ENGAGED/IDLE/DORMANT/DREAM —
 * a four-rung ladder built for the loop's own cadence logic, not for a reader
 * skimming a character list. Every *user-facing* display collapses to this
 * coarser six-bucket vocabulary instead, so "resting" means the same thing
 * whether you're looking at the switchboard or her own inner-life panel.
 *
 * The one place this deliberately does NOT reach is the /mind debug page
 * (mind/mind.js): that page exists to show the exact ladder transitions, and
 * bucketing IDLE and DORMANT together there would hide the thing it's for.
 */

const STATE_ALIASES = Object.freeze({
  active: "awake",
  running: "awake",
  ready: "awake",
  online: "awake",
  idle: "resting",
  dormant: "resting",
  paused: "resting",
  dream: "dreaming",
  sleeping: "dreaming",
  stopped: "offline",
  disabled: "offline",
  error: "attention",
  failed: "attention",
});

export const STATE_META = Object.freeze({
  awake: { label: "awake", color: "#d7ff58", rank: 0 },
  engaged: { label: "engaged", color: "#76e8bd", rank: 1 },
  dreaming: { label: "dreaming", color: "#b39cf4", rank: 2 },
  resting: { label: "resting", color: "#f4bd62", rank: 3 },
  attention: { label: "attention", color: "#ef786f", rank: 4 },
  offline: { label: "offline", color: "#69736d", rank: 5 },
  unknown: { label: "unknown", color: "#69736d", rank: 6 },
});

export function canonicalState(value) {
  const input = String(value ?? "unknown").trim().toLowerCase();
  const state = STATE_ALIASES[input] || input;
  return STATE_META[state] ? state : "unknown";
}
