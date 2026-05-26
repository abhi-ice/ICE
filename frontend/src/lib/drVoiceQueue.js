/**
 * IndexedDB voice-queue helpers for offline DR intake.
 *
 * 2026-05-25 — supports Phase A of the multi-modal DR voice feature.
 *
 * Storage layout
 *   DB:     ice-dr-voice
 *   Store:  pendingClips  (key: auto-generated id)
 *   Schema: { id, dailyReportId, blob, mimeType, durationMs,
 *             createdAt, retryCount, lastError }
 *
 * 30 minute clip cap is enforced at the recorder boundary (MediaRecorder
 * stop() is called by the component); we only refuse oversized blobs
 * here as a final guard.
 */

const DB_NAME = "ice-dr-voice";
const DB_VERSION = 1;
const STORE = "pendingClips";
const MAX_BLOB_BYTES = 30 * 1024 * 1024;   // 30 MB upper bound

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

export async function queueClip({ dailyReportId, blob, mimeType, durationMs }) {
  if (!blob || !(blob instanceof Blob)) throw new Error("blob required");
  if (blob.size > MAX_BLOB_BYTES) {
    throw new Error(`recording too large (${blob.size} > ${MAX_BLOB_BYTES}); record a shorter clip`);
  }
  const db = await openDb();
  const id = `clip_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const record = {
    id, dailyReportId, blob, mimeType,
    durationMs: durationMs || null,
    createdAt: new Date().toISOString(),
    retryCount: 0, lastError: null,
  };
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(record);
    tx.oncomplete = () => resolve(record);
    tx.onerror = () => reject(tx.error);
  });
}

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

export async function removeClip(id) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function markClipFailed(id, errMsg) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    const getReq = store.get(id);
    getReq.onsuccess = () => {
      const r = getReq.result;
      if (!r) return resolve();
      r.retryCount = (r.retryCount || 0) + 1;
      r.lastError = String(errMsg || "").slice(0, 500);
      store.put(r);
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/**
 * Background sync routine. Drains all queued clips by posting them to
 * /api/daily-reports/{drId}/voice/transcribe-and-extract.
 * Caller passes their authenticated axios `api` instance so we don't
 * re-implement auth here.
 *
 * On success: clip is removed from IndexedDB.
 * On error:   clip is left in place with incremented retryCount.
 *
 * Returns { synced, failed, remaining }.
 */
export async function drainQueue(api) {
  if (!navigator.onLine) return { synced: 0, failed: 0, remaining: -1 };
  const pending = await listPending();
  let synced = 0, failed = 0;
  for (const clip of pending) {
    try {
      const fd = new FormData();
      fd.append(
        "audio",
        clip.blob,
        `voice_${clip.id}.${(clip.mimeType || "audio/webm").split("/")[1] || "webm"}`,
      );
      await api.post(
        `/daily-reports/${clip.dailyReportId}/voice/transcribe-and-extract`,
        fd,
        { headers: { "Content-Type": "multipart/form-data" }, timeout: 180000 },
      );
      await removeClip(clip.id);
      synced += 1;
    } catch (e) {
      failed += 1;
      await markClipFailed(clip.id, e?.response?.data?.detail || e?.message);
    }
  }
  const remaining = (await listPending()).length;
  return { synced, failed, remaining };
}

/**
 * localStorage manual-draft cache for Route 4 (offline manual entry).
 * Keys: `ice-dr-draft-{dailyReportId}` → JSON
 * Stored on every form edit; cleared after a successful save online.
 */
const DRAFT_PREFIX = "ice-dr-draft-";
export function saveOfflineDraft(dailyReportId, patch) {
  try {
    const k = DRAFT_PREFIX + dailyReportId;
    const existing = JSON.parse(localStorage.getItem(k) || "{}");
    const next = { ...existing, ...patch, _savedAt: new Date().toISOString() };
    localStorage.setItem(k, JSON.stringify(next));
    return next;
  } catch { return null; }
}
export function readOfflineDraft(dailyReportId) {
  try { return JSON.parse(localStorage.getItem(DRAFT_PREFIX + dailyReportId) || "null"); }
  catch { return null; }
}
export function clearOfflineDraft(dailyReportId) {
  try { localStorage.removeItem(DRAFT_PREFIX + dailyReportId); } catch { /* ignore */ }
}
