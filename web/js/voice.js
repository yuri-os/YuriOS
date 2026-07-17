/* The client half of the voice loop (SPEC §9; B2 §4, §10) — Build #2's voice.js
 * ported to an ES module, with two changes and no others:
 *
 *   - playback routes through the VisemeDriver's analyser graph (SPEC §5), so
 *     the mouth is driven by the audio itself in the stage's render loop; this
 *     file never touches the mouth;
 *   - the wire is audio-only now (SPEC §10): expressions and chat text ride
 *     /api/events, so this file never touches the body either — it is ears,
 *     playback, and barge-in.
 *
 * Everything latency-critical about barge-in still lives here at the edge
 * (→ ch. 24): this file decides when the user is speaking, kills local playback
 * the instant it hears speech over her voice, and sends {"type":"bargein"} so
 * the server tears down TTS + generation.
 */

const FRAME = 512;              // samples per mic frame @16k ≈ 32 ms
const SPEECH_RMS = 0.02;        // energy gate (tune to your mic)
const HANGOVER_MS = 250;        // silence after speech before we endpoint (B2 §4.2)
// Debounce, mirroring the server's SpeechGate (desktop/voice/speech_gate.py):
// act only after N *consecutive* speech frames, not on the first spike.
const ONSET_FRAMES = 3;         // consecutive speech frames to start a new turn
const BARGEIN_FRAMES = 5;       // consecutive frames to interrupt her (stricter)
const PREROLL = 8;              // frames kept before onset so the first word isn't clipped

export function initVoice({ viseme, els }) {
  const WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/voice';

  let ws = null;
  let sessionId = window.localStorage?.getItem('yuri.session') || null;
  let listening = false;

  // ---- playback (her voice) through the viseme graph (SPEC §5) -------------
  let sinks = [], playing = false, playT = 0;

  function enqueueAudio(pcm, sr) {
    const ctx = viseme.context();
    const buf = ctx.createBuffer(1, pcm.length, sr);
    buf.copyToChannel(pcm, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(viseme.analyser);       // the analyser IS the lip-sync tap (§5.1)
    const startAt = Math.max(ctx.currentTime, playT);
    src.start(startAt);
    playT = startAt + buf.duration;
    sinks.push(src);
    setSpeaking(true);
    src.onended = () => {
      sinks = sinks.filter((s) => s !== src);
      if (sinks.length === 0) setSpeaking(false);
    };
  }

  function stopPlayback() {             // barge-in / new turn: silence her at once
    for (const s of sinks) { try { s.stop(); } catch (_) {} }
    sinks = []; playT = 0; setSpeaking(false);
  }

  // ---- the websocket --------------------------------------------------------
  function connect() {
    // One socket at a time: the server greets on every new session and several
    // connections can park in the voice-warm wait — never stack a reconnect on
    // a live/opening one (CONNECTING=0, OPEN=1). (B2's hard-won rule.)
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    ws = new WebSocket(WS_URL);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      stopPlayback();                   // a fresh connection may greet; no overlap
      setStatus('live', '· online');
      ws.send(JSON.stringify({ type: 'hello', session_id: sessionId }));
    };
    ws.onclose = () => { setStatus('', '· offline'); setTimeout(connect, 1500); };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  }

  function onMessage(m) {
    switch (m.type) {
      case 'session':
        sessionId = m.session_id;
        window.localStorage?.setItem('yuri.session', sessionId);
        break;
      case 'filler':
      case 'audio':
        if (m.text && m.type === 'audio') els.caption.textContent = m.text;
        enqueueAudio(decodePCM(m.pcm), m.sr);
        break;
      case 'done':
        showLatency(m.latency);
        break;
      case 'cancelled':
        els.caption.textContent = '';   // she yielded — the floor is yours
        break;
      case 'error':
        setStatus('', '· error');
        console.warn('server:', m.message);
        break;
    }
  }

  function decodePCM(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Float32Array(bytes.buffer);
  }

  // ---- microphone + edge VAD ------------------------------------------------
  let micCtx = null, micNode = null, speaking = false, silenceMs = 0, ring = [];
  let speechRun = 0;

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
      if (speechRun >= need) {
        if (playing) { stopPlayback(); ws.send(JSON.stringify({ type: 'bargein' })); }
        speaking = true; silenceMs = 0; speechRun = 0; setStatus('listening', '· listening');
        for (const f of ring) ws.send(f.buffer); ring = [];   // flush pre-roll
      }
    } else {
      ws.send(copy.buffer);
      silenceMs = isSpeech ? 0 : silenceMs + (FRAME / 16000) * 1000;
      if (silenceMs >= HANGOVER_MS) {   // endpoint (B2 §4.2)
        speaking = false; speechRun = 0; setStatus('live', '· online');
        ws.send(JSON.stringify({ type: 'endpoint' }));
      }
    }
  }

  // ---- UI --------------------------------------------------------------------
  function setStatus(cls, text) {
    els.status.className = 'status ' + cls;
    els.status.textContent = text;
  }
  function setSpeaking(v) {
    playing = v;
    if (v) setStatus('speaking', '· speaking');
    else if (listening) setStatus('live', '· online');
  }
  function showLatency(lat) {
    if (!lat || lat.first_audio_ms == null) { els.latency.textContent = ''; return; }
    const over = lat.over_budget && Object.keys(lat.over_budget).length > 0;
    els.latency.textContent = `${Math.round(lat.first_audio_ms)} ms${lat.masked ? ' (masked)' : ''}`;
    els.latency.className = 'latency' + (over ? ' over' : '');
  }

  els.mic.addEventListener('click', async () => {
    listening = !listening;
    els.mic.classList.toggle('on', listening);
    els.micLabel.textContent = listening ? 'listening…' : 'start listening';
    if (listening && !micCtx) { try { await startMic(); } catch (e) { console.warn(e); } }
    if (micCtx) (listening ? micCtx.resume() : micCtx.suspend());
    if (listening) viseme.context().resume();
  });

  els.text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && els.text.value.trim() && ws?.readyState === 1) {
      if (playing) { stopPlayback(); ws.send(JSON.stringify({ type: 'bargein' })); }
      ws.send(JSON.stringify({ type: 'text', text: els.text.value.trim() }));
      els.text.value = '';
    }
  });

  connect();
}
