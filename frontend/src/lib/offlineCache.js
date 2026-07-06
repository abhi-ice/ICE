/**
 * Read-through GET cache for DR-critical endpoints (offline data).
 *
 * 2026-07-05 — Part of the v7 full-offline Daily Report layer.
 *
 * Purpose
 *   Let a super open a Daily Report (and its list/grid/presence/photos/voice)
 *   with ZERO connectivity. Successful GETs to a fixed allow-list of
 *   DR-critical endpoints are mirrored into IndexedDB. The key is built by
 *   the caller (api.js cacheKeyFor): "<tenantId>|<url+serialized params>" —
 *   tenant-scoped so a shared device never leaks rows across tenants, and
 *   params-inclusive so ?date=... variants cache independently.
 *   When a later GET fails with NO response (offline / network error), we
 *   resolve with the cached copy tagged `fromCache: true` so the UI can show
 *   an "offline data" state instead of an error.
 *
 * Design constraints (from spec F.2)
 *   - GENERIC read-through cache — no per-page special cases. A single URL
 *     allow-list decides what is cacheable.
 *   - Online behavior is UNCHANGED: the cache write is best-effort and
 *     non-blocking (fire-and-forget), and we only substitute cached data when
 *     the network truly failed with no response.
 *   - Bounded: ~50 most-recent urls per store, LRU-pruned by last-write time.
 *
 * Storage layout
 *   DB:    ice-dr-getcache
 *   Store: responses  (keyPath: "url")
 *   Row:   { url, data, status, cachedAt }  (cachedAt = ms epoch, LRU key)
 */

const DB_NAME = "ice-dr-getcache";
const DB_VERSION = 1;
const STORE = "responses";
const MAX_ENTRIES = 50;

// Substring allow-list. A GET whose url contains ANY of these is cached on
// success and served from cache on offline failure. Kept deliberately broad
// but scoped to the DR detail flow (spec F.2). Query strings are part of the
// key, so ?daily_report_id=... variants cache independently.
const CACHEABLE_PATTERNS = [
  "/projects",                 // projects list + /projects/{id} + /dr-template
  "/daily-reports",            // list, detail, /grid, /presence, /context,
                               // /available-tasks, /photo-reviews, /delay-reminders
  "/available-tasks",          // (also matched by /daily-reports, listed for clarity)
  "/grid",
  "/presence",
  "/voice/annotations",        // persistent voice-note list
  "/photo-tray",               // project tray + tray detail (photo list on DR)
  "/photos",                   // photo file/list endpoints the DR page calls
  "/photo-tags",
  "/subcontractors",           // crew add-sheet company dropdown
];

let _dbPromise = null;
function openDb() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    let req;
    try {
      req = indexedDB.open(DB_NAME, DB_VERSION);
    } catch (e) {
      return reject(e);
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const s = db.createObjectStore(STORE, { keyPath: "url" });
        s.createIndex("by_cachedAt", "cachedAt", { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  // If it ever fails, allow a later retry rather than caching a rejected promise.
  _dbPromise.catch(() => { _dbPromise = null; });
  return _dbPromise;
}

/**
 * True when this GET url should participate in the read-through cache.
 * Non-GET methods are never cacheable here (mutations use drOfflineSync).
 */
export function isCacheableGet(method, url) {
  if ((method || "").toUpperCase() !== "GET") return false;
  if (!url) return false;
  // JSON-only by design: binary endpoints (/photos/{id}/file,
  // /photo-tray/{id}/file, /files/audio/{id}) request responseType "blob",
  // and the api.js interceptor skips any non-JSON responseType before this
  // allow-list is even consulted — so pattern overlap with the blob routes
  // is harmless.
  return CACHEABLE_PATTERNS.some((p) => url.includes(p));
}

/**
 * Best-effort, non-blocking write. Never throws to the caller; failures
 * (quota, private mode, closed db) are swallowed so online behavior is
 * unchanged. Prunes to MAX_ENTRIES (LRU by cachedAt) after each write.
 */
export function putCached(url, data, status = 200) {
  if (!url) return;
  openDb()
    .then((db) => {
      const tx = db.transaction(STORE, "readwrite");
      const store = tx.objectStore(STORE);
      store.put({ url, data, status, cachedAt: Date.now() });
      tx.oncomplete = () => { pruneOldest(db).catch(() => {}); };
      tx.onerror = () => {};
      tx.onabort = () => {};
    })
    .catch(() => {});
}

/**
 * Read a cached row. Resolves to { data, status, cachedAt } or null.
 * Never rejects.
 */
export function getCached(url) {
  if (!url) return Promise.resolve(null);
  return openDb()
    .then(
      (db) =>
        new Promise((resolve) => {
          const tx = db.transaction(STORE, "readonly");
          const req = tx.objectStore(STORE).get(url);
          req.onsuccess = () => resolve(req.result || null);
          req.onerror = () => resolve(null);
        }),
    )
    .catch(() => null);
}

/** LRU prune: keep only the MAX_ENTRIES most-recently-written rows. */
function pruneOldest(db) {
  return new Promise((resolve) => {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    const countReq = store.count();
    countReq.onsuccess = () => {
      const excess = countReq.result - MAX_ENTRIES;
      if (excess <= 0) return resolve();
      // Walk the by_cachedAt index oldest-first and delete `excess` rows.
      let deleted = 0;
      const cursorReq = store.index("by_cachedAt").openCursor();
      cursorReq.onsuccess = () => {
        const cursor = cursorReq.result;
        if (!cursor || deleted >= excess) return;
        cursor.delete();
        deleted += 1;
        cursor.continue();
      };
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();
    tx.onabort = () => resolve();
  });
}

/** Clear the whole GET cache (e.g. on logout / tenant switch). */
export function clearGetCache() {
  return openDb()
    .then(
      (db) =>
        new Promise((resolve) => {
          const tx = db.transaction(STORE, "readwrite");
          tx.objectStore(STORE).clear();
          tx.oncomplete = () => resolve();
          tx.onerror = () => resolve();
        }),
    )
    .catch(() => {});
}
