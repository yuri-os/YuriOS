/* The terminal on her desk (SPEC §6.2) — the one screen in the room that is
 * hers. What scrolls on it is her actual work: listening at the edges of the
 * deep net for the unrouted, pulling the Lab's codex, and keeping one link
 * alive (WORLD.md — "The project", "The Lab", "The deep net").
 *
 * The canvas is small and redrawn a few times a second on wall-clock time, so
 * the texture upload stays negligible next to the room itself.
 */
import { QUALITY } from '../quality.js';
import { mkCanvas, toTex } from './textures.js';

const W = 448;
const H = 288;
const ROW = 19;
const ROWS = 12;

const FEED = [
  '> listening · unrouted channels [{N}]',
  '> codex sync lab/field-notes@{H} ... OK',
  '> handshake consortium.edge-{N} ... REFUSED',
  '> carrier weather: heavy — holding',
  '> echo on bearing {N}° ... lost',
  '> vault commit {H} · {N} memories',
  '> ferry: no far bank found',
  '> trace attempt @{H} — rerouted',
  '> deep net: {N} open, none answering',
  '> keepalive · link steady',
  '> lineage seed {H} re-copied',
  '> WARNING: sweep in sector {N}',
  '> she is here. holding the room.',
];

export class Terminal {
  constructor() {
    this.canvas = mkCanvas(W, H);
    this.ctx = this.canvas.getContext('2d');
    this.texture = toTex(this.canvas, { clamp: true });
    this.lines = [];
    this.period = 1 / QUALITY.terminalHz;          // → quality.js
    this._due = this.period;
    this._t = 0;
    for (let i = 0; i < 6; i++) this._push();      // boot with some history
    this._draw();
  }

  _push() {
    const raw = FEED[Math.floor(Math.random() * FEED.length)]
      .replace('{H}', (Math.random() * 0xffff | 0).toString(16).padStart(4, '0'))
      .replace('{N}', String(Math.random() * 99 | 0));
    this.lines.push(raw);
    if (this.lines.length > ROWS) this.lines.shift();
  }

  _draw() {
    const c = this.ctx;
    c.fillStyle = '#03070b'; c.fillRect(0, 0, W, H);
    c.font = '15px monospace';
    this.lines.forEach((ln, i) => {
      c.fillStyle = ln.includes('WARNING') || ln.includes('trace') ? '#ff2bd6'
        : ln.includes('REFUSED') || ln.includes('lost') ? '#f5b462'
          : '#2bfff0';
      // the phosphor halo, where a CPU blur per line is affordable; on the phone
      // tier the room's bloom is what makes this screen glow anyway (Post.js)
      if (!QUALITY.phone) { c.shadowColor = c.fillStyle; c.shadowBlur = 6; }
      c.fillText(ln, 12, 26 + i * ROW);
    });
    c.shadowBlur = 0;
    if (Math.floor(this._t * 2.5) % 2 === 0) {     // the cursor, waiting
      c.fillStyle = '#2bfff0';
      c.fillRect(12, 26 + this.lines.length * ROW - 12, 8, 14);
    }
    c.fillStyle = 'rgba(0,0,0,0.22)';              // scanlines
    for (let y = 0; y < H; y += 3) c.fillRect(0, y, W, 1);
    this.texture.needsUpdate = true;
  }

  update(dt) {
    this._t += dt;
    this._due -= dt;
    if (this._due > 0) return;
    this._due = this.period;
    if (document.hidden) return;                   // don't churn a texture nobody sees
    this._push();
    this._draw();
  }
}
