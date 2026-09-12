// Minimal service worker — exists mainly to satisfy browsers that require
// one before offering "Install app". Deliberately does NOT cache API
// responses (orders/inventory change too often to serve stale) — it just
// passes requests through untouched. Static assets (CSS/JS/fonts/icons)
// are cached opportunistically so the app shell loads instantly on repeat
// visits; everything else always goes to the network.
const CACHE_NAME = "orderflow-shell-v1";
const SHELL_PATHS = ["/static/css/style.css", "/static/js/app.js"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_PATHS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isShellAsset = event.request.method === "GET" && SHELL_PATHS.some((p) => url.pathname === p);
  if (!isShellAsset) return; // let the network handle everything else, incl. all /api/* calls

  event.respondWith(
    caches.match(event.request).then((cached) =>
      cached ||
      fetch(event.request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return resp;
      })
    )
  );
});
