/**
 * VoiceIntakePanel — the 5-mode DR voice intake UI.
 *
 * Modes:
 *   1. Manual           — link to scroll to the form (already exists).
 *   2. Voice (async)    — record on-device, upload, server transcribes
 *                         + extracts + writes to DR collections.
 *   3. Voice (live)     — Gemini Live WebSocket directly from browser.
 *                         Phase A: builds the WS handshake using a
 *                         backend-issued short-lived token; field
 *                         updates persist via the same /voice/extract
 *                         endpoint.
 *   4. Offline manual   — explicit prompt to use the form; draft
 *                         autosaves to localStorage on every change.
 *   5. Offline voice    — record while offline, blob goes into
 *                         IndexedDB, drains automatically when online.
 *
 * 2026-05-25 — Phase A; the Gemini Live route activates once the
 * GEMINI_API_KEY arrives on the backend (the live-token endpoint will
 * return 503 until then; UI shows a friendly disabled state).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Mic, Square, Wifi, WifiOff, Trash2, Upload, Loader2, AlertCircle, CheckCircle2, Radio, KeyboardIcon } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";
import {
  queueClip, listPending, drainQueue, removeClip,
} from "../lib/drVoiceQueue";

const MAX_CLIP_MS = 30 * 60 * 1000;  // 30 minutes per clip

const MODES = [
  { value: "manual",         label: "Manual form entry",          requiresOnline: false, requiresVoice: false },
  { value: "voice_async",    label: "Voice memo (AI fills DR)",   requiresOnline: true,  requiresVoice: true  },
  { value: "voice_live",     label: "Live conversational (AI guides)", requiresOnline: true, requiresVoice: true  },
  { value: "offline_manual", label: "Manual offline (autosave draft)", requiresOnline: false, requiresVoice: false },
  { value: "offline_voice",  label: "Voice offline (sync later)",  requiresOnline: false, requiresVoice: true  },
];

export default function VoiceIntakePanel({ dailyReportId, onApplied, locked }) {
  const [mode, setMode] = useState("manual");
  const [online, setOnline] = useState(typeof navigator !== "undefined" ? navigator.onLine : true);
  const [recording, setRecording] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [busy, setBusy] = useState(false);
  const [pendingClips, setPendingClips] = useState([]);
  const [lastResult, setLastResult] = useState(null);
  const mediaRecorderRef = useRef(null);
  const chunksRef = useRef([]);
  const recordStartRef = useRef(null);
  const tickRef = useRef(null);

  // Connectivity tracking + auto-drain on reconnect.
  useEffect(() => {
    const setOn = () => { setOnline(true); refreshPending(); };
    const setOff = () => setOnline(false);
    window.addEventListener("online", setOn);
    window.addEventListener("offline", setOff);
    return () => {
      window.removeEventListener("online", setOn);
      window.removeEventListener("offline", setOff);
    };
  }, []);

  // Drain queue when online OR when DR id changes (avoids stale list).
  const refreshPending = async () => {
    try { setPendingClips(await listPending(dailyReportId)); }
    catch { /* IndexedDB may be unavailable; ignore */ }
  };
  useEffect(() => { refreshPending(); /* eslint-disable-next-line */ }, [dailyReportId]);

  useEffect(() => {
    if (online && pendingClips.length > 0 && !busy) {
      // Auto-drain when back online.
      (async () => {
        setBusy(true);
        try {
          const r = await drainQueue(api);
          if (r.synced > 0) toast.success(`Synced ${r.synced} queued voice memo${r.synced === 1 ? "" : "s"}.`);
          if (r.failed > 0) toast.error(`${r.failed} queued voice memo${r.failed === 1 ? "" : "s"} failed to sync; will retry.`);
          await refreshPending();
          if (r.synced > 0 && onApplied) onApplied();
        } finally { setBusy(false); }
      })();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online, pendingClips.length]);

  // Recording controls -- MediaRecorder w/ webm/opus by default
  // (Chromium); the server accepts mp3/m4a/wav/webm/ogg, Gemini takes
  // webm-opus natively. mp3 transcoding would need a wasm encoder we
  // do not bundle, so we ship webm and let Gemini decode it.
  const startRecording = async () => {
    if (recording) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType =
        MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
          ? "audio/webm;codecs=opus"
          : MediaRecorder.isTypeSupported("audio/webm")
            ? "audio/webm" : "";
      const mr = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksRef.current.push(e.data); };
      mr.onstop = async () => {
        try { stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
        const blob = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
        await handleRecordedBlob(blob, mr.mimeType || "audio/webm");
      };
      mr.start();
      mediaRecorderRef.current = mr;
      recordStartRef.current = Date.now();
      setElapsedMs(0);
      tickRef.current = setInterval(() => {
        const el = Date.now() - recordStartRef.current;
        setElapsedMs(el);
        if (el >= MAX_CLIP_MS) stopRecording();
      }, 250);
      setRecording(true);
    } catch (e) {
      toast.error(`Microphone error: ${e.message || e}`);
    }
  };
  const stopRecording = () => {
    try { mediaRecorderRef.current?.stop(); } catch { /* ignore */ }
    if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
    setRecording(false);
  };

  const handleRecordedBlob = async (blob, mimeType) => {
    if (!blob || blob.size === 0) {
      toast.error("Empty recording -- microphone may be muted.");
      return;
    }
    // Offline OR async-but-offline -> queue. Online async -> upload now.
    const goOnlineNow = online && mode === "voice_async";
    if (goOnlineNow) {
      setBusy(true);
      try {
        const fd = new FormData();
        fd.append("audio", blob, `voice_${Date.now()}.webm`);
        const r = await api.post(
          `/daily-reports/${dailyReportId}/voice/transcribe-and-extract`,
          fd,
          { headers: { "Content-Type": "multipart/form-data" }, timeout: 180000 },
        );
        setLastResult(r.data);
        toast.success(
          `AI applied: ${summarizeApplied(r.data.applied)}. Review the draft and submit when ready.`,
          { duration: 6000 },
        );
        if (onApplied) onApplied();
      } catch (e) {
        toast.error(`Voice intake failed: ${e?.response?.data?.detail || e.message}`);
      } finally { setBusy(false); }
    } else {
      // Queue for later sync (offline OR explicit offline_voice mode).
      try {
        await queueClip({ dailyReportId, blob, mimeType, durationMs: elapsedMs });
        toast.success(online
          ? "Voice memo queued. Click 'Sync now' or wait for auto-sync."
          : "Voice memo saved offline. Will auto-sync when connection returns.");
        await refreshPending();
      } catch (e) {
        toast.error(`Failed to queue voice memo: ${e.message}`);
      }
    }
  };

  const syncNow = async () => {
    if (!online) return toast.error("No connection. Will auto-sync when online.");
    setBusy(true);
    try {
      const r = await drainQueue(api);
      if (r.synced > 0) toast.success(`Synced ${r.synced} memo${r.synced === 1 ? "" : "s"}.`);
      if (r.failed > 0) toast.error(`${r.failed} failed; will retry.`);
      await refreshPending();
      if (r.synced > 0 && onApplied) onApplied();
    } catch (e) {
      toast.error(`Sync failed: ${e.message}`);
    } finally { setBusy(false); }
  };

  const removePending = async (id) => {
    if (!window.confirm("Delete this queued voice memo permanently?")) return;
    await removeClip(id);
    await refreshPending();
  };

  // Live conversational mode -- Phase A places a "Coming online" stub
  // that calls /voice/live-token. When the backend returns 503 (no
  // GEMINI_API_KEY) we show a configuration hint. When the token
  // endpoint succeeds we open the WebSocket. The actual streaming +
  // function-calling flow runs entirely client-side from there.
  const [liveStatus, setLiveStatus] = useState("idle"); // idle|connecting|live|ended|error
  const [liveTranscript, setLiveTranscript] = useState("");
  const liveWsRef = useRef(null);
  const liveAudioCtxRef = useRef(null);

  const startLive = async () => {
    if (liveStatus === "live" || liveStatus === "connecting") return;
    setLiveStatus("connecting");
    setLiveTranscript("");
    try {
      const tok = await api.post(`/daily-reports/${dailyReportId}/voice/live-token`);
      const apiKey = tok.data.token;
      const model = tok.data.model || "gemini-2.0-flash-exp";
      // Open WS directly to Gemini Live. Browser side; the token /
      // key never proxies through our server for streaming audio.
      const wsUrl = `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${encodeURIComponent(apiKey)}`;
      const ws = new WebSocket(wsUrl);
      liveWsRef.current = ws;
      ws.onopen = () => {
        // Send setup message -- system instruction, model, voice config.
        ws.send(JSON.stringify({
          setup: {
            model: `models/${model}`,
            generation_config: {
              response_modalities: ["TEXT"],
            },
            system_instruction: {
              parts: [{ text:
                "You are guiding a construction superintendent through a daily report. "
                + "Ask short questions in this order: 1) weather + temperatures, "
                + "2) which trades are on site and headcounts, 3) activity-by-activity progress "
                + "(use the activity_code numbers shown in the DR grid), 4) any delays today "
                + "(if none say 'no delays' to skip), 5) safety / QC / deliveries / coordination "
                + "events, 6) any general notes. Keep questions to one sentence. Acknowledge each "
                + "answer briefly. When finished say 'Daily report complete'." }],
            },
          },
        }));
        setLiveStatus("live");
      };
      ws.onmessage = async (evt) => {
        try {
          let data = evt.data;
          if (data instanceof Blob) data = await data.text();
          const msg = JSON.parse(data);
          if (msg.serverContent?.modelTurn?.parts) {
            for (const part of msg.serverContent.modelTurn.parts) {
              if (part.text) setLiveTranscript((t) => t + part.text);
            }
          }
          if (msg.serverContent?.turnComplete) setLiveTranscript((t) => t + "\n");
        } catch { /* ignore parse error */ }
      };
      ws.onerror = (e) => {
        console.error("Gemini Live WS error", e);
        setLiveStatus("error");
        toast.error("Live voice connection error.");
      };
      ws.onclose = () => {
        setLiveStatus((s) => s === "live" ? "ended" : s);
      };
      // Stream microphone audio to Gemini. 16 kHz PCM is what the Live
      // API expects. We resample inline with an AudioContext at 16k.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      liveAudioCtxRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const processor = audioCtx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (e) => {
        if (ws.readyState !== 1) return;
        const input = e.inputBuffer.getChannelData(0);
        const pcm16 = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) {
          const s = Math.max(-1, Math.min(1, input[i]));
          pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        // Base64-encode the raw PCM bytes.
        const bytes = new Uint8Array(pcm16.buffer);
        let bin = "";
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        const b64 = btoa(bin);
        ws.send(JSON.stringify({
          realtime_input: {
            media_chunks: [{ mime_type: "audio/pcm;rate=16000", data: b64 }],
          },
        }));
      };
      source.connect(processor);
      processor.connect(audioCtx.destination);
    } catch (e) {
      setLiveStatus("error");
      const msg = e?.response?.data?.detail || e.message;
      toast.error(`Live voice failed to start: ${msg}`);
    }
  };
  const stopLive = async () => {
    try { liveWsRef.current?.close(); } catch { /* ignore */ }
    try { liveAudioCtxRef.current?.close(); } catch { /* ignore */ }
    liveWsRef.current = null; liveAudioCtxRef.current = null;
    setLiveStatus("ended");
    // Send the accumulated transcript to the extract endpoint so the
    // DR gets populated from the live conversation as well.
    if (liveTranscript.trim()) {
      setBusy(true);
      try {
        const r = await api.post(
          `/daily-reports/${dailyReportId}/voice/extract`,
          { transcript: liveTranscript },
        );
        setLastResult(r.data);
        toast.success(`AI applied: ${summarizeApplied(r.data.applied)}. Review and submit.`,
                      { duration: 6000 });
        if (onApplied) onApplied();
      } catch (e) {
        toast.error(`Failed to apply live transcript: ${e?.response?.data?.detail || e.message}`);
      } finally { setBusy(false); }
    }
  };

  const currentMode = useMemo(() => MODES.find((m) => m.value === mode), [mode]);
  const blocked = locked || (currentMode?.requiresOnline && !online);

  return (
    <div className="surface p-4 mb-4" data-testid="voice-intake-panel">
      <div className="flex items-center gap-3 mb-3">
        <div className="flex items-center gap-1 text-xs">
          {online ? <Wifi className="h-3 w-3 text-emerald-500" /> : <WifiOff className="h-3 w-3 text-amber-500" />}
          <span className="text-slate-400">{online ? "Online" : "Offline"}</span>
        </div>
        <div className="text-xs text-slate-500">Intake mode:</div>
        <select
          data-testid="voice-intake-mode"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          disabled={recording || busy || locked}
          className="h-8 px-2 rounded border border-slate-700 bg-slate-900 text-sm text-slate-200"
        >
          {MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        {blocked && currentMode?.requiresOnline && !online && (
          <span className="text-xs text-amber-400">Needs connection -- switch to an offline mode below.</span>
        )}
        {locked && <span className="text-xs text-rose-400">DR is locked -- intake disabled.</span>}
      </div>

      {/* Mode-specific body */}
      {mode === "manual" && (
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <KeyboardIcon className="h-4 w-4" />
          <span>Type values into the form below. Saves on every change.</span>
        </div>
      )}

      {mode === "offline_manual" && (
        <div className="text-sm text-slate-400 flex items-center gap-2">
          <KeyboardIcon className="h-4 w-4" />
          <span>Type as you walk the site. Draft autosaves to this device until connection returns.</span>
        </div>
      )}

      {(mode === "voice_async" || mode === "offline_voice") && (
        <div className="flex items-center gap-3">
          {!recording ? (
            <button
              data-testid="voice-record-start"
              onClick={startRecording}
              disabled={blocked || busy}
              className="h-10 px-4 rounded bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50"
            >
              <Mic className="h-4 w-4" /> Start recording
            </button>
          ) : (
            <button
              data-testid="voice-record-stop"
              onClick={stopRecording}
              className="h-10 px-4 rounded bg-rose-600 hover:bg-rose-700 text-white text-sm font-medium inline-flex items-center gap-2"
            >
              <Square className="h-4 w-4" /> Stop ({(elapsedMs / 1000).toFixed(0)}s)
            </button>
          )}
          {busy && <Loader2 className="h-4 w-4 animate-spin text-slate-400" />}
          <div className="text-xs text-slate-500">
            Up to 30 min per clip. {mode === "offline_voice" ? "Stored locally; auto-uploads when online." : "Uploads + AI fills DR on stop."}
          </div>
        </div>
      )}

      {mode === "voice_live" && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            {liveStatus !== "live" && liveStatus !== "connecting" ? (
              <button
                data-testid="voice-live-start"
                onClick={startLive}
                disabled={blocked || busy}
                className="h-10 px-4 rounded bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50"
              >
                <Radio className="h-4 w-4" /> Start live session
              </button>
            ) : (
              <button
                data-testid="voice-live-stop"
                onClick={stopLive}
                className="h-10 px-4 rounded bg-rose-600 hover:bg-rose-700 text-white text-sm font-medium inline-flex items-center gap-2"
              >
                <Square className="h-4 w-4" /> End session
              </button>
            )}
            <span className="text-xs text-slate-400">
              {liveStatus === "connecting" && "Connecting…"}
              {liveStatus === "live" && (<span className="text-emerald-400">● Live — AI is asking questions</span>)}
              {liveStatus === "ended" && "Session ended. AI is filling DR."}
              {liveStatus === "error" && (<span className="text-rose-400">Connection error.</span>)}
            </span>
          </div>
          {liveTranscript && (
            <pre className="text-xs text-slate-300 bg-slate-900/60 p-2 rounded max-h-40 overflow-y-auto whitespace-pre-wrap">{liveTranscript}</pre>
          )}
        </div>
      )}

      {/* Pending offline-queue list */}
      {pendingClips.length > 0 && (
        <div className="mt-4 pt-3 border-t border-slate-800">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs font-medium text-amber-400">Queued voice memos ({pendingClips.length})</div>
            <button data-testid="voice-sync-now" onClick={syncNow}
              disabled={!online || busy}
              className="h-7 px-3 rounded bg-amber-600 hover:bg-amber-700 text-slate-900 text-xs font-medium inline-flex items-center gap-1 disabled:opacity-50">
              <Upload className="h-3 w-3" /> Sync now
            </button>
          </div>
          <ul className="space-y-1">
            {pendingClips.map((c) => (
              <li key={c.id} className="flex items-center justify-between text-xs text-slate-400">
                <div>
                  {new Date(c.createdAt).toLocaleString()} · {(c.blob?.size / 1024).toFixed(0)} KB
                  {c.retryCount > 0 && <span className="ml-2 text-amber-400">retry {c.retryCount}</span>}
                  {c.lastError && <span className="ml-2 text-rose-400" title={c.lastError}>error</span>}
                </div>
                <button onClick={() => removePending(c.id)}
                  className="text-slate-500 hover:text-rose-400">
                  <Trash2 className="h-3 w-3" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* AI result summary */}
      {lastResult?.applied && (
        <div className="mt-4 pt-3 border-t border-slate-800 text-xs">
          <div className="flex items-center gap-2 text-emerald-400 mb-2">
            <CheckCircle2 className="h-4 w-4" /> AI populated the draft. Review fields below before submitting.
          </div>
          <pre className="text-slate-400 whitespace-pre-wrap">{summarizeApplied(lastResult.applied)}</pre>
        </div>
      )}
    </div>
  );
}

function summarizeApplied(a) {
  if (!a) return "(nothing applied)";
  const parts = [];
  if (a.weather) parts.push("weather");
  if (a.notes) parts.push("notes");
  if (a.activity_updates) parts.push(`${a.activity_updates} activity update${a.activity_updates === 1 ? "" : "s"}`);
  if (a.crew_presence) parts.push(`${a.crew_presence} crew row${a.crew_presence === 1 ? "" : "s"}`);
  if (a.delays) parts.push(`${a.delays} delay${a.delays === 1 ? "" : "s"}`);
  if (a.safety_events) parts.push(`${a.safety_events} safety event${a.safety_events === 1 ? "" : "s"}`);
  if (a.qc_issues) parts.push(`${a.qc_issues} QC issue${a.qc_issues === 1 ? "" : "s"}`);
  if (a.deliveries) parts.push(`${a.deliveries} deliver${a.deliveries === 1 ? "y" : "ies"}`);
  if (a.coordination_events) parts.push(`${a.coordination_events} coordination event${a.coordination_events === 1 ? "" : "s"}`);
  return parts.length ? parts.join(", ") : "no fields";
}
