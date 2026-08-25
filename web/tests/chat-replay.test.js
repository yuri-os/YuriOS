/** @vitest-environment jsdom */
/* The client half of the replay button (SPEC §9.11): chat.js draws it, voice.js
 * owns the socket that answers it, and controls.js holds the mute open for the
 * length of one line. The three seams between them are what these pin. */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

/* chat.js is a classic script shared by all three rooms, not a module, so it is
 * evaluated rather than imported — chat-report.test.js's pattern. Vitest runs
 * from `web/`, and `WorldChat.confirmUser` is `addMsg`: it renders now, where
 * `receiveMessage` would queue behind a backfill this test never runs. */
const CHAT_SOURCE = readFileSync(resolve(process.cwd(), 'js/chat.js'), 'utf8');

class FakeWebSocket {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.send = vi.fn();
    this.close = vi.fn(() => { this.readyState = 3; });
    FakeWebSocket.instances.push(this);
  }

  /** what the server said, as the page's onmessage sees it */
  deliver(payload) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  /** the frames the page sent, decoded */
  sent() {
    return this.send.mock.calls
      .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
      .filter(Boolean);
  }
}

const ROOM = `
  <div id="messages"></div>
  <div class="composer"><input id="text"><button id="send">send</button></div>
  <button id="mic"></button><span id="mic-label"></span>
  <span id="status"></span><span id="caption"></span><span id="latency"></span>
`;

beforeEach(() => {
  document.body.innerHTML = ROOM;
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })));
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    httpPath: (path) => path,
    wsUrl: (path) => path,
    sessionKey: () => 'test.session',
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  delete window.WorldChat;
  delete window.WorldVoice;
  delete window.WorldControls;
  delete window.YuriOSRuntime;
  FakeWebSocket.instances = [];
  window.localStorage.clear();
  document.body.innerHTML = '';
});

/** chat.js binds to #messages when it is evaluated, so every test re-evaluates
 *  it against its own fresh DOM. */
function loadChat() {
  // eslint-disable-next-line no-new-func
  new Function(CHAT_SOURCE)();
  return window.WorldChat;
}

async function loadVoice({ muted = true } = {}) {
  vi.resetModules();
  const release = vi.fn();
  window.WorldControls = {
    isVoiceMuted: () => muted,
    hearThrough: vi.fn(() => release),
  };
  const { initVoice } = await import('../js/voice.js');
  initVoice({
    viseme: { context: () => ({}), analyser: {} },
    els: {
      text: document.getElementById('text'),
      send: document.getElementById('send'),
      mic: document.getElementById('mic'),
      micLabel: document.getElementById('mic-label'),
      status: document.getElementById('status'),
      caption: document.getElementById('caption'),
      latency: document.getElementById('latency'),
    },
  });
  return { release };
}

it('draws "read it out" on her committed lines and on nothing else', async () => {
  const chat = loadChat();
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'There you are.' });
  chat.confirmUser({ id: 'u1', role: 'user', text: 'I am back.' });
  chat.confirmUser({ role: 'assistant', text: 'A line with no id.' });
  chat.addPendingUser('still sending', 'client-1');

  const buttons = [...document.querySelectorAll('.msg-speak')];
  expect(buttons.map((b) => b.dataset.messageId)).toEqual(['a1']);
  // it lives in the header row, beside the stamp — not in the words themselves
  expect(buttons[0].closest('.who')).not.toBeNull();
  expect(document.querySelector('.msg.her').textContent).toContain('There you are.');
});

it('asks the socket by id, never by text, and lights the button it asked for',
  async () => {
    const chat = loadChat();
    await loadVoice({ muted: false });
    chat.confirmUser({ id: 'a1', role: 'assistant', text: 'There you are.' });
    const socket = FakeWebSocket.instances.at(-1);

    document.querySelector('.msg-speak').click();
    const ask = socket.sent().find((m) => m.type === 'speak');
    expect(ask).toEqual({ type: 'speak', message_id: 'a1' });
    // the words are not on the wire — the server resolves them from her transcript
    expect(JSON.stringify(socket.sent())).not.toContain('There you are.');
    expect(document.querySelector('.msg-speak').className).toContain('waiting');

    socket.deliver({ type: 'speaking', message_id: 'a1' });
    expect(document.querySelector('.msg-speak').className).toContain('speaking');

    socket.deliver({ type: 'spoken', message_id: 'a1' });
    expect(document.querySelector('.msg-speak').className).not.toContain('speaking');
    expect(document.querySelector('.msg-speak').title).toBe('read it out');
  });

it('holds the mute open for the length of one line, and never moves the switch',
  async () => {
    const chat = loadChat();
    const { release } = await loadVoice({ muted: true });
    chat.confirmUser({ id: 'a1', role: 'assistant', text: 'There you are.' });

    document.querySelector('.msg-speak').click();
    // muted is "not by default", not "no": the socket opens for this one line
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(window.WorldControls.hearThrough).toHaveBeenCalledTimes(1);
    expect(release).not.toHaveBeenCalled();

    const socket = FakeWebSocket.instances.at(-1);
    socket.deliver({ type: 'ready', tts: 'kokoro' });     // her voice landed
    expect(socket.sent().some((m) => m.type === 'speak')).toBe(true);

    socket.deliver({ type: 'speaking', message_id: 'a1' });
    socket.deliver({ type: 'spoken', message_id: 'a1' });
    expect(release).toHaveBeenCalledTimes(1);             // …and the gain goes back
    expect(socket.close).toHaveBeenCalled();              // nothing else wanted it (§9.9)
  });

it('says why a refused line did not happen, and puts the button out', async () => {
  const chat = loadChat();
  await loadVoice({ muted: false });
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'There you are.' });
  const socket = FakeWebSocket.instances.at(-1);

  document.querySelector('.msg-speak').click();
  socket.deliver({ type: 'spoken', message_id: 'a1',
                  message: 'she’s still talking' });
  expect(document.getElementById('caption').textContent)
    .toBe('she’s still talking');
  expect(document.querySelector('.msg-speak').className).not.toContain('waiting');
});

it('switches to the line you pressed instead, and ignores the old one ending',
  async () => {
    const chat = loadChat();
    await loadVoice({ muted: false });
    chat.confirmUser({ id: 'a1', role: 'assistant', text: 'First line.' });
    chat.confirmUser({ id: 'a2', role: 'assistant', text: 'Second line.' });
    const socket = FakeWebSocket.instances.at(-1);
    const buttons = [...document.querySelectorAll('.msg-speak')];

    buttons[0].click();
    socket.deliver({ type: 'speaking', message_id: 'a1' });
    buttons[1].click();
    expect(buttons[0].className).not.toContain('speaking');
    expect(buttons[1].className).toContain('waiting');

    // the first line's closing frame is still in flight; it must not put out
    // the button that belongs to the line now being read
    socket.deliver({ type: 'spoken', message_id: 'a1' });
    expect(buttons[1].className).toContain('waiting');
    socket.deliver({ type: 'speaking', message_id: 'a2' });
    expect(buttons[1].className).toContain('speaking');
  });

it('gives the floor back the moment you type to her', async () => {
  const chat = loadChat();
  await loadVoice({ muted: false });
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'There you are.' });
  const socket = FakeWebSocket.instances.at(-1);
  socket.deliver({ type: 'ready', tts: 'kokoro' });    // the composer is open

  document.querySelector('.msg-speak').click();
  socket.deliver({ type: 'speaking', message_id: 'a1' });

  const input = document.getElementById('text');
  input.value = 'sorry, go on';
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

  expect(document.querySelector('.msg-speak').className).not.toContain('speaking');
  expect(socket.sent().some((m) => m.type === 'text')).toBe(true);
});

it('survives a muted room closing the socket between two presses', async () => {
  const chat = loadChat();
  const { release } = await loadVoice({ muted: true });
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'First line.' });
  chat.confirmUser({ id: 'a2', role: 'assistant', text: 'Second line.' });
  const buttons = [...document.querySelectorAll('.msg-speak')];

  buttons[0].click();                                  // the socket opens for it
  const first = FakeWebSocket.instances.at(-1);
  first.deliver({ type: 'ready', tts: 'kokoro' });
  first.deliver({ type: 'speaking', message_id: 'a1' });
  first.deliver({ type: 'spoken', message_id: 'a1' }); // …and closes again (§9.9)
  expect(first.close).toHaveBeenCalled();
  expect(release).toHaveBeenCalledTimes(1);

  buttons[1].click();                                  // a second line: a second socket
  expect(FakeWebSocket.instances).toHaveLength(2);
  expect(buttons[1].className).toContain('waiting');

  // the first socket's close event only lands now — it must not put out the
  // button that belongs to the line the *new* socket is opening for
  first.onclose?.({ code: 1000, reason: '' });
  expect(buttons[1].className).toContain('waiting');
  expect(release).toHaveBeenCalledTimes(1);
});

it('stops mid-line when the mute switch is pressed', async () => {
  const chat = loadChat();
  const { release } = await loadVoice({ muted: false });
  chat.confirmUser({ id: 'a1', role: 'assistant', text: 'There you are.' });
  const socket = FakeWebSocket.instances.at(-1);
  socket.deliver({ type: 'ready', tts: 'kokoro' });

  document.querySelector('.msg-speak').click();
  socket.deliver({ type: 'speaking', message_id: 'a1' });
  expect(document.querySelector('.msg-speak').className).toContain('speaking');

  // the switch outranks the hold a replay keeps on the gain (§9.10)
  window.dispatchEvent(new CustomEvent('voice-mute-change',
                                       { detail: { muted: true } }));
  expect(document.querySelector('.msg-speak').className).not.toContain('speaking');
  expect(release).toHaveBeenCalledTimes(1);
});
