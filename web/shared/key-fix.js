/* A key, taken where the listing that wanted it was refused (SPEC §31.1).
 *
 * A model picker's browse asks a server what it serves; one with "Require API
 * key" on answers 401, and the host says so with a `key` object beside the
 * error: which server, whether a key was sent and refused, and where a key for
 * this listing goes. Before this the answer was a sentence pointing at a
 * setting on another page, which is a dead end in a room. Now the picker that
 * was refused takes the key itself:
 *
 * - `scope: "profile"` — a character's listing. The key goes to her connection
 *   profile through `save`, either one already on this node (picked by name;
 *   values never leave the host) or a new one, written to the host's `.env`
 *   under the profile's own name. The answer carries the listing asked again,
 *   so the picker fills in the same step.
 * - `scope: "house"` — the house's own listing. The key is the house line
 *   (`field`), saved like any other `.env` field, and the browse runs again.
 *
 * Classic IIFE, like settings.js: served raw at /shared/ to the rooms, the
 * settings page and the studio alike. `render(key, opts)` returns the node;
 * the caller places it and decides what a fresh listing means for its list.
 */
(() => {
  if (window.YuriOSKeyFix) return;

  const NEW_KEY = '__new__';
  const HOUSE_KEY = '__house__';

  const el = (tag, props = {}, ...kids) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...kids);
    return node;
  };

  async function answer(response) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${response.status}`);
    }
    return data;
  }

  function whereItGoes(key) {
    if (key.scope === 'house') {
      return `Saved to this machine's .env as ${key.field}, the house's key for ${key.label}.`
        + ' Characters already running pick it up when they restart.';
    }
    const others = key.shared_with || [];
    const sharing = !others.length ? ''
      : ` — ${others.join(', ')} ${others.length === 1 ? 'uses' : 'use'} it too`;
    return `Given to her “${key.profile}” connection${sharing}.`;
  }

  /**
   * @param {object} key  the listing's `key` object
   * @param {object} opts
   *   onListing(listing)  profile scope: the listing asked again with the key
   *   retry()             house scope: browse again once the key is saved
   *   apiPath(path)       how this page reaches the house's own routes
   *   inputClass, buttonClass  the page's own control styles
   */
  function render(key, opts = {}) {
    const inputClass = opts.inputClass || '';
    const buttonClass = opts.buttonClass || '';
    const apiPath = opts.apiPath || ((path) => path);
    const box = el('div', { className: 'key-fix' });
    const names = key.scope === 'profile' ? (key.key_envs || []) : [];
    // on a house server's own address, the house's key is a choice too: what a
    // profile there with no key of its own is sent (SPEC §31.2)
    const house = key.scope === 'profile' && key.house_key && key.current ? key.house_key : '';

    const source = el('select', { className: `${inputClass} key-fix-source`,
      ariaLabel: `${key.label} API key` });
    if (house) {
      source.append(el('option', { value: HOUSE_KEY,
        textContent: `use ${house}, the house's key${key.house_key_set ? '' : ' (not set)'}` }));
    }
    for (const name of names) {
      const refused = key.refused && name === key.current;
      source.append(el('option', { value: name,
        textContent: `use ${name}${refused ? ' (refused)' : ''}` }));
    }
    source.append(el('option', { value: NEW_KEY, textContent: 'a new key…' }));
    // a key already here is the likelier fix — unless it is the one refused
    const first = names.find((name) => !(key.refused && name === key.current));
    source.value = (house && key.house_key_set ? HOUSE_KEY : '') || first || NEW_KEY;
    source.hidden = !names.length && !house;

    const typed = el('input', { className: `${inputClass} key-fix-key`, type: 'password',
      autocomplete: 'off', spellcheck: false,
      placeholder: `paste ${key.label}'s API key` });
    const save = el('button', { type: 'button', className: `${buttonClass} key-fix-save`,
      textContent: 'use key' });
    const note = el('span', { className: 'key-fix-note', textContent: whereItGoes(key) });
    const error = el('span', { className: 'key-fix-error', role: 'alert' });

    const showTyped = () => { typed.hidden = source.value !== NEW_KEY; };
    source.addEventListener('change', () => { showTyped(); if (!typed.hidden) typed.focus(); });
    showTyped();

    async function submit() {
      error.textContent = '';
      const fresh = source.value === NEW_KEY;
      const value = typed.value.trim();
      if (fresh && !value) { error.textContent = `Paste ${key.label}'s API key first.`; return; }
      save.disabled = true;
      save.textContent = 'saving…';
      try {
        if (key.scope === 'house') {
          await answer(await fetch(apiPath('/api/settings'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key.field]: value }),
          }));
          typed.value = '';
          opts.retry?.();
          return;
        }
        const body = fresh ? { api_key: value }
          : source.value === HOUSE_KEY ? { house_key: true } : { api_key_env: source.value };
        const data = await answer(await fetch(key.save, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...body, provider: key.provider }),
        }));
        typed.value = '';
        if (data.warning) error.textContent = data.warning;
        opts.onListing?.(data.listing || { models: [] }, data.warning || '');
      } catch (err) {
        error.textContent = String(err.message || err);
      } finally {
        save.disabled = false;
        save.textContent = 'use key';
      }
    }

    save.addEventListener('click', submit);
    // the picker sits inside a bigger form: Enter here saves the key, not that
    typed.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); submit(); }
    });

    box.append(el('div', { className: 'key-fix-row' }, source, typed, save), note, error);
    return box;
  }

  window.YuriOSKeyFix = Object.freeze({ render, NEW_KEY });
})();
