/* The room's sound (SPEC §6.2) — synthesized, no audio assets: the rain bed,
 * looped filtered noise whose gain follows the `rain` command, so the window
 * you see and the hiss you hear are one weather.
 *
 * (It had two generative "tracks" beside it once, for a `play_music` hand. The
 * hand was retired and nothing else could start them, so they went too.)
 *
 * Shares the playback AudioContext (created by the VisemeDriver after the
 * enter gesture, SPEC §6.4) so nothing here fights autoplay policy. The bed
 * connects straight to the destination — NOT through the viseme analyser, or
 * the rain would move her mouth.
 */

export class RainSound {
  /** @param getCtx () => AudioContext — the shared, user-activated context */
  constructor(getCtx) {
    this.getCtx = getCtx;
    this.rain = undefined;       // { gain }
    this.rainLevel = 0;          // last intensity the `rain` command asked for
    this.rainMuted = false;      // the mute button; overrides the bed to silence
  }

  // ---- the rain bed ----------------------------------------------------------

  ensureRain() {
    if (this.rain) return this.rain;
    const ctx = this.getCtx();
    // 2 s of white noise, looped, shaped down to rain-on-glass
    const len = ctx.sampleRate * 2;
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    const hp = ctx.createBiquadFilter();
    hp.type = 'highpass'; hp.frequency.value = 400;
    const lp = ctx.createBiquadFilter();
    lp.type = 'lowpass'; lp.frequency.value = 1400;
    const gain = ctx.createGain();
    gain.gain.value = 0;
    src.connect(hp).connect(lp).connect(gain).connect(ctx.destination);
    src.start();
    this.rain = { gain };
    return this.rain;
  }

  /** Follows the `rain` command's intensity (0..1). */
  setRainLevel(i) {
    this.rainLevel = Math.min(1, Math.max(0, i ?? 0));
    this.applyRainGain(1.2);                                    // weather eases in
  }

  /** The mute button: silence (or restore) the audible rain bed without losing
   *  the intensity the weather is at, so the window keeps raining either way.
   *  @param muted boolean, or undefined to toggle. @returns the new muted state. */
  setRainMuted(muted) {
    this.rainMuted = muted ?? !this.rainMuted;
    this.applyRainGain(0.25);                                   // a quick, clean cut
    return this.rainMuted;
  }

  applyRainGain(timeConstant) {
    const ctx = this.getCtx();
    const { gain } = this.ensureRain();
    const target = this.rainMuted ? 0 : 0.09 * this.rainLevel;
    gain.gain.setTargetAtTime(target, ctx.currentTime, timeConstant);
  }
}
