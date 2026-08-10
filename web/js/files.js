/* The files tab is a small local terminal, not a second Vault debugger. It can
 * write only workspace scratch; research sources stay intact and can be copied
 * to the desk before changing them. */
(() => {
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
  const panel = document.getElementById('files');
  if (!panel) return;

  const state = { workspace: [], research: [], active: null, dirty: false, loading: false };
  let serial = 0;

  function esc(value) {
    const node = document.createElement('div');
    node.textContent = value ?? '';
    return node.innerHTML;
  }

  function bytes(value = 0) {
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function stamp(value) {
    if (!value) return '';
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  async function request(path, init) {
    await runtimeReady;
    const response = await fetch(apiPath(path), init);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function fileRow(kind, file) {
    const path = kind === 'workspace' ? file.path : file.name;
    const active = state.active?.kind === kind && state.active?.path === path;
    const folder = Boolean(file.dir);
    return `<button class="fs-entry${active ? ' on' : ''}${folder ? ' folder' : ''}" ` +
      `data-kind="${kind}" data-path="${esc(path)}"${folder ? ' disabled' : ''}>` +
      `<span class="fs-glyph">${folder ? '>' : '·'}</span>` +
      `<span class="fs-entry-path">${esc(path)}</span>` +
      (folder ? '' : `<small>${bytes(file.bytes)}</small>`) + '</button>';
  }

  function tree() {
    const workspace = state.workspace.length
      ? state.workspace.map(file => fileRow('workspace', file)).join('')
      : '<p class="fs-empty">no notes on the desk</p>';
    const research = state.research.length
      ? state.research.map(file => fileRow('research', file)).join('')
      : '<p class="fs-empty">the shelf is quiet</p>';
    return `<aside class="fs-tree" aria-label="documents">` +
      `<div class="fs-root"><p><i></i> workspace <span>${state.workspace.filter(f => !f.dir).length}</span></p>` +
      `<small>scratch · editable</small>${workspace}</div>` +
      `<div class="fs-root"><p><i></i> research shelf <span>${state.research.length}</span></p>` +
      `<small>sources · preserved</small>${research}</div></aside>`;
  }

  function detail() {
    const active = state.active;
    if (!active) {
      return `<section class="fs-detail fs-idle"><span class="fs-cursor">_</span>` +
        '<h2>select a document</h2><p>your working desk and the research shelf are live here.</p></section>';
    }
    if (state.loading) {
      return `<section class="fs-detail fs-idle"><span class="fs-cursor">_</span>` +
        `<h2>opening ${esc(active.path)}</h2><p>reading the local file system…</p></section>`;
    }
    const meta = `${bytes(active.bytes)}${active.mtime ? ` · ${stamp(active.mtime)}` : ''}`;
    const mode = active.kind === 'workspace' ? 'workspace / editable' : 'research / source file';
    const action = active.kind === 'workspace'
      ? `<button class="fs-save"${state.dirty ? '' : ' disabled'}>save file</button>`
      : '<button class="fs-fork">edit a working copy</button>';
    const body = active.kind === 'workspace'
      ? `<textarea class="fs-editor" spellcheck="false" aria-label="${esc(active.path)}">${esc(active.text)}</textarea>`
      : `<pre class="fs-source">${esc(active.text)}</pre>`;
    return `<section class="fs-detail"><header class="fs-file-head">` +
      `<div><p class="fs-breadcrumb">${mode}</p><h2>${esc(active.path)}</h2>` +
      `<small>${meta}</small></div>${action}</header>${body}` +
      `<footer class="fs-foot">${active.kind === 'workspace'
        ? `<span class="fs-write-state">${state.dirty ? 'unsaved changes' : 'saved to local workspace'}</span>`
        : '<span>source files stay original · make a working copy to edit</span>'}</footer></section>`;
  }

  function render() {
    panel.innerHTML = `<div class="fs-shell">${tree()}${detail()}</div>`;
  }

  async function refresh({ reload = false } = {}) {
    // Do not let an SSE refresh erase text the person is still composing.
    if (state.dirty) return;
    try {
      const [workspace, research] = await Promise.all([
        request('/api/mind/workspace'), request('/api/mind/research'),
      ]);
      state.workspace = workspace.files || [];
      state.research = research.files || [];
      render();
      if (reload && state.active && !state.dirty) openFile(state.active.kind, state.active.path);
    } catch (error) {
      panel.innerHTML = `<div class="fs-shell"><section class="fs-detail fs-idle">` +
        `<span class="fs-cursor">_</span><h2>files unavailable</h2>` +
        `<p>${esc(error.message || 'the local service did not answer')}</p></section></div>`;
    }
  }

  async function openFile(kind, path) {
    const token = ++serial;
    const from = kind === 'workspace'
      ? state.workspace.find(file => file.path === path)
      : state.research.find(file => file.name === path);
    if (!from || from.dir) return;
    state.active = { kind, path, text: '', bytes: from.bytes, mtime: from.mtime };
    state.dirty = false;
    state.loading = true;
    render();
    try {
      const query = kind === 'workspace' ? `path=${encodeURIComponent(path)}` :
        `name=${encodeURIComponent(path)}`;
      const data = await request(`/api/mind/${kind}/file?${query}`);
      if (token !== serial) return;
      state.active.text = data.text;
      state.loading = false;
      render();
    } catch (error) {
      if (token !== serial) return;
      state.loading = false;
      state.active = null;
      render();
      panel.querySelector('.fs-idle')?.insertAdjacentHTML('beforeend',
        `<p>${esc(error.message || 'could not open that file')}</p>`);
    }
  }

  async function save() {
    const editor = panel.querySelector('.fs-editor');
    if (!state.active || !editor || !state.dirty) return;
    const button = panel.querySelector('.fs-save');
    button.disabled = true;
    button.textContent = 'saving…';
    try {
      const data = await request('/api/mind/workspace/file', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: state.active.path, text: editor.value }),
      });
      state.active.text = editor.value;
      state.active.bytes = data.file.bytes;
      state.active.mtime = data.file.mtime;
      state.dirty = false;
      render();
      refresh();
    } catch (error) {
      button.disabled = false;
      button.textContent = 'save failed';
    }
  }

  async function forkResearch() {
    if (!state.active || state.active.kind !== 'research') return;
    const path = `research/${state.active.path}`;
    if (state.workspace.some(file => file.path === path) &&
        !window.confirm(`${path} already exists. Replace it?`)) return;
    try {
      await request('/api/mind/workspace/file', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, text: state.active.text }),
      });
      await refresh();
      openFile('workspace', path);
    } catch (error) {
      panel.querySelector('.fs-foot')?.insertAdjacentHTML('beforeend',
        `<span class="fs-error">${esc(error.message || 'could not make a copy')}</span>`);
    }
  }

  panel.addEventListener('click', (event) => {
    const entry = event.target.closest('.fs-entry:not([disabled])');
    if (entry) openFile(entry.dataset.kind, entry.dataset.path);
    if (event.target.closest('.fs-save')) save();
    if (event.target.closest('.fs-fork')) forkResearch();
  });

  panel.addEventListener('input', (event) => {
    if (!event.target.matches('.fs-editor')) return;
    state.dirty = event.target.value !== state.active?.text;
    const save = panel.querySelector('.fs-save');
    const note = panel.querySelector('.fs-write-state');
    if (save) save.disabled = !state.dirty;
    if (note) note.textContent = state.dirty ? 'unsaved changes' : 'saved to local workspace';
  });

  window.addEventListener('files-open', () => refresh({ reload: true }));
  window.addEventListener('files-refresh', () => refresh({ reload: true }));
  window.addEventListener('world-ev', (event) => {
    if (!panel.hidden && event.detail?.type === 'research_status') refresh();
  });
})();
