import '@testing-library/jest-dom/vitest';

/**
 * jsdom has no canvas, and the console has three components that want one.
 *
 * Left alone, `getContext` raises jsdom's "Not implemented" straight to the
 * virtual console, which vitest surfaces as an unhandled error and fails the
 * whole file -- including tests that were only ever asserting on a network
 * call and happened to render a canvas on the way.
 *
 * Returning `null` is not a fudge: it is exactly what a browser without WebGL2
 * returns, and every component here already reads that as "this display cannot
 * draw" and renders its stated fallback. So the stub exercises the honest path
 * rather than hiding one.
 */
HTMLCanvasElement.prototype.getContext = (() =>
  null) as unknown as typeof HTMLCanvasElement.prototype.getContext;
