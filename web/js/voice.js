/* The client half of the voice loop (SPEC §9; B2 §4, §10) — Build #2's voice.js
 * ported to an ES module, with three changes and no others:
 *
 *   - playback routes through the VisemeDriver's analyser graph (SPEC §5), so
 *     the mouth is driven by the audio itself in the stage's render loop; this
 *     file never touches the mouth;
 *   - the wire is audio-only now (SPEC §10): expressions and chat text ride
 *     /api/events, so this file never touches the body either — it is ears,
 *     playback, and barge-in;
 *   - and it reads a line back out on request (SPEC §9.11): chat.js draws the
 *     button on her bubbles, this owns what a press does. The ask rides this
 *     socket because a replay wants exactly what the socket already has —
 *     playback through the viseme graph, barge-in, and the §9.9 listener count
 *     — and the wire carries the message id, never the words.
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

  // ---- reading one of her lines back out (SPEC §9.11) -----------------------
  let replayId = null;          // the line the socket is (about to be) reading
  let replaySent = false;       // …the ask is on the wire (not just queued behind a warm)
  let replayDrained = false;    // …the server has sent it all; audio may still play
  let replayHeard = null;       // the mute hold, while a muted room reads one out
  let replayTimer = null;       // …and the "did the server even hear me?" net

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

  /* "read it out" on one of her chat bubbles (SPEC §9.11). chat.js draws the
   * button and this owns what happens: the ask rides the voice socket rather
   * than an endpoint of its own, because the socket already has the three
   * things a replay wants and an endpoint would have to grow copies of —
   * playback through the viseme graph (so her mouth moves on a replay too),
   * barge-in (talking over her silences it, exactly as over a reply), and the
   * §9.9 listener count that keeps her voice loaded only while somebody wants
   * it. The wire carries the message id, never the text: what comes back is
   * resolved out of her own transcript server-side, so this can ask her to say
   * something again and cannot ask her to say something new.
   *
   * A muted room still reads a line out. Muting is "not by default"; pressing
   * this is asking for *this line*, so the gain is held open for the length of
   * it (WorldControls.hearThrough) and the switch never moves. */
  function speak(messageId) {
    if (!messageId) return;
    const again = messageId === replayId;
    // A switch keeps the socket it is about to use. Letting go of it first
    // would dip through "nothing wants her voice", close the connection, and
    // then open a second one between two presses — and the old one's `onclose`
    // would arrive to put out a button that now belongs to the new line.
    endReplay({ keepSocket: !again });
    if (again) return;                  // a second press on the same line stops it
    replayId = messageId;
    replayHeard = window.WorldControls?.hearThrough?.() || null;
    window.WorldChat?.markSpeaking?.(replayId, 'waiting');
    if (ws?.readyState === 1) sendReplay();
    else connect();                     // …and `ready` sends it once she can talk
  }

  function sendReplay() {
    if (!replayId || replaySent || ws?.readyState !== 1) return;
    replaySent = true;
    if (playing) stopPlayback();        // this line takes the floor from the last
    ws.send(JSON.stringify({ type: 'speak', message_id: replayId }));
    // The net under a server that does not know this frame (an older host, the
    // B2 desktop route): nothing would ever come back and the button would spin
    // for the rest of the session. `speaking` clears it, and it is generous
    // because a cold voice may still have been warming when we sent.
    clearTimeout(replayTimer);
    replayTimer = setTimeout(() => {
      notice('she didn\u2019t pick that up');
      endReplay();
    }, 30000);
  }

  /** The line is over — read, stopped, barged in on, or never started. Puts the
   *  button out, gives the mute back, and lets go of her voice if this was the
   *  only thing that wanted it (§9.9). Idempotent. `keepSocket` is for the two
   *  callers that own the connection themselves: a switch to another line, and
   *  `disconnectVoice`, which is already closing it. */
  function endReplay({ keepSocket = false } = {}) {
    if (!replayId) return;
    const id = replayId;
    replayId = null;
    replaySent = false;
    replayDrained = false;
    clearTimeout(replayTimer);
    replayTimer = null;
    replayHeard?.();
    replayHeard = null;
    window.WorldChat?.markSpeaking?.(id, '');
    // Stop the audio only when the reader asked us to — a line that simply
    // finished is already silent, and the last chunks are queued in the
    // AudioContext, which does not need the socket any more.
    if (ws?.readyState === 1 && playing) ws.send(JSON.stringify({ type: 'bargein' }));
    if (playing) stopPlayback();
    if (keepSocket) return;
    if (!wantsVoice() && !(processing && transport === 'voice')) disconnectVoice();
  }

  // ---- the websocket --------------------------------------------------------
  // A replay counts: pressing "read it out" in a muted room is somebody
  // asking to hear her, so it opens the socket (and warms her voice) the
  // same way unmuting does — and lets go again when the line is done.
  const wantsVoice = () => !muted || listening || replayId !== null;

  function connect() {
    if (!wantsVoice()) return;
    // One socket at a time: the server greets on every new session and several
    // connections can park in the voice-warm wait — never stack a reconnect on
    // a live/opening one (CONNECTING=0, OPEN=1). (B2's hard-won rule.)
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    setWarming(true, 'loading her voice…');
    const socket = new WebSocket(WS_URL);
    ws = socket;
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
      // A socket we have already replaced is history: `disconnectVoice` nulls
      // `ws` and a new connection can be open before the old one's close lands,
      // and this handler writes the room's shared state (the warm notice, the
      // status pill, the replay button). Only the current one may.
      if (ws !== socket) return;
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
      endReplay();                      // a dropped socket mid-line is a stopped line
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
        // A voice seam that refused to load falls back to the fake and the fake
        // is silent — the turn still runs, her line still lands in the
        // transcript, and the only difference out here is that nothing plays.
        // Say it in the room rather than leaving "why is she not answering?"
        // to a WARNING in the log nobody is looking at.
        if (m.tts === 'fake') sayMute();
        // A replay pressed in a muted room is what opened this socket; her
        // voice is only now loaded, so this is the first moment it can be asked
        // for. (Also the reconnect path: a warm stack answers `ready` at once.)
        sendReplay();
        break;                          // otherwise handled above
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
        if (m.message) window.WorldChat?.receiveMessage?.(m.message);
        completeLlm(m.client_id, m.active_selfies || []);
        break;
      // she took the ask (SPEC §9.11) — `audio` frames follow on the same wire
      // and through the same playback, so a replay moves her mouth too
      case 'speaking':
        if (m.message_id !== replayId) break;
        clearTimeout(replayTimer);
        replayTimer = null;
        window.WorldChat?.markSpeaking?.(replayId, 'speaking');
        break;
      // …and it is over, however it ended. `message` is why it did not happen —
      // a line off the end of the ring, or her mid-reply. Ignored when it names
      // a line this page has already moved on from (press A, then press B).
      case 'spoken':
        if (m.message_id !== replayId) break;
        if (m.message) notice(m.message);
        replayDrained = true;
        if (!playing) endReplay();      // …otherwise when the last sample plays
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
        endReplay();                    // talking over a replay ends it too (§9.11)
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
    // The paperclip stays live while she answers: the composer is locked, so an
    // armed picture simply waits for the next line — which is better than a
    // paste that silently does nothing because a turn happened to be running.
    if (attachBtn) attachBtn.disabled = warming;
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
    // Sticky, so this arrives on subscribe as well as on a live model swap.
    if (m?.type === 'capabilities') {
      setCanSendPictures(m.image_input);
      return;
    }
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
    endReplay({ keepSocket: true });    // …before the playback it is holding open
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
    // The switch outranks the hold a replay is keeping on the gain (§9.10):
    // reaching for mute while she is reading a line out means silence *now*,
    // not "after this one". Ending the replay is what releases that hold.
    if (muted) endReplay();
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
  /* Her voice didn't load. Written once per connection, into the same line the
   * warm notice used — it's where you're already looking when you're waiting to
   * hear her, and the next caption she speaks would overwrite it anyway (there
   * won't be one). Deliberately not phrased as "she's broken": she is answering
   * normally, in text, which is the part that isn't obvious. */
  function sayMute() {
    els.caption.textContent = 'her voice didn\u2019t load \u2014 she\u2019s answering ' +
                              'in text only (the log says why)';
    setStatus('live', 'text only');
  }
  function setSpeaking(v) {
    playing = v;
    if (v) setStatus('speaking', 'speaking');
    else if (listening) setStatus('live', 'online');
    // A replay is over when the last sample has *played*, not when the last
    // frame arrived: `spoken` lands while the tail is still queued in the
    // AudioContext, and putting the button out there would cut her off.
    if (!v && replayId && replayDrained) endReplay();
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

  /* ---- pictures you send her (SPEC §35) -------------------------------
   *
   * The paperclip exists only when the model on the other end of the chat seam
   * can actually look at one. The host settles that at boot by asking her
   * provider and publishes it as a sticky `capabilities` event, so this arrives
   * on the same bus as everything else and — because it is sticky — a page that
   * opens an hour later still gets it, and a model swapped live changes the
   * composer in every open room at once.
   *
   * The controls are built here rather than written into three index.html files
   * for the reason chat.js gives for existing at all: three rooms run this one
   * script, and a per-page copy of an affordance is three places for it to
   * drift. Their looks are /shared/composer.css, which all three load.
   *
   * A chosen file goes up over HTTP immediately (POST /api/uploads) and the
   * turn that follows carries only the id it answered with — so a 3 MB photo is
   * never inside a voice-socket frame, and a picture that is refused is refused
   * before you have typed the sentence to go with it.
   */
  const PICTURE_TYPES = 'image/png,image/jpeg,image/webp,image/gif,image/bmp';
  let canSendPictures = false;
  let picture = null;                 // {id, url} — armed, ready to ride a turn
  let pictureBusy = false;            // the file is still going up
  let pictureSeq = 0;                 // …and which choice the chip belongs to
  let attachBtn = null, fileInput = null, chip = null;

  function buildAttach() {
    const composer = els.text?.closest('.composer');
    if (!composer || attachBtn) return;
    fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = PICTURE_TYPES;
    fileInput.hidden = true;
    attachBtn = document.createElement('button');
    attachBtn.type = 'button';
    attachBtn.className = 'icon-button attach';
    attachBtn.hidden = true;
    attachBtn.title = 'send a picture';
    attachBtn.setAttribute('aria-label', 'send a picture');
    attachBtn.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24">' +
      '<path d="M16.5 6.5 9 14a2.5 2.5 0 0 0 3.5 3.5l7-7a4.5 4.5 0 0 0-6.4-6.3l-7 7a6.5 6.5 0 0 0 9.2 9.2l5.2-5.2"/>' +
      '</svg>';
    chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'pic-chip';
    chip.hidden = true;
    chip.title = 'take the picture off this line';
    chip.setAttribute('aria-label', chip.title);
    chip.innerHTML = '<img alt="">';
    composer.insertBefore(attachBtn, els.text);
    composer.insertBefore(chip, els.text);
    composer.appendChild(fileInput);
    attachBtn.addEventListener('click', () => fileInput.click());
    chip.addEventListener('click', clearPicture);
    fileInput.addEventListener('change', () => {
      const file = fileInput.files?.[0];
      fileInput.value = '';                    // …so the same file can be re-picked
      if (file) sendPicture(file);
    });
    // The two ways nobody should have to use a file dialog for: pasting a
    // screenshot into the line you are typing, and dropping a photo on the bar.
    els.text.addEventListener('paste', (e) => {
      if (!canSendPictures) return;
      const item = [...(e.clipboardData?.items || [])]
        .find((i) => i.kind === 'file' && i.type.startsWith('image/'));
      const file = item?.getAsFile();
      if (!file) return;
      e.preventDefault();
      sendPicture(file);
    });
    for (const type of ['dragenter', 'dragover']) {
      composer.addEventListener(type, (e) => {
        if (!canSendPictures || !e.dataTransfer?.types?.includes('Files')) return;
        e.preventDefault();
        composer.classList.add('dropping');
      });
    }
    for (const type of ['dragleave', 'drop']) {
      composer.addEventListener(type, () => composer.classList.remove('dropping'));
    }
    composer.addEventListener('drop', (e) => {
      if (!canSendPictures) return;
      const file = [...(e.dataTransfer?.files || [])]
        .find((f) => f.type.startsWith('image/'));
      if (!file) return;
      e.preventDefault();
      sendPicture(file);
    });
  }

  function setCanSendPictures(on) {
    canSendPictures = !!on;
    if (!attachBtn) buildAttach();
    if (attachBtn) attachBtn.hidden = !canSendPictures;
    if (!canSendPictures) clearPicture();
  }

  function clearPicture() {
    picture = null;
    pictureBusy = false;
    if (chip) { chip.hidden = true; chip.classList.remove('busy'); }
    if (attachBtn) attachBtn.classList.remove('armed');
  }

  function showChip(src, busy) {
    if (!chip) return;
    chip.hidden = false;
    chip.classList.toggle('busy', !!busy);
    chip.querySelector('img').src = src;
    attachBtn?.classList.toggle('armed', !busy);
  }

  // The bus may have opened before this file did — the Live2D room imports it
  // behind its avatar load — and a sticky event only replays on subscribe. So
  // read what chat.js already holds, then keep listening for a live swap.
  if (window.WorldChat?.capabilities) setCanSendPictures(
    window.WorldChat.capabilities.image_input);

  /* Up it goes, immediately — one picture at a time, so choosing a second
   * replaces the first rather than queueing behind it. The preview is the local
   * file (an object URL) and not the server's copy: it paints in the same frame
   * the file was chosen, which is what makes the choice feel taken. */
  async function sendPicture(file) {
    if (!canSendPictures) return;
    const mine = ++pictureSeq;               // this choice owns the chip…
    const preview = URL.createObjectURL(file);
    picture = null;
    pictureBusy = true;
    showChip(preview, true);
    const body = new FormData();
    body.append('file', file, file.name || 'picture');
    try {
      const response = await fetch(runtime.apiPath('/api/uploads'),
                                   { method: 'POST', body });
      const data = await response.json().catch(() => ({}));
      if (mine !== pictureSeq) return;       // …until a later one takes it over
      if (!response.ok) throw new Error(data.detail || 'that picture was refused');
      picture = { id: data.id, url: data.url };
      pictureBusy = false;
      showChip(preview, false);
      if (!processing) els.text?.focus();      // …but never steal a locked input
    } catch (e) {
      clearPicture();
      // The caption is hidden in the text room, so a refusal shown only there
      // would be a silent one — the placeholder is the line every room has.
      notice(e.message);
    } finally {
      // The <img> has the bytes; the handle can go.
      setTimeout(() => URL.revokeObjectURL(preview), 10000);
    }
  }

  /** Say something to the person at the composer, briefly, wherever they are. */
  function notice(text, ms = 6000) {
    if (els.caption) els.caption.textContent = text;
    if (!els.text || warming) return;          // a warm owns the placeholder
    const was = composerPlaceholder === null ? els.text.placeholder : composerPlaceholder;
    els.text.placeholder = text;
    setTimeout(() => {
      if (els.text.placeholder === text) els.text.placeholder = was;
    }, ms);
  }

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
      // SSE provides the progressive draft, but the HTTP response is the
      // authoritative completion. Render it too: addMsg deduplicates by id when
      // the live event already arrived, and this saves a reply if a suspended
      // tab's stale EventSource missed the final event.
      if (data.message) window.WorldChat?.receiveMessage?.(data.message);
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
    endReplay();                        // answering her outranks re-reading her
    const text = els.text.value.trim();
    // A picture is a turn on its own — words are optional once one is armed.
    // A picture still going up is not: sending would name an id that does not
    // exist yet, so the line waits for the chip to settle.
    if ((!text && !picture) || warming || pictureBusy) return;
    const sent = picture;
    const clientId = newRequestId();
    window.WorldChat?.addPendingUser?.(text, clientId, sent?.url);
    els.text.value = '';
    clearPicture();
    if (wantsVoice() && ws?.readyState === 1) {
      beginProcessing(clientId, 'voice');
      if (playing) { stopPlayback(); ws.send(JSON.stringify({ type: 'bargein' })); }
      // Through the socket even with a picture on it: only the id rides here,
      // and going around to HTTP would cost her the voice for that one turn.
      ws.send(JSON.stringify({ type: 'text', text, client_id: clientId,
                               image_id: sent?.id }));
    } else {
      beginProcessing(clientId, 'http');
      sendHttp('/api/chat', {
        text, session_id: sessionId, channel: 'web', client_id: clientId,
        image_id: sent?.id,
      }, clientId);
    }
  }

  els.text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendText(); }
  });
  els.send?.addEventListener('click', () => processing ? stopProcessing() : sendText());

  // chat.js draws the per-message "read it out" button; this owns the socket
  // that answers it. A global handle, like WorldChat and WorldControls, for the
  // reason those are: one room runs one voice client, and the three pages that
  // load this file must not each grow their own way of reaching it.
  window.WorldVoice = { speak };

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
