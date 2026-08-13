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

import '../shared/runtime.js';

const FRAME = 512;              // samples per mic frame @16k ≈ 32 ms
const SPEECH_RMS = 0.02;        // energy gate (tune to your mic)
const HANGOVER_MS = 250;        // silence after speech before we endpoint (B2 §4.2)
// Debounce, mirroring the server's SpeechGate (desktop/voice/speech_gate.py):
// act only after N *consecutive* speech frames, not on the first spike.
const ONSET_FRAMES = 3;         // consecutive speech frames to start a new turn
const BARGEIN_FRAMES = 5;       // consecutive frames to interrupt her (stricter)
const PREROLL = 8;              // frames kept before onset so the first word isn't clipped

export function initVoice({ viseme, els }) {
  const runtime = window.YuriOSRuntime;
  const WS_URL = runtime.wsUrl('/ws/voice');
  const sessionKey = runtime.sessionKey();

  let ws = null;
  let sessionId = window.localStorage?.getItem(sessionKey) || null;
  let listening = false;
  let warming = false;              // the server is loading her voice for us (§9.9)
  let composerPlaceholder = null;   // the room's own wording, restored after a warm
  let muted = window.WorldControls?.isVoiceMuted?.() ?? true;
  let reconnectTimer = null;

  // One in-flight request per composer. The send affordance remains enabled and
  // becomes Stop while the input itself is locked.
  let processing = false;
  let requestId = null;
  let transport = null;
  let aborter = null;
  let llmDone = false;
  let activeSelfies = new Set();
  let completedSelfies = new Set();
  const sendMarkup = els.send?.innerHTML || '';

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
  const wantsVoice = () => !muted || listening;

  function connect() {
    if (!wantsVoice()) return;
    // One socket at a time: the server greets on every new session and several
    // connections can park in the voice-warm wait — never stack a reconnect on
    // a live/opening one (CONNECTING=0, OPEN=1). (B2's hard-won rule.)
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    setWarming(true, 'loading her voice…');
    ws = new WebSocket(WS_URL);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      stopPlayback();                   // a fresh connection may greet; no overlap
      ws.send(JSON.stringify({ type: 'hello', session_id: sessionId }));
    };
    // 4404 is the host saying "this character has no runtime" (world/host.py) —
    // a card still waiting on review, a disabled character, a failed start. The
    // wall does not move on its own, so stop throwing a reconnect at it twice a
    // second and leave the reason on screen.
    ws.onclose = (e) => {
      const parked = e.code === 4404;
      const capacity = e.code === 4429;
      setWarming(false);
      setStatus(parked || capacity ? 'error' : 'live', parked ? 'not running' :
        (capacity ? 'voice busy' :
        (wantsVoice() ? 'offline' : 'text online')));
      // The error frame that came just before says it in full; a close frame has
      // 123 bytes for a reason, so only fall back to it if nothing arrived.
      if (parked && e.reason && !els.caption.textContent) els.caption.textContent = e.reason;
      if (processing && transport === 'voice') finishProcessing();
      clearTimeout(reconnectTimer);
      if (wantsVoice()) reconnectTimer = setTimeout(connect,
        parked || capacity ? 15000 : 1500);
    };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  }

  function onMessage(m) {
    // The all-clear is `ready`, but any turn traffic means her voice landed too
    // — a server that never sent one still reopens the room. `session` is NOT
    // in that set: it arrives *during* the warm, right after the notice.
    if (warming && ['ready', 'filler', 'audio', 'done', 'error'].includes(m.type)) {
      setWarming(false);
    }
    switch (m.type) {
      case 'session':
        sessionId = m.session_id;
        window.localStorage?.setItem(sessionKey, sessionId);
        break;
      // her voice loads when the first client enters the room and is freed when
      // the last one leaves (SPEC §9.9) — so the first socket in can wait ~20 s
      // for cold models. Say what the silence is instead of looking hung.
      case 'warming':
        setWarming(true, m.message);
        break;
      case 'ready':
        break;                          // handled above; here so it isn't "unknown"
      case 'ping':
        if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'pong' }));
        break;
      case 'processing':
        beginProcessing(m.client_id || null, 'voice');
        break;
      case 'accepted':
        if (m.message) window.WorldChat?.confirmUser?.(m.message);
        break;
      case 'rejected':
        window.WorldChat?.failPending?.(m.client_id, 'not received');
        if (m.client_id === requestId) finishProcessing();
        break;
      case 'filler':
      case 'audio':
        if (m.text && m.type === 'audio') els.caption.textContent = m.text;
        enqueueAudio(decodePCM(m.pcm), m.sr);
        break;
      case 'done':
        showLatency(m.latency);
        completeLlm(m.client_id, m.active_selfies || []);
        break;
      case 'cancelled':
        els.caption.textContent = '';   // she yielded — the floor is yours
        if (!m.client_id || m.client_id === requestId) finishProcessing();
        break;
      case 'error':
        setStatus('error', 'error');
        if (m.message) els.caption.textContent = m.message;
        console.warn('server:', m.message);
        finishProcessing();
        break;
    }
  }

  function decodePCM(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Float32Array(bytes.buffer);
  }

  // ---- microphone + edge VAD ------------------------------------------------
  let micCtx = null, micNode = null, micStream = null;
  let speaking = false, silenceMs = 0, ring = [], speechRequestId = null;
  let speechRun = 0;

  async function startMic() {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    micCtx = new AudioContext({ sampleRate: 16000 });
    const src = micCtx.createMediaStreamSource(micStream);
    micNode = micCtx.createScriptProcessor(FRAME, 1, 1);
    src.connect(micNode); micNode.connect(micCtx.destination);
    micNode.onaudioprocess = (e) => onFrame(e.inputBuffer.getChannelData(0));
  }

  async function stopMic() {
    if (ws?.readyState === 1) {
      ws.send(JSON.stringify({ type: 'reset_audio' }));
    }
    for (const track of micStream?.getTracks?.() || []) track.stop();
    micStream = null;
    micNode?.disconnect();
    micNode = null;
    if (micCtx) await micCtx.close().catch(() => {});
    micCtx = null;
    speaking = false;
    speechRun = 0;
    ring = [];
  }

  function onFrame(frame) {
    // Voice keeps its intentional barge-in path. Only an HTTP text turn blocks
    // microphone frames, since that turn is not owned by this socket.
    if ((processing && (transport !== 'voice' || llmDone)) ||
        !ws || ws.readyState !== 1) return;
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
        speechRequestId = newRequestId();
        speaking = true; silenceMs = 0; speechRun = 0; setStatus('listening', 'listening');
        for (const f of ring) ws.send(f.buffer); ring = [];   // flush pre-roll
      }
    } else {
      ws.send(copy.buffer);
      silenceMs = isSpeech ? 0 : silenceMs + (FRAME / 16000) * 1000;
      if (silenceMs >= HANGOVER_MS) {   // endpoint (B2 §4.2)
        speaking = false; speechRun = 0; setStatus('live', 'online');
        ws.send(JSON.stringify({ type: 'endpoint', client_id: speechRequestId }));
        speechRequestId = null;
      }
    }
  }

  // ---- UI --------------------------------------------------------------------
  function setStatus(cls, text) {
    els.status.className = 'status ' + cls;
    els.status.textContent = text;
  }

  function newRequestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll('-', '');
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  }

  function renderProcessing() {
    if (els.text) els.text.disabled = processing || warming;
    if (els.mic) {
      els.mic.disabled = warming ||
        (processing && (transport !== 'voice' || llmDone));
    }
    if (!els.send) return;
    els.send.disabled = warming && !processing;
    els.send.classList.toggle('processing', processing);
    els.send.title = processing ? 'stop processing' : 'send';
    els.send.setAttribute('aria-label', els.send.title);
    els.send.innerHTML = processing
      ? '<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1"/></svg>'
      : sendMarkup;
  }

  function beginProcessing(clientId = null, via = transport || 'voice') {
    if (processing) return;
    processing = true;
    requestId = clientId;
    transport = via;
    llmDone = false;
    activeSelfies = new Set();
    completedSelfies = new Set();
    renderProcessing();
  }

  function finishProcessing() {
    processing = false;
    requestId = null;
    transport = null;
    llmDone = false;
    activeSelfies.clear();
    completedSelfies.clear();
    aborter = null;
    renderProcessing();
    if (!wantsVoice()) disconnectVoice();
    // Only reclaim focus if the composer still has it — if the user dismissed
    // the mobile keyboard (or never focused it, e.g. a mic turn) to read her
    // reply, don't yank the keyboard back open under them.
    if (document.activeElement === els.text) els.text?.focus();
  }

  function completeLlm(clientId, selfieIds = []) {
    if (!processing || (clientId && requestId && clientId !== requestId)) return;
    llmDone = true;
    for (const id of selfieIds) {
      if (!completedSelfies.has(id)) activeSelfies.add(id);
    }
    renderProcessing();
    if (activeSelfies.size === 0) finishProcessing();
  }

  function onWorldEvent(e) {
    const m = e.detail;
    if (!processing || m?.client_id !== requestId) return;
    if (m.type === 'message' && m.selfie_id) {
      completedSelfies.add(m.selfie_id);
      activeSelfies.delete(m.selfie_id);
      if (llmDone && activeSelfies.size === 0) finishProcessing();
      return;
    }
    if (m.type !== 'selfie_status') return;
    if (m.state === 'started') activeSelfies.add(m.id);
    else {
      completedSelfies.add(m.id);
      activeSelfies.delete(m.id);
      if (llmDone && activeSelfies.size === 0) finishProcessing();
    }
  }
  window.addEventListener('world-ev', onWorldEvent);
  window.addEventListener('chat-history-message', onWorldEvent);
  if (document.documentElement.classList.contains('desktop')) {
    window.addEventListener('chat-sending', (e) => {
      els.caption.textContent = `you: ${e.detail.text} · sending…`;
    });
    window.addEventListener('chat-received', (e) => {
      els.caption.textContent = `you: ${e.detail.text} · received`;
    });
  }

  function disconnectVoice() {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    stopPlayback();
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) ws.close(1000, 'voice muted');
    ws = null;
    setWarming(false);
    setStatus('live', 'text online');
  }

  window.addEventListener('voice-mute-change', (e) => {
    muted = Boolean(e.detail?.muted);
    if (wantsVoice()) connect();
    else if (!(processing && transport === 'voice')) disconnectVoice();
  });

  /* Her voice is loading, and until it lands she cannot answer anything (SPEC
   * §9.9). The socket is OPEN the whole time — the server just isn't reading it
   * yet, it's inside `acquire` — so a composer that still looks live takes a
   * typed line, clears the box, and drops it into a buffer nobody reads for the
   * next twenty seconds. Shut the door and say why: the placeholder is the
   * message you actually read, because it's the thing you were about to type
   * into. The caption and the status pill carry it for the room at large. */
  function setWarming(on, message) {
    warming = on;
    const note = message || 'loading her voice…';
    els.caption.textContent = on ? note : '';
    setStatus(on ? '' : 'live', on ? 'waking' : 'online');
    if (els.text) {
      if (on && composerPlaceholder === null) composerPlaceholder = els.text.placeholder;
      els.text.disabled = on || processing;
      if (!on && composerPlaceholder !== null) els.text.placeholder = composerPlaceholder;
      else if (on) els.text.placeholder = note;
    }
    if (els.send) els.send.disabled = on && !processing;
    // The mic too: it opens a turn by the same route her voice has to answer.
    if (els.mic) els.mic.disabled = on;
    // Each room words its own composer; the class is the hook their CSS uses.
    els.text?.closest('.composer')?.classList.toggle('warming', on);
    renderProcessing();
  }
  function setSpeaking(v) {
    playing = v;
    if (v) setStatus('speaking', 'speaking');
    else if (listening) setStatus('live', 'online');
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
    if (listening) connect();
    if (listening && !micCtx) {
      try { await startMic(); }
      catch (e) {
        console.warn(e);
        listening = false;
        els.mic.classList.remove('on');
        els.micLabel.textContent = 'start listening';
        if (muted) disconnectVoice();
        return;
      }
    }
    if (listening && micCtx) micCtx.resume();
    else if (!listening) await stopMic();
    if (listening) viseme.context().resume();
    if (!listening && muted) disconnectVoice();
  });

  // One send path for the two ways of asking: Enter, and the composer's button
  // (an affordance the switchboard's language wants visible — and the only one
  // a touch keyboard without a newline key can offer).
  async function sendHttp(path, body, clientId) {
    aborter = new AbortController();
    try {
      const response = await fetch(runtime.apiPath(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: aborter.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `request failed: ${response.status}`);
      if (data.session_id) {
        sessionId = data.session_id;
        window.localStorage?.setItem(sessionKey, sessionId);
      }
      if (data.user_message) window.WorldChat?.confirmUser?.(data.user_message);
      completeLlm(clientId, data.active_selfies || []);
    } catch (e) {
      if (e.name === 'AbortError') return;
      window.WorldChat?.failPending?.(clientId, 'not received');
      els.caption.textContent = e.message;
      finishProcessing();
    }
  }

  function stopProcessing() {
    if (!processing) return;
    const clientId = requestId;
    const selfieIds = [...activeSelfies];
    window.WorldChat?.stopPending?.(clientId);
    if (transport === 'voice' && ws?.readyState === 1) {
      ws.send(JSON.stringify({ type: 'cancel', client_id: clientId, selfie_ids: selfieIds }));
    } else {
      aborter?.abort();
      fetch(runtime.apiPath('/api/chat/cancel'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: clientId, selfie_ids: selfieIds }),
      }).catch(() => {});
    }
    finishProcessing();
  }

  function sendText() {
    if (processing) return;
    const text = els.text.value.trim();
    if (!text || warming) return;
    const clientId = newRequestId();
    window.WorldChat?.addPendingUser?.(text, clientId);
    els.text.value = '';
    if (wantsVoice() && ws?.readyState === 1) {
      beginProcessing(clientId, 'voice');
      if (playing) { stopPlayback(); ws.send(JSON.stringify({ type: 'bargein' })); }
      ws.send(JSON.stringify({ type: 'text', text, client_id: clientId }));
    } else {
      beginProcessing(clientId, 'http');
      sendHttp('/api/chat', {
        text, session_id: sessionId, channel: 'web', client_id: clientId,
      }, clientId);
    }
  }

  els.text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendText(); }
  });
  els.send?.addEventListener('click', () => processing ? stopProcessing() : sendText());

  if (wantsVoice()) connect();
  else {
    setStatus('live', 'text online');
    const greetingId = newRequestId();
    beginProcessing(greetingId, 'http');
    sendHttp('/api/greeting', {
      session_id: sessionId, channel: 'web', client_id: greetingId,
    }, greetingId);
  }
}
