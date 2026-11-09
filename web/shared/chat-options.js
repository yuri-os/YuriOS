/* Shared room controls (SPEC §6.7): persistent local layout, live brain settings.
 * The initial read also works before the enter gesture opens the shared bus.
 * After that, sticky `brain` events keep other rooms' edits in sync (§31.4).
 */
(() => {
  function initChatOptions() {
    const drawer = document.getElementById('chat-options');
    if (!drawer || drawer.dataset.wired) return;
    drawer.dataset.wired = 'true';
    // Native pet windows hide the chat column. Move the whole composer so the
    // same options, attachments and input remain reachable on the hover bar.
    if (document.documentElement.classList.contains('desktop')) {
      const composer = drawer.closest('.composer');
      const bar = document.querySelector('.controls');
      if (composer && bar) bar.appendChild(composer);
    }
    const model = document.getElementById('chat-model');
    const thinking = document.getElementById('chat-thinking');
    const note = document.getElementById('chat-options-note');
    const runtime = window.YuriOSRuntime;
    const path = runtime?.apiPath('/api/brain') || '/api/brain';
    const pref = runtime?.sessionKey('yurios.pref.chatOptions') || 'yurios.pref.chatOptions';
    let state = null;
    let busy = false;
    let loading = false;
    let revision = 0;

    try { drawer.open = window.localStorage?.getItem(pref) === '1'; } catch { /* no storage */ }

    function message(text = '') {
      note.textContent = text;
      note.hidden = !text;
    }

    function render() {
      const name = state?.chat_model || 'No model configured';
      model.textContent = name;
      model.title = name;
      thinking.textContent = `Thinking: ${state?.chat_thinking ? 'on' : 'off'}`;
      thinking.setAttribute('aria-pressed', String(Boolean(state?.chat_thinking)));
      thinking.disabled = !state || busy;
      thinking.setAttribute('aria-busy', String(busy));
    }

    function receive(event) {
      if (event?.type !== 'brain' || typeof event.chat_thinking !== 'boolean') return;
      revision += 1;
      state = event;
      message();
      render();
    }

    function fromPayload(data) {
      // Older hosts carry the effective boolean as an override or its inherited
      // default in `fields`. Never use Boolean("false") for that wire value.
      const field = data.fields?.find((f) => f.key === 'chat_thinking');
      const value = data.effective?.chat_thinking ??
        (field?.value === '' || field?.value == null ? field?.inherited : field?.value);
      if (value == null) throw new Error('chat thinking setting unavailable');
      return { type: 'brain', chat_model: data.effective?.chat_model,
        chat_thinking: ['true', '1', 'yes', 'on'].includes(String(value).trim().toLowerCase()) };
    }

    async function responseData(response) {
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      return data;
    }

    async function load() {
      if (loading) return;
      loading = true;
      const at = revision;
      try {
        const data = await responseData(await fetch(path, { cache: 'no-store' }));
        if (at === revision) receive(fromPayload(data));
      } catch (error) {
        if (!state) {
          model.textContent = 'Unavailable';
          model.title = 'Chat settings unavailable';
          thinking.textContent = 'Thinking: unavailable';
          message(`Could not load chat settings: ${error.message}. Reopen options to retry.`);
        }
      } finally { loading = false; }
    }

    drawer.addEventListener('toggle', () => {
      try { window.localStorage?.setItem(pref, drawer.open ? '1' : '0'); } catch { /* no storage */ }
      if (drawer.open && !state) load();
    });
    window.addEventListener('world-ev', (event) => receive(event.detail));
    receive(window.WorldChat?.brain);

    thinking.addEventListener('click', async () => {
      if (!state || busy) return;
      const next = !state.chat_thinking;
      const at = revision;
      busy = true;
      message();
      render();
      try {
        const data = await responseData(await fetch(path, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ chat_thinking: next }),
        }));
        if (at === revision) receive(fromPayload(data));
      } catch (error) {
        message(`Thinking not changed: ${error.message}`);
      } finally {
        busy = false;
        render();
      }
    });
    load();
  }

  window.ChatOptions = { init: initChatOptions };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initChatOptions, { once: true });
  } else {
    initChatOptions();
  }
})();
