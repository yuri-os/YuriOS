/** @vitest-environment jsdom */
import { afterEach, expect, it, vi } from 'vitest';

class FakeWebSocket {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.send = vi.fn();
    FakeWebSocket.instances.push(this);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.WorldChat;
  delete window.WorldControls;
  delete window.YuriOSRuntime;
  FakeWebSocket.instances = [];
  window.localStorage.clear();
  document.body.innerHTML = '';
});

it('renders the authoritative HTTP reply when SSE misses it', async () => {
  document.body.innerHTML = `
    <div class="composer"><details><summary>Options</summary></details>
      <div class="composer-input"><input id="text"><button id="send">send</button></div></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>
  `;
  const assistant = {
    id: 'assistant-1', role: 'assistant', text: 'Returned with the request.',
  };
  const user = {
    id: 'user-1', role: 'user', text: 'Are you there?', client_id: 'client-1',
  };
  const fetchMock = vi.fn(async (url) => {
    if (url === '/api/greeting') {
      return { ok: true, json: async () => ({ session_id: 'session-1' }) };
    }
    if (url === '/api/chat') {
      return {
        ok: true,
        json: async () => ({
          session_id: 'session-1', user_message: user, message: assistant,
        }),
      };
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    wsUrl: (path) => path,
    sessionKey: () => 'test.session',
  };
  window.WorldControls = { isVoiceMuted: () => true };
  window.WorldChat = {
    capabilities: { image_input: true },
    addPendingUser: vi.fn(),
    confirmUser: vi.fn(),
    receiveMessage: vi.fn(),
    failPending: vi.fn(),
    stopPending: vi.fn(),
  };
  const { initVoice } = await import('../js/voice.js');
  const els = {
    text: document.getElementById('text'),
    send: document.getElementById('send'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    status: document.getElementById('status'),
    caption: document.getElementById('caption'),
    latency: document.getElementById('latency'),
  };

  initVoice({ viseme: { context: vi.fn() }, els });
  expect(document.querySelector('.attach').parentElement.className).toBe('composer-input');
  expect(document.querySelector('.pic-chip').parentElement.className).toBe('composer-input');
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/greeting', expect.any(Object)));
  await vi.waitFor(() => expect(els.text.disabled).toBe(false));

  els.text.value = 'Are you there?';
  els.send.click();

  await vi.waitFor(() => expect(window.WorldChat.receiveMessage)
    .toHaveBeenCalledWith(assistant));
  expect(window.WorldChat.confirmUser).toHaveBeenCalledWith(user);
});

it('renders the committed reply carried by voice WebSocket done', async () => {
  document.body.innerHTML = `
    <div class="composer"><input id="text"><button id="send">send</button></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>
  `;
  const assistant = {
    id: 'assistant-voice', role: 'assistant', text: 'Spoken and written.',
  };
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('fetch', vi.fn());
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    wsUrl: (path) => path,
    sessionKey: () => 'test.session',
  };
  window.WorldControls = { isVoiceMuted: () => false };
  window.WorldChat = {
    addPendingUser: vi.fn(),
    confirmUser: vi.fn(),
    receiveMessage: vi.fn(),
    failPending: vi.fn(),
    stopPending: vi.fn(),
  };
  const { initVoice } = await import('../js/voice.js');
  const els = {
    text: document.getElementById('text'),
    send: document.getElementById('send'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    status: document.getElementById('status'),
    caption: document.getElementById('caption'),
    latency: document.getElementById('latency'),
  };

  initVoice({ viseme: { context: vi.fn() }, els });
  const socket = FakeWebSocket.instances[0];
  socket.onmessage({ data: JSON.stringify({
    type: 'processing', client_id: 'voice-client',
  }) });
  socket.onmessage({ data: JSON.stringify({
    type: 'done', client_id: 'voice-client', message: assistant,
  }) });

  expect(window.WorldChat.receiveMessage).toHaveBeenCalledWith(assistant);
});

it('tells the room when the turn itself failed, not just the console', async () => {
  // `.caption` is display:none in the browser room, so a failure written only
  // there is a failure nobody sees. The composer placeholder is the line every
  // room has, and 'no reply' is the honest word: the text reached her, the
  // answer is what did not come back.
  document.body.innerHTML = `
    <div class="composer"><input id="text"><button id="send">send</button></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>
  `;
  const fetchMock = vi.fn(async (url) => {
    if (url === '/api/greeting') {
      return { ok: true, json: async () => ({ session_id: 'session-1' }) };
    }
    if (url === '/api/chat') {
      return { ok: false, status: 502, json: async () => ({
        detail: 'turn failed: the index was opened on another thread',
      }) };
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    wsUrl: (path) => path,
    sessionKey: () => 'test.session',
  };
  window.WorldControls = { isVoiceMuted: () => true };
  window.WorldChat = {
    addPendingUser: vi.fn(),
    confirmUser: vi.fn(),
    receiveMessage: vi.fn(),
    failPending: vi.fn(),
    stopPending: vi.fn(),
  };
  const { initVoice } = await import('../js/voice.js');
  const els = {
    text: document.getElementById('text'),
    send: document.getElementById('send'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    status: document.getElementById('status'),
    caption: document.getElementById('caption'),
    latency: document.getElementById('latency'),
  };

  initVoice({ viseme: { context: vi.fn() }, els });
  await vi.waitFor(() => expect(els.text.disabled).toBe(false));

  els.text.value = 'did the install take?';
  els.send.click();

  await vi.waitFor(() => expect(window.WorldChat.failPending)
    .toHaveBeenCalledWith(expect.any(String), 'no reply', expect.any(String)));
  expect(els.text.placeholder).toContain('turn failed');
});

it('says not received when the request never landed', async () => {
  document.body.innerHTML = `
    <div class="composer"><input id="text"><button id="send">send</button></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>
  `;
  const fetchMock = vi.fn(async (url) => {
    if (url === '/api/greeting') {
      return { ok: true, json: async () => ({ session_id: 'session-1' }) };
    }
    throw new TypeError('Failed to fetch');
  });
  vi.stubGlobal('fetch', fetchMock);
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    wsUrl: (path) => path,
    sessionKey: () => 'test.session',
  };
  window.WorldControls = { isVoiceMuted: () => true };
  window.WorldChat = {
    addPendingUser: vi.fn(),
    confirmUser: vi.fn(),
    receiveMessage: vi.fn(),
    failPending: vi.fn(),
    stopPending: vi.fn(),
  };
  const { initVoice } = await import('../js/voice.js');
  const els = {
    text: document.getElementById('text'),
    send: document.getElementById('send'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    status: document.getElementById('status'),
    caption: document.getElementById('caption'),
    latency: document.getElementById('latency'),
  };

  initVoice({ viseme: { context: vi.fn() }, els });
  await vi.waitFor(() => expect(els.text.disabled).toBe(false));

  els.text.value = 'anyone home?';
  els.send.click();

  await vi.waitFor(() => expect(window.WorldChat.failPending)
    .toHaveBeenCalledWith(expect.any(String), 'not received', expect.any(String)));
});

it('marks the line when a spoken turn comes back with no reply', async () => {
  // The socket is fine; the turn is what failed. Same words as the HTTP path:
  // 'no reply' on the line, the reason in the composer — and not a red status
  // light, which would say the connection broke when it did not.
  document.body.innerHTML = `
    <div class="composer"><input id="text"><button id="send">send</button></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>
  `;
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('fetch', vi.fn());
  window.YuriOSRuntime = {
    apiPath: (path) => path,
    wsUrl: (path) => path,
    sessionKey: () => 'test.session',
  };
  window.WorldControls = { isVoiceMuted: () => false };
  window.WorldChat = {
    addPendingUser: vi.fn(),
    confirmUser: vi.fn(),
    receiveMessage: vi.fn(),
    failPending: vi.fn(),
    stopPending: vi.fn(),
  };
  const { initVoice } = await import('../js/voice.js');
  const els = {
    text: document.getElementById('text'),
    send: document.getElementById('send'),
    mic: document.getElementById('mic'),
    micLabel: document.getElementById('mic-label'),
    status: document.getElementById('status'),
    caption: document.getElementById('caption'),
    latency: document.getElementById('latency'),
  };

  initVoice({ viseme: { context: vi.fn() }, els });
  const socket = FakeWebSocket.instances[0];
  socket.onmessage({ data: JSON.stringify({
    type: 'processing', client_id: 'voice-client',
  }) });
  socket.onmessage({ data: JSON.stringify({
    type: 'error', client_id: 'voice-client',
    message: "she didn't answer — the model refused to answer. Send it again to retry.",
  }) });

  expect(window.WorldChat.failPending).toHaveBeenCalledWith('voice-client', 'no reply');
  expect(els.text.placeholder).toContain('the model refused to answer');
  expect(els.status.textContent).not.toBe('error');
});

it('sends a line that got no reply again through the composer (SPEC §2.6)', async () => {
  document.body.innerHTML = `
    <div class="composer"><details><summary>Options</summary></details>
      <div class="composer-input"><input id="text"><button id="send">send</button></div></div>
    <button id="mic"></button><span id="mic-label"></span>
    <span id="status"></span><span id="caption"></span><span id="latency"></span>
  `;
  const bodies = [];
  let answer;
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    if (url === '/api/greeting') return { ok: true, json: async () => ({ session_id: 'session-1' }) };
    if (url === '/api/chat/resend') {
      bodies.push(JSON.parse(options.body));
      await new Promise((resolve) => { answer = resolve; });
      return { ok: true, json: async () => ({ session_id: 'session-1', message: null }) };
    }
    throw new Error(`unexpected request: ${url}`);
  }));
  window.YuriOSRuntime = { apiPath: (p) => p, wsUrl: (p) => p, sessionKey: () => 'test.session' };
  window.WorldControls = { isVoiceMuted: () => true };
  window.WorldChat = {
    capabilities: {}, addPendingUser: vi.fn(), confirmUser: vi.fn(), receiveMessage: vi.fn(),
    failPending: vi.fn(), stopPending: vi.fn(), beginResend: vi.fn(), setBusy: vi.fn(),
  };
  const { initVoice } = await import('../js/voice.js');
  const els = Object.fromEntries(['text', 'send', 'mic', 'status', 'caption', 'latency']
    .map((id) => [id, document.getElementById(id)]));
  els.micLabel = document.getElementById('mic-label');
  initVoice({ viseme: { context: vi.fn() }, els });
  await vi.waitFor(() => expect(window.WorldVoice.resend('u1', 'hello', undefined)).toBe(true));

  const clientId = window.WorldChat.addPendingUser.mock.calls.at(-1)[1];
  expect(window.WorldChat.beginResend).toHaveBeenCalledWith(clientId, 'u1');
  expect(window.WorldChat.addPendingUser).toHaveBeenLastCalledWith('hello', clientId, undefined);
  await vi.waitFor(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toEqual({ id: 'u1', session_id: 'session-1', channel: 'web', client_id: clientId });
  // a turn like any other: a second press while it runs is refused
  expect(window.WorldVoice.resend('u1', 'hello', undefined)).toBe(false);
  answer();
});
