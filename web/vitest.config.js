import { defineConfig } from 'vitest/config';

/* The frontend's tests (`npm test`, or ./scripts/check.sh from the repo root).
 *
 * Its own file rather than a `test` block inside vite.config.js: that file is the
 * BUILD contract — the four rollup entries FastAPI serves out of web/dist — and it
 * is read by people asking what ships. Nothing here changes what ships.
 *
 * Scope, deliberately narrow: the modules that decide something. The room itself
 * (js/stage/**) is three.js talking to a GPU, and a test that mocks WebGL asserts
 * only that the mock was called; the things worth pinning are the pure ones the
 * room and the dashboard both lean on — the state ladder's vocabulary, the shapes
 * the API's rows are normalised into, the gauge thresholds, the quality tier.
 *
 * `node` is the default environment because most of that touches no DOM at all.
 * The two files that do say so themselves, in a `@vitest-environment jsdom`
 * docblock at the top — a per-file cost, paid only by the files that need it.
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.js'],
    // The room's runtime assets and the vendored Live2D client are not ours to
    // walk; dist/ is build output. None of them hold tests.
    exclude: ['node_modules/**', 'dist/**', 'live2d/**', 'models/**'],
  },
});
