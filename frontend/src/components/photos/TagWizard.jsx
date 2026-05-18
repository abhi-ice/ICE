// Bucket D Phase 2 -- 3-step tag wizard. Pick DR -> Pick event type ->
// Pick the specific event row on that DR. Creates a Photo (tagging
// junction) row via POST /photo-tags. XOR enforced server-side.
//
// Bucket F Phase 3 (2026-05-08) -- AI pre-fill. When the tray row
// carries an AI suggestion (status=complete with non-empty fields),
// the wizard pre-selects suggested event type on step 2 + filters
// the step-3 list by suggested trade (with a "Show all trades"
// toggle so the user can override -- some QC issues span multiple
// trades and we don't hide options). On submit, the Photo row gets
// created_via_ai_suggestion + ai_suggestion_id + ai_confidence_at_
// tag_time stamped on it.
import { useEffect, useMemo, useState } from "react";
import { ChevronRight, ChevronLeft, Check, Loader2, Sparkles, Filter } from "lucide-react";
import api from "../../lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { toast } from "../ui/sonner";
import { isDrActivityComplete } from "../../lib/taskCompletion";

// Map AI-suggested event type values to the wizard's EVENT_TYPES.value.
// AI returns "qc_issue" / "safety_event" / etc.; wizard fields are
// "qc_issue_id" / "safety_event_id" / etc. "none" / "delay" / others
// fall through to the default activity tagging.
const AI_TO_WIZARD_EVENT_TYPE = {
  delay: "delay_id",
  rfi: "rfi_id",
  qc_issue: "qc_issue_id",
  safety_event: "safety_event_id",
  coordination_event: "coordination_event_id",
  delivery: "delivery_id",
  none: "daily_report_activity_id",
};

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

// Bucket D Addition 5 (2026-05-12) -- new `isLocked` + `lockedAddReason`
// props. Mirrors DelayForm gate: client-side validation rejects submit
// if isLocked + reason too short. The PhotosPanel ALSO pre-validates
// before kicking off the upload, but this is the defense-in-depth
// at the tag-create step.
const TAG_REASON_MIN = 10;
const tagIsReasonValid = (v) => (v || "").trim().length >= TAG_REASON_MIN;
export default function TagWizard({ open, onOpenChange, tray, projectId, onTagged,
                                    initialDailyReportId = null,
                                    isLocked = false,
                                    lockedAddReason = null }) {
  // Bucket F Phase 4 -- when the wizard is opened from the DR PhotosPanel
  // upload flow, the DR is already known. Skip step 1 in that case
  // (pre-select the DR + start at step 2) so the user lands directly
  // on the AI-suggested event-type picker.
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
  // Bucket F Phase 3 -- AI pre-fill state. aiSuggestion is the most
  // recent AI suggestion blob for this tray (loaded via the same
  // /ai-analysis endpoint AiAnalysisCard polls). showAllTrades=false
  // filters step-3 options by the suggested trade; true expands.
  const [aiSuggestion, setAiSuggestion] = useState(null);
  const [showAllTrades, setShowAllTrades] = useState(false);
  // Feature F5 (2026-05-18) -- for the Activity (WBS row) event type,
  // completed activity rows are hidden from step 3 by default; this
  // toggle reveals them.
  const [showCompletedActivities, setShowCompletedActivities] = useState(false);

  // Reset when the dialog opens for a new tray entry.
  useEffect(() => {
    if (!open) return;
    // Bucket F Phase 4 -- skip step 1 when DR is pre-supplied.
    setStep(initialDailyReportId ? 2 : 1);
    setEventOptions([]);
    setEventId("");
    setShowAllTrades(false);
    setShowCompletedActivities(false);
    // Pull the AI suggestion + use it for the initial selections.
    // The tray row carries the AI fields directly from the upload
    // response, but we re-fetch to make sure we have the freshest
    // values (the analysis may have completed after the modal opened).
    let trayAi = null;
    if (tray?.ai_analysis_status === "complete") {
      trayAi = {
        ai_analysis_status: tray.ai_analysis_status,
        ai_suggested_class: tray.ai_suggested_class,
        ai_suggested_caption: tray.ai_suggested_caption,
        ai_suggested_event_type: tray.ai_suggested_event_type,
        ai_suggested_trade: tray.ai_suggested_trade,
        ai_confidence_score: tray.ai_confidence_score,
      };
    }
    setAiSuggestion(trayAi);
    if (tray?.id) {
      api.get(`/photo-tray/${tray.id}/ai-analysis`)
        .then((r) => {
          if (r.data?.ai_analysis_status === "complete") {
            setAiSuggestion(r.data);
          }
        })
        .catch(() => { /* keep tray-row values; non-fatal */ });
    }
    // Default initial event type + class + caption from AI suggestion
    // when available. User can still change everything before save.
    const aiEvent = trayAi?.ai_suggested_event_type;
    const wizardEv = AI_TO_WIZARD_EVENT_TYPE[aiEvent] || "daily_report_activity_id";
    setEventType(wizardEv);
    const aiClass = trayAi?.ai_suggested_class;
    setPhotoClass(aiClass && aiClass !== "other" ? aiClass : "progress");
    setCaption(trayAi?.ai_suggested_caption || "");
    // Pre-pick DR from tray's exif_taken_at date if available, else today.
    const exifDate = tray?.exif_taken_at ? String(tray.exif_taken_at).slice(0, 10) : null;
    // Bucket F Phase 4 -- when the wizard was launched from a known DR
    // (DR PhotosPanel upload flow), pin the DR id immediately. We
    // still pull the DR list so optionLabel still works if step 1 is
    // surfaced via a back-button.
    setDrId(initialDailyReportId || "");
    if (projectId) {
      setDrsLoading(true);
      api.get(`/projects/${projectId}/daily-reports`)
        .then((r) => {
          const list = Array.isArray(r.data) ? r.data : [];
          setDrs(list);
          // Default selection: explicit initial DR > EXIF-date DR > today's DR > newest.
          if (initialDailyReportId) {
            // Already set above; just make sure the list is loaded.
          } else {
            const targetDate = exifDate || isoToday();
            const match = list.find((d) => d.report_date === targetDate);
            if (match) setDrId(match.id);
            else if (list.length) setDrId(list[0].id);
          }
        })
        .catch(() => setDrs([]))
        .finally(() => setDrsLoading(false));
    }
  }, [open, projectId, tray?.id, tray?.ai_analysis_status,
       tray?.ai_suggested_event_type, tray?.ai_suggested_class,
       tray?.ai_suggested_caption, tray?.ai_suggested_trade,
       tray?.ai_confidence_score, initialDailyReportId]);

  // Bucket F Phase 3 -- step-3 trade filter. When AI suggested a
  // trade AND the user hasn't toggled showAllTrades, filter options
  // by trade_code (events carry trade_code on most collections;
  // daily_report_activities have it via the linked task).
  // Feature F5 (2026-05-18) -- for the Activity (WBS row) event type,
  // completed activity rows are hidden by default. Photos still tag to
  // events of other types unchanged (those are events, not tasks).
  const isActivityType = eventType === "daily_report_activity_id";
  const hiddenCompletedActivities = isActivityType
    ? eventOptions.filter(isDrActivityComplete).length
    : 0;

  const filteredEventOptions = useMemo(() => {
    // F5 -- drop completed activity rows unless the toggle is on.
    let base = eventOptions;
    if (eventType === "daily_report_activity_id" && !showCompletedActivities) {
      base = base.filter((o) => !isDrActivityComplete(o));
    }
    const suggestedTrade = aiSuggestion?.ai_suggested_trade;
    if (!suggestedTrade || showAllTrades) return base;
    const wanted = suggestedTrade.toLowerCase().trim();
    const matched = base.filter((o) => {
      const t = (o.trade_code || o.trade || "").toLowerCase().trim();
      return t && (t === wanted || t.includes(wanted) || wanted.includes(t));
    });
    // If filter removes everything, fall back to all options so the
    // user isn't stranded with an empty picker.
    return matched.length ? matched : base;
  }, [eventOptions, aiSuggestion, showAllTrades, eventType, showCompletedActivities]);

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
        // Bucket F Phase 3 -- AI lineage. True when an AI suggestion
        // was loaded for this tray AND the user didn't override the
        // suggested class/event/caption. The override path keeps the
        // flag true; the lineage IS that this tag started from AI.
        // Use ai_suggestion_id = the tray id (each tray has one
        // analysis cycle; tray.id is the durable handle).
        created_via_ai_suggestion: !!aiSuggestion,
        ai_suggestion_id: aiSuggestion ? tray.id : null,
        ai_confidence_at_tag_time: aiSuggestion?.ai_confidence_score ?? null,
      };
      payload[eventType] = eventId;
      // Bucket D Addition 5 -- client-side gate + always-include on
      // locked-DR creates. Uniform across roles; backend bypass for
      // admin/PM still operates as the safety net.
      if (isLocked) {
        if (!tagIsReasonValid(lockedAddReason)) {
          toast.error(
            `Add reason required (min ${TAG_REASON_MIN} chars) when tagging a photo on a locked daily report.`,
          );
          setBusy(false);
          return;
        }
        payload.add_reason = (lockedAddReason || "").trim();
      }
      const res = await api.post("/photo-tags", payload);
      toast.success(lockedAddReason
        ? "Photo tagged (post-submit). Audit chain captured."
        : "Photo tagged");
      onTagged?.(res.data);
      onOpenChange(false);
    } catch (e) {
      const status = e.response?.status;
      const detail = e.response?.data?.detail;
      if (
        status === 403 && detail && typeof detail === "object"
        && detail.error === "edit_request_required"
      ) {
        toast.error(
          "Self-edit window (24h) has closed. File an edit request from "
          + "any locked-DR edit pencil -- approval opens a 1-hour window "
          + "for adds (including photo tags) too.",
        );
      } else {
        toast.error(detail || "Tagging failed");
      }
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
              <div className="text-xs text-slate-500 flex items-center gap-2">
                <span>Pick the event type.</span>
                {aiSuggestion?.ai_suggested_event_type && (
                  <span data-testid="tag-wizard-ai-event-badge"
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-blue-100 text-blue-800 border border-blue-200 text-[10px]">
                    <Sparkles className="h-3 w-3" />
                    AI suggested: {(aiSuggestion.ai_suggested_event_type || "").replace(/_/g, " ")}
                  </span>
                )}
              </div>
              <div className="grid grid-cols-2 gap-2">
                {EVENT_TYPES.map((t) => {
                  const isAiPick = AI_TO_WIZARD_EVENT_TYPE[
                    aiSuggestion?.ai_suggested_event_type
                  ] === t.value;
                  return (
                    <button key={t.value} data-testid={`tag-wizard-type-${t.value}`}
                      onClick={() => setEventType(t.value)}
                      className={`h-10 rounded border text-sm text-left px-3 flex items-center justify-between ${
                        eventType === t.value ? "border-blue-500 bg-blue-50 text-blue-800" : "border-slate-200 hover:border-slate-300"
                      }`}>
                      <span>{t.label}</span>
                      {isAiPick && (
                        <Sparkles className="h-3 w-3 text-blue-600" title="AI suggested this" />
                      )}
                    </button>
                  );
                })}
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
              {/* Bucket F Phase 3 -- trade filter toggle. Visible only
                  when AI suggested a trade. Default ON = filter; OFF
                  = show all. We never hide options entirely (some QC
                  issues span trades); the toggle is the escape hatch. */}
              {aiSuggestion?.ai_suggested_trade && eventOptions.length > 0 && (
                <div data-testid="tag-wizard-trade-filter"
                  className="flex items-center justify-between px-2 py-1 rounded bg-blue-50 border border-blue-200 text-xs">
                  <span className="inline-flex items-center gap-1 text-blue-900">
                    <Filter className="h-3 w-3" />
                    {showAllTrades
                      ? `Showing all trades`
                      : `Filtered by AI-suggested trade: ${aiSuggestion.ai_suggested_trade}`}
                    {!showAllTrades && (
                      <span className="text-blue-700">
                        ({filteredEventOptions.length} of {eventOptions.length})
                      </span>
                    )}
                  </span>
                  <button data-testid="tag-wizard-show-all-trades"
                    onClick={() => setShowAllTrades((v) => !v)}
                    className="text-[10px] text-blue-700 hover:underline">
                    {showAllTrades ? "Re-apply trade filter" : "Show all trades"}
                  </button>
                </div>
              )}
              {/* Feature F5 -- "Show completed tasks" toggle for the
                  Activity (WBS row) event type, default OFF. Completed
                  activity rows are hidden from the picker by default. */}
              {isActivityType && eventOptions.length > 0 && (
                <label className="flex items-center gap-1.5 text-[11px] text-slate-500 cursor-pointer">
                  <input type="checkbox" data-testid="tag-wizard-show-completed"
                    checked={showCompletedActivities}
                    onChange={(e) => setShowCompletedActivities(e.target.checked)} />
                  Show completed tasks
                  {hiddenCompletedActivities > 0 && !showCompletedActivities && (
                    <span className="text-slate-400">({hiddenCompletedActivities} hidden)</span>
                  )}
                </label>
              )}
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
                  {filteredEventOptions.map((o) => <option key={o.id} value={o.id}>{optionLabel(o)}</option>)}
                </select>
              )}
              <div>
                <label className="text-[11px] text-slate-500 uppercase inline-flex items-center gap-1">
                  Photo class
                  {aiSuggestion?.ai_suggested_class && photoClass === aiSuggestion.ai_suggested_class && (
                    <span className="text-[9px] text-blue-700 inline-flex items-center gap-0.5">
                      <Sparkles className="h-2.5 w-2.5" /> AI
                    </span>
                  )}
                </label>
                <select data-testid="tag-wizard-class" value={photoClass} onChange={(e) => setPhotoClass(e.target.value)}
                  className="w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
                  {PHOTO_CLASSES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <div>
                <label className="text-[11px] text-slate-500 uppercase inline-flex items-center gap-1">
                  Caption (optional)
                  {aiSuggestion?.ai_suggested_caption && caption === aiSuggestion.ai_suggested_caption && (
                    <span className="text-[9px] text-blue-700 inline-flex items-center gap-0.5">
                      <Sparkles className="h-2.5 w-2.5" /> AI
                    </span>
                  )}
                </label>
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
