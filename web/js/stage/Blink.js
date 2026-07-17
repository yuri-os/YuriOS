/* Procedural blink (SPEC §3.5; CS §5.3) — port of the vrm-viewer Blink.ts.
 * Random interval 1–6 s, single blink 0.2 s, weight follows sin(π·progress).
 * Independent of emotion/viseme: it owns the 'blink' expression (SPEC §3.4). */
export class Blink {
  constructor() {
    this.DURATION = 0.2;
    this.MIN = 1;
    this.MAX = 6;
    this.blinking = false;
    this.progress = 0;
    this.sinceLast = 0;
    this.nextAt = Math.random() * (this.MAX - this.MIN) + this.MIN;
  }

  update(vrm, delta) {
    const mgr = vrm.expressionManager;
    if (!mgr) return;

    this.sinceLast += delta;
    if (!this.blinking && this.sinceLast >= this.nextAt) {
      this.blinking = true;
      this.progress = 0;
    }
    if (!this.blinking) return;

    this.progress += delta / this.DURATION;
    mgr.setValue('blink', Math.sin(Math.PI * Math.min(1, this.progress)));

    if (this.progress >= 1) {
      this.blinking = false;
      this.sinceLast = 0;
      mgr.setValue('blink', 0);
      this.nextAt = Math.random() * (this.MAX - this.MIN) + this.MIN;
    }
  }
}
