// Minimal service worker — runtime cache for /static/ + offline fallback.
// Master prompt §27 — PWA + offline mode.
const CACHE = "ice-ops-v1";
const SHELL = ["/", "/manifest.json"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => null));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Never intercept API calls
  if (url.pathname.startsWith("/api/")) return;
  // Static assets — cache first
  if (url.pathname.startsWith("/static/") || /\.(woff2?|css|js|png|svg|ico)$/.test(url.pathname)) {
    e.respondWith(
      caches.open(CACHE).then(async (c) => {
        const cached = await c.match(req);
        if (cached) return cached;
        try {
          const resp = await fetch(req);
          if (resp.ok) c.put(req, resp.clone());
          return resp;
        } catch {
          return cached || Response.error();
        }
      })
    );
  }
});
