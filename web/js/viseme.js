/* Visemes, amplitude tier (SPEC §5; CS §5.4 one tier down).
 *
 * The mouth follows the RMS of the audio *actually playing*: an AnalyserNode
 * sits between every queued TTS buffer and the speakers, so mouth and voice
 * read the same samples and cannot drift. `level()` is registered as the
 * stage's viseme source and sampled once per frame in loop step 9 — the value
 * stages the `aa` expression there, in CS §4 order, never out-of-band.
 *
 * Constants adapted from CS §5.4: perceptual amp^0.7 curve, fast attack /
 * slower release smoothing, a silence gate so noise doesn't chatter the lips,
 * and a weight cap so `aa` never slams fully open.
 */

const SILENCE_GATE = 0.04;   // shaped values below this are closed-mouth
const WEIGHT_CAP = 0.7;      // aa weight ceiling — full 1.0 looks unhinged
const CURVE = 0.7;           // amp^0.7 — perceptual loudness → openness
const ATTACK = 50;           // per-second lerp rate opening (fast)
const RELEASE = 30;          // per-second lerp rate closing (slower)
const GAIN = 5.0;            // RMS (~0..0.2 for speech) → 0..1 before the curve

export class VisemeDriver {
  constructor() {
    this.ctx = undefined;
    this.analyser = undefined;
    this.buf = undefined;
    this.value = 0;              // the smoothed weight the stage reads
    this.lastT = performance.now();
  }

  /** Lazily create the output AudioContext (must follow the enter gesture,
   *  SPEC §6.4). voice.js connects every playback source to `analyser`. */
  context() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.connect(this.ctx.destination);
      this.buf = new Float32Array(this.analyser.fftSize);
    }
    return this.ctx;
  }

  /** The stage's viseme source: () => 0..1, sampled once per render frame. */
  level = () => {
    const now = performance.now();
    const dt = Math.min((now - this.lastT) / 1000, 0.1);
    this.lastT = now;
    if (!this.analyser) return 0;

    this.analyser.getFloatTimeDomainData(this.buf);
    let s = 0;
    for (const v of this.buf) s += v * v;
    const rms = Math.sqrt(s / this.buf.length);

    let target = Math.pow(Math.min(1, rms * GAIN), CURVE);
    if (target < SILENCE_GATE) target = 0;
    target = Math.min(target, WEIGHT_CAP);

    const rate = target > this.value ? ATTACK : RELEASE;
    this.value += (target - this.value) * Math.min(1, rate * dt);
    if (this.value < 0.001) this.value = 0;
    return this.value;
  };
}
