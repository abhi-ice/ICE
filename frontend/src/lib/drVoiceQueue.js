/**
 * IndexedDB voice-queue helpers for offline DR intake.
 *
 * 2026-05-25 — supports Phase A of the multi-modal DR voice feature.
 * 2026-07-05 — v7 guided flow (spec D + F.5): the same store now also
 *   queues persistent VOICE NOTES. A record's `target` field routes the
 *   drain:
 *     target absent / "extract"  -> POST /daily-reports/{id}/voice/
 *                                   transcribe-and-extract  (legacy intake)
 *     target "annotate"          -> POST /voice/annotate    (voice-note-as-
 *                                   record; multipart field name is `file`,
 *                                   plus kind + linked_entity_type/id — see
 *                                   backend/routers/voice_router.py:14-49)
 *   Use queueVoiceNote(...) for the new notes; queueClip(...) keeps its
 *   original signature for VoiceIntakePanel.
 *
 * Storage layout
 *   DB:     ice-dr-voice
 *   Store:  pendingClips  (key: auto-generated id)
 *   Schema: { id, dailyReportId, blob, mimeType, durationMs,
 *             createdAt, retryCount, lastError,
 *             target?, kind?, linkedEntityType?, linkedEntityId? }
 *
 * 30 minute clip cap is enforced at the recorder boundary (MediaRecorder
 * stop() is called by the component); we only refuse oversized blobs
 * here as a final guard.
 *
 * Queue/remove dispatch window CustomEvent "ice-offline-queue-changed" so
 * the flow landing's "N items will sync" chip can live-update.
 */

const DB_NAME = "ice-dr-voice";
const DB_VERSION = 1;
const STORE = "pendingClips";
const MAX_BLOB_BYTES = 30 * 1024 * 1024;   // 30 MB upper bound

function notifyQueueChanged() {
  try {
    window.dispatchEvent(new CustomEvent("ice-offline-queue-changed", {
      detail: { source: "voice" },
    }));
  } catch { /* non-window env */ }
}

// Hardened like drPhotoQueue (2026-07-06): cached connection, one retry on
// a failed open (iOS Safari flake right after a network flip / app switch),
// self-heal when Safari closes the connection, and a last-resort in-session
// memory queue so a recording is NEVER refused.
function openDbOnce() {
  return new Promise((resolve, reject) => {
    let req;
    try {
      req = indexedDB.open(DB_NAME, DB_VERSION);
    } catch (e) {
      return reject(e);
    }
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

let _dbPromise = null;
function openDb() {
  if (_dbPromise) return _dbPromise;
  const p = openDbOnce().catch(
    () => new Promise((r) => setTimeout(r, 300)).then(() => openDbOnce()),
  );
  _dbPromise = p;
  p.then((db) => {
    try {
      // Identity-guarded resets — a stale handler must never wipe a newer
      // healthy connection (reopen churn).
      db.onclose = () => { if (_dbPromise === p) _dbPromise = null; };
      db.onversionchange = () => {
        try { db.close(); } catch { /* already closing */ }
        if (_dbPromise === p) _dbPromise = null;
      };
    } catch { /* older engines */ }
  }).catch(() => { if (_dbPromise === p) _dbPromise = null; });
  return p;
}

// Safari can kill a connection WITHOUT firing onclose — every operation's
// failure path drops the cache so the next call reopens fresh.
function healDb() { _dbPromise = null; }

// Bounded in-session memory fallback (see drPhotoQueue for the rationale).
const _memPending = [];
const MEM_MAX_BYTES = 150 * 1024 * 1024;
let _memBytes = 0;

async function _idbPut(record) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(record);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("tx aborted"));
  });
}

export async function queueClip({
  dailyReportId, blob, mimeType, durationMs,
  // v7 voice notes (optional; absent for legacy intake clips):
  target = null, kind = null, linkedEntityType = null, linkedEntityId = null,
}) {
  if (!blob || !(blob instanceof Blob)) throw new Error("blob required");
  if (blob.size > MAX_BLOB_BYTES) {
    throw new Error(`recording too large (${blob.size} > ${MAX_BLOB_BYTES}); record a shorter clip`);
  }
  const id = `clip_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const record = {
    id, dailyReportId, blob, mimeType,
    durationMs: durationMs || null,
    createdAt: new Date().toISOString(),
    retryCount: 0, lastError: null,
    target, kind, linkedEntityType, linkedEntityId,
  };
  try {
    await _idbPut(record);
  } catch {
    healDb(); // heal a dead cached connection, retry fresh
    try {
      await _idbPut(record);
    } catch {
      if (_memBytes + (blob.size || 0) > MEM_MAX_BYTES) {
        throw new Error(
          "offline storage is unavailable and session memory is full — reconnect to upload recordings",
        );
      }
      record._memoryOnly = true; // storage blocked — keep it this session
      _memPending.push(record);
      _memBytes += blob.size || 0;
    }
  }
  notifyQueueChanged();
  return record;
}

/**
 * v7 guided flow (spec D): queue a persistent VOICE NOTE recorded offline.
 * Drains to POST /voice/annotate linked to the daily report.
 *
 * kind MUST be "voice_note": the backend voice_annotate handler ONLY skips
 * Whisper transcription + field extraction for kind=="voice_note" (see
 * backend/routers/voice_router.py:33). Any other kind — including
 * "work_item" — falls through to process_voice_annotation and runs the full
 * transcription/extraction pipeline, which spec D forbids for these notes.
 * Returns the stored record.
 */
export function queueVoiceNote({ dailyReportId, blob, mimeType, durationMs }) {
  if (!dailyReportId) throw new Error("dailyReportId required");
  return queueClip({
    dailyReportId, blob, mimeType, durationMs,
    target: "annotate",
    kind: "voice_note",
    linkedEntityType: "daily_report",
    linkedEntityId: dailyReportId,
  });
}

/** Pending voice NOTES only (target "annotate"), optionally per report. */
export async function listPendingNotes(dailyReportId = null) {
  const all = await listPending(dailyReportId);
  return all.filter((c) => c.target === "annotate");
}

async function _idbList(dailyReportId) {
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

export async function listPending(dailyReportId = null) {
  let rows = [];
  try {
    rows = await _idbList(dailyReportId);
  } catch {
    healDb(); // dead cached connection: heal + retry so the queue can't go invisible
    try { rows = await _idbList(dailyReportId); } catch { rows = []; }
  }
  const mem = dailyReportId
    ? _memPending.filter((r) => r.dailyReportId === dailyReportId)
    : [..._memPending];
  return [...rows, ...mem];
}

async function _idbDelete(id) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function removeClip(id) {
  const i = _memPending.findIndex((r) => r.id === id);
  if (i >= 0) {
    _memBytes -= (_memPending[i].blob && _memPending[i].blob.size) || 0;
    if (_memBytes < 0) _memBytes = 0;
    _memPending.splice(i, 1);
  }
  try {
    await _idbDelete(id);
  } catch {
    // Heal + retry — a swallowed delete would re-post this clip on every
    // later drain (duplicate voice-note rows server-side).
    healDb();
    try { await _idbDelete(id); } catch { /* memory-only entry or IDB dead */ }
  }
  notifyQueueChanged();
}

export async function markClipFailed(id, errMsg) {
  const mem = _memPending.find((r) => r.id === id);
  if (mem) {
    mem.retryCount = (mem.retryCount || 0) + 1;
    mem.lastError = String(errMsg || "").slice(0, 500);
    return;
  }
  try {
    const db = await openDb();
    await new Promise((resolve, reject) => {
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
  } catch {
    healDb(); // bookkeeping only — but heal so the NEXT operation recovers
  }
}

/**
 * Background sync routine. Drains all queued clips. Each clip posts to its
 * own target endpoint:
 *   - target "annotate" (v7 voice notes) -> POST /voice/annotate
 *     (multipart field `file` + kind + linked_entity_type/id)
 *   - otherwise (legacy intake clips)    -> POST /daily-reports/{drId}/voice/
 *     transcribe-and-extract (multipart field `audio`)
 * Caller passes their authenticated axios `api` instance so we don't
 * re-implement auth here.
 *
 * On success: clip is removed from IndexedDB.
 * On error:   clip is left in place with incremented retryCount.
 *
 * MUTEX-GUARDED: VoiceIntakePanel auto-drains on reconnect AND api.js wires
 * a global 'online' drain; concurrent calls share one in-flight drain so a
 * clip can never upload twice.
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
  // Offline-created DRs (2026-07-06): clips recorded against a provisional
  // "offline-dr-*" id can only upload after the request queue's drain has
  // created the real report and recorded the id mapping.
  const { resolveOfflineId, isUnresolvedOfflineId, isPermanentRejection } = await import("./drOfflineSync");
  const pending = await listPending();
  let synced = 0, failed = 0;
  for (const clip of pending) {
    // No server mapping yet: leave it queued — the remap-ready event or the
    // next 'online' drain retries once the DR create has replayed.
    if (isUnresolvedOfflineId(clip.dailyReportId)) continue;
    const drId = resolveOfflineId(clip.dailyReportId);
    try {
      const ext = (clip.mimeType || "audio/webm").split("/")[1] || "webm";
      const fd = new FormData();
      if (clip.target === "annotate") {
        // v7 voice note — voice_router.voice_annotate expects `file`.
        fd.append("file", clip.blob, `voice_note_${clip.id}.${ext}`);
        fd.append("kind", clip.kind || "work_item");
        if (clip.linkedEntityType && clip.linkedEntityId) {
          fd.append("linked_entity_type", clip.linkedEntityType);
          fd.append("linked_entity_id", resolveOfflineId(clip.linkedEntityId));
        }
        await api.post("/voice/annotate", fd, {
          headers: { "Content-Type": "multipart/form-data" }, timeout: 180000,
        });
      } else {
        fd.append("audio", clip.blob, `voice_${clip.id}.${ext}`);
        await api.post(
          `/daily-reports/${drId}/voice/transcribe-and-extract`,
          fd,
          { headers: { "Content-Type": "multipart/form-data" }, timeout: 180000 },
        );
      }
      await removeClip(clip.id);
      synced += 1;
    } catch (e) {
      failed += 1;
      await markClipFailed(clip.id, e?.response?.data?.detail || e?.message);
      if (!e?.response) {
        // Network-only: still offline — keep everything, stop draining
        // (hammering the rest of the queue is pointless). Mirrors the
        // photo queue's policy.
        break;
      }
      // Deterministic server rejection (404/413/422 — e.g. the target DR
      // was deleted): drop the clip so the 60s heartbeat doesn't re-upload
      // a multi-megabyte blob every minute forever. 401/403 are kept —
      // auth comes back after re-login. Day-1 fix (2026-10-25, finding 35):
      // only a genuine JSON rejection from the app counts; 408/425/429 and
      // non-JSON replies (the HTML 404 of a sleeping backend) are kept.
      const permanent = isPermanentRejection(e);
      if (permanent) {
        await removeClip(clip.id).catch(() => {});
      }
    }
  }
  const remaining = (await listPending()).length;
  return { synced, failed, remaining };
}

// v7 full-offline — register a single global reconnect drain (mirrors
// drPhotoQueue.wireAutoDrain). Called once from lib/api.js with the
// authenticated client; idempotent.
let _wired = false;
export function wireAutoDrain(api) {
  if (_wired) return;
  _wired = true;
  window.addEventListener("online", () => {
    drainQueue(api).catch(() => {});
  });
  // Re-drain when the request queue resolves offline-created DR ids: clips
  // skipped as "unresolved" above become uploadable once the map lands.
  // Chained twice so an already-in-flight drain (whose pass predates the
  // remap) can't swallow the event — the second pass sees the fresh map.
  window.addEventListener("ice-offline-remap-ready", () => {
    drainQueue(api).then(() => drainQueue(api)).catch(() => {});
  });
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
