/**
 * BioCodeTechPay — Service Worker
 * Enables PWA installability (standalone mode, no URL bar).
 * Cache strategy: network-first for API calls, cache-first for static assets.
 */

const CACHE_NAME = "biocodetechpay-v1";

const STATIC_ASSETS = ["/static/img/logo.png", "/static/manifest.json"];

// Install: pre-cache static assets
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

// Activate: remove outdated caches
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

// Fetch: network-first — never cache authenticated endpoints
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Always bypass cache for API, auth and dynamic routes
  const bypassPaths = [
    "/auth/",
    "/pix/",
    "/boleto/",
    "/cards/",
    "/extrato",
    "/admin",
  ];
  const isBypass =
    event.request.method !== "GET" ||
    bypassPaths.some((p) => url.pathname.startsWith(p));

  if (isBypass) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Static assets: cache-first
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        return (
          cached ||
          fetch(event.request).then((response) => {
            const clone = response.clone();
            caches
              .open(CACHE_NAME)
              .then((cache) => cache.put(event.request, clone));
            return response;
          })
        );
      }),
    );
    return;
  }

  // All other GET requests: network-first, no cache
  event.respondWith(fetch(event.request));
});
