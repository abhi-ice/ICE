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
  "/daily-reports/",       // PATCH report body, POST grid rows, presence &
                           // daily-presence aliases, unplanned-activity, etc.
  "/daily-presence/",      // PATCH/DELETE an existing presence row (id in URL)
  "/daily-presence",       // POST top-level create alias (daily_report_id in body,
                           // no trailing slash) — must precede id-less create match
  "/daily-report-activities/", // v7 guided flow: PATCH EOD% / DELETE a grid row
  // Event endpoints — BARE, no trailing slash (2026-07-06 field fix): the
  // create POSTs go to "/delays", "/rfis", ... and `url.includes("/delays/")`
  // does NOT match them, so every offline issue log died with "Network
  // Error" while only id-suffixed edits were queueable. Bare prefixes match
  // both the create POST and /xxx/{id} mutations.
  "/delays",
  "/safety-events",
  "/qc-issues",
  "/rfis",
  "/deliveries",
  "/coordination-events",
  "/photo-tags",           // v7 guided flow: POST tag/pin a photo to an activity,
                           // DELETE /photo-tags/{id}. (Offline photo CAPTURE is
                           // owned end-to-end by lib/drPhotoQueue; this only
                           // catches tag mutations on already-uploaded photos.)
  "/photo-tray/attach-to-dr", // attach an already-uploaded tray photo to the DR
  // NOT queued: /photo-tray/review — that's the AI analysis run, which the
  // offline spec explicitly scopes out ("everything except AI analysis").
  // Queueing it would silently spend AI budget on reconnect; it fails
  // loudly instead and the super can re-run it online.
  // NOT queued: multipart uploads (/photo-tray, /voice/annotate,
  // /daily-reports/{id}/voice/transcribe-and-extract) — FormData can't
  // round-trip through localStorage JSON; lib/drPhotoQueue.js and
  // lib/drVoiceQueue.js own those end-to-end in IndexedDB.
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
  // Cap the queue, but NEVER evict a DR create (offlineCreate): it is by
  // construction the OLDEST entry for a provisional report, and slicing it
  // away would orphan every queued child that references its temp id.
  // Creates stay at the front, preserving the create-before-children order.
  let out = arr;
  if (arr.length > MAX_QUEUE_SIZE) {
    const creates = arr.filter((r) => r.offlineCreate);
    const rest = arr.filter((r) => !r.offlineCreate);
    out = [...creates, ...rest.slice(-(MAX_QUEUE_SIZE - creates.length))];
  }
  try { localStorage.setItem(QUEUE_KEY, JSON.stringify(out)); }
  catch { /* quota or private-mode; nothing we can do */ }
  // v7 (2026-07-05): let the flow landing's "N items will sync" chip
  // live-update instead of polling. Same event name as the photo/voice
  // queues (lib/drPhotoQueue.js, lib/drVoiceQueue.js).
  try {
    window.dispatchEvent(new CustomEvent("ice-offline-queue-changed", {
      detail: { source: "requests" },
    }));
  } catch { /* non-window env */ }
}

export function isQueueable(method, url) {
  const m = (method || "").toUpperCase();
  if (!["PATCH", "POST", "DELETE", "PUT"].includes(m)) return false;
  if (!url) return false;
  return QUEUEABLE_PREFIXES.some((p) => url.includes(p));
}

export function enqueueRequest({ method, url, data, offlineCreate = null }) {
  const q = readQueue();
  // Offline DR create (2026-07-06): deterministic dedupe — a double-tap on
  // "+ New" while offline must not queue two creates for the same
  // (url, tempId). The backend create is idempotent anyway; this just keeps
  // the queue clean.
  if (offlineCreate?.tempId) {
    const dup = q.find((r) => r.offlineCreate?.tempId === offlineCreate.tempId);
    if (dup) return dup.id;
  }
  const id = `req_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  q.push({
    id,
    method: (method || "").toUpperCase(),
    url,
    data: data ?? null,
    createdAt: new Date().toISOString(),
    retryCount: 0,
    lastError: null,
    ...(offlineCreate ? { offlineCreate } : {}),
  });
  writeQueue(q);
  return id;
}

// ---------------------------------------------------------------------------
// Offline-created record id remap (2026-07-06). A DR created offline gets a
// provisional id ("offline-dr-<project>-<date>"); every queued child mutation
// references it. When the queued create replays, the server returns the REAL
// id — the map below lets the drain (and the photo/voice IndexedDB queues)
// rewrite provisional ids before replaying. Persisted so a partially-drained
// queue survives a reload between drains.
// ---------------------------------------------------------------------------
const REMAP_KEY = "ice-offline-id-remap";

function readRemap() {
  try { return JSON.parse(localStorage.getItem(REMAP_KEY) || "{}") || {}; }
  catch { return {}; }
}

function saveRemap(map) {
  try { localStorage.setItem(REMAP_KEY, JSON.stringify(map)); } catch { /* ignore */ }
}

/** Resolve a possibly-provisional id to its synced real id (or itself). */
export function resolveOfflineId(id) {
  if (!id || typeof id !== "string" || !id.startsWith("offline-")) return id;
  const map = readRemap();
  return map[id] || id;
}

/** True when `id` is a provisional offline id with no server mapping yet. */
export function isUnresolvedOfflineId(id) {
  return typeof id === "string" && id.startsWith("offline-")
    && resolveOfflineId(id) === id;
}

function applyRemapToRequest(req, map) {
  const entries = Object.entries(map);
  if (!entries.length) return req;
  let { url, data } = req;
  for (const [tempId, realId] of entries) {
    if (url && url.includes(tempId)) url = url.split(tempId).join(realId);
    if (data != null) {
      try {
        const s = JSON.stringify(data);
        if (s.includes(tempId)) data = JSON.parse(s.split(tempId).join(realId));
      } catch { /* leave data as-is */ }
    }
  }
  return { ...req, url, data };
}

export function getQueueSize() {
  return readQueue().length;
}

/**
 * v7 (2026-07-05) — combined pending-sync counts for the flow landing's
 * "N items will sync" chip (spec F.6: photos + voice + requests).
 * Each source is best-effort; a broken IndexedDB never breaks the chip.
 * Returns { requests, photos, voice, total }.
 */
export async function getPendingSyncCounts(dailyReportId = null) {
  const counts = { requests: 0, photos: 0, voice: 0, total: 0 };
  counts.requests = getQueueSize();
  try {
    const { listPendingPhotos } = await import("./drPhotoQueue");
    counts.photos = (await listPendingPhotos(dailyReportId)).length;
  } catch { /* ignore */ }
  try {
    const { listPending } = await import("./drVoiceQueue");
    counts.voice = (await listPending(dailyReportId)).length;
  } catch { /* ignore */ }
  counts.total = counts.requests + counts.photos + counts.voice;
  return counts;
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
  // Offline-create remap (2026-07-06): start from any persisted mappings
  // (a previous partial drain), rewrite every request's provisional ids
  // before replaying, and capture new mappings as creates land. FIFO order
  // guarantees a DR create replays before its children.
  const remap = readRemap();
  let remapGrew = false;
  // Temp ids whose CREATE was permanently rejected: their children can never
  // succeed (the report will never exist) — drop them instead of replaying
  // orphan writes or wedging the queue.
  const deadTempIds = new Set();
  const referencesDeadId = (req) => {
    if (!deadTempIds.size) return false;
    try {
      const hay = `${req.url || ""}|${req.data != null ? JSON.stringify(req.data) : ""}`;
      return [...deadTempIds].some((t) => hay.includes(t));
    } catch { return false; }
  };
  // Provisional GRID-ROW resolution (round 3, 2026-07-06): rows synthesized
  // for an offline-created report carry ids "offline-row-<tempDr>__<taskId>".
  // The server generates its own row ids when the queued create replays, so
  // requests referencing a provisional row (EOD% PATCH, photo tags) are
  // rewritten by matching the real grid's schedule_activity_id. Grid fetched
  // once per report per drain; unresolvable yet -> kept for the next drain.
  const gridCache = {};
  // Temp DR ids whose create still sits in this queue — a row token whose
  // DR is neither mapped nor pending can never resolve (orphan -> drop).
  const pendingCreates = new Set(
    q.filter((r) => r.offlineCreate?.tempId).map((r) => r.offlineCreate.tempId),
  );
  const resolveRowIds = async (req) => {
    const hay = `${req.url || ""}|${req.data != null ? JSON.stringify(req.data) : ""}`;
    const tokens = [...new Set(hay.match(/offline-row-[A-Za-z0-9_-]+/g) || [])];
    if (!tokens.length) return { status: "ok", req };
    const subs = {};
    for (const token of tokens) {
      const body = token.slice("offline-row-".length);
      const cut = body.lastIndexOf("__");
      if (cut < 0) return { status: "drop" };
      const tempDr = body.slice(0, cut);
      const taskId = body.slice(cut + 2);
      // The DR-id slot may ALREADY hold the real id: applyRemapToRequest
      // blindly rewrites the temp DR id substring INSIDE this token (review
      // blocker, 2026-07-06) — so a non-"offline-" value here IS the real
      // id. This also heals queue entries persisted with rewritten tokens.
      const realDr = remap[tempDr]
        || (tempDr.startsWith("offline-") ? null : tempDr);
      if (!realDr) {
        // Still provisional: defer while its create is pending in this
        // queue; if the create is GONE (dropped/evicted), the row can never
        // resolve — drop instead of deferring forever.
        return pendingCreates.has(tempDr)
          ? { status: "defer" }
          : { status: "drop" };
      }
      if (!(realDr in gridCache)) {
        try {
          const g = await api.get(`/daily-reports/${realDr}/grid`,
            { headers: { "X-Drain-Replay": "1" } });
          gridCache[realDr] = Array.isArray(g.data) ? g.data : [];
        } catch { gridCache[realDr] = null; }
      }
      const rows = gridCache[realDr];
      if (rows === null) return { status: "defer" }; // grid unreadable — retry later
      const row = rows.find((r) => r && r.schedule_activity_id === taskId);
      if (!row) return { status: "drop" }; // task no longer on that day's grid
      subs[token] = row.id;
    }
    let { url, data } = req;
    for (const [t, real] of Object.entries(subs)) {
      if (url && url.includes(t)) url = url.split(t).join(real);
      if (data != null) {
        try {
          const s = JSON.stringify(data);
          if (s.includes(t)) data = JSON.parse(s.split(t).join(real));
        } catch { /* leave data as-is */ }
      }
    }
    return { status: "ok", req: { ...req, url, data } };
  };

  const keep = [];
  for (const raw of q) {
    let req = applyRemapToRequest(raw, remap);
    if (referencesDeadId(req)) { failed += 1; continue; } // dropped with its create
    const rowRes = await resolveRowIds(req);
    if (rowRes.status === "drop") { failed += 1; continue; }
    if (rowRes.status === "defer") {
      // Waiting on its DR create / a readable grid — NOT a failure of this
      // request, so do NOT burn its retry budget (a slow sync must never
      // silently discard EOD data). The orphan case above bounds this.
      keep.push({
        ...req, id: raw.id,
        ...(raw.offlineCreate ? { offlineCreate: raw.offlineCreate } : {}),
        retryCount: raw.retryCount || 0,
        lastError: "waiting for the synced report's grid rows",
      });
      continue;
    }
    req = rowRes.req;
    try {
      const resp = await api.request({
        method: req.method,
        url: req.url,
        data: req.data,
        // mark so the interceptor skips re-queueing on a second
        // network failure during drain.
        headers: { "X-Drain-Replay": "1" },
      });
      if (raw.offlineCreate?.tempId && resp?.data?.id) {
        remap[raw.offlineCreate.tempId] = resp.data.id;
        remapGrew = true;
        saveRemap(remap); // persist immediately — children may fail later
      }
      synced += 1;
    } catch (e) {
      failed += 1;
      const status = e?.response?.status;
      const networkOnly = !e.response;
      // Retryable: network-only OR 5xx ("server waking up" is a known
      // transient here — one 502 must not throw away a day's records).
      // 401/403 also retry: auth comes back after re-login.
      const retryable = networkOnly || status >= 500 || status === 401 || status === 403;
      const next = {
        ...req, // keep the REMAPPED url/data so a later drain needs no map
        id: raw.id,
        ...(raw.offlineCreate ? { offlineCreate: raw.offlineCreate } : {}),
        retryCount: (raw.retryCount || 0) + 1,
        lastError: e?.response?.data?.detail || e.message || "unknown",
      };
      if (retryable && next.retryCount < 5) {
        keep.push(next);
      } else if (raw.offlineCreate?.tempId) {
        // The CREATE itself is permanently rejected (validation/404/409):
        // everything referencing its temp id is unsyncable — mark it dead.
        deadTempIds.add(raw.offlineCreate.tempId);
      }
      // other permanent 4xx (validation / 404 / 409) -- drop it.
    }
  }
  writeQueue(keep.map((r) => applyRemapToRequest(r, remap)));
  if (remapGrew) {
    // Wake the photo/voice IndexedDB queues — they may hold rows keyed to a
    // provisional DR id that just became resolvable.
    try { window.dispatchEvent(new Event("ice-offline-remap-ready")); }
    catch { /* non-window env */ }
  }
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
        // Multipart bodies can't survive a localStorage JSON round-trip —
        // e.g. /daily-reports/{id}/voice/transcribe-and-extract matches the
        // /daily-reports/ prefix but posts FormData. Those belong to the
        // IndexedDB queues (drVoiceQueue / drPhotoQueue); queueing a
        // stringified FormData here would replay as an empty body.
        const isMultipart = typeof FormData !== "undefined" && cfg.data instanceof FormData;
        if (!isDrain && !isMultipart && isQueueable(cfg.method, cfg.url || "")) {
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
