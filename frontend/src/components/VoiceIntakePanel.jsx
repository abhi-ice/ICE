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
import { Mic, Square, Wifi, WifiOff, Trash2, Upload, Loader2, AlertCircle, CheckCircle2, Radio, KeyboardIcon, Headphones } from "lucide-react";

/**
 * Live state pill -- shows whether the AI is currently speaking
 * (mic muted, blue dot) or actively listening for the super
 * (mic hot, green pulse). Re-renders on a 200ms tick so the user
 * gets immediate visual feedback as the conversation alternates.
 */
function LiveStateIndicator({ isAiSpeaking }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((x) => x + 1), 200);
    return () => clearInterval(t);
  }, []);
  if (isAiSpeaking) {
    return (
      <span className="text-blue-300 inline-flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full bg-blue-400" />
        AI speaking · mic muted
      </span>
    );
  }
  return (
    <span className="text-emerald-300 inline-flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
      Your turn · speak now
    </span>
  );
}
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

export default function VoiceIntakePanel({ dailyReportId, onApplied, locked, initialMode }) {
  const [mode, setMode] = useState(initialMode || "manual");
  const [online, setOnline] = useState(typeof navigator !== "undefined" ? navigator.onLine : true);
  const [recording, setRecording] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [busy, setBusy] = useState(false);
  const [pendingClips, setPendingClips] = useState([]);
  const [lastResult, setLastResult] = useState(null);
  // 2026-02-XX (Abhi) -- keep a blob URL of the most-recent recording
  // so the super can play it back before/after the AI extract runs.
  // Cleared on the next recording start; never persisted across page
  // reloads (per request: "only visible first time when they recorded").
  const [lastRecordingUrl, setLastRecordingUrl] = useState(null);
  // 2026-05-25 — live transcription preview using the browser's
  // built-in Web Speech API. This runs IN PARALLEL with MediaRecorder
  // so the super sees text appearing as they speak. Gemini still
  // produces the canonical, more-accurate transcript that gets sent
  // to Claude on stop; this preview is just a UX confidence signal.
  const [livePreview, setLivePreview] = useState("");
  const [finalTranscript, setFinalTranscript] = useState("");
  const [extractWarning, setExtractWarning] = useState(null);
  const mediaRecorderRef = useRef(null);
  const chunksRef = useRef([]);
  const recordStartRef = useRef(null);
  const tickRef = useRef(null);
  const speechRecRef = useRef(null);

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

  const liveFinalRef = useRef("");

  // Recording controls -- MediaRecorder w/ webm/opus by default
  // (Chromium); the server accepts mp3/m4a/wav/webm/ogg, Gemini takes
  // webm-opus natively. mp3 transcoding would need a wasm encoder we
  // do not bundle, so we ship webm and let Gemini decode it.
  const startRecording = async () => {
    if (recording) return;
    try {
      // Disable echoCancellation + noiseSuppression. With these on,
      // Chrome can mute the MediaRecorder stream when another mic
      // consumer (Web Speech API) is active -- which leaves Gemini
      // with near-silence and the super gets a 3-char transcript.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      const mimeType =
        MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
          ? "audio/webm;codecs=opus"
          : MediaRecorder.isTypeSupported("audio/webm")
            ? "audio/webm" : "";
      const mr = mimeType
        ? new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 128000 })
        : new MediaRecorder(stream, { audioBitsPerSecond: 128000 });
      chunksRef.current = [];
      mr.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksRef.current.push(e.data); };
      mr.onstop = async () => {
        try { stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
        const blob = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
        // Capture the live browser-STT transcript as a fallback so we
        // can use it if Gemini transcription failed / returned empty.
        const fallback = (liveFinalRef.current || "").trim();
        // Expose a playback URL for the super to listen back before
        // the extract result lands.
        try {
          if (lastRecordingUrl) URL.revokeObjectURL(lastRecordingUrl);
          setLastRecordingUrl(URL.createObjectURL(blob));
        } catch { /* ignore */ }
        await handleRecordedBlob(blob, mr.mimeType || "audio/webm", fallback);
      };
      // Use a 1-second timeslice so dataavailable fires periodically.
      // Helps catch corrupt blobs early and gives a smoother size meter.
      mr.start(1000);
      mediaRecorderRef.current = mr;
      recordStartRef.current = Date.now();
      setElapsedMs(0);
      setLivePreview("");
      setFinalTranscript("");
      setExtractWarning(null);
      setLastResult(null);
      liveFinalRef.current = "";
      tickRef.current = setInterval(() => {
        const el = Date.now() - recordStartRef.current;
        setElapsedMs(el);
        if (el >= MAX_CLIP_MS) stopRecording();
      }, 250);
      setRecording(true);

      // Start live transcription preview (Web Speech API). This runs
      // entirely in the browser, free, real-time. It's BOTH a UX
      // confidence signal AND a fallback transcript if Gemini fails.
      // If the browser doesn't support it (Firefox without flag,
      // Safari < 14.1), we silently skip.
      try {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SR) {
          const sr = new SR();
          sr.continuous = true;
          sr.interimResults = true;
          sr.lang = "en-US";
          sr.onresult = (event) => {
            let interim = "";
            let finalChunk = "";
            for (let i = event.resultIndex; i < event.results.length; i++) {
              const t = event.results[i][0].transcript;
              if (event.results[i].isFinal) finalChunk += t + " ";
              else interim += t;
            }
            if (finalChunk) {
              liveFinalRef.current = (liveFinalRef.current + " " + finalChunk).trim();
            }
            const merged = liveFinalRef.current
              + (interim ? ` [… ${interim}]` : "");
            setLivePreview(merged);
          };
          sr.onerror = () => { /* ignore browser STT errors -- not critical */ };
          sr.onend = () => {
            // Auto-restart while recording (browser STT times out after ~60s)
            if (mediaRecorderRef.current?.state === "recording") {
              try { sr.start(); } catch { /* race; ignore */ }
            }
          };
          sr.start();
          speechRecRef.current = sr;
        }
      } catch (e) {
        // Web Speech not supported / blocked; carry on without preview.
        console.warn("Live transcript preview unavailable:", e);
      }
    } catch (e) {
      toast.error(`Microphone error: ${e.message || e}`);
    }
  };
  const stopRecording = () => {
    try { mediaRecorderRef.current?.stop(); } catch { /* ignore */ }
    try { speechRecRef.current?.stop(); } catch { /* ignore */ }
    speechRecRef.current = null;
    if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
    setRecording(false);
  };

  const handleRecordedBlob = async (blob, mimeType, fallbackTranscript = "") => {
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
        let data = r.data;
        const geminiTranscript = (data.transcript || "").trim();

        // Fallback path -- Gemini returned little/no text but the
        // browser's Web Speech API heard you. Send the browser
        // transcript through the same /voice/extract path to get
        // structured fields anyway.
        const geminiUseless = geminiTranscript.length < 20;
        if (geminiUseless && fallbackTranscript.length >= 20) {
          console.info(
            `Gemini transcript too short (${geminiTranscript.length} chars). `
            + `Falling back to browser STT (${fallbackTranscript.length} chars).`
          );
          toast.message(
            "AI couldn't make out the audio clearly. Using your browser's "
            + "live transcript instead — quality may vary.",
            { duration: 5000 },
          );
          try {
            const r2 = await api.post(
              `/daily-reports/${dailyReportId}/voice/extract`,
              { transcript: fallbackTranscript },
              { timeout: 60000 },
            );
            // Synthesize a unified response shape so downstream UI
            // doesn't need to branch.
            data = {
              transcript: fallbackTranscript,
              transcript_source: "browser_stt",
              applied: r2.data.applied,
              draft: r2.data.draft,
              warning: null,
            };
          } catch (e2) {
            console.error("Fallback extraction failed:", e2);
          }
        } else if (geminiTranscript) {
          data.transcript_source = "gemini";
        }

        setLastResult(data);
        setFinalTranscript(data.transcript || "");
        setExtractWarning(data.warning || null);
        if (data.warning === "no_speech_detected" && !fallbackTranscript) {
          toast.error(
            "AI couldn't hear any speech in the recording. Check your "
            + "mic, move closer, and try again.",
            { duration: 8000 },
          );
        } else {
          const s = summarizeApplied(data.applied);
          const skipped = data.skipped_duplicates;
          const totalSkipped = skipped
            ? Object.values(skipped).reduce((sum, v) => sum + (v || 0), 0)
            : 0;
          const skipNote = totalSkipped > 0
            ? ` (Skipped ${totalSkipped} duplicate entr${totalSkipped === 1 ? "y" : "ies"} already on this DR.)`
            : "";
          if (s === "no fields" && totalSkipped === 0) {
            toast.error(
              "AI heard you but couldn't extract any DR fields. "
              + "Review the transcript below and either re-record more clearly "
              + "or edit the DR by hand.",
              { duration: 8000 },
            );
          } else if (s === "no fields" && totalSkipped > 0) {
            toast.message(
              `All ${totalSkipped} item${totalSkipped === 1 ? "" : "s"} from this recording `
              + `were duplicates of entries already on this DR. Nothing new was added.`,
              { duration: 7000 },
            );
          } else {
            toast.success(
              `AI applied: ${s}.${skipNote} Review the draft and submit when ready.`,
              { duration: 6000 },
            );
          }
        }
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
  const livePlaybackCtxRef = useRef(null);
  const liveNextPlaybackAtRef = useRef(0);
  // 2026-05-25 — turn-by-turn caption buffers. The native-audio model
  // streams `inputTranscription.text` (what the super said) and
  // `outputTranscription.text` (what the AI said) as tiny chunks
  // (often 1-3 chars apiece). They are NOT individually complete turns
  // — Gemini only sends `turnComplete` when the speaker fully finishes
  // their turn. So we accumulate per-speaker into a buffer and only
  // flush to the "completed turns" list when (a) the OTHER speaker
  // starts talking, or (b) turnComplete fires.
  const userTurnRef = useRef("");
  const aiTurnRef = useRef("");
  const userTurnsAllRef = useRef([]);   // all completed user utterances
  const aiTurnsAllRef = useRef([]);     // all completed AI utterances

  // Re-render the display from the accumulated turns + the active
  // (in-progress) buffer. This is called frequently as chunks arrive.
  const _redraw = () => {
    const userTurns = userTurnsAllRef.current;
    const aiTurns = aiTurnsAllRef.current;
    const lines = [];
    const max = Math.max(userTurns.length, aiTurns.length);
    for (let i = 0; i < max; i++) {
      if (aiTurns[i]) lines.push(`AI: ${aiTurns[i]}`);
      if (userTurns[i]) lines.push(`You: ${userTurns[i]}`);
    }
    // Append the in-progress buffer for whichever speaker is currently
    // streaming. We don't push it to the completed list until the
    // other speaker starts or turnComplete arrives.
    if (aiTurnRef.current.trim()) lines.push(`AI: ${aiTurnRef.current.trim()}`);
    if (userTurnRef.current.trim()) lines.push(`You: ${userTurnRef.current.trim()}`);
    setLiveTranscript(lines.join("\n"));
  };

  // Push whichever buffer (or both) has content into the completed
  // list, then clear the buffers. Used on turnComplete.
  const _commitTurns = () => {
    const u = userTurnRef.current.trim();
    const a = aiTurnRef.current.trim();
    if (a) aiTurnsAllRef.current.push(a);
    if (u) userTurnsAllRef.current.push(u);
    userTurnRef.current = "";
    aiTurnRef.current = "";
    _redraw();
  };

  // Hand off mid-conversation: the OTHER speaker just started talking,
  // so flush whichever buffer is currently active to the completed
  // list. Mirrors a phone call's natural turn-taking.
  const _handoffTo = (speaker /* "user" | "ai" */) => {
    if (speaker === "user" && aiTurnRef.current.trim()) {
      aiTurnsAllRef.current.push(aiTurnRef.current.trim());
      aiTurnRef.current = "";
    } else if (speaker === "ai" && userTurnRef.current.trim()) {
      userTurnsAllRef.current.push(userTurnRef.current.trim());
      userTurnRef.current = "";
    }
  };

  // Gemini Live returns AI audio as base64 PCM at 24 kHz, 16-bit signed,
  // mono. Decode each chunk and schedule it onto an AudioContext play
  // queue so chunks flow seamlessly into one voice line.
  const _playLiveAudioChunk = async (b64) => {
    try {
      if (!livePlaybackCtxRef.current || livePlaybackCtxRef.current.state === "closed") {
        livePlaybackCtxRef.current = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
        liveNextPlaybackAtRef.current = 0;
      }
      const ctx = livePlaybackCtxRef.current;
      const bin = atob(b64);
      const len = bin.length / 2;
      if (len === 0) return;
      const buffer = ctx.createBuffer(1, len, 24000);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < len; i++) {
        const lo = bin.charCodeAt(i * 2);
        const hi = bin.charCodeAt(i * 2 + 1);
        const s16 = (hi << 8) | lo;
        const signed = s16 > 0x7fff ? s16 - 0x10000 : s16;
        channel[i] = signed / 0x8000;
      }
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      // Robust scheduling: prefetch ~250ms ahead of currentTime so
      // network jitter between chunks doesn't cause gaps. If we ever
      // fall behind (queue exhausted), restart the queue right now.
      const PREBUFFER = 0.25;
      let startAt = liveNextPlaybackAtRef.current;
      if (startAt < ctx.currentTime + 0.02) {
        // Queue is empty / we're catching up -- restart with prebuffer.
        startAt = ctx.currentTime + PREBUFFER;
      }
      src.start(startAt);
      liveNextPlaybackAtRef.current = startAt + buffer.duration;
    } catch (e) {
      console.warn("Live audio playback error:", e);
    }
  };

  const _stopLivePlayback = () => {
    // Called when the user interrupts or the session ends -- close the
    // playback context so any queued chunks stop immediately.
    try { livePlaybackCtxRef.current?.close(); } catch { /* ignore */ }
    livePlaybackCtxRef.current = null;
    liveNextPlaybackAtRef.current = 0;
  };

  const startLive = async () => {
    if (liveStatus === "live" || liveStatus === "connecting") return;
    setLiveStatus("connecting");
    setLiveTranscript("");
    userTurnRef.current = "";
    aiTurnRef.current = "";
    userTurnsAllRef.current = [];
    aiTurnsAllRef.current = [];
    try {
      const tok = await api.post(`/daily-reports/${dailyReportId}/voice/live-token`);
      const apiKey = tok.data.token;
      const model = tok.data.model || "gemini-2.5-flash-native-audio-latest";
      // Open WS directly to Gemini Live. Browser side; the token /
      // key never proxies through our server for streaming audio.
      const wsUrl = `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${encodeURIComponent(apiKey)}`;
      const ws = new WebSocket(wsUrl);
      liveWsRef.current = ws;
      ws.onopen = () => {
        // Send setup message -- system instruction, model, voice config.
        // The native-audio Live model REQUIRES response_modalities=["AUDIO"];
        // text-only responses are rejected with code 1007. We additionally
        // request input + output transcriptions so we can render BOTH
        // the AI's question and the super's answer as scrollback text.
        ws.send(JSON.stringify({
          setup: {
            model: `models/${model}`,
            generation_config: {
              response_modalities: ["AUDIO"],
            },
            input_audio_transcription: {},
            output_audio_transcription: {},
            // Robust voice-activity detection -- LOW start sensitivity
            // so the server doesn't false-trigger on speaker bleed or
            // background noise, HIGH end sensitivity so it knows when
            // the super stopped talking and can respond quickly.
            realtime_input_config: {
              automatic_activity_detection: {
                disabled: false,
                start_of_speech_sensitivity: "START_SENSITIVITY_LOW",
                end_of_speech_sensitivity: "END_SENSITIVITY_HIGH",
                silence_duration_ms: 800,
                prefix_padding_ms: 200,
              },
            },
            system_instruction: {
              parts: [{ text:
                "You are a friendly, fast assistant guiding a construction "
                + "superintendent through their daily report by voice. "
                + "RULES OF ENGAGEMENT:\n"
                + "• Keep every question to ONE short sentence (under 12 words).\n"
                + "• Acknowledge each answer in 3 words or fewer ('Got it.' / 'Thanks.' / 'Noted.').\n"
                + "• If the super gives partial info, ask ONE follow-up to fill the gap. "
                + "Example: super says 'logged a delay because shaft broke' — you ask "
                + "'How many days impact?' or 'Severity — minor, moderate, or major?'\n"
                + "• If the super says 'skip' or 'no' or 'next' — accept it, do NOT push further, move on.\n"
                + "• NEVER repeat yourself. If you already heard the answer, don't ask again.\n"
                + "• DO NOT speak your internal reasoning out loud. Just ask the question.\n"
                + "• Weather is ALREADY pre-filled from the forecast. Do NOT ask weather questions. "
                + "Only capture weather if the super volunteers a correction unprompted.\n"
                + "• If the super says they want to LOG AN RFI / REQUEST FOR INFORMATION / "
                + "needs clarification from owner or architect or engineer, capture it AS AN RFI, "
                + "not a delay. Ask which scheduled activity it gates and the priority "
                + "(low / normal / high / urgent). Only ALSO log a delay if the super explicitly "
                + "says the RFI cost them days.\n\n"
                + "ORDER OF QUESTIONS (start with #1, do NOT ask about weather first):\n"
                + "1. Manpower log — for each trade on site today, capture these fields: "
                + "Company (contractor name), Trade (e.g. electrical, plumbing, mech, carp), "
                + "Task (which scheduled activity they worked on — match by activity code or "
                + "description), Headcount (# of workers), Hours per person, and any Notes. "
                + "Ask one trade at a time, then 'any other trades on site?' until super says no.\n"
                + "2. Activity progress: which scheduled activities advanced, percent complete.\n"
                + "3. Delays: was there one? If yes, ask which activity, what happened, "
                + "and impact in days.\n"
                + "4. Safety, QC, deliveries, coordination — one combined question, "
                + "skip silently if super says no.\n"
                + "5. General notes.\n"
                + "When all five are covered, say 'Daily report complete' and stop talking." }],
            },
          },
        }));
        // Don't flip to "live" yet -- wait for setupComplete from server.
      };
      ws.onmessage = async (evt) => {
        try {
          let data = evt.data;
          if (data instanceof Blob) data = await data.text();
          const msg = JSON.parse(data);
          if (msg.setupComplete) {
            // Server acknowledged setup -- now we can start streaming mic.
            setLiveStatus("live");
            void _startLiveMicStreaming(ws);
            return;
          }
          const sc = msg.serverContent;
          if (!sc) return;

          // Audio playback -- AI's spoken voice.
          if (sc.modelTurn?.parts) {
            for (const part of sc.modelTurn.parts) {
              if (part.inlineData?.data && part.inlineData?.mimeType?.startsWith("audio/")) {
                void _playLiveAudioChunk(part.inlineData.data);
              }
              // Note: part.text from modelTurn is the model's *internal
              // chain-of-thought*. The native-audio model emits these
              // (e.g. "**Initiating Daily Report**") as scratch reasoning.
              // We deliberately ignore them — only outputTranscription
              // below represents what the AI actually SAID OUT LOUD.
            }
          }

          // Captions: aggregate input + output transcription chunks
          // into per-turn lines so the display reads as conversation,
          // not as one char per row. Critically: when one speaker
          // starts streaming, we hand off the OTHER speaker's
          // accumulated buffer to the completed list. Without this,
          // chunks like "con", "di", "tion" never aggregate into
          // "conditions".
          if (sc.outputTranscription?.text) {
            _handoffTo("ai");   // user just stopped → commit their turn
            aiTurnRef.current += sc.outputTranscription.text;
            _redraw();
          }
          if (sc.inputTranscription?.text) {
            _handoffTo("user"); // AI just stopped → commit its turn
            userTurnRef.current += sc.inputTranscription.text;
            _redraw();
          }

          // Interruption -- user started talking while AI was speaking.
          // Stop the audio queue immediately so the user isn't talking
          // over a half-finished question.
          if (sc.interrupted) {
            _stopLivePlayback();
          }

          // Turn boundary -- finalize whichever buffer has content.
          if (sc.turnComplete) {
            _commitTurns();
          }
        } catch (parseErr) {
          console.warn("Live WS parse error:", parseErr);
        }
      };
      ws.onerror = (e) => {
        console.error("Gemini Live WS error", e);
        setLiveStatus("error");
        toast.error("Live voice connection error. Check console for details.");
      };
      ws.onclose = (evt) => {
        console.info(`Gemini Live WS closed code=${evt.code} reason=${evt.reason}`);
        if (liveStatus === "connecting") {
          // Closed before we got setupComplete -- typically auth/model failure.
          toast.error(
            evt.code === 1008
              ? "Live voice rejected by Gemini (likely model or quota). Use Voice Memo instead."
              : `Live voice closed unexpectedly (code ${evt.code}).`
          );
          setLiveStatus("error");
        } else {
          setLiveStatus((s) => s === "live" ? "ended" : s);
        }
      };
      // Mic streaming starts only after setupComplete (see _startLiveMicStreaming).
    } catch (e) {
      setLiveStatus("error");
      const msg = e?.response?.data?.detail || e.message;
      toast.error(`Live voice failed to start: ${msg}`);
    }
  };
  const _startLiveMicStreaming = async (ws) => {
    try {
      // Stream microphone audio to Gemini. 16 kHz PCM is what the Live
      // API expects. We resample inline with an AudioContext at 16k.
      // IMPORTANT: echoCancellation + noiseSuppression MUST be on for
      // Live Voice. Without them the laptop mic picks up the AI's own
      // voice from the speakers and feeds it back as "user" speech,
      // causing the AI to interrupt itself, fragment its replies, and
      // even "respond" to its own words. This is the root cause of
      // the choppy 3-word-per-chunk playback the super reported.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      liveAudioCtxRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const processor = audioCtx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (e) => {
        if (ws.readyState !== 1) return;
        // Suppress mic transmission while the AI is actively speaking.
        // This is belt-and-suspenders over echoCancellation: if the
        // user is on speakers (no headphones), the speaker output can
        // still bleed into the mic. Skipping mic chunks during AI
        // playback prevents the server from VAD-triggering on echo.
        const ctxNow = livePlaybackCtxRef.current?.currentTime ?? 0;
        const isAiSpeaking = liveNextPlaybackAtRef.current > ctxNow + 0.05;
        if (isAiSpeaking) return;
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
      console.error("Failed to start mic streaming:", e);
      toast.error(`Microphone error: ${e.message || e}`);
    }
  };
  const stopLive = async () => {
    // Flush any in-progress turn so the last spoken bits are captured.
    _commitTurns();
    try { liveWsRef.current?.close(); } catch { /* ignore */ }
    try { liveAudioCtxRef.current?.close(); } catch { /* ignore */ }
    try { livePlaybackCtxRef.current?.close(); } catch { /* ignore */ }
    liveWsRef.current = null;
    liveAudioCtxRef.current = null;
    livePlaybackCtxRef.current = null;
    liveNextPlaybackAtRef.current = 0;
    setLiveStatus("ended");
    // Build a clean, Claude-friendly transcript from ONLY the user's
    // utterances. The AI's questions are context; they don't carry
    // DR data. We label each as "Q: ... A: ..." so Claude can pair
    // them and infer fields from short answers like "yes" or "skip".
    const userTurns = userTurnsAllRef.current.filter(Boolean);
    const aiTurns = aiTurnsAllRef.current.filter(Boolean);
    const max = Math.max(userTurns.length, aiTurns.length);
    const lines = [];
    for (let i = 0; i < max; i++) {
      if (aiTurns[i]) lines.push(`Q (AI): ${aiTurns[i]}`);
      if (userTurns[i]) lines.push(`A (super): ${userTurns[i]}`);
    }
    const finalTranscript = lines.join("\n");
    setFinalTranscript(finalTranscript);
    if (!finalTranscript.trim()) {
      toast.message("Live session ended -- nothing was transcribed.");
      return;
    }
    setBusy(true);
    try {
      const r = await api.post(
        `/daily-reports/${dailyReportId}/voice/extract`,
        { transcript: finalTranscript, source_kind: "live_voice" },
      );
      setLastResult({ ...r.data, transcript: finalTranscript, transcript_source: "gemini_live" });
      const s = summarizeApplied(r.data.applied);
      const skipped = r.data.skipped_duplicates;
      const totalSkipped = skipped
        ? Object.values(skipped).reduce((sum, v) => sum + (v || 0), 0)
        : 0;
      const skipNote = totalSkipped > 0
        ? ` (Skipped ${totalSkipped} duplicate.)`
        : "";
      if (s === "no fields") {
        toast.error("Live session ended but AI couldn't extract any DR fields from the conversation.",
                    { duration: 8000 });
      } else {
        toast.success(`AI applied from live session: ${s}.${skipNote} Review and submit.`,
                      { duration: 7000 });
      }
      if (onApplied) onApplied();
    } catch (e) {
      toast.error(`Failed to apply live transcript: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
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
        <div className="flex flex-col gap-3">
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
            {busy && (
              <span className="text-xs text-slate-500 inline-flex items-center gap-1.5">
                <Loader2 className="h-4 w-4 animate-spin" />
                Transcribing &amp; extracting…
              </span>
            )}
            {recording && (
              <span className="text-xs text-emerald-500 inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-rose-500 animate-pulse" />
                Recording — speak clearly
              </span>
            )}
          </div>
          <div className="text-xs text-slate-500">
            Up to 30 min per clip. {mode === "offline_voice"
              ? "Stored locally; auto-uploads when online."
              : "Uploads on stop. AI fills the DR from your speech."}
          </div>

          {/* Live preview (browser Web Speech API, free & real-time).
              Just a UX confidence signal; Gemini does the final
              authoritative transcription server-side. */}
          {recording && (
            <div data-testid="voice-live-preview"
              className="rounded border border-emerald-700/40 bg-emerald-950/30 p-3">
              <div className="text-[11px] uppercase tracking-wide text-emerald-300 mb-1.5 flex items-center gap-1.5">
                <Radio className="h-3 w-3" /> Live transcript preview
                <span className="text-slate-500 normal-case">(browser-only; AI uses the final recording)</span>
              </div>
              <div className="text-sm text-slate-100 min-h-[2.5rem] whitespace-pre-wrap leading-relaxed">
                {livePreview || <span className="text-slate-500 italic">Listening… start talking.</span>}
              </div>
            </div>
          )}
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
              {liveStatus === "live" && (
                <LiveStateIndicator
                  isAiSpeaking={Boolean(livePlaybackCtxRef.current
                    && liveNextPlaybackAtRef.current > (livePlaybackCtxRef.current.currentTime + 0.05))}
                />
              )}
              {liveStatus === "ended" && "Session ended. AI is filling DR."}
              {liveStatus === "error" && (<span className="text-rose-400">Connection error.</span>)}
            </span>
          </div>
          {/* Critical UX hint -- echo will trash the conversation if
              the user is on speakers. */}
          <div className="rounded border border-amber-700/40 bg-amber-950/30 px-3 py-2 text-xs text-amber-200 flex items-start gap-2">
            <Headphones className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            <span>
              <strong>Use headphones for best results.</strong> If you're on laptop
              speakers, the mic may pick up the AI's voice and confuse the conversation.
              The system mutes the mic while the AI speaks, but headphones eliminate echo
              entirely.
            </span>
          </div>
          {liveTranscript ? (
            <div className="rounded border border-emerald-700/40 bg-slate-950/60 p-3 max-h-72 overflow-y-auto" data-testid="voice-live-transcript">
              <div className="text-[11px] uppercase tracking-wide text-emerald-300 mb-2">Conversation transcript</div>
              <div className="text-sm leading-relaxed space-y-1.5">
                {liveTranscript.split("\n").filter(Boolean).map((line, i) => {
                  const isUser = line.startsWith("You:");
                  const isAi = line.startsWith("AI:");
                  const body = isUser ? line.slice(4).trim() : isAi ? line.slice(3).trim() : line;
                  return (
                    <div key={i} className="flex gap-2">
                      <span className={`text-[10px] font-semibold uppercase shrink-0 w-10 pt-0.5 ${isUser ? "text-emerald-400" : "text-blue-300"}`}>
                        {isUser ? "You" : isAi ? "AI" : ""}
                      </span>
                      <span className="text-slate-100">{body}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          ) : liveStatus === "live" ? (
            <div className="text-xs text-slate-500 italic">Listening… the AI will speak first.</div>
          ) : null}
          <div className="text-xs text-slate-500">
            Speak naturally; the AI will guide you through manpower, activities,
            delays, events, and notes. Weather is already pre-filled — skip it.
            Say "skip" to move on. Click End session when done.
          </div>
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

      {/* AI transcript + result summary -- the diagnostic surface
          the super sees after recording. Shows EXACTLY what Gemini
          heard so they can compare it to what they actually said,
          and clearly explains why fields weren't filled if Claude
          couldn't extract structured data. */}
      {(finalTranscript || extractWarning || lastResult?.applied || lastRecordingUrl) && (
        <div className="mt-4 pt-3 border-t border-slate-800 space-y-3" data-testid="voice-result-panel">
          {lastRecordingUrl && (
            <div className="rounded border border-slate-700 bg-slate-950/60 p-3" data-testid="voice-playback">
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1.5">
                Playback (your last recording)
              </div>
              <audio src={lastRecordingUrl} controls
                className="w-full h-9"
                data-testid="voice-playback-audio" />
            </div>
          )}
          {extractWarning === "no_speech_detected" ? (
            <div className="rounded border border-rose-700 bg-rose-950/40 p-3">
              <div className="flex items-center gap-2 text-rose-300 text-sm font-medium mb-1">
                <AlertCircle className="h-4 w-4" /> AI couldn't hear any speech
              </div>
              <div className="text-xs text-slate-300">
                Your microphone may be muted, the recording was silent,
                or the audio quality wasn't clear enough. Try:
                <ul className="list-disc list-inside mt-1 space-y-0.5 text-slate-400">
                  <li>Check that your browser has microphone permission</li>
                  <li>Move closer to the mic (within arm's length)</li>
                  <li>Speak louder and slower</li>
                  <li>Reduce background noise if possible</li>
                </ul>
              </div>
            </div>
          ) : finalTranscript ? (
            <div className="rounded border border-slate-700 bg-slate-950/60 p-3">
              <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-1.5 flex items-center justify-between">
                <span>
                  What the AI heard (final transcript)
                  {lastResult?.transcript_source === "browser_stt" && (
                    <span className="ml-2 px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 text-[10px] normal-case">browser fallback</span>
                  )}
                  {lastResult?.transcript_source === "gemini" && (
                    <span className="ml-2 px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-300 text-[10px] normal-case">Gemini</span>
                  )}
                </span>
                <span className="text-slate-500 normal-case">{finalTranscript.length} chars</span>
              </div>
              <pre className="text-sm text-slate-100 whitespace-pre-wrap font-sans leading-relaxed max-h-48 overflow-y-auto">
                {finalTranscript}
              </pre>
            </div>
          ) : null}

          {lastResult?.applied && (() => {
            const applied = lastResult.applied;
            const summary = summarizeApplied(applied);
            const isEmpty = summary === "no fields";
            return (
              <div className={`rounded border p-3 ${
                isEmpty
                  ? "border-amber-700 bg-amber-950/30"
                  : "border-emerald-700 bg-emerald-950/30"
              }`}>
                <div className={`flex items-center gap-2 text-sm font-medium mb-1 ${
                  isEmpty ? "text-amber-300" : "text-emerald-300"
                }`}>
                  {isEmpty
                    ? <><AlertCircle className="h-4 w-4" /> No DR fields extracted</>
                    : <><CheckCircle2 className="h-4 w-4" /> Draft populated — review fields below</>}
                </div>
                <div className="text-xs text-slate-300">
                  {isEmpty
                    ? "The AI transcribed your speech but couldn't map it to specific DR fields. Try mentioning things explicitly — e.g. 'three carpenters from Acme on site eight hours', 'activity 1.4 is 50 percent complete', 'minor delay on electrical, two day impact'."
                    : <span>AI applied: <strong>{summary}</strong></span>}
                </div>
              </div>
            );
          })()}
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
