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

// Day-1 fix (2026-10-25, findings 10/11): report STATE CHANGES are never
// queued. A queued submit used to come back as a fake success, so the super
// was told "submitted and locked" while the server still had a draft. With
// this deny-list the request fails as a normal network error and the page
// tells the super the report is NOT submitted yet. Checked on the URL
// suffix because the allow-list matches "/daily-reports/" anywhere.
const STATE_TRANSITION_RE =
  /\/daily-reports\/[^/?#]+\/(submit|approve|reopen|unlock|reject)\/?(?:[?#]|$)/;

export function isQueueable(method, url) {
  const m = (method || "").toUpperCase();
  if (!["PATCH", "POST", "DELETE", "PUT"].includes(m)) return false;
  if (!url) return false;
  if (STATE_TRANSITION_RE.test(url)) return false;
  return QUEUEABLE_PREFIXES.some((p) => url.includes(p));
}

/**
 * Day-1 fix (2026-10-25, finding 35): true ONLY for a genuine rejection from
 * the app itself — a 4xx whose body is the app's JSON `{detail: ...}`.
 * Everything else is TRANSIENT and must be kept for a later retry:
 *  - no response (offline), any 5xx (Cloudflare 520 pages included)
 *  - 401/403 (auth comes back after re-login)
 *  - 408/425/429 (timeout / too early / rate limited)
 *  - any non-JSON response, e.g. the HTML 404 a sleeping or restarting
 *    backend answers with — that is NOT the app saying "no".
 * Every app error carries `detail` (FastAPI HTTPException + the 422
 * handler in server.py), so a 4xx without one did not come from the app.
 */
export function isPermanentRejection(err) {
  const resp = err?.response;
  if (!resp) return false;
  const status = resp.status;
  if (!(status >= 400 && status < 500)) return false;
  if ([401, 403, 408, 425, 429].includes(status)) return false;
  let ct = "";
  try {
    const h = resp.headers || {};
    ct = String((typeof h.get === "function" ? h.get("content-type") : null)
      || h["content-type"] || h["Content-Type"] || "");
  } catch { ct = ""; }
  if (ct && !/json/i.test(ct)) return false;
  const d = resp.data;
  if (!d || typeof d !== "object" || d.detail == null) return false;
  // The framework's own route-miss replies ({"detail": "Not Found"} /
  // "Method Not Allowed") mean the server we reached does not have the
  // route right now — a half-started or mid-deploy backend — not that the
  // app rejected this record. No app handler raises these exact texts on a
  // queueable route, so keep the save and retry.
  if ((status === 404 && d.detail === "Not Found")
      || (status === 405 && d.detail === "Method Not Allowed")) return false;
  return true;
}

// Progress PATCH on one activity row (EOD%, hours, notes...).
const ACTIVITY_PATCH_RE = /^\/daily-report-activities\/[^/?#]+\/?$/;

// Ids of the entries the drain currently in flight is replaying. Coalescing
// must never merge into one of them (the drain works from its own snapshot,
// so the merged fields would be lost when it finishes).
let _drainingIds = null;

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
  // Day-1 fix (2026-10-25, finding 8): coalesce progress PATCHes to the same
  // activity row. Every slider release sends a PATCH carrying the SAME
  // stale-write token from the cached report; queued separately, the second
  // one replayed into a 409 and was dropped, losing the super's final value.
  // Merge into the latest queued entry for that URL instead (newest fields
  // win, the OLD token is kept) — only when that entry is a PATCH (never
  // across a later DELETE) and is not being replayed right now.
  const m = (method || "").toUpperCase();
  if (m === "PATCH" && !offlineCreate && ACTIVITY_PATCH_RE.test(url || "")
      && data && typeof data === "object" && !Array.isArray(data)) {
    let last = null;
    for (let i = q.length - 1; i >= 0; i -= 1) {
      if (q[i].url === url) { last = q[i]; break; }
    }
    if (last && last.method === "PATCH" && !last.offlineCreate
        && !(_drainingIds && _drainingIds.has(last.id))
        && last.data && typeof last.data === "object" && !Array.isArray(last.data)) {
      const merged = { ...last.data, ...data };
      if ("task_last_modified_at" in last.data) {
        merged.task_last_modified_at = last.data.task_last_modified_at;
      }
      last.data = merged;
      writeQueue(q);
      return last.id;
    }
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
// MUTEX (round 4 review blocker): the drain now has FOUR triggers — the
// 'online' listener, the post-load sweep, the 60s heartbeat, and the manual
// "Sync now" button. Without a shared in-flight promise, two overlapping
// drains read the SAME queue snapshot and replay every non-idempotent POST
// twice (duplicate delays/crew rows server-side). Mirrors drPhotoQueue.
let _drainInFlight = null;
export function drainOfflineQueue(api) {
  if (_drainInFlight) return _drainInFlight;
  _drainInFlight = _drainQueueOnce(api).finally(() => {
    _drainInFlight = null;
    _drainingIds = null;
  });
  return _drainInFlight;
}

async function _drainQueueOnce(api) {
  if (!navigator.onLine) {
    return { synced: 0, failed: 0, remaining: -1 };
  }
  const q = readQueue();
  if (!q.length) return { synced: 0, failed: 0, remaining: 0 };
  _drainingIds = new Set(q.map((r) => r.id));
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
    } catch (e0) {
      let e = e0;
      let staleOverride = false;
      // Day-1 fix (2026-10-25, finding 8): a progress PATCH recorded on site
      // that hits the stale-write check (the schedule row was touched after
      // the report was cached — often by our OWN earlier replay or a delay
      // logged on the same activity) is resent ONCE without the token: the
      // value the super entered in the field wins. Never dropped silently.
      if (req.method === "PATCH"
          && /\/daily-report-activities\//.test(req.url || "")
          && e?.response?.status === 409
          && e?.response?.data?.detail?.error === "stale_write") {
        staleOverride = true;
        const rest = { ...(req.data || {}) };
        delete rest.task_last_modified_at;
        req = { ...req, data: rest };
        try {
          // eslint-disable-next-line no-console
          console.warn("[offline-sync] schedule changed while offline; saving the field value", req.url);
          window.dispatchEvent(new CustomEvent("ice-offline-conflict", {
            detail: { url: req.url, taskId: e.response.data.detail.task_id || null },
          }));
        } catch { /* non-window env */ }
        try {
          await api.request({
            method: req.method,
            url: req.url,
            data: req.data,
            headers: { "X-Drain-Replay": "1" },
          });
          synced += 1;
          continue;
        } catch (e2) {
          e = e2; // fall through to the normal keep/drop policy below
        }
      }
      failed += 1;
      const status = e?.response?.status;
      // Retryable: anything that is not a genuine JSON rejection from the
      // app — network-only, 5xx ("server waking up" is a known transient),
      // 401/403 (auth comes back after re-login), 408/425/429, and non-JSON
      // replies such as the HTML 404 of a sleeping backend (finding 35). A
      // queued field record is NEVER dropped for these — with the 60s drain
      // heartbeat (round 4) a capped retry budget would burn through ~5
      // minutes of downtime or a logged-out tab and silently lose a day's
      // records. retryCount is informational only (mirrors drPhotoQueue's
      // policy); only a DEFINITIVE server rejection drops.
      const retryable = !isPermanentRejection(e);
      // Auth failures don't even bump the informational counter — a tab
      // idling on /login must leave the queue byte-identical (no churn).
      const authWait = status === 401 || status === 403;
      const next = {
        ...req, // keep the REMAPPED url/data so a later drain needs no map
        id: raw.id,
        ...(raw.offlineCreate ? { offlineCreate: raw.offlineCreate } : {}),
        retryCount: authWait ? (raw.retryCount || 0) : (raw.retryCount || 0) + 1,
        lastError: staleOverride
          ? "The schedule changed while you were offline. Your field entry will be saved on the next sync."
          : (e?.response?.data?.detail || e.message || "unknown"),
      };
      if (retryable) {
        keep.push(next);
      } else if (raw.offlineCreate?.tempId) {
        // The CREATE itself is permanently rejected (validation/404/409):
        // everything referencing its temp id is unsyncable — mark it dead.
        deadTempIds.add(raw.offlineCreate.tempId);
      }
      // other permanent 4xx (validation / 404 / 409) -- drop it.
    }
  }
  // Skip the rewrite + event when the queue is byte-identical (e.g. every
  // entry deferred or auth-waited) — otherwise the 60s heartbeat would
  // dispatch ice-offline-queue-changed every minute and cycle the DR list
  // reload + chip recount for no reason.
  // Saves queued WHILE this drain was running (a request that failed offline
  // mid-drain) are not in our snapshot — append them after the kept entries
  // so writing the queue back can never wipe them out.
  const snapshotIds = new Set(q.map((r) => r.id));
  const addedDuringDrain = readQueue().filter((r) => !snapshotIds.has(r.id));
  const finalQueue = [...keep, ...addedDuringDrain].map((r) => applyRemapToRequest(r, remap));
  try {
    if (JSON.stringify(finalQueue) !== JSON.stringify(q)) writeQueue(finalQueue);
  } catch {
    writeQueue(finalQueue);
  }
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
