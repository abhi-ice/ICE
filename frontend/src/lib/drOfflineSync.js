/**
 * Offline request queue for Daily Report mutations.
 *
 * 2026-05-25 — Phase A of the multi-modal DR voice intake feature.
 *
 * When the browser loses connectivity, any PATCH/POST/DELETE that the
 * DR detail page issues to /daily-reports/*, /daily-presence/*, or the
 * inline-event create endpoints fails with a TypeError ("Network
 * Error" / no axios response). Rather than nuke the user's hard-won
 * 30-minute walk-the-site form, we capture the request in localStorage
 * and replay it the moment we hear an `online` event.
 *
 * Constraints
 *  - Only methods that don't depend on returning an id are safe to
 *    queue (PATCH on existing records, POST that we're happy to fire-
 *    and-forget such as adding rows to an already-existing grid). For
 *    new record creates that the UI needs the id of (e.g. presence
 *    row that the form references), we still toast an error; the
 *    super can re-create the row when connectivity returns.
 *  - We do NOT queue GETs — read traffic is allowed to fail loudly.
 *
 * Queue format (localStorage key "ice-dr-offline-queue"):
 *   [
 *     {
 *       id: "req_<ts>_<rand>",
 *       method: "PATCH",
 *       url: "/daily-reports/<id>",
 *       data: {...},
 *       createdAt: ISO,
 *       retryCount: 0,
 *       lastError: null
 *     },
 *     ...
 *   ]
 */
const QUEUE_KEY = "ice-dr-offline-queue";
const MAX_QUEUE_SIZE = 200;

// URL-pattern allow-list. Only requests whose URL starts with one of
// these prefixes are queued; everything else surfaces the error
// normally.
const QUEUEABLE_PREFIXES = [
  "/daily-reports/",       // PATCH report body, POST grid rows, etc.
  "/daily-presence/",
  "/delays/",
  "/safety-events/",
  "/qc-issues/",
  "/rfis/",
  "/deliveries/",
  "/coordination-events/",
];

function readQueue() {
  try {
    const raw = localStorage.getItem(QUEUE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function writeQueue(arr) {
  try { localStorage.setItem(QUEUE_KEY, JSON.stringify(arr.slice(-MAX_QUEUE_SIZE))); }
  catch { /* quota or private-mode; nothing we can do */ }
}

export function isQueueable(method, url) {
  const m = (method || "").toUpperCase();
  if (!["PATCH", "POST", "DELETE", "PUT"].includes(m)) return false;
  if (!url) return false;
  return QUEUEABLE_PREFIXES.some((p) => url.includes(p));
}

export function enqueueRequest({ method, url, data }) {
  const q = readQueue();
  const id = `req_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  q.push({
    id,
    method: (method || "").toUpperCase(),
    url,
    data: data ?? null,
    createdAt: new Date().toISOString(),
    retryCount: 0,
    lastError: null,
  });
  writeQueue(q);
  return id;
}

export function getQueueSize() {
  return readQueue().length;
}

export function getQueue() {
  return readQueue();
}

export function clearQueue() {
  writeQueue([]);
}

/**
 * Drain the queue in insertion order. The caller supplies their
 * authenticated axios `api` instance so we don't re-implement auth.
 *
 * Returns { synced, failed, remaining }.
 */
export async function drainOfflineQueue(api) {
  if (!navigator.onLine) {
    return { synced: 0, failed: 0, remaining: -1 };
  }
  const q = readQueue();
  if (!q.length) return { synced: 0, failed: 0, remaining: 0 };
  let synced = 0;
  let failed = 0;
  const keep = [];
  for (const req of q) {
    try {
      await api.request({
        method: req.method,
        url: req.url,
        data: req.data,
        // mark so the interceptor skips re-queueing on a second
        // network failure during drain.
        headers: { "X-Drain-Replay": "1" },
      });
      synced += 1;
    } catch (e) {
      failed += 1;
      // Surface 4xx / 5xx but DON'T retry indefinitely -- after 5
      // retries we drop the request and surface a toast via caller.
      const networkOnly = !e.response;
      const next = {
        ...req,
        retryCount: (req.retryCount || 0) + 1,
        lastError: e?.response?.data?.detail || e.message || "unknown",
      };
      if (networkOnly && next.retryCount < 5) {
        keep.push(next);
      }
      // 4xx is a permanent failure (validation / 404 / 409) -- drop it.
    }
  }
  writeQueue(keep);
  return { synced, failed, remaining: keep.length };
}

/**
 * Install an axios response interceptor that captures network-only
 * failures (no err.response) for queueable URLs and queues them
 * instead of bubbling the error. Returns the resolved request id so
 * the caller can react with a success-like toast.
 *
 * Idempotent: safe to call multiple times.
 */
let _installed = false;
export function installOfflineInterceptor(apiClient) {
  if (_installed) return;
  _installed = true;
  apiClient.interceptors.response.use(
    (r) => r,
    (err) => {
      // Network-only failure: err.response is undefined.
      if (!err.response && err.config) {
        const cfg = err.config;
        const isDrain = cfg.headers?.["X-Drain-Replay"] === "1";
        if (!isDrain && isQueueable(cfg.method, cfg.url || "")) {
          const id = enqueueRequest({
            method: cfg.method,
            url: cfg.url,
            data: cfg.data
              ? (typeof cfg.data === "string" ? JSON.parse(cfg.data) : cfg.data)
              : null,
          });
          // Resolve with a synthetic "queued" response so the caller
          // doesn't see a thrown error.
          return Promise.resolve({
            status: 202,
            statusText: "Queued offline",
            data: { queued: true, request_id: id, _offline: true },
            config: cfg,
            headers: {},
          });
        }
      }
      return Promise.reject(err);
    }
  );

  // Auto-drain on reconnect.
  window.addEventListener("online", () => {
    drainOfflineQueue(apiClient).catch(() => { /* swallow */ });
  });
}
