/**
 * Minimal service worker for the CoWorker PWA shell.
 *
 * Goals (Phase 12-4 scope):
 *   - Make the install prompt fire on Android/Chrome (requires a
 *     registered SW + a valid manifest).
 *   - Cache the app shell so the icon-tap launch shows a useful
 *     page even on a slow first-paint.
 *   - Stay out of the way of authenticated API calls — every
 *     /approval, /auth, /mail, /webhooks, /health request goes
 *     straight to network with no caching (auth-sensitive).
 *
 * Cache strategy:
 *   - install: precache /, /icon.svg, /manifest.webmanifest.
 *   - fetch (navigation, ``request.mode === "navigate"``):
 *     network-first, fall back to the cached / on failure so the
 *     SPA shell shows offline.
 *   - fetch (other): network-only, untouched. The hashed Vite
 *     bundles get HTTP-cache headers from the static server;
 *     re-implementing that here would just confuse versioning.
 *
 * Versioning: bumping CACHE_NAME on every release invalidates the
 * precache. Vite hashes bundle filenames so the new index.html
 * references new asset URLs; old bundles drop out of cache
 * naturally on the next prune.
 */
const CACHE_NAME = "coworker-shell-v1";
const SHELL_URLS = ["/", "/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      await cache.addAll(SHELL_URLS);
      // Activate the new SW immediately instead of waiting for the
      // last tab using the old one to close.
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME)
          .map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  if (request.mode === "navigate") {
    event.respondWith(networkThenShell(request));
  }
  // All other requests pass through.
});

async function networkThenShell(request) {
  try {
    const response = await fetch(request);
    return response;
  } catch (_) {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match("/");
    if (cached) return cached;
    return new Response(
      "<h1>Offline</h1><p>The CoWorker shell isn't available yet.</p>",
      { status: 503, headers: { "Content-Type": "text/html" } },
    );
  }
}
