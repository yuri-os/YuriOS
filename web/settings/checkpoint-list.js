/* The Checkpoints page's decisions, kept apart from the DOM (SPEC §11.5).
 *
 * The server sends the catalog (each card already sorted into what this build
 * will do with it), the folder's files and the downloads in flight. What is
 * decided here is how those meet: whether a card is on this machine, half on it,
 * downloading or still out there; which cards a filter keeps; which previews a
 * card shows while NSFW is off. Tested in web/tests/checkpoints.test.js. */

/** Civitai's PG and PG-13: what a card shows while NSFW is off. */
export const SFW_IMAGE_LEVEL = 2;
export const ISSUES_URL = "https://github.com/yuri-os/YuriOS/issues/new";

export const STATUS_LABELS = Object.freeze({
  best: "Ready", works: "Loads", likely: "Check", caution: "Check", no: "Not supported",
});

/** Where one card stands on this machine:
 *  downloading | downloaded | paused (a .part to resume) | failed | available. */
export function cardState(card, { files = [], partial = {}, downloads = [] } = {}) {
  const job = downloads.find((d) => d.version_id === card.version_id);
  if (job && (job.state === "queued" || job.state === "downloading")) return "downloading";
  if (files.some((f) => f.file === card.file)) return "downloaded";
  if (job && job.state === "failed") return "failed";
  if (partial[card.file] != null) return "paused";
  return "available";
}

const ON_DISK = new Set(["downloaded", "downloading", "paused"]);

/** The cards a set of filters keeps, most-downloaded first within a family,
 *  with what this build can't load sunk to the end of it. */
export function visibleCards(cards, { family = "all", status = "all", nsfw = false,
  downloaded = false } = {}, local = {}) {
  const keep = (card) => {
    if (!nsfw && card.nsfw) return false;
    if (family !== "all" && card.family !== family) return false;
    if (status === "best" && card.status !== "best") return false;
    if (status === "usable" && !["best", "works"].includes(card.status)) return false;
    if (downloaded && !ON_DISK.has(cardState(card, local))) return false;
    return true;
  };
  return cards.filter(keep).sort((a, b) => (a.status === "no") - (b.status === "no")
    || b.downloads - a.downloads);
}

/** Up to `n` preview images: only PG / PG-13 ones while NSFW is off. */
export function previews(card, nsfw, n = 2) {
  return (card.images || []).filter((i) => nsfw || i.nsfw_level <= SFW_IMAGE_LEVEL).slice(0, n);
}

/** The files in the folder no listed card accounts for — a model you fetched
 *  yourself, or one that has since dropped out of the top. */
export function unlistedFiles(files, cards) {
  const listed = new Set(cards.map((c) => c.file));
  return files.filter((f) => !listed.has(f.file));
}

/** A prefilled GitHub issue asking for a checkpoint this build can't load. */
export function supportIssueUrl(card) {
  const title = `Checkpoint support: ${card.name} (${card.base_model})`;
  const body = [
    `**Model:** [${card.name} · ${card.version}](${card.url})`,
    `**Base model:** ${card.base_model}`,
    `**File:** \`${card.file}\`${card.size_bytes ? ` (${bytes(card.size_bytes)})` : ""}`,
    "",
    `**Why it doesn't load today:** ${card.note}`,
    "",
    "What I'd like to use it for:",
  ].join("\n");
  const query = new URLSearchParams({ title, body, labels: "checkpoint-support" });
  return `${ISSUES_URL}?${query}`;
}

export function bytes(n) {
  const value = Number(n) || 0;
  if (value >= 1e9) return `${(value / 1e9).toFixed(value >= 1e10 ? 0 : 1)} GB`;
  if (value >= 1e6) return `${Math.round(value / 1e6)} MB`;
  if (value >= 1e3) return `${Math.round(value / 1e3)} kB`;
  return `${value} B`;
}

export function count(n) {
  const value = Number(n) || 0;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${Math.round(value / 1e3)}k`;
  return String(value);
}

/** "312 MB of 6.9 GB · 18 MB/s · 6 min left" */
export function progressLine(job) {
  const parts = [job.total ? `${bytes(job.done)} of ${bytes(job.total)}` : bytes(job.done)];
  if (job.speed > 0) {
    parts.push(`${bytes(job.speed)}/s`);
    if (job.total > job.done) {
      const s = (job.total - job.done) / job.speed;
      parts.push(s < 90 ? `${Math.max(1, Math.round(s))} s left` : `${Math.round(s / 60)} min left`);
    }
  }
  return parts.join(" · ");
}

export function percent(job) {
  return job.total ? Math.min(100, (100 * job.done) / job.total) : 0;
}

/** How long ago a unix time was, in the largest unit that fits. */
export function ago(seconds, now = Date.now() / 1000) {
  const s = Math.max(0, now - seconds);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
