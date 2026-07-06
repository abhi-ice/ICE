/* ICE Site Ops — offline service worker (v7 guided Daily Report).
 *
 * HISTORY / WHY THIS IS SAFE
 * --------------------------
 * The previous SW was cache-first for EVERYTHING, so after every deploy it
 * kept serving the OLD index.html (and thus the old JS bundle) until the
 * cache was manually nuked — users saw a deleted Google sign-in button long
 * after it was removed. It was ripped out and replaced with a self-destruct
 * SW. This rewrite brings the SW back for offline support WITHOUT that bug,
 * by making the document (HTML) NETWORK-FIRST:
 *
 *   1. Navigation / HTML  -> NETWORK-FIRST with a ~3s timeout, cache fallback.
 *        We ALWAYS try the network for the app shell first, so a fresh deploy
 *        is picked up immediately. The cached copy is only used when the
 *        network is unreachable or too slow (offline / dead site signal).
 *        This is the specific fix for the stale-bundle failure.
 *
 *   2. Hashed static assets (/static/*) -> CACHE-FIRST, cache forever.
 *        Create React App content-hashes these filenames (main.abc123.js),
 *        so a given URL's bytes never change. A new deploy ships NEW
 *        filenames referenced by the freshly-fetched index.html, so there is
 *        no staleness risk. Caching them forever is what makes the app boot
 *        offline.
 *
 *   3. /api/*  -> NEVER handled here. The app layer (offlineCache.js +
 *        drOfflineSync.js + drPhotoQueue.js + drVoiceQueue.js) owns data
 *        caching and mutation queueing. The SW must not shadow authenticated,
 *        tenant-scoped API traffic.
 *
 * Cache names are VERSIONED; bumping CACHE_VERSION on a breaking SW change
 * makes activate() delete every older ICE cache. skipWaiting + clients.claim
 * mean a new SW takes over as soon as it installs.
 */

const CACHE_VERSION = "v1";
const SHELL_CACHE = `ice-shell-${CACHE_VERSION}`;
const STATIC_CACHE = `ice-static-${CACHE_VERSION}`;
const OUR_CACHES = [SHELL_CACHE, STATIC_CACHE];

// Old self-destruct/cache-first builds and any prior ICE caches we want gone.
const LEGACY_CACHE_PREFIXES = ["ice-shell-", "ice-static-", "workbox-", "ice-"];

const NAV_TIMEOUT_MS = 3000;

self.addEventListener("install", (event) => {
  // Pre-cache the app shell so the very first offline boot has an index.html.
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(["/", "/index.html"]).catch(() => {}))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.map((k) => {
          if (OUR_CACHES.includes(k)) return Promise.resolve();
          // Delete any prior ICE cache (including the old cache-first names
          // and workbox caches) so stale bundles can never resurface.
          if (LEGACY_CACHE_PREFIXES.some((p) => k.startsWith(p))) {
            return caches.delete(k);
          }
          return Promise.resolve();
        }),
      );
      await self.clients.claim();
    })(),
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith("/static/");
}

function isApi(url) {
  // Same-origin /api/* (the app also talks to an absolute BACKEND_URL/api,
  // which is cross-origin and skipped by the request-mode check below).
  return url.pathname.startsWith("/api/");
}

// NETWORK-FIRST for the document. Try the network with a short timeout so a
// fresh deploy is always picked up; fall back to the cached shell only when
// the network is unreachable/slow (offline).
async function networkFirstNavigation(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), NAV_TIMEOUT_MS);
    let netResp;
    try {
      netResp = await fetch(request, { signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
    // Cache the latest good HTML under a stable key so offline reloads work
    // regardless of the specific route the user was on (SPA: same shell).
    if (netResp && netResp.ok) {
      cache.put("/index.html", netResp.clone()).catch(() => {});
    }
    return netResp;
  } catch (err) {
    // Offline / timed out -> serve the last good shell.
    const cached =
      (await cache.match("/index.html")) ||
      (await cache.match("/")) ||
      (await caches.match(request));
    if (cached) return cached;
    throw err;
  }
}

// CACHE-FIRST for hashed static assets. Bytes for a given hashed URL never
// change, so once cached we can serve forever and only hit the network on a
// miss.
async function cacheFirstStatic(request) {
  const cache = await caches.open(STATIC_CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const netResp = await fetch(request);
  if (netResp && netResp.ok) {
    cache.put(request, netResp.clone()).catch(() => {});
  }
  return netResp;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only handle GET; let the browser deal with everything else.
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }

  // NEVER touch API traffic — the app layer owns data caching + queueing.
  // This covers same-origin /api/* and any absolute cross-origin call whose
  // path is /api/*.
  if (isApi(url)) return;

  // Skip cross-origin requests entirely (fonts/CDN/backend host). We only
  // manage our own origin's shell + static assets.
  if (url.origin !== self.location.origin) return;

  // Navigation / document requests -> network-first.
  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  // Hashed static assets -> cache-first.
  if (isStaticAsset(url)) {
    event.respondWith(cacheFirstStatic(request));
    return;
  }

  // Everything else (favicon, manifest, icons, root-level assets): try cache,
  // fall back to network, and opportunistically cache a good response. Never
  // block on it — if both fail we just let the error surface.
  event.respondWith(
    (async () => {
      const cache = await caches.open(STATIC_CACHE);
      const hit = await cache.match(request);
      if (hit) {
        // Refresh in the background (stale-while-revalidate) but don't wait.
        fetch(request)
          .then((r) => {
            if (r && r.ok) cache.put(request, r.clone()).catch(() => {});
          })
          .catch(() => {});
        return hit;
      }
      try {
        const netResp = await fetch(request);
        if (netResp && netResp.ok) cache.put(request, netResp.clone()).catch(() => {});
        return netResp;
      } catch (err) {
        const fallback = await caches.match(request);
        if (fallback) return fallback;
        throw err;
      }
    })(),
  );
});

// Allow the page to tell a waiting SW to activate immediately (used after a
// deploy prompt, if the app ever adds one).
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});
