// VoiceCaptureButton — global FAB. Records via MediaRecorder, uploads to
// /api/voice/annotate with a chosen `kind`, then shows the transcript +
// extracted structured fields in a sheet.
import { useState, useRef } from "react";
import { Mic, Square, Loader2, X } from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

const KINDS = [
  { value: "work_item", label: "Field issue" },
  { value: "delay", label: "Delay" },
  { value: "safety_event", label: "Safety event" },
  { value: "qc_issue", label: "QC issue" },
  { value: "rfi", label: "RFI" },
];

export default function VoiceCaptureButton() {
  const [kind, setKind] = useState("work_item");
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const mediaRef = useRef(null);
  const chunksRef = useRef([]);

  const start = async () => {
    setResult(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream, { mimeType: "audio/webm" });
      chunksRef.current = [];
      mr.ondataavailable = (e) => { if (e.data.size) chunksRef.current.push(e.data); };
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        await upload(blob);
      };
      mr.start();
      mediaRef.current = mr;
      setRecording(true);
    } catch (e) {
      toast.error("Microphone access denied: " + e.message);
    }
  };

  const stop = () => {
    if (mediaRef.current && mediaRef.current.state !== "inactive") {
      mediaRef.current.stop();
    }
    setRecording(false);
  };

  const upload = async (blob) => {
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", blob, `voice-${Date.now()}.webm`);
      fd.append("kind", kind);
      const { data } = await api.post("/voice/annotate", fd);
      setResult(data);
      toast.success("Voice annotation processed");
    } catch (e) {
      toast.error("Voice processing failed: " + (e.response?.data?.detail || e.message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="fixed bottom-5 right-20 flex flex-col items-end gap-2 z-20">
        <button
          data-testid="voice-fab"
          onClick={recording ? stop : start}
          disabled={busy}
          title={recording ? "Stop recording" : "Voice annotation"}
          className={`h-12 w-12 rounded-full grid place-items-center shadow-lg transition ${
            recording ? "bg-red-600 text-white animate-pulse" : "bg-white text-slate-800 border border-slate-200 hover:bg-slate-50"
          }`}
        >
          {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : recording ? <Square className="h-5 w-5" /> : <Mic className="h-5 w-5" strokeWidth={1.6} />}
        </button>
      </div>

      {result && (
        <div data-testid="voice-result-sheet" className="fixed right-4 bottom-20 w-[380px] surface p-4 z-30 max-h-[70vh] overflow-auto scrollbar-soft">
          <div className="flex items-center gap-2 mb-3">
            <Mic className="h-4 w-4 text-blue-600" />
            <div className="font-medium text-sm">Voice annotation</div>
            <button onClick={() => setResult(null)} className="ml-auto h-6 w-6 grid place-items-center rounded hover:bg-slate-100"><X className="h-4 w-4" /></button>
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <div className="field-label">Kind</div>
              <select value={kind} onChange={(e) => setKind(e.target.value)} className="w-full h-8 px-2 rounded border border-slate-200 text-sm">
                {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
              </select>
            </div>
            <div>
              <div className="field-label">Transcript</div>
              <div className="text-xs bg-slate-50 border border-slate-200 rounded p-2 max-h-32 overflow-y-auto">
                {result.transcript || "(no transcript)"}
              </div>
            </div>
            <div>
              <div className="field-label">Extracted fields</div>
              <pre className="text-[11px] bg-slate-900 text-slate-200 p-2 rounded overflow-x-auto">{JSON.stringify(result.structured_fields, null, 2)}</pre>
            </div>
            <div className="text-[11px] text-slate-400">
              Annotation saved (id <code>{result.id?.slice(0, 8)}</code>). Use these fields to seed a new entity below.
            </div>
          </div>
        </div>
      )}
    </>
  );
}
