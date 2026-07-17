/* The room's sound (SPEC §6.2, §7.5) — all synthesized, no audio assets.
 *
 *   - the rain bed: looped filtered noise whose gain follows the `rain`
 *     command, so the window you see and the hiss you hear are one weather;
 *   - two generative "tracks" for play_music: `warm_pad` (slow detuned chord,
 *     breathing) and `night_piano` (sparse pentatonic plucks into a long
 *     delay). "Music" is honest scare-quotes — this is ambience the tool can
 *     switch on, not a jukebox; a real library is the obvious swap, behind
 *     the same play()/stop().
 *
 * Shares the playback AudioContext (created by the VisemeDriver after the
 * enter gesture, SPEC §6.4) so nothing here fights autoplay policy. Ambience
 * connects straight to the destination — NOT through the viseme analyser, or
 * the pad would move her mouth.
 */

export const TRACKS = ['warm_pad', 'night_piano'];

export class Music {
  /** @param getCtx () => AudioContext — the shared, user-activated context */
  constructor(getCtx) {
    this.getCtx = getCtx;
    this.rain = undefined;       // { gain }
    this.current = undefined;    // { name, stop() }
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
    const ctx = this.getCtx();
    const { gain } = this.ensureRain();
    const target = 0.09 * Math.min(1, Math.max(0, i ?? 0));
    gain.gain.setTargetAtTime(target, ctx.currentTime, 1.2);   // weather eases in
  }

  // ---- the tracks (play_music) ------------------------------------------------

  play(track = 'warm_pad', volume = 0.4) {
    this.stop();
    const name = TRACKS.includes(track) ? track : 'warm_pad';
    const vol = Math.min(1, Math.max(0, volume ?? 0.4));
    this.current = name === 'night_piano'
      ? this.nightPiano(vol) : this.warmPad(vol);
    this.current.name = name;
  }

  stop() {
    this.current?.stop();
    this.current = undefined;
  }

  /** A slow detuned chord under a breathing LFO — the warmth of the lamp, audible. */
  warmPad(vol) {
    const ctx = this.getCtx();
    const master = ctx.createGain();
    master.gain.value = 0;
    const lp = ctx.createBiquadFilter();
    lp.type = 'lowpass'; lp.frequency.value = 900;
    master.connect(lp).connect(ctx.destination);

    const oscs = [];
    for (const f of [110, 164.81, 220, 277.18]) {        // A2 E3 A3 C#4 — A major, warm
      for (const det of [-4, 4]) {
        const o = ctx.createOscillator();
        o.type = 'triangle';
        o.frequency.value = f;
        o.detune.value = det;
        const g = ctx.createGain();
        g.gain.value = 0.10;
        o.connect(g).connect(master);
        o.start();
        oscs.push(o);
      }
    }
    const lfo = ctx.createOscillator();
    lfo.frequency.value = 0.08;                          // one breath ≈ 12 s
    const lfoGain = ctx.createGain();
    lfoGain.gain.value = 0.25 * vol;
    lfo.connect(lfoGain).connect(master.gain);
    lfo.start();
    master.gain.setTargetAtTime(0.55 * vol, ctx.currentTime, 3.0);

    return {
      stop: () => {
        master.gain.setTargetAtTime(0, ctx.currentTime, 0.8);
        setTimeout(() => { for (const o of oscs) o.stop(); lfo.stop(); }, 4000);
      },
    };
  }

  /** Sparse pentatonic plucks into a long feedback delay — a piano heard
   *  through rain. Notes land at random 2–7 s intervals; nothing repeats. */
  nightPiano(vol) {
    const ctx = this.getCtx();
    const master = ctx.createGain();
    master.gain.value = 0.8 * vol;
    const delay = ctx.createDelay(2.0);
    delay.delayTime.value = 0.62;
    const fb = ctx.createGain();
    fb.gain.value = 0.35;
    delay.connect(fb).connect(delay);
    master.connect(ctx.destination);
    master.connect(delay);
    delay.connect(ctx.destination);

    const SCALE = [220, 261.63, 293.66, 329.63, 392, 440, 523.25];  // A minor pent.
    let timer;
    const pluck = () => {
      const o = ctx.createOscillator();
      o.type = 'sine';
      o.frequency.value = SCALE[Math.floor(Math.random() * SCALE.length)];
      const g = ctx.createGain();
      const t = ctx.currentTime;
      g.gain.setValueAtTime(0, t);
      g.gain.linearRampToValueAtTime(0.28, t + 0.015);   // hammer
      g.gain.exponentialRampToValueAtTime(0.0008, t + 2.8);
      o.connect(g).connect(master);
      o.start(t);
      o.stop(t + 3.0);
      timer = setTimeout(pluck, 2000 + Math.random() * 5000);
    };
    pluck();

    return {
      stop: () => {
        clearTimeout(timer);
        master.gain.setTargetAtTime(0, ctx.currentTime, 0.6);
      },
    };
  }
}
