/* Live2D's audio adapter. Turn transport, cancellation, request receipts, mic
 * VAD and the composer all live in the shared /js/voice.js implementation so
 * the three browser rooms cannot drift apart.
 */
(() => {
  const els = {
    status: document.getElementById('status'),
    latency: document.getElementById('latency'),
    caption: document.getElementById('caption'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    text: document.getElementById('text'),
    send: document.getElementById('send'),
  };

  let outCtx = null;
  let analyser = null;
  let outGain = null;
  let muted = true;

  function outputContext() {
    if (outCtx) return outCtx;
    outCtx = new AudioContext();
    analyser = outCtx.createAnalyser();
    analyser.fftSize = 512;
    outGain = outCtx.createGain();
    outGain.gain.value = muted ? 0 : 1;
    analyser.connect(outGain);
    outGain.connect(outCtx.destination);
    requestAnimationFrame(lipsyncLoop);
    return outCtx;
  }

  function setMuted(value) {
    muted = Boolean(value);
    if (!outGain) return;
    const t = outCtx.currentTime;
    outGain.gain.cancelScheduledValues(t);
    outGain.gain.setTargetAtTime(muted ? 0 : 1, t, 0.015);
  }

  function lipsyncLoop() {
    if (analyser) {
      const samples = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const value of samples) sum += value * value;
      Avatar.setMouth?.(Math.min(1, Math.sqrt(sum / samples.length) * 6));
    }
    requestAnimationFrame(lipsyncLoop);
  }

  const viseme = {
    context: outputContext,
    get analyser() {
      outputContext();
      return analyser;
    },
  };

  window.WorldControls?.init({ setMuted });
  Avatar.init?.().finally(async () => {
    const { initVoice } = await import('/js/voice.js');
    initVoice({ viseme, els });
  });
})();
