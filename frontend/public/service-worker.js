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

const CACHE_VERSION = "v2";
const SHELL_CACHE = `ice-shell-${CACHE_VERSION}`;
const STATIC_CACHE = `ice-static-${CACHE_VERSION}`;
const OUR_CACHES = [SHELL_CACHE, STATIC_CACHE];

// Old self-destruct/cache-first builds and any prior ICE caches we want gone.
const LEGACY_CACHE_PREFIXES = ["ice-shell-", "ice-static-", "workbox-", "ice-"];

const NAV_TIMEOUT_MS = 3000;

// Install-time precache (2026-07-06): the shell AND the script/style assets
// it references. Previously install cached only "/" + "/index.html"; the
// bundle was cached lazily by the fetch handler — which only controls the
// page from the SECOND visit onward (the first load's requests happen before
// this SW takes over). Net effect: offline after exactly one online visit
// served the cached HTML with NO JavaScript — a blank page (field-reported).
// Parsing the fresh index.html for its /static asset URLs and caching them
// here makes ONE online visit enough to boot offline, for both the dev
// bundle (/static/js/bundle.js) and hashed production files.
async function precacheShell() {
  let resp;
  try {
    resp = await fetch("/index.html", { cache: "no-cache" });
  } catch {
    return; // offline during install — nothing to precache
  }
  if (!resp || !resp.ok) return;
  const shell = await caches.open(SHELL_CACHE);
  await shell.put("/index.html", resp.clone()).catch(() => {});
  await shell.put("/", resp.clone()).catch(() => {});
  let html = "";
  try {
    html = await resp.clone().text();
  } catch {
    return;
  }
  const urls = new Set();
  const re = /(?:src|href)=["'](\/[^"']+\.(?:m?js|css))["']/g;
  let m;
  while ((m = re.exec(html)) !== null) urls.add(m[1]);
  const stat = await caches.open(STATIC_CACHE);
  await Promise.all([...urls].map(async (u) => {
    try {
      const r = await fetch(u);
      if (r && r.ok) await stat.put(u, r.clone());
    } catch { /* best-effort per asset */ }
  }));
}

self.addEventListener("install", (event) => {
  event.waitUntil(precacheShell().then(() => self.skipWaiting()));
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

// A filename is content-HASHED only if it carries an 8+ hex-char fingerprint
// segment (CRA production: main.a1b2c3d4.js, 453.f9e8...chunk.js, logo.abcd1234.png).
// Emergent serves a DEV build whose entry bundle is the STABLE name
// static/js/bundle.js — NOT hashed — so it must never be cache-first (its bytes
// change on every deploy while the URL stays the same == permanent stale bundle,
// the exact failure the old SW was deleted for). Only truly-hashed URLs are safe
// to cache forever; everything else goes network-first.
function isHashedAsset(url) {
  return /\.[0-9a-f]{8,}\.[a-z0-9]+$/i.test(url.pathname);
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

// NETWORK-FIRST for NON-hashed static assets (the dev build's bundle.js): fetch
// fresh so a deploy is always picked up online, but keep the last-good copy so
// the app still loads offline. This is what makes offline work without ever
// serving a stale bundle when the network is reachable.
async function networkFirstStatic(request) {
  const cache = await caches.open(STATIC_CACHE);
  try {
    const netResp = await fetch(request);
    if (netResp && netResp.ok) {
      cache.put(request, netResp.clone()).catch(() => {});
    }
    return netResp;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
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

  // Static assets: cache-first ONLY when the filename is content-hashed
  // (safe forever); the stable-named dev bundle (static/js/bundle.js) goes
  // network-first so every deploy is picked up online, cached for offline.
  if (isStaticAsset(url)) {
    event.respondWith(
      isHashedAsset(url) ? cacheFirstStatic(request) : networkFirstStatic(request),
    );
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
