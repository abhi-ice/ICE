// v7 guided Daily Report flow — persistent VOICE NOTES (spec section D).
//
// Voice-note-as-record: audio is uploaded via the EXISTING backend
// POST /voice/annotate with kind="voice_note" (a note-only kind added to
// backend/routers/voice_router.py that stores the clip WITHOUT running the
// transcription / extraction pipeline — that pipeline stays the separate
// voice-intake feature). Notes are linked to the report via
// linked_entity_type="daily_report" + linked_entity_id, which is exactly the
// filter GET /daily-reports/{id}/context already applies when building
// bundle.voice_annotations — so the list arrives with the normal reload().
//
// Playback: GET /files/audio/{annotation_id} is authenticated, so bytes are
// fetched as a blob through the api client and played via an object URL.
//
// OFFLINE: when the upload fails with no response (offline), the clip is
// queued in a small IndexedDB store ("ice-dr-voice-notes") and drained on
// the next 'online' event. This is a PARALLEL store to lib/drVoiceQueue.js
// on purpose: drVoiceQueue's drainQueue() posts to the voice-intake
// transcribe-and-extract endpoint, which would misroute a plain note.
// (Flagged for the offline agent to consolidate if desired.)
import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, Square, Play, Pause, Loader2, CloudOff, Trash2 } from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

// ---------------- pending-note IndexedDB store ----------------
const DB_NAME = "ice-dr-voice-notes";
const STORE = "pendingNotes";

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
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

async function queuePendingNote({ dailyReportId, blob, mimeType, durationSec }) {
  const db = await openDb();
  const rec = {
    id: `note_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    dailyReportId, blob, mimeType,
    durationSec: durationSec || null,
    createdAt: new Date().toISOString(),
  };
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(rec);
    tx.oncomplete = () => resolve(rec);
    tx.onerror = () => reject(tx.error);
  });
}

export async function listPendingNotes(dailyReportId = null) {
  try {
    const db = await openDb();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const store = tx.objectStore(STORE);
      const req = dailyReportId
        ? store.index("by_dr").getAll(IDBKeyRange.only(dailyReportId))
        : store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  } catch { return []; }
}

async function removePendingNote(id) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function postNote(dailyReportId, blob, mimeType, durationSec) {
  const fd = new FormData();
  const ext = ((mimeType || "audio/webm").split("/")[1] || "webm").split(";")[0];
  fd.append("file", blob, `note_${Date.now()}.${ext}`);
  fd.append("kind", "voice_note");
  fd.append("linked_entity_type", "daily_report");
  fd.append("linked_entity_id", dailyReportId);
  if (durationSec != null) fd.append("duration_sec", String(durationSec));
  await api.post("/voice/annotate", fd, {
    headers: { "Content-Type": "multipart/form-data" }, timeout: 120000,
  });
}

// Drain every queued note (all reports). Returns count synced.
export async function drainPendingNotes() {
  if (typeof navigator !== "undefined" && !navigator.onLine) return 0;
  const pending = await listPendingNotes();
  let synced = 0;
  for (const n of pending) {
    try {
      await postNote(n.dailyReportId, n.blob, n.mimeType, n.durationSec);
      await removePendingNote(n.id);
      synced += 1;
    } catch { /* stays queued */ }
  }
  return synced;
}

// ---------------- UI ----------------
const fmtDur = (s) => {
  const n = Math.max(0, Math.round(Number(s) || 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
};

function NotePlayer({ note }) {
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const audioRef = useRef(null);
  const urlRef = useRef(null);
  useEffect(() => () => {
    try { audioRef.current?.pause(); } catch { /* ignore */ }
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
  }, []);
  const toggle = async () => {
    if (playing) { try { audioRef.current?.pause(); } catch { /* ignore */ } return; }
    if (!audioRef.current) {
      setLoading(true);
      try {
        const res = await api.get(`/files/audio/${note.id}`, { responseType: "blob" });
        urlRef.current = URL.createObjectURL(res.data);
        const a = new Audio(urlRef.current);
        a.onended = () => setPlaying(false);
        a.onpause = () => setPlaying(false);
        a.onplay = () => setPlaying(true);
        audioRef.current = a;
      } catch {
        toast.error(navigator.onLine ? "Audio unavailable." : "Audio needs a connection.");
        setLoading(false);
        return;
      }
      setLoading(false);
    }
    audioRef.current.play().catch(() => toast.error("Playback failed."));
  };
  const when = note.created_at
    ? new Date(note.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : "";
  return (
    <div className="dr-flow-it dr-flow-note-row" data-testid={`dr-flow-note-${note.id}`}>
      <button type="button" className="dr-flow-note-play" onClick={toggle}
        data-testid={`dr-flow-note-play-${note.id}`}
        aria-label={playing ? "Pause" : "Play"}>
        {loading ? <Loader2 className="h-4 w-4 animate-spin" />
          : playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
      </button>
      <span className="dr-flow-note-meta">
        Voice note{note.audio_duration_sec ? ` · ${fmtDur(note.audio_duration_sec)}` : ""}
      </span>
      <span className="dr-flow-note-when">{when}</span>
    </div>
  );
}

export default function DrVoiceNotes({ reportId, notes = [], reload, allowRecord = false, onPendingChange }) {
  const [recState, setRecState] = useState("idle"); // idle | rec | uploading
  const [secs, setSecs] = useState(0);
  const [pending, setPending] = useState([]);
  const mrRef = useRef(null);
  const chunksRef = useRef([]);
  const timerRef = useRef(null);
  const startedAtRef = useRef(0);

  const refreshPending = useCallback(async () => {
    const rows = await listPendingNotes(reportId);
    setPending(rows);
    onPendingChange?.(rows.length);
  }, [reportId, onPendingChange]);

  useEffect(() => { refreshPending(); }, [refreshPending]);
  useEffect(() => {
    const drain = async () => {
      const n = await drainPendingNotes();
      if (n > 0) { toast.success(`${n} voice note${n === 1 ? "" : "s"} uploaded.`); reload?.(); }
      refreshPending();
    };
    window.addEventListener("online", drain);
    drain();
    return () => window.removeEventListener("online", drain);
  }, [refreshPending, reload]);

  const upload = useCallback(async (blob, mimeType, durationSec) => {
    try {
      await postNote(reportId, blob, mimeType, durationSec);
      toast.success("Voice note saved.");
      reload?.();
    } catch (e) {
      if (!e.response) {
        try {
          await queuePendingNote({ dailyReportId: reportId, blob, mimeType, durationSec });
          toast("Offline — voice note will upload when you reconnect.");
          refreshPending();
        } catch { toast.error("Could not store the note offline."); }
      } else {
        toast.error(e.response?.data?.detail || "Voice note upload failed.");
      }
    }
  }, [reportId, reload, refreshPending]);

  const stopRecording = () => {
    try {
      if (mrRef.current && mrRef.current.state !== "inactive") mrRef.current.stop();
    } catch { /* ignore */ }
  };

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : (MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "");
      const mr = mimeType
        ? new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 128000 })
        : new MediaRecorder(stream, { audioBitsPerSecond: 128000 });
      chunksRef.current = [];
      mr.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksRef.current.push(e.data); };
      mr.onstop = async () => {
        try { stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
        clearInterval(timerRef.current);
        const blob = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
        const durationSec = Math.round((Date.now() - startedAtRef.current) / 1000);
        setRecState("uploading");
        if (blob.size > 0) await upload(blob, mr.mimeType || "audio/webm", durationSec);
        setRecState("idle");
        setSecs(0);
      };
      mr.start(1000);
      mrRef.current = mr;
      startedAtRef.current = Date.now();
      setSecs(0);
      setRecState("rec");
      timerRef.current = setInterval(() => {
        setSecs((s) => {
          const next = s + 1;
          if (next >= 600) stopRecording(); // 10-min cap
          return next;
        });
      }, 1000);
    } catch {
      toast.error("Microphone unavailable — check permissions.");
    }
  };

  useEffect(() => () => {
    try { if (mrRef.current && mrRef.current.state !== "inactive") mrRef.current.stop(); } catch { /* ignore */ }
    clearInterval(timerRef.current);
  }, []);

  return (
    <div className="dr-flow-voice" data-testid="dr-flow-voice-notes">
      {allowRecord && (
        <button type="button"
          data-testid="dr-flow-voice-record"
          className={`dr-flow-ghost dr-flow-voice-rec${recState === "rec" ? " rec" : ""}`}
          disabled={recState === "uploading"}
          onClick={() => (recState === "rec" ? stopRecording() : startRecording())}>
          {recState === "rec"
            ? <><Square className="h-4 w-4" /> Stop · {fmtDur(secs)}</>
            : recState === "uploading"
              ? <><Loader2 className="h-4 w-4 animate-spin" /> Saving…</>
              : <><Mic className="h-4 w-4" /> Add voice note</>}
        </button>
      )}
      {(notes.length > 0 || pending.length > 0) && (
        <div className="dr-flow-items" data-testid="dr-flow-voice-list">
          {pending.map((p) => (
            <div key={p.id} className="dr-flow-it dr-flow-note-row pend" data-testid={`dr-flow-note-pending-${p.id}`}>
              <span className="dr-flow-note-play"><CloudOff className="h-4 w-4" /></span>
              <span className="dr-flow-note-meta">
                Voice note{p.durationSec ? ` · ${fmtDur(p.durationSec)}` : ""}
              </span>
              <span className="pill pill-amber">will upload</span>
              <button type="button" className="dr-flow-note-del" aria-label="Discard queued note"
                onClick={async () => { await removePendingNote(p.id); refreshPending(); }}>
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          {notes.map((n) => <NotePlayer key={n.id} note={n} />)}
        </div>
      )}
    </div>
  );
}
