// Self-destructing service worker.
// Older builds shipped a cache-first SW that kept serving stale bundles after
// auth + UI changes (users were still seeing the old Google sign-in button
// even though it was deleted from the codebase). This version unregisters
// itself and clears every cache it ever owned, so the next page load fetches
// fresh HTML/JS/CSS from the network.
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.registration.unregister();
    const clientList = await self.clients.matchAll({ type: "window" });
    clientList.forEach((c) => c.navigate(c.url));
  })());
});

// Never intercept any request — pass everything through to the network.
self.addEventListener("fetch", () => {});
