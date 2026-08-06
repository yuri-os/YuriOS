import { defineConfig } from 'vite';
import { resolve } from 'node:path';

// The sanctuary frontend (web/index.html + web/js/**). Vite resolves the bare
// `three` / `@pixiv/three-vrm` imports from node_modules and bundles them into
// web/dist, which FastAPI serves at / (yurios/world/main.py). This replaces the
// old no-build importmap + web/vendor/*.min.js: the libraries are now pinned in
// package-lock.json and get security updates through `npm audit` / `npm update`.
//
// Out of scope on purpose: the vendored Live2D client under web/live2d/ is its
// own self-contained app served raw at /live2d/ (its runtime is fetched by
// scripts/fetch_live2d.py), and the runtime assets FastAPI serves directly
// (/models, /live2d, /selfies, /api) — hence publicDir:false, nothing is copied
// into the bundle that the server already owns.
export default defineConfig({
  root: '.',
  base: '/',
  publicDir: false,
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    // keep the bundle debuggable in the wild; drop if you want it opaque
    sourcemap: true,
    rollupOptions: {
      input: {
        sanctuary: resolve(import.meta.dirname, 'index.html'),
        dashboard: resolve(import.meta.dirname, 'dashboard/index.html'),
        // the bodyless client (SPEC §6.7): served at /text/ — same bus, same
        // voice socket, no WebGL. Its own entry so the room's bundle (three.js,
        // three-vrm — about a megabyte) is not on a page that never draws.
        text: resolve(import.meta.dirname, 'text/index.html'),
        // the card studio (SPEC §28): authoring and export, served at /studio/.
        // Selects its character by query parameter, so one entry covers both
        // "create" and "edit" without a route that would shadow the mount.
        studio: resolve(import.meta.dirname, 'studio/index.html'),
        // the mind debug page (SPEC §24.3): the activity timeline, the tick
        // traces, every context window she was given, her hands, and the Vault's
        // own history. Character-scoped by path like the rooms, so
        // shared/runtime.js aims its calls; its own entry because none of the
        // room's rendering belongs on a page that only reads files.
        mind: resolve(import.meta.dirname, 'mind/index.html'),
      },
    },
  },
});
