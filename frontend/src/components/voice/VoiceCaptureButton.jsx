import { useState, useRef } from "react";
import { Mic, Square, Loader2, X, Wand2 } from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
import EntityFromVoiceDialog from "./EntityFromVoiceDialog";

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
  const [createOpen, setCreateOpen] = useState(false);
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
        {!recording && !busy && (
          <select
            data-testid="voice-kind-select"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className="h-7 px-2 rounded-full bg-white border border-slate-200 text-[11px] shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            title="Voice kind"
          >
            {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
        )}
        <button
          data-testid="voice-fab"
          onClick={recording ? stop : start}
          disabled={busy}
          title={recording ? "Stop recording" : `Voice annotation (${kind})`}
          className={`h-12 w-12 rounded-full grid place-items-center shadow-lg transition ${
            recording ? "bg-red-600 text-white animate-pulse" : "bg-white text-slate-800 border border-slate-200 hover:bg-slate-50"
          }`}
        >
          {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : recording ? <Square className="h-5 w-5" /> : <Mic className="h-5 w-5" strokeWidth={1.6} />}
        </button>
      </div>

      {result && (
        <div data-testid="voice-result-sheet" className="fixed right-4 bottom-20 w-[400px] surface p-4 z-30 max-h-[70vh] overflow-auto scrollbar-soft">
          <div className="flex items-center gap-2 mb-3">
            <Mic className="h-4 w-4 text-blue-600" />
            <div className="font-medium text-sm">Voice annotation</div>
            <button onClick={() => setResult(null)} className="ml-auto h-6 w-6 grid place-items-center rounded hover:bg-slate-100"><X className="h-4 w-4" /></button>
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <div className="field-label">Kind</div>
              <div className="px-2 py-1 inline-block rounded bg-blue-50 text-blue-700 text-xs">{result.kind}</div>
            </div>
            <div>
              <div className="field-label">Transcript</div>
              <div className="text-xs bg-slate-50 border border-slate-200 rounded p-2 max-h-24 overflow-y-auto">
                {result.transcript || <em className="text-slate-400">(no speech detected)</em>}
              </div>
            </div>
            <div>
              <div className="field-label">Extracted fields (Claude)</div>
              <pre className="text-[11px] bg-slate-900 text-slate-200 p-2 rounded overflow-x-auto max-h-44 scrollbar-soft">{JSON.stringify(result.structured_fields || {}, null, 2)}</pre>
            </div>
            <button
              data-testid="voice-create-entity"
              onClick={() => setCreateOpen(true)}
              disabled={!result.structured_fields || result.structured_fields._error}
              className="w-full h-9 rounded bg-blue-600 hover:bg-blue-700 text-white text-sm flex items-center justify-center gap-2 disabled:opacity-50"
            >
              <Wand2 className="h-4 w-4" /> Create {result.kind?.replace(/_/g, " ")} from this
            </button>
            <div className="text-[10px] text-slate-400">
              Annotation saved (id <code>{result.id?.slice(0, 8)}</code>). Audio in object storage. Pre-filled fields will open in the create dialog.
            </div>
          </div>
        </div>
      )}

      <EntityFromVoiceDialog
        open={createOpen}
        voiceAnnotation={result}
        onClose={(created) => {
          setCreateOpen(false);
          if (created) setResult(null);
        }}
      />
    </>
  );
}
