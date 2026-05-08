// Bucket D Phase 2 -- 3-step tag wizard. Pick DR -> Pick event type ->
// Pick the specific event row on that DR. Creates a Photo (tagging
// junction) row via POST /photo-tags. XOR enforced server-side.
import { useEffect, useMemo, useState } from "react";
import { ChevronRight, ChevronLeft, Check, Loader2 } from "lucide-react";
import api from "../../lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { toast } from "../ui/sonner";

const EVENT_TYPES = [
  { value: "daily_report_activity_id", label: "Activity (WBS row)", coll: "activities" },
  { value: "delay_id",                  label: "Delay",              coll: "delays" },
  { value: "rfi_id",                    label: "RFI",                coll: "rfis" },
  { value: "qc_issue_id",               label: "QC issue",           coll: "qc_issues" },
  { value: "safety_event_id",           label: "Safety event",       coll: "safety_events" },
  { value: "coordination_event_id",     label: "Coordination event", coll: "coordination_events" },
  { value: "delivery_id",               label: "Delivery",           coll: "deliveries" },
  // drawing_pin_id intentionally NOT exposed in this UI -- pin tagging
  // is its own (existing) drawing-viewer flow per spec.
];

const PHOTO_CLASSES = ["progress", "discovery", "damage", "quality",
                        "safety", "coordination", "closeout"];

function isoToday() { return new Date().toISOString().slice(0, 10); }

export default function TagWizard({ open, onOpenChange, tray, projectId, onTagged }) {
  const [step, setStep] = useState(1);
  const [drs, setDrs] = useState([]);
  const [drsLoading, setDrsLoading] = useState(false);
  const [drId, setDrId] = useState("");
  const [eventType, setEventType] = useState("daily_report_activity_id");
  const [eventOptions, setEventOptions] = useState([]);
  const [eventOptionsLoading, setEventOptionsLoading] = useState(false);
  const [eventId, setEventId] = useState("");
  const [photoClass, setPhotoClass] = useState("progress");
  const [caption, setCaption] = useState("");
  const [busy, setBusy] = useState(false);

  // Reset when the dialog opens for a new tray entry.
  useEffect(() => {
    if (!open) return;
    setStep(1);
    setEventType("daily_report_activity_id");
    setEventOptions([]);
    setEventId("");
    setPhotoClass("progress");
    setCaption("");
    // Pre-pick DR from tray's exif_taken_at date if available, else today.
    const exifDate = tray?.exif_taken_at ? String(tray.exif_taken_at).slice(0, 10) : null;
    setDrId("");
    if (projectId) {
      setDrsLoading(true);
      api.get(`/projects/${projectId}/daily-reports`)
        .then((r) => {
          const list = Array.isArray(r.data) ? r.data : [];
          setDrs(list);
          // Default selection: DR with report_date == exifDate, else today's, else newest.
          const targetDate = exifDate || isoToday();
          const match = list.find((d) => d.report_date === targetDate);
          if (match) setDrId(match.id);
          else if (list.length) setDrId(list[0].id);
        })
        .catch(() => setDrs([]))
        .finally(() => setDrsLoading(false));
    }
  }, [open, projectId, tray?.id]);

  // Load event options for step 3 whenever drId / eventType change.
  useEffect(() => {
    if (step !== 3 || !drId || !eventType) return;
    setEventOptionsLoading(true);
    setEventOptions([]);
    setEventId("");
    const t = EVENT_TYPES.find((e) => e.value === eventType);
    let endpoint;
    if (t.coll === "activities") {
      endpoint = `/daily-reports/${drId}/grid`;
    } else {
      // Per-collection list endpoint, then client-filter to ones with daily_report_id===drId.
      endpoint = `/projects/${projectId}/${t.coll.replace("_", "-")}`;
      // routers expose: /projects/{pid}/delays, /qc-issues, /safety-events,
      // /rfis, /coordination-events, /deliveries
      if (t.coll === "qc_issues") endpoint = `/projects/${projectId}/qc-issues`;
      if (t.coll === "safety_events") endpoint = `/projects/${projectId}/safety-events`;
      if (t.coll === "coordination_events") endpoint = `/projects/${projectId}/coordination-events`;
    }
    api.get(endpoint)
      .then((r) => {
        const all = Array.isArray(r.data) ? r.data : [];
        let list = all;
        if (t.coll !== "activities") {
          // Filter to events on the picked DR.
          const drField = t.coll === "coordination_events" ? "linked_daily_report_id" : "daily_report_id";
          list = all.filter((e) => e[drField] === drId);
        }
        setEventOptions(list);
      })
      .catch(() => setEventOptions([]))
      .finally(() => setEventOptionsLoading(false));
  }, [step, drId, eventType, projectId]);

  const drLabel = (d) => `${d.report_date} · ${d.status || "draft"}`;
  const optionLabel = useMemo(() => {
    const t = EVENT_TYPES.find((e) => e.value === eventType);
    return (o) => {
      if (!t) return o.id;
      switch (t.coll) {
        case "activities": return `[${o.activity_code || ""}] ${(o.activity_description || "").slice(0, 60)}`;
        case "delays":     return `${(o.severity || "").toUpperCase()} · ${(o.description || "").slice(0, 60)}`;
        case "rfis":       return `${o.rfi_number || ""} ${(o.subject || "").slice(0, 60)}`;
        case "qc_issues":  return `${(o.severity || "").toUpperCase()} · ${(o.description || "").slice(0, 60)}`;
        case "safety_events": return `${o.event_type || ""} · ${(o.description || "").slice(0, 60)}`;
        case "coordination_events": return `${(o.topic || "")} · ${(o.summary || "").slice(0, 60)}`;
        case "deliveries": return `${o.vendor || ""} · ${(o.description || "").slice(0, 60)}`;
        default: return o.id;
      }
    };
  }, [eventType]);

  const submit = async () => {
    if (!eventId) { toast.error("Pick an event row"); return; }
    setBusy(true);
    try {
      const payload = {
        tray_photo_id: tray.id,
        daily_report_id: drId,
        photo_class: photoClass,
        tag_caption: caption || null,
        // XOR -- exactly one event ref:
        daily_report_activity_id: null, delay_id: null, rfi_id: null,
        qc_issue_id: null, safety_event_id: null,
        coordination_event_id: null, delivery_id: null, drawing_pin_id: null,
      };
      payload[eventType] = eventId;
      const res = await api.post("/photo-tags", payload);
      toast.success("Photo tagged");
      onTagged?.(res.data);
      onOpenChange(false);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Tagging failed");
    } finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Tag photo to event ({step}/3)</DialogTitle></DialogHeader>
        <div className="space-y-3 text-sm">
          {step === 1 && (
            <>
              <div className="text-xs text-slate-500">
                Pick the daily report this photo belongs to.
                {tray?.exif_taken_at && (
                  <> EXIF date: <span className="font-mono">{String(tray.exif_taken_at).slice(0, 10)}</span></>
                )}
              </div>
              {drsLoading ? <div className="text-slate-400">Loading DRs…</div> : (
                <select data-testid="tag-wizard-dr" value={drId} onChange={(e) => setDrId(e.target.value)}
                  className="w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
                  <option value="">— pick a DR —</option>
                  {drs.map((d) => <option key={d.id} value={d.id}>{drLabel(d)}</option>)}
                </select>
              )}
              <div className="flex justify-end">
                <button data-testid="tag-wizard-next-1" onClick={() => setStep(2)} disabled={!drId}
                  className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
                  Next <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </>
          )}

          {step === 2 && (
            <>
              <div className="text-xs text-slate-500">Pick the event type.</div>
              <div className="grid grid-cols-2 gap-2">
                {EVENT_TYPES.map((t) => (
                  <button key={t.value} data-testid={`tag-wizard-type-${t.value}`}
                    onClick={() => setEventType(t.value)}
                    className={`h-10 rounded border text-sm text-left px-3 ${
                      eventType === t.value ? "border-blue-500 bg-blue-50 text-blue-800" : "border-slate-200 hover:border-slate-300"
                    }`}>
                    {t.label}
                  </button>
                ))}
              </div>
              <div className="flex justify-between">
                <button onClick={() => setStep(1)} className="h-9 px-3 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200 inline-flex items-center gap-1">
                  <ChevronLeft className="h-3.5 w-3.5" /> Back
                </button>
                <button data-testid="tag-wizard-next-2" onClick={() => setStep(3)}
                  className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1">
                  Next <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <div className="text-xs text-slate-500">
                Pick the specific {EVENT_TYPES.find((t) => t.value === eventType)?.label || "event"} on this DR.
              </div>
              {eventOptionsLoading ? (
                <div className="text-slate-400 inline-flex items-center gap-1"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…</div>
              ) : !eventOptions.length ? (
                <div className="text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 text-xs">
                  No matching events on this DR. Pick a different event type or DR.
                </div>
              ) : (
                <select data-testid="tag-wizard-event" value={eventId} onChange={(e) => setEventId(e.target.value)}
                  className="w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
                  <option value="">— pick —</option>
                  {eventOptions.map((o) => <option key={o.id} value={o.id}>{optionLabel(o)}</option>)}
                </select>
              )}
              <div>
                <label className="text-[11px] text-slate-500 uppercase">Photo class</label>
                <select data-testid="tag-wizard-class" value={photoClass} onChange={(e) => setPhotoClass(e.target.value)}
                  className="w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
                  {PHOTO_CLASSES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <div>
                <label className="text-[11px] text-slate-500 uppercase">Caption (optional)</label>
                <input data-testid="tag-wizard-caption" value={caption} onChange={(e) => setCaption(e.target.value)}
                  className="w-full h-9 px-2 rounded border border-slate-300 text-sm" />
              </div>
              <div className="flex justify-between">
                <button onClick={() => setStep(2)} className="h-9 px-3 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200 inline-flex items-center gap-1">
                  <ChevronLeft className="h-3.5 w-3.5" /> Back
                </button>
                <button data-testid="tag-wizard-submit" onClick={submit} disabled={busy || !eventId}
                  className="h-9 px-3 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-50 inline-flex items-center gap-1">
                  <Check className="h-3.5 w-3.5" /> {busy ? "Tagging…" : "Tag photo"}
                </button>
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
