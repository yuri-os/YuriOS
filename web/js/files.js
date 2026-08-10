/* The files tab is a small local terminal dressed as an OS volume browser, not
 * a second Vault debugger. It can write only workspace scratch; research
 * sources stay intact and can be copied to the desk before changing them. */
(() => {
  const runtimeReady = window.YuriOSRuntime
    ? Promise.resolve()
    : import('/shared/runtime.js').catch(() => {});
  const apiPath = (path) => window.YuriOSRuntime?.apiPath(path) || path;
  const panel = document.getElementById('files');
  if (!panel) return;

  // The two mounted volumes. `id` is what appears in fs:// paths, `kind` is
  // the API namespace the volume's files live under.
  const VOLUMES = [
    { id: 'workspace', kind: 'workspace', mode: 'rw', desc: 'scratch · editable' },
    { id: 'shelf', kind: 'research', mode: 'ro', desc: 'sources · preserved' },
  ];

  const state = {
    workspace: [], research: [],
    active: null, dirty: false, loading: false,
    cwd: [], // fs path segments, e.g. ['workspace', 'research']; [] is the mount table
  };
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

  // ------------------------------------------------------------------ icons
  // Stroked currentColor SVG, per house style: glyph fonts are tofu on Linux.
  const ICON = {
    folder: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5h4l1.8 2H14v7H2z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>',
    file: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2h5.5L13 5.5V14H4z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9.5 2v3.5H13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>',
    up: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V4M3.5 8.5L8 4l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    drive: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="4" width="12" height="8" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M11 8.5h1.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
  };

  // -------------------------------------------------------------- navigation
  function volume() {
    return VOLUMES.find(v => v.id === state.cwd[0]) || null;
  }

  function filesOf(kind) {
    return kind === 'workspace' ? state.workspace : state.research;
  }

  // Folders first, then files, inside the current directory. Folders are
  // derived from path prefixes so a dir the server never listed still opens.
  function children() {
    const vol = volume();
    if (!vol) return { dirs: [], docs: [] };
    const prefix = state.cwd.length > 1 ? state.cwd.slice(1).join('/') + '/' : '';
    const dirs = new Set();
    const docs = [];
    for (const file of filesOf(vol.kind)) {
      const path = vol.kind === 'workspace' ? file.path : file.name;
      if (!path.startsWith(prefix)) continue;
      const rest = path.slice(prefix.length);
      const slash = rest.indexOf('/');
      if (slash === -1) {
        if (file.dir) dirs.add(rest); else docs.push(file);
      } else {
        dirs.add(rest.slice(0, slash));
      }
    }
    docs.sort((a, b) => {
      const an = vol.kind === 'workspace' ? a.path : a.name;
      const bn = vol.kind === 'workspace' ? b.path : b.name;
      return an.localeCompare(bn);
    });
    return { dirs: [...dirs].sort(), docs };
  }

  // ------------------------------------------------------------------ render
  function addressBar() {
    const parts = ['<button class="fs-seg fs-root-seg" data-depth="-1">fs://</button>'];
    state.cwd.forEach((seg, i) => {
      parts.push('<span class="fs-slash">/</span>');
      parts.push(`<button class="fs-seg" data-depth="${i}">${esc(seg)}</button>`);
    });
    return `<div class="fs-addr"><span class="fs-prompt" aria-hidden="true"></span>${parts.join('')}</div>`;
  }

  function row(icon, name, meta, attrs, cls = '') {
    return `<button class="fs-row${cls}"${attrs}>` +
      `<span class="fs-icon">${icon}</span>` +
      `<span class="fs-name">${esc(name)}</span>${meta}</button>`;
  }

  function listing() {
    const vol = volume();
    if (!vol) {
      // Mount table: the root of the virtual file system.
      const mounts = VOLUMES.map(v => {
        const count = filesOf(v.kind).filter(f => !f.dir).length;
        return `<button class="fs-vol" data-vol="${v.id}">` +
          `<span class="fs-icon">${ICON.drive}</span>` +
          `<span class="fs-vol-name">${esc(v.id)}<small>${esc(v.desc)}</small></span>` +
          `<span class="fs-vol-meta"><em>${v.mode}</em>${count} obj</span></button>`;
      }).join('');
      return `<div class="fs-list">${mounts}</div>` +
        `<div class="fs-status">${VOLUMES.length} volumes mounted</div>`;
    }
    const { dirs, docs } = children();
    const rows = [];
    if (state.cwd.length) {
      rows.push(row(ICON.up, '..', '<small></small>', ' data-up="1"'));
    }
    for (const dir of dirs) {
      rows.push(row(ICON.folder, dir, '<small>dir</small>',
        ` data-dir="${esc(dir)}"`, ' folder'));
    }
    for (const doc of docs) {
      const path = vol.kind === 'workspace' ? doc.path : doc.name;
      const name = path.slice(path.lastIndexOf('/') + 1);
      const active = state.active?.kind === vol.kind && state.active?.path === path;
      rows.push(row(ICON.file, name,
        `<small>${bytes(doc.bytes)}</small><small class="fs-mtime">${stamp(doc.mtime)}</small>`,
        ` data-path="${esc(path)}"${active ? ' data-on="1"' : ''}`));
    }
    const body = rows.length
      ? rows.join('')
      : '<p class="fs-empty">empty directory</p>';
    const total = docs.reduce((sum, doc) => sum + (doc.bytes || 0), 0);
    return `<div class="fs-list">${body}</div>` +
      `<div class="fs-status">${dirs.length + docs.length} objects · ${bytes(total)} · ${vol.mode}</div>`;
  }

  function browser() {
    return `<section class="fs-os" aria-label="file system">` +
      `<header class="fs-titlebar"><span class="fs-dots"><i></i><i></i><i></i></span>` +
      `<span class="fs-title">yurios/files</span></header>` +
      `${addressBar()}${listing()}</section>`;
  }

  function detail() {
    const active = state.active;
    if (!active) {
      return `<section class="fs-detail fs-idle"><span class="fs-cursor">_</span>` +
        '<h2>select a document</h2><p>mount a volume, open a folder, pick a file.</p></section>';
    }
    if (state.loading) {
      return `<section class="fs-detail fs-idle"><span class="fs-cursor">_</span>` +
        `<h2>opening ${esc(active.path)}</h2><p>reading the local file system…</p></section>`;
    }
    const meta = `${bytes(active.bytes)}${active.mtime ? ` · ${stamp(active.mtime)}` : ''}`;
    const vol = VOLUMES.find(v => v.kind === active.kind);
    const fsPath = `fs://${vol ? vol.id : active.kind}/${active.path}`;
    const mode = active.kind === 'workspace' ? 'workspace / editable' : 'research / source file';
    const action = active.kind === 'workspace'
      ? `<button class="fs-save"${state.dirty ? '' : ' disabled'}>save file</button>`
      : '<button class="fs-fork">edit a working copy</button>';
    const body = active.kind === 'workspace'
      ? `<textarea class="fs-editor" spellcheck="false" aria-label="${esc(active.path)}">${esc(active.text)}</textarea>`
      : `<pre class="fs-source">${esc(active.text)}</pre>`;
    return `<section class="fs-detail"><header class="fs-file-head">` +
      `<div><p class="fs-breadcrumb">${mode} · ${esc(fsPath)}</p><h2>${esc(active.path)}</h2>` +
      `<small>${meta}</small></div>${action}</header>${body}` +
      `<footer class="fs-foot">${active.kind === 'workspace'
        ? `<span class="fs-write-state">${state.dirty ? 'unsaved changes' : 'saved to local workspace'}</span>`
        : '<span>source files stay original · make a working copy to edit</span>'}</footer></section>`;
  }

  function render() {
    panel.innerHTML = `<div class="fs-shell">${browser()}${detail()}</div>`;
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
    const from = filesOf(kind).find(file =>
      (kind === 'workspace' ? file.path : file.name) === path);
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
      state.cwd = ['workspace', 'research'];
      openFile('workspace', path);
    } catch (error) {
      panel.querySelector('.fs-foot')?.insertAdjacentHTML('beforeend',
        `<span class="fs-error">${esc(error.message || 'could not make a copy')}</span>`);
    }
  }

  panel.addEventListener('click', (event) => {
    const volButton = event.target.closest('.fs-vol');
    if (volButton) {
      state.cwd = [volButton.dataset.vol];
      render();
      return;
    }
    const seg = event.target.closest('.fs-seg');
    if (seg) {
      const depth = Number(seg.dataset.depth);
      state.cwd = depth < 0 ? [] : state.cwd.slice(0, depth + 1);
      render();
      return;
    }
    const up = event.target.closest('.fs-row[data-up]');
    if (up) {
      state.cwd = state.cwd.slice(0, -1);
      render();
      return;
    }
    const dir = event.target.closest('.fs-row[data-dir]');
    if (dir) {
      state.cwd = [...state.cwd, dir.dataset.dir];
      render();
      return;
    }
    const file = event.target.closest('.fs-row[data-path]');
    if (file) {
      const vol = volume();
      if (vol) openFile(vol.kind, file.dataset.path);
      return;
    }
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
