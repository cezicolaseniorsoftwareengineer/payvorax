/**
 * BioCodeTechPay — Service Worker
 * Enables PWA installability (standalone mode, no URL bar).
 * Cache strategy: network-first for API calls, cache-first for static assets.
 * Navigation fallback: redirects to /login when offline or on network error.
 */

const CACHE_NAME = "biocodetechpay-v3";

const STATIC_ASSETS = [
  "/static/css/tailwind.min.css",
  "/static/img/logo.png",
  "/static/manifest.json",
  "/login",
];

// Install: pre-cache static assets and login page for offline fallback
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
    "/ia/",
    "/admin",
  ];
  const isBypass =
    event.request.method !== "GET" ||
    bypassPaths.some((p) => url.pathname.startsWith(p));

  if (isBypass) {
    event.respondWith(
      fetch(event.request).catch(() => {
        // Offline fallback for non-GET or API routes
        if (event.request.mode === "navigate") {
          return (
            caches.match("/login") || new Response("Offline", { status: 503 })
          );
        }
        return new Response("Offline", { status: 503 });
      }),
    );
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

  // Navigation requests (HTML pages): network-first with offline fallback to /login
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => {
        return (
          caches.match("/login") || new Response("Offline", { status: 503 })
        );
      }),
    );
    return;
  }

  // All other GET requests: network-first, no cache
  event.respondWith(fetch(event.request));
});
