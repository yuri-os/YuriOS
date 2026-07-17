/* The client half of the voice loop (SPEC §4, §10).
 *
 * Everything latency-critical about barge-in lives at the edge (→ ch. 24: "VAD at
 * the edge … so you never round-trip a server just to know she should stop").
 * So this file, not the server, decides when the user is speaking, and the
 * instant it hears speech over her voice it (1) kills local playback and (2)
 * sends {"type":"bargein"} so the server tears down TTS + generation. Waiting for
 * a server round-trip to stop the audio would be exactly the lag that breaks it.
 *
 * VAD here is a simple energy gate with hysteresis + a hangover — deliberately
 * plain and readable. Swap in @ricky0123/vad-web (Silero in the browser) for
 * robustness; the loop around it does not change.
 */
(() => {
  const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/voice";
  const FRAME = 512;              // samples per mic frame @16k ≈ 32 ms
  const SPEECH_RMS = 0.02;        // energy gate (tune to your mic)
  const HANGOVER_MS = 250;        // silence after speech before we endpoint (§4.2)
  // Debounce, mirroring the server's SpeechGate (desktop/voice/speech_gate.py):
  // act only after N *consecutive* speech frames, not on the first spike. A
  // mechanical-keyboard click is a 1–2 frame transient; requiring a sustained run
  // rejects "I typed and she stopped" false triggers. Interrupting her (barge-in)
  // is held to a higher bar than starting a fresh turn.
  const ONSET_FRAMES = 3;        // consecutive speech frames to start a new turn
  const BARGEIN_FRAMES = 5;      // consecutive frames to interrupt her (stricter)
  const PREROLL = 8;             // frames kept before onset so the first word isn't
                                 //   clipped — must exceed BARGEIN_FRAMES so the
                                 //   speech that confirmed onset is still in the ring

  const els = {
    status: document.getElementById("status"),
    latency: document.getElementById("latency"),
    caption: document.getElementById("caption"),
    mic: document.getElementById("mic"),
    micLabel: document.getElementById("mic-label"),
    text: document.getElementById("text"),
  };

  // `window.localStorage?.` (never a bare `localStorage`): engines without web
  // storage — WebKitGTK in pywebview's default private mode — don't define the
  // global at all, so a bare reference is a ReferenceError that kills this whole
  // script (and with it her body and the voice loop). Without storage she still
  // runs; the session just doesn't survive a reload.
  let ws = null, sessionId = window.localStorage?.getItem("yuri.session") || null;
  let listening = false;

  // ---- playback (her voice) + lipsync analyser -----------------------------
  let outCtx = null, analyser = null, sinks = [], playing = false, playT = 0;

  function outputContext() {
    if (!outCtx) {
      outCtx = new AudioContext();
      analyser = outCtx.createAnalyser();
      analyser.fftSize = 512;
      analyser.connect(outCtx.destination);
      requestAnimationFrame(lipsyncLoop);
    }
    return outCtx;
  }

  function enqueueAudio(pcm, sr) {
    const ctx = outputContext();
    const buf = ctx.createBuffer(1, pcm.length, sr);
    buf.copyToChannel(pcm, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf; src.connect(analyser);
    const startAt = Math.max(ctx.currentTime, playT);
    src.start(startAt);
    playT = startAt + buf.duration;
    sinks.push(src);
    setSpeaking(true);
    src.onended = () => {
      sinks = sinks.filter((s) => s !== src);
      if (sinks.length === 0) { setSpeaking(false); Avatar.setMouth?.(0); }
    };
  }

  function stopPlayback() {           // barge-in / new turn: silence her at once
    for (const s of sinks) { try { s.stop(); } catch (_) {} }
    sinks = []; playT = 0; setSpeaking(false); Avatar.setMouth?.(0);
  }

  function lipsyncLoop() {            // drive her mouth from the audio she's making
    if (analyser && playing) {
      const b = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(b);
      let s = 0; for (const v of b) s += v * v;
      const rms = Math.sqrt(s / b.length);
      Avatar.setMouth?.(Math.min(1, rms * 6));
    }
    requestAnimationFrame(lipsyncLoop);
  }

  // ---- the websocket -------------------------------------------------------
  function connect() {
    // One socket at a time. The server greets on *every* connection (she speaks
    // first, §7), so a second socket opened while one is still alive means two
    // greetings talking over each other — the reconnect timer must never stack a
    // new connection on a live/opening one (CONNECTING=0, OPEN=1).
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    ws = new WebSocket(WS_URL);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      // A fresh connection is about to greet; drop any audio still queued from a
      // previous connection so the new greeting can't overlap the old one.
      stopPlayback();
      setStatus("live", "· online");
      ws.send(JSON.stringify({ type: "hello", session_id: sessionId }));
    };
    ws.onclose = () => { setStatus("", "· offline"); setTimeout(connect, 1500); };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  }

  function onMessage(m) {
    switch (m.type) {
      case "session":
        sessionId = m.session_id; window.localStorage?.setItem("yuri.session", sessionId); break;
      case "expression":
        Avatar.setExpression?.(m.expression); break;
      case "filler":
      case "audio":
        if (m.text) els.caption.textContent = m.type === "audio" ? m.text : els.caption.textContent;
        enqueueAudio(decodePCM(m.pcm), m.sr); break;
      case "done":
        showLatency(m.latency); break;
      case "cancelled":
        els.caption.textContent = ""; break;    // she yielded — the floor is yours
      case "error":
        setStatus("", "· error"); console.warn("server:", m.message); break;
    }
  }

  function decodePCM(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Float32Array(bytes.buffer);
  }

  // ---- microphone + edge VAD ----------------------------------------------
  let micCtx = null, micNode = null, speaking = false, silenceMs = 0, ring = [];
  let speechRun = 0;             // consecutive speech frames seen while not yet in a turn

  async function startMic() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    micCtx = new AudioContext({ sampleRate: 16000 });
    const src = micCtx.createMediaStreamSource(stream);
    micNode = micCtx.createScriptProcessor(FRAME, 1, 1);
    src.connect(micNode); micNode.connect(micCtx.destination);
    micNode.onaudioprocess = (e) => onFrame(e.inputBuffer.getChannelData(0));
  }

  function onFrame(frame) {
    if (!ws || ws.readyState !== 1) return;
    let s = 0; for (const v of frame) s += v * v;
    const rms = Math.sqrt(s / frame.length);
    const isSpeech = rms >= SPEECH_RMS;
    const copy = Float32Array.from(frame);

    if (!speaking) {
      ring.push(copy); if (ring.length > PREROLL) ring.shift();
      speechRun = isSpeech ? speechRun + 1 : 0;
      // interrupting her costs more confidence than a fresh turn (rejects clatter)
      const need = playing ? BARGEIN_FRAMES : ONSET_FRAMES;
      if (speechRun >= need) {                  // sustained speech, not a transient
        if (playing) { stopPlayback(); ws.send(JSON.stringify({ type: "bargein" })); }
        speaking = true; silenceMs = 0; speechRun = 0; setStatus("listening", "· listening");
        for (const f of ring) ws.send(f.buffer); ring = [];   // flush pre-roll
      }
    } else {
      ws.send(copy.buffer);                     // stream speech frames to STT
      silenceMs = isSpeech ? 0 : silenceMs + (FRAME / 16000) * 1000;
      if (silenceMs >= HANGOVER_MS) {           // endpoint (§4.2)
        speaking = false; speechRun = 0; setStatus("live", "· online");
        ws.send(JSON.stringify({ type: "endpoint" }));
      }
    }
  }

  // ---- UI ------------------------------------------------------------------
  function setStatus(cls, text) { els.status.className = "status " + cls; els.status.textContent = text; }
  function setSpeaking(v) {
    playing = v;
    if (v) setStatus("speaking", "· speaking");
    else if (listening) setStatus("live", "· online");
  }
  function showLatency(lat) {
    if (!lat || lat.first_audio_ms == null) { els.latency.textContent = ""; return; }
    const over = lat.over_budget && Object.keys(lat.over_budget).length > 0;
    els.latency.textContent = `${Math.round(lat.first_audio_ms)} ms${lat.masked ? " (masked)" : ""}`;
    els.latency.className = "latency" + (over ? " over" : "");
  }

  els.mic.addEventListener("click", async () => {
    listening = !listening;
    els.mic.classList.toggle("on", listening);
    els.micLabel.textContent = listening ? "listening…" : "start listening";
    if (listening && !micCtx) { try { await startMic(); } catch (e) { console.warn(e); } }
    if (micCtx) (listening ? micCtx.resume() : micCtx.suspend());
    if (listening && outCtx) outCtx.resume();
  });

  els.text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && els.text.value.trim() && ws?.readyState === 1) {
      if (playing) { stopPlayback(); ws.send(JSON.stringify({ type: "bargein" })); }
      ws.send(JSON.stringify({ type: "text", text: els.text.value.trim() }));
      els.text.value = "";
    }
  });

  Avatar.init?.().finally(connect);   // load the body if present, then dial in
})();
