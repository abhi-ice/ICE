import { useState, useRef, useEffect } from "react";
import { Mic, Square, Loader2, X, Wand2, MessagesSquare } from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
import EntityFromVoiceDialog from "./EntityFromVoiceDialog";
import { subscribeDailyReportId } from "../../lib/copilotContext";

// Followup-fix 4 (2026-05-07): coordination_event added so the C3b
// Phase 2 voice extractor for coordination events has a UI entrypoint.
// Backend auto-creates the coordination_events row on submit (see
// services/voice_pipeline.process_voice_annotation), so the result
// sheet for this kind shows a "view on Coordination page" cue rather
// than the generic Create dialog.
const KINDS = [
  { value: "work_item", label: "Field issue" },
  { value: "delay", label: "Delay" },
  { value: "safety_event", label: "Safety event" },
  { value: "qc_issue", label: "QC issue" },
  { value: "rfi", label: "RFI" },
  { value: "coordination_event", label: "Coordination event" },
];

export default function VoiceCaptureButton() {
  const [kind, setKind] = useState("work_item");
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  // Followup-fix 4: subscribe to the active daily report id so a voice
  // memo recorded while a DR is open auto-links to that DR. Backend
  // links via linked_entity_type=daily_report + linked_entity_id; the
  // coordination event auto-extracted from the transcript inherits the
  // same linkage (no quick-log button hop required).
  const [activeDrId, setActiveDrId] = useState(null);
  useEffect(() => subscribeDailyReportId(setActiveDrId), []);
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
      // Followup-fix 4 (2026-05-07): forward DR context so backend can
      // auto-link both the voice annotation AND any auto-extracted
      // coordination event to this DR.
      if (activeDrId) {
        fd.append("linked_entity_type", "daily_report");
        fd.append("linked_entity_id", activeDrId);
      }
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
          <>
            {activeDrId && (
              <span data-testid="voice-fab-dr-badge"
                className="text-xs px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 shadow-sm"
                title="Voice memo will auto-link to the open daily report">
                ↓ linked to open DR
              </span>
            )}
            <select
              data-testid="voice-kind-select"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className="h-7 px-2 rounded-full bg-white border border-slate-200 text-xs shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              title="Voice kind"
            >
              {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </select>
          </>
        )}
        <button
          data-testid="voice-fab"
          onClick={recording ? stop : start}
          disabled={busy}
          title={recording ? "Stop recording" : `Voice annotation (${kind})${activeDrId ? " — linked to open daily report" : ""}`}
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
              <pre className="text-xs bg-slate-900 text-slate-200 p-2 rounded overflow-x-auto max-h-44 scrollbar-soft">{JSON.stringify(result.structured_fields || {}, null, 2)}</pre>
            </div>
            {result.kind === "coordination_event" ? (
              // Followup-fix 4 (2026-05-07): backend auto-creates a draft
              // coordination_events row when kind=coordination_event. The
              // EntityFromVoiceDialog has no handler for this kind, so
              // we surface the auto-created event with a link to the
              // Coordination page instead of a Create button.
              <div data-testid="voice-coordination-confirmation"
                className="rounded border border-blue-200 bg-blue-50 p-2 text-xs text-slate-700 flex items-start gap-2">
                <MessagesSquare className="h-4 w-4 text-blue-600 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="font-medium text-blue-900">Coordination event drafted</div>
                  <div>Open the <a href="/coordination" className="underline">Coordination page</a> to review, edit, or delete the auto-created draft.</div>
                </div>
              </div>
            ) : (
              <button
                data-testid="voice-create-entity"
                onClick={() => setCreateOpen(true)}
                disabled={!result.structured_fields || result.structured_fields._error}
                className="w-full h-9 rounded bg-blue-600 hover:bg-blue-700 text-white text-sm flex items-center justify-center gap-2 disabled:opacity-50"
              >
                <Wand2 className="h-4 w-4" /> Create {result.kind?.replace(/_/g, " ")} from this
              </button>
            )}
            <div className="text-xs text-slate-400">
              Annotation saved (id <code>{result.id?.slice(0, 8)}</code>). Audio in object storage.
              {activeDrId ? " Linked to the open daily report." : " Pre-filled fields will open in the create dialog."}
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
