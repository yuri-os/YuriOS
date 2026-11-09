/** @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import '../shared/chat-options.js';

const initChatOptions = () => window.ChatOptions.init();

const payload = (thinking = false, model = 'ollama/qwen3') => ({
  effective: { chat_model: model },
  fields: [{ key: 'chat_thinking', value: '', inherited: thinking }],
});
const response = (data, ok = true) => ({ ok, json: async () => data });
const button = () => document.getElementById('chat-thinking');
const model = () => document.getElementById('chat-model');
const announce = (thinking, name = 'ollama/second') => window.dispatchEvent(
  new CustomEvent('world-ev', { detail: { type: 'brain', chat_model: name, chat_thinking: thinking } }));

beforeEach(() => {
  document.body.innerHTML = `<details id="chat-options"><summary>Options</summary>
    <span id="chat-model"></span><button id="chat-thinking" disabled></button>
    <p id="chat-options-note" hidden></p></details>`;
  window.localStorage.clear();
  window.YuriOSRuntime = {
    apiPath: (path) => path.replace('/api/', '/api/characters/yuri/'),
    sessionKey: (key) => `${key}.yuri`,
  };
  vi.stubGlobal('fetch', vi.fn(async () => response(payload())));
});
afterEach(() => {
  document.documentElement.classList.remove('desktop');
  vi.unstubAllGlobals();
  delete window.YuriOSRuntime;
  delete window.WorldChat;
  document.body.innerHTML = '';
});

it('moves the complete composer onto the desktop hover bar without losing its controls', async () => {
  const drawer = document.getElementById('chat-options');
  const composer = document.createElement('div');
  composer.className = 'composer';
  composer.append(drawer);
  const row = document.createElement('div');
  row.className = 'composer-input';
  row.innerHTML = '<button class="attach"></button><input id="text"><button id="send"></button>';
  composer.append(row);
  const bar = document.createElement('footer');
  bar.className = 'controls';
  document.body.append(composer, bar);
  document.documentElement.classList.add('desktop');
  initChatOptions();
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  expect(bar.firstElementChild).toBe(composer);
  expect(composer.querySelector('#chat-thinking')).toBe(button());
  expect(composer.querySelector('.attach').parentElement).toBe(row);
  expect(row.querySelector('#text').nextElementSibling.id).toBe('send');
  initChatOptions();
  button().click();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
});

it('loads the inherited thinking value and full model for this character before entering', async () => {
  fetch.mockResolvedValue(response(payload('false', 'openrouter/vendor/a-very-long-model')));
  initChatOptions();
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  expect(button().getAttribute('aria-pressed')).toBe('false');
  expect(model().textContent).toBe('openrouter/vendor/a-very-long-model');
  expect(model().title).toBe(model().textContent);
  expect(document.getElementById('chat-options').open).toBe(false);
  expect(fetch).toHaveBeenCalledWith('/api/characters/yuri/brain', { cache: 'no-store' });
});

it('waits for a save, disables duplicate presses, and persists the thinking override', async () => {
  initChatOptions();
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  let finish;
  fetch.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  button().click();
  button().click();
  expect(button().disabled).toBe(true);
  expect(button().getAttribute('aria-pressed')).toBe('false');
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ chat_thinking: true });
  expect(fetch.mock.calls[1][0]).toBe('/api/characters/yuri/brain');
  finish(response(payload(true)));
  await vi.waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('true'));
  expect(button().disabled).toBe(false);
});

it('keeps the old value and displays a rejected save', async () => {
  initChatOptions();
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  fetch.mockResolvedValueOnce(response({ detail: 'save refused' }, false));
  button().click();
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  expect(button().getAttribute('aria-pressed')).toBe('false');
  const note = document.getElementById('chat-options-note');
  expect(note.hidden).toBe(false);
  expect(note.textContent).toContain('save refused');
});

it('keeps live events newer than a slow initial read and follows other rooms', async () => {
  let finish;
  fetch.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  initChatOptions();
  announce(true);
  finish(response(payload(false)));
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  expect(model().textContent).toBe('ollama/second');
  expect(button().getAttribute('aria-pressed')).toBe('true');
  announce(false, 'ollama/third');
  expect(model().textContent).toBe('ollama/third');
  expect(button().getAttribute('aria-pressed')).toBe('false');
});

it('remembers an expanded drawer per character', async () => {
  window.localStorage.setItem('yurios.pref.chatOptions.yuri', '1');
  initChatOptions();
  const drawer = document.getElementById('chat-options');
  expect(drawer.open).toBe(true);
  drawer.open = false;
  drawer.dispatchEvent(new Event('toggle'));
  expect(window.localStorage.getItem('yurios.pref.chatOptions.yuri')).toBe('0');
});

it('keeps controls usable when the browser denies storage', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  try {
    initChatOptions();
    await vi.waitFor(() => expect(button().disabled).toBe(false));
    const drawer = document.getElementById('chat-options');
    drawer.open = true;
    drawer.dispatchEvent(new Event('toggle'));
    fetch.mockResolvedValueOnce(response(payload(true)));
    button().click();
    await vi.waitFor(() => expect(button().getAttribute('aria-pressed')).toBe('true'));
  } finally { vi.restoreAllMocks(); }
});

it('allows retrying a failed initial read by reopening options', async () => {
  fetch.mockRejectedValueOnce(new Error('offline'));
  initChatOptions();
  await vi.waitFor(() => expect(model().textContent).toBe('Unavailable'));
  expect(button().disabled).toBe(true);
  const drawer = document.getElementById('chat-options');
  drawer.open = true;
  drawer.dispatchEvent(new Event('toggle'));
  await vi.waitFor(() => expect(button().disabled).toBe(false));
  expect(document.getElementById('chat-options-note').hidden).toBe(true);
});
