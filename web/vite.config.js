import { defineConfig } from 'vite';

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
  },
});
