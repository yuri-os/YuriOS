/* First-run model selection (SPEC §11.3.4).
 *
 * A fresh install selects no language model on purpose, and this is where you
 * choose one: the switchboard, the room and the text client all mount it, so
 * whichever page you open first after ./install.sh asks. It covers everything
 * `yurios configure` does in the terminal — a GGUF to download, or a model on
 * LM Studio, Ollama, OpenRouter or another OpenAI-compatible server, with that
 * server's address and key — and then finishes the job: it waits out a GGUF
 * download, restarts the house (§11.3.1) and reloads onto the new boot. A
 * server nobody supervises can't be restarted from here, so the panel says what
 * to do instead.
 *
 * The panel builds its own markup, so a page adds it with two tags (this
 * script and /shared/model-setup.css) and nothing else.
 */
import { detailMessage } from './http.js';

const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** How long a restart may take to answer with a new boot before the panel
 *  stops waiting and points at the logs. */
export const RESTART_WAIT_MS = 180_000;
const POLL_MS = 1500;

const PLACEHOLDERS = {
  gguf: 'Hugging Face repo, e.g. mradermacher/Qwen3-14B-Uncensored-GGUF',
  lmstudio: 'the id LM Studio lists, e.g. qwen/qwen3-14b',
  ollama: 'the model name, e.g. qwen3',
  openrouter: 'vendor/model, e.g. qwen/qwen3-235b-a22b',
  openai_compatible: 'the id the server lists',
  custom: 'any LiteLLM id, e.g. ollama/qwen3',
};

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function call(path, options = {}) {
  const init = { cache: 'no-store', ...options };
  if (init.body !== undefined) {
    init.method ||= 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw Object.assign(new Error(detailMessage(body, `HTTP ${response.status}`)),
                        { status: response.status });
  }
  return body;
}

/** The house's restart state, asked of the house itself rather than through a
 *  character's path; null when this server has no house (a bare runtime). */
async function houseState() {
  try { return await call('/api/house'); } catch { return null; }
}

export function mountModelSetup(setup, { root = document.body,
                                        reload = () => location.reload() } = {}) {
  const status = el('p', { className: 'model-setup-status', role: 'status' });
  const say = (text, tone = '') => {
    status.textContent = text;
    status.dataset.tone = tone;
  };

  const providers = setup.providers || [];
  const select = el('select', { name: 'provider', id: 'model-setup-provider' },
    ...providers.map((p) => el('option', { value: p.id, textContent: p.label })));
  const baseUrl = el('input', { name: 'base_url', id: 'model-setup-url', type: 'url',
    autocomplete: 'off', spellcheck: false });
  const apiKey = el('input', { name: 'api_key', id: 'model-setup-key', type: 'password',
    autocomplete: 'off' });
  const model = el('input', { name: 'model', id: 'model-setup-model', autocomplete: 'off',
    spellcheck: false, required: true });
  const datalist = el('datalist', { id: 'model-setup-models' });
  model.setAttribute('list', datalist.id);
  const listButton = el('button', { type: 'button', className: 'model-setup-secondary',
    textContent: 'List models' });
  const submit = el('button', { type: 'submit', className: 'model-setup-primary',
    textContent: 'Use this model' });

  const field = (label, input, extra) => el('label', { className: 'model-setup-field' },
    el('span', { textContent: label }), extra || input);
  const urlField = field('Server address', baseUrl);
  const keyField = field('API key', apiKey);
  const modelField = field('Model', model,
    el('span', { className: 'model-setup-row' }, model, listButton));

  const form = el('form', { className: 'model-setup-form' },
    field('Connect through', select), urlField, keyField, modelField, datalist, submit);

  const recommendations = el('div', { className: 'model-recommendations' },
    ...(setup.recommendations || []).map((item) => {
      const button = el('button', { type: 'button', className: 'model-recommendation' },
        el('b', { textContent: item.label }),
        el('small', { textContent: `${item.hardware} · ${item.quant} · downloads once` }));
      button.addEventListener('click', () => choose({ model: item.id }));
      return button;
    }));

  const mark = el('span', { className: 'brand-mark' });
  mark.setAttribute('aria-hidden', 'true');
  const panel = el('div', { className: 'model-setup', role: 'dialog' },
    el('div', { className: 'model-setup-card' },
      mark,
      el('p', { className: 'model-setup-eyebrow', textContent: 'First run' }),
      el('h1', { id: 'model-setup-title', textContent: 'Choose her language model' }),
      el('p', { className: 'model-setup-copy', textContent:
        'YuriOS isn’t connected to a language model yet. Pick one and she starts on it — '
        + 'nothing to run in a terminal.' }),
      recommendations,
      el('p', { className: 'model-setup-or', textContent: 'or connect your own' }),
      form, status));
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', 'model-setup-title');

  const current = () => providers.find((p) => p.id === select.value) || {};
  const lists = () => 'prefix' in current();          // a server with a listing

  function showProvider() {
    const p = current();
    urlField.hidden = !('base_url' in p);
    keyField.hidden = !('key_saved' in p);
    listButton.hidden = !lists();
    baseUrl.value = p.base_url || '';
    apiKey.value = '';
    apiKey.placeholder = p.key_saved ? 'saved — leave blank to keep it'
      : (p.id === 'openrouter' ? 'required' : 'only if the server asks for one');
    model.placeholder = PLACEHOLDERS[p.id] || '';
    datalist.replaceChildren();
    say('');
    if (lists()) listModels();
  }

  function connection() {
    return { provider: select.value, model: model.value.trim(),
             base_url: baseUrl.value.trim(), api_key: apiKey.value };
  }

  let listing = 0;
  async function listModels() {
    const asked = ++listing;
    say(`Asking ${current().label}…`);
    let result;
    try {
      result = await call(apiPath('/api/onboarding/models'), { body: connection() });
    } catch (error) {
      result = { models: [], error: error.message };
    }
    if (asked !== listing) return;                    // a newer ask has the floor
    const loaded = result.loaded || [];
    const names = [...loaded, ...(result.models || []).filter((m) => !loaded.includes(m))];
    datalist.replaceChildren(...names.map((name) => el('option', { value: name,
      label: loaded.includes(name) ? 'loaded now' : '' })));
    if (result.error) { say(result.error, 'error'); return; }
    if (!model.value && loaded.length) model.value = loaded[0];
    say(`${current().label} answered — ${names.length} model${names.length === 1 ? '' : 's'} `
      + 'to choose from.');
  }


  async function waitForDownload() {
    for (;;) {
      await pause(POLL_MS);
      const now = await call(apiPath('/api/onboarding'));
      const download = now.download || {};
      if (download.state === 'ready') return;
      if (download.state === 'failed') throw new Error(`Download failed: ${download.detail}`);
      say(`${download.detail} This is a one-time download of several GB.`);
    }
  }

  async function startHer(chosen) {
    const house = await houseState();
    if (!house?.restartable) {
      say(`Saved ${chosen}. ${house?.why_not
        || 'Restart YuriOS to start her on it.'}`, 'warn');
      return;
    }
    say(`Starting her on ${chosen}…`);
    await call('/api/house/restart', { method: 'POST' });
    const started = Date.now();
    // A new boot id, not just an open port: the old server answers for the
    // moment it takes to stop, and reloading into it would show this panel again.
    while (Date.now() - started < RESTART_WAIT_MS) {
      await pause(POLL_MS);
      const now = await houseState();
      if (now?.boot_id && now.boot_id !== house.boot_id) {
        reload();
        return;
      }
    }
    say('YuriOS has not come back after three minutes. `yurios status` and `yurios log` '
      + 'say why.', 'error');
  }

  function busy(on) {
    for (const control of panel.querySelectorAll('button, input, select')) control.disabled = on;
  }

  async function choose(body) {
    busy(true);
    say(body.model?.startsWith('gguf/') || body.provider === 'gguf'
      ? 'Saving…' : 'Checking the connection…');
    try {
      const saved = await call(apiPath('/api/onboarding'), { body });
      if (saved.state === 'downloading') {
        say(`${saved.detail} This is a one-time download of several GB.`);
        await waitForDownload();
      }
      await startHer(saved.model);
    } catch (error) {
      say(error.message || 'Could not save that model', 'error');
      busy(false);
      return;
    }
    // Saved but nothing to restart: the choice is made, so leave it shown.
  }

  select.addEventListener('change', showProvider);
  baseUrl.addEventListener('change', () => { if (lists()) listModels(); });
  listButton.addEventListener('click', listModels);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    choose(connection());
  });

  root.append(panel);
  showProvider();
  return { panel, choose, busy, say, waitForDownload, startHer };
}

async function load() {
  let setup;
  try {
    setup = await call(apiPath('/api/onboarding'));
  } catch {
    return;    // not local, or not up yet — the boot poller already says so
  }
  if (setup.configured) return;
  const mounted = mountModelSetup(setup);
  // Reopened mid-way: a download that is still running, or one that finished
  // while nobody was watching, carries on to the restart it was heading for.
  const download = setup.download || {};
  if (download.state === 'downloading' || download.state === 'ready') {
    mounted.busy(true);
    try {
      if (download.state === 'downloading') await mounted.waitForDownload();
      await mounted.startHer(download.model);
    } catch (error) {
      mounted.say(error.message, 'error');
      mounted.busy(false);
    }
  }
}

if (!window.__YURIOS_MODEL_SETUP_TEST__) load();
