/**
 * IndexedDB photo-upload queue for offline Daily Report intake.
 *
 * 2026-07-05 — Part of the v7 full-offline Daily Report layer (spec F.4).
 * Mirrors the structure of lib/drVoiceQueue.js.
 *
 * When a photo capture/upload happens offline (or navigator.onLine is false),
 * the guided flow calls queuePhoto(...) to stash the raw bytes locally and
 * shows the thumbnail with a "will upload" badge. On the next 'online' event
 * (or an explicit drainQueue call) the clips are re-uploaded through the
 * EXISTING photo endpoints and, if the caller supplied a task, tagged to that
 * activity — exactly the online path:
 *
 *     POST /photo-tray            (multipart: file, project_id, capture_source)
 *       -> returns { id: tray_id }
 *     then EITHER (activityRowId set):
 *       POST /photo-tags          (tray_photo_id, daily_report_id,
 *                                  daily_report_activity_id, photo_class,
 *                                  tag_caption) — one photos row that both
 *                                  shows on the DR and links the activity
 *     OR (no activity, or the tag was rejected):
 *       POST /photo-tray/attach-to-dr  (tray_ids, daily_report_id)
 *                                  — plain DR-level attach
 *
 * AI analysis is server-side and out of scope: it simply runs after the tray
 * upload lands, nothing offline needs to happen for it.
 *
 * Storage layout
 *   DB:    ice-dr-photos
 *   Store: pendingPhotos  (keyPath: "id")
 *   Row:   { id, blob, mimeType, dailyReportId, projectId,
 *            activityRowId, taskId, caption, photoClass,
 *            createdAt, retryCount, lastError }
 */

const DB_NAME = "ice-dr-photos";
const DB_VERSION = 1;
const STORE = "pendingPhotos";
const MAX_BLOB_BYTES = 25 * 1024 * 1024; // matches backend /photo-tray 25MB cap

// Offline-created DRs (2026-07-06): a photo queued against a provisional
// "offline-dr-*" id can only upload after the request queue's drain has
// created the real report and recorded the id mapping.
import { resolveOfflineId, isUnresolvedOfflineId } from "./drOfflineSync";

// Queue/remove dispatch this event so the flow landing's "N items will
// sync" chip can live-update without polling.
function notifyQueueChanged() {
  try {
    window.dispatchEvent(new CustomEvent("ice-offline-queue-changed", {
      detail: { source: "photos" },
    }));
  } catch { /* non-window env */ }
}

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const s = db.createObjectStore(STORE, { keyPath: "id" });
        s.createIndex("by_dr", "dailyReportId", { unique: false });
        s.createIndex("by_created", "createdAt", { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/**
 * Queue a photo for later upload.
 * @param {Object} p
 * @param {Blob}   p.blob            required image bytes
 * @param {string} p.mimeType        e.g. "image/jpeg"
 * @param {string} p.dailyReportId   the DR this photo belongs to
 * @param {string} [p.projectId]     required to re-upload to the tray
 * @param {string} [p.activityRowId] daily_report_activity_id to tag to (optional)
 * @param {string} [p.taskId]        alias accepted; activityRowId preferred
 * @param {string} [p.caption]       tag caption
 * @param {string} [p.photoClass]    defaults to "progress"
 * @returns the stored record (its `id` is a stable local handle for the thumb).
 */
export async function queuePhoto({
  blob,
  mimeType,
  dailyReportId,
  projectId = null,
  activityRowId = null,
  taskId = null,
  caption = null,
  photoClass = "progress",
}) {
  if (!blob || !(blob instanceof Blob)) throw new Error("blob required");
  if (blob.size > MAX_BLOB_BYTES) {
    throw new Error(
      `photo too large (${blob.size} > ${MAX_BLOB_BYTES}); use a photo under 25MB`,
    );
  }
  const db = await openDb();
  const id = `photo_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const record = {
    id,
    blob,
    mimeType: mimeType || blob.type || "image/jpeg",
    dailyReportId: dailyReportId || null,
    projectId,
    activityRowId: activityRowId || null,
    taskId: taskId || null,
    caption: caption || null,
    photoClass: photoClass || "progress",
    createdAt: new Date().toISOString(),
    retryCount: 0,
    lastError: null,
  };
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(record);
    tx.oncomplete = () => { notifyQueueChanged(); resolve(record); };
    tx.onerror = () => reject(tx.error);
  });
}

/** List pending photos, optionally filtered to one daily report.
 *  Exported both as listPending (drVoiceQueue parity) and as the
 *  explicit listPendingPhotos alias the DR flow imports. */
export async function listPending(dailyReportId = null) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const store = tx.objectStore(STORE);
    const req = dailyReportId
      ? store.index("by_dr").getAll(IDBKeyRange.only(dailyReportId))
      : store.getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

/** Remove a queued photo (after a successful upload, or a user discard). */
export async function removePhoto(id) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => { notifyQueueChanged(); resolve(); };
    tx.onerror = () => reject(tx.error);
  });
}

/** Mark a queued photo failed: bump retryCount, record the error. */
export async function markFailed(id, errMsg) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    const getReq = store.get(id);
    getReq.onsuccess = () => {
      const r = getReq.result;
      if (!r) return;
      r.retryCount = (r.retryCount || 0) + 1;
      r.lastError = String(errMsg || "").slice(0, 500);
      store.put(r);
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/** Explicit alias — the guided flow calls listPendingPhotos(drId). */
export const listPendingPhotos = listPending;

/** Count pending photos (for the "N items will sync" chip). */
export async function pendingCount(dailyReportId = null) {
  return (await listPending(dailyReportId)).length;
}

function extForMime(mime) {
  const m = (mime || "").toLowerCase();
  if (m.includes("png")) return "png";
  if (m.includes("webp")) return "webp";
  if (m.includes("heic")) return "heic";
  if (m.includes("heif")) return "heif";
  if (m.includes("gif")) return "gif";
  return "jpg";
}

/**
 * Re-upload one queued photo through the existing endpoints. Throws on any
 * failure so drainQueue can record it and keep the row for a later retry.
 */
async function uploadOne(api, photo) {
  if (!photo.projectId) {
    // Without a project we can't reach the tray; treat as a permanent failure
    // so it doesn't wedge the queue forever.
    throw Object.assign(new Error("missing projectId"), { permanent: true });
  }
  // 1) Upload bytes to the tray.
  const fd = new FormData();
  const filename = `dr_${photo.id}.${extForMime(photo.mimeType)}`;
  fd.append("file", photo.blob, filename);
  fd.append("project_id", photo.projectId);
  fd.append("capture_source", "mobile_camera");
  const trayResp = await api.post("/photo-tray", fd, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 120000,
  });
  const trayId = trayResp?.data?.id;
  if (!trayId) throw new Error("tray upload returned no id");

  // 2) Land the photo on the daily report. ORDER MATTERS: when an activity
  //    was named we tag FIRST — the /photo-tags row carries daily_report_id,
  //    so it both shows on the DR and links the activity in ONE photos row.
  //    Calling attach-to-dr first and photo-tags second would create TWO
  //    rows for the same image (the Bug #30 "Tagged to (2)" duplicate),
  //    because the tag dedup only matches on the same event-ref field.
  const activityId = photo.activityRowId || null;
  let tagged = false;
  if (activityId && photo.dailyReportId) {
    const tagPayload = {
      tray_photo_id: trayId,
      daily_report_id: photo.dailyReportId,
      photo_class: photo.photoClass || "progress",
      tag_caption: photo.caption || null,
      // XOR: exactly one event ref set (same payload TagWizard sends).
      daily_report_activity_id: activityId,
      delay_id: null,
      rfi_id: null,
      qc_issue_id: null,
      safety_event_id: null,
      coordination_event_id: null,
      delivery_id: null,
      drawing_pin_id: null,
      created_via_ai_suggestion: false,
      ai_suggestion_id: null,
      ai_confidence_at_tag_time: null,
    };
    // Retry the TAG (not the whole upload) on a transient failure so a
    // momentary network blip or a server waking up doesn't silently DROP the
    // activity link and downgrade this to a plain DR attach. Only a genuine
    // 4xx reject — the activity was deleted while we were offline (404/409/
    // 422) — falls through to the plain attach so the photo still lands.
    for (let attempt = 0; attempt < 2 && !tagged; attempt++) {
      try {
        await api.post("/photo-tags", tagPayload);
        tagged = true;
      } catch (e) {
        const status = e?.response?.status;
        const transient = !e?.response || status >= 500;
        if (transient && attempt === 0) {
          await new Promise((res) => setTimeout(res, 700));
          continue;
        }
        if (transient) {
          // Still failing on a transient error after the retry — keep the
          // whole photo queued (throw) rather than lose the activity tag by
          // downgrading to a plain attach. The next drain retries the tag.
          throw e;
        }
        // Deterministic reject (activity gone): fall through to plain attach.
        break;
      }
    }
  }
  if (!tagged && photo.dailyReportId) {
    // Plain DR-level attach (idempotent server-side: an existing photos row
    // for this tray+DR — tagged or not — makes this a no-op).
    await api.post("/photo-tray/attach-to-dr", {
      tray_ids: [trayId],
      daily_report_id: photo.dailyReportId,
    });
  }
  return trayId;
}

/**
 * Drain the photo queue. Caller passes their authenticated axios `api`.
 *
 * Failure policy — a queued site photo is a construction RECORD, so we only
 * ever drop a row the server has definitively rejected:
 *   - network error (no response): keep it AND stop draining (we're still
 *     offline; hammering the rest of the queue is pointless).
 *   - 401/403: keep it (auth comes back after re-login) and keep going.
 *   - other 4xx (validation / 404 / 413) or a structurally-broken row
 *     (missing projectId): permanent — drop so it can't wedge the queue.
 *   - 5xx: keep it with an incremented retryCount ("server waking up" is a
 *     known transient in this deployment).
 *
 * MUTEX-GUARDED: the global 'online' listener and any page-level "sync now"
 * call share one in-flight drain, so a photo can never upload twice
 * concurrently (server-side SHA-256/pHash dedup is the second belt).
 *
 * Returns { synced, failed, remaining }.
 */
let _drainInFlight = null;
export function drainQueue(api) {
  if (_drainInFlight) return _drainInFlight;
  _drainInFlight = _drainAll(api).finally(() => { _drainInFlight = null; });
  return _drainInFlight;
}

async function _drainAll(api) {
  if (!navigator.onLine) return { synced: 0, failed: 0, remaining: -1 };
  const pending = await listPending();
  let synced = 0;
  let failed = 0;
  for (const photo of pending) {
    // Provisional DR id with no server mapping yet: leave it queued (its
    // create is still in the request queue) — a remap-ready event or the
    // next 'online' drain picks it up once the mapping exists.
    if (isUnresolvedOfflineId(photo.dailyReportId)) continue;
    try {
      await uploadOne(api, {
        ...photo,
        dailyReportId: resolveOfflineId(photo.dailyReportId),
      });
      await removePhoto(photo.id);
      synced += 1;
    } catch (e) {
      failed += 1;
      const status = e?.response?.status;
      await markFailed(photo.id, e?.response?.data?.detail || e?.message).catch(() => {});
      if (!e?.response && !e?.permanent) {
        // Network-only failure: still offline — keep everything, bail out.
        break;
      }
      const permanent =
        e?.permanent
        || (status >= 400 && status < 500 && status !== 401 && status !== 403);
      if (permanent) {
        await removePhoto(photo.id).catch(() => {});
      }
    }
  }
  const remaining = (await listPending()).length;
  if (synced > 0) notifyQueueChanged();
  return { synced, failed, remaining };
}

// Auto-drain on reconnect. Registered once at module load; the api instance is
// resolved lazily to avoid an import cycle (api.js -> nothing here, but keep it
// decoupled). The flow also calls drainQueue directly after recording online.
let _wired = false;
export function wireAutoDrain(api) {
  if (_wired) return;
  _wired = true;
  window.addEventListener("online", () => {
    drainQueue(api).catch(() => {});
  });
  // Re-drain when the request queue resolves offline-created DR ids: rows
  // skipped as "unresolved" above become uploadable the moment the map lands.
  // Chained twice: if a drain is already in flight (the mutex returns its
  // promise), the second pass runs AFTER it with the fresh remap — otherwise
  // the event would be swallowed by the stale in-flight drain.
  window.addEventListener("ice-offline-remap-ready", () => {
    drainQueue(api).then(() => drainQueue(api)).catch(() => {});
  });
}
