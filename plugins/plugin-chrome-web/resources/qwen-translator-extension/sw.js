/* Chrome MV3 service worker entry: polyfill `browser` before loading shared scripts. */
if (typeof globalThis.browser === 'undefined') {
  globalThis.browser = globalThis.chrome;
}
importScripts('cache.js', 'background.js');
