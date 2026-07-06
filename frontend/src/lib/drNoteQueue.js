/**
 * Persistent VOICE-NOTE offline queue (2026-07-06, round 7).
 *
 * Extracted from components/dr/DrVoiceNotes.jsx so it can be drained GLOBALLY
 * (api.js — on load, on reconnect, and on the 60s heartbeat) instead of only
 * while that component happens to be mounted. The field bug this fixes: a
 * super records an audio note offline, backs out to the DR list (the notes
 * component unmounts), then reconnects — the 'online' event had no listener,
 * so the note never synced and never appeared.
 *
 * Storage: IndexedDB "ice-dr-voice-notes" (SAME db/store as before, so notes
 * already queued keep draining). Hardened like lib/drPhotoQueue: cached
 * identity-guarded connection, retry-once open, heal-on-failure everywhere,
 * bounded in-session memory fallback — a recording is never refused.
 *
 * Notes post to POST /voice/annotate with kind="voice_note" (note-only; NO
 * transcription pipeline). drainNotes(api) is DEPENDENCY-INJECTED with the
 * authenticated axios client so this module never imports lib/api (avoids the
 * api.js <-> component import cycle). MUTEX-guarded so the component drain,
 * the reconnect drain, and the heartbeat can't double-post a note.
 */
import { resolveOfflineId, isUnresolvedOfflineId } from "./drOfflineSync";

const DB_NAME = "ice-dr-voice-notes";
const STORE = "pendingNotes";
const MEM_MAX_BYTES = 150 * 1024 * 1024;

function notifyQueueChanged() {
  try {
    window.dispatchEvent(new CustomEvent("ice-offline-queue-changed", {
      detail: { source: "notes" },
    }));
  } catch { /* non-window env */ }
}

function openDbOnce() {
  return new Promise((resolve, reject) => {
    let req;
    try {
      req = indexedDB.open(DB_NAME, 1);
    } catch (e) {
      return reject(e);
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const s = db.createObjectStore(STORE, { keyPath: "id" });
        s.createIndex("by_dr", "dailyReportId", { unique: false });
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
      db.onclose = () => { if (_dbPromise === p) _dbPromise = null; };
      db.onversionchange = () => {
        try { db.close(); } catch { /* already closing */ }
        if (_dbPromise === p) _dbPromise = null;
      };
    } catch { /* older engines */ }
  }).catch(() => { if (_dbPromise === p) _dbPromise = null; });
  return p;
}
function healDb() { _dbPromise = null; }

const _memPending = [];
let _memBytes = 0;

async function _idbPut(rec) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(rec);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("tx aborted"));
  });
}

export async function queuePendingNote({ dailyReportId, blob, mimeType, durationSec }) {
  if (!blob || !(blob instanceof Blob)) throw new Error("blob required");
  const size = blob.size || 0;
  // Store raw BYTES (ArrayBuffer) — iOS Safari can refuse to clone Blobs; the
  // ArrayBuffer clones reliably. Audio blobs are memory-backed so this is
  // belt-and-suspenders parity with the photo queue (round 6).
  let bytes = null;
  try { bytes = await blob.arrayBuffer(); } catch { bytes = null; }
  const rec = {
    id: `note_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    dailyReportId,
    bytes,
    blob: bytes ? null : blob,
    _size: size,
    mimeType,
    durationSec: durationSec || null,
    createdAt: new Date().toISOString(),
  };
  try {
    await _idbPut(rec);
  } catch {
    healDb();
    try {
      await _idbPut(rec);
    } catch {
      if (_memBytes + size > MEM_MAX_BYTES) {
        throw new Error(
          "offline storage is unavailable and session memory is full — reconnect to upload recordings",
        );
      }
      rec._memoryOnly = true;
      _memPending.push(rec);
      _memBytes += size;
    }
  }
  notifyQueueChanged();
  return rec;
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

export async function listPendingNotes(dailyReportId = null) {
  let rows = [];
  try {
    rows = await _idbList(dailyReportId);
  } catch {
    healDb();
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

export async function removePendingNote(id) {
  const i = _memPending.findIndex((r) => r.id === id);
  if (i >= 0) {
    _memBytes -= _memPending[i]._size || (_memPending[i].blob && _memPending[i].blob.size) || 0;
    if (_memBytes < 0) _memBytes = 0;
    _memPending.splice(i, 1);
  }
  try {
    await _idbDelete(id);
  } catch {
    healDb(); // a swallowed delete would re-post the note (duplicates)
    try { await _idbDelete(id); } catch { /* memory-only or IDB dead */ }
  }
  notifyQueueChanged();
}

/** POST one note to /voice/annotate. `api` is the authenticated axios client
 *  (injected so this module never imports lib/api). Throws on failure. */
export async function postNote(api, dailyReportId, body, mimeType, durationSec) {
  const fd = new FormData();
  const ext = ((mimeType || "audio/webm").split("/")[1] || "webm").split(";")[0];
  const blob = body instanceof Blob
    ? body
    : new Blob([body], { type: mimeType || "audio/webm" });
  fd.append("file", blob, `note_${Date.now()}.${ext}`);
  fd.append("kind", "voice_note");
  fd.append("linked_entity_type", "daily_report");
  fd.append("linked_entity_id", dailyReportId);
  if (durationSec != null) fd.append("duration_sec", String(durationSec));
  await api.post("/voice/annotate", fd, {
    headers: { "Content-Type": "multipart/form-data" }, timeout: 120000,
  });
}

/** Drain every queued note (all reports). MUTEX-guarded. Returns count synced. */
let _drainInFlight = null;
export function drainNotes(api) {
  if (_drainInFlight) return _drainInFlight;
  _drainInFlight = _drainAll(api).finally(() => { _drainInFlight = null; });
  return _drainInFlight;
}

async function _drainAll(api) {
  if (typeof navigator !== "undefined" && !navigator.onLine) return 0;
  const pending = await listPendingNotes();
  let synced = 0;
  for (const n of pending) {
    // Provisional report id with no server mapping yet: WAIT — posting now
    // would orphan the note against a report id that will never exist.
    if (isUnresolvedOfflineId(n.dailyReportId)) continue;
    try {
      await postNote(api, resolveOfflineId(n.dailyReportId),
        n.bytes || n.blob, n.mimeType, n.durationSec);
      await removePendingNote(n.id);
      synced += 1;
    } catch (e) {
      // Network error: still offline — stop (rest will retry). A definitive
      // 4xx (the DR was deleted) drops the note so it can't loop forever;
      // 401/403 keep it (auth returns after re-login).
      const status = e?.response?.status;
      if (!e?.response) break;
      if (status >= 400 && status < 500 && status !== 401 && status !== 403) {
        await removePendingNote(n.id).catch(() => {});
      }
    }
  }
  if (synced > 0) notifyQueueChanged();
  return synced;
}

/** Register the global reconnect + remap-ready drains once. */
let _wired = false;
export function wireAutoDrain(api) {
  if (_wired) return;
  _wired = true;
  try {
    window.addEventListener("online", () => { drainNotes(api).catch(() => {}); });
    window.addEventListener("ice-offline-remap-ready", () => { drainNotes(api).catch(() => {}); });
  } catch { /* non-window env */ }
}
