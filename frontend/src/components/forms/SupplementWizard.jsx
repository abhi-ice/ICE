// Bucket D Phase 4 -- supplement creation wizard.
//
// Opens from any of the DR-detail panels' "+ Supplement" button when
// the parent DR is locked (status=submitted). Walks the user through:
//   1. Confirm parent DR + reason (required)
//   2. Pick affected activities from PARENT DR's grid (not today's)
//   3. Submit -> POST to /delays | /rfis | /qc-issues | /safety-events
//      with is_supplement=true, parent_daily_report_id=<parent>,
//      daily_report_id=<today's container DR>.
//
// The wizard only handles the supplement contract layer; the inner
// event payload (description, severity, etc.) is collected as a
// minimal "what happened?" field. Heavyweight forms (mitigation
// scenarios, multi-task float impact for Delays) stay on their own
// pages -- supplements deliberately keep the contract simple.
import { useEffect, useState } from "react";
import { ChevronRight, ChevronLeft, Check, AlertTriangle } from "lucide-react";
import api from "../../lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { toast } from "../ui/sonner";

const FACT_TYPES = [
  { value: "delay",          label: "Delay",          endpoint: "/delays",          requireDescr: true },
  { value: "rfi",            label: "RFI",            endpoint: "/rfis",            requireDescr: true,  hasSubject: true },
  { value: "qc_issue",       label: "QC issue",       endpoint: "/qc-issues",       requireDescr: true },
  { value: "safety_event",   label: "Safety event",   endpoint: "/safety-events",   requireDescr: true },
];

const SEVERITY_OPTS = ["minor", "moderate", "major", "critical"];

export default function SupplementWizard({
  open, onOpenChange, parentDailyReport, projectId, containerDailyReportId, onSaved,
}) {
  const [step, setStep] = useState(1);
  const [factType, setFactType] = useState("delay");
  const [reason, setReason] = useState("");
  const [parentActivities, setParentActivities] = useState([]);
  const [pickedIds, setPickedIds] = useState([]);
  const [description, setDescription] = useState("");
  const [subject, setSubject] = useState("");
  const [severity, setSeverity] = useState("minor");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setStep(1); setFactType("delay"); setReason(""); setPickedIds([]);
    setDescription(""); setSubject(""); setSeverity("minor");
    if (parentDailyReport?.id) {
      api.get(`/daily-reports/${parentDailyReport.id}/grid`)
        .then(({ data }) => setParentActivities(Array.isArray(data) ? data : []))
        .catch(() => setParentActivities([]));
    }
  }, [open, parentDailyReport?.id]);

  const togglePicked = (id) => {
    setPickedIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]);
  };

  const submit = async () => {
    const cfg = FACT_TYPES.find((t) => t.value === factType);
    if (!cfg) return;
    if (!reason.trim() || reason.trim().length < 5) {
      toast.error("Supplement reason required (5+ chars)"); return;
    }
    if (!pickedIds.length) {
      toast.error("Pick at least one affected activity from the parent DR"); return;
    }
    if (cfg.requireDescr && !description.trim()) {
      toast.error("Describe what happened"); return;
    }
    if (cfg.hasSubject && !subject.trim()) {
      toast.error("Subject required for RFIs"); return;
    }
    setBusy(true);
    try {
      // Bucket D Corrective (2026-05-08) -- backend now expects
      // linked_task_ids (master schedule scope). The wizard still
      // lets the user pick from the parent DR's grid for UX clarity
      // ("this is what was happening on that day"); translate each
      // picked daily_report_activities row to its schedule_activity_id
      // before posting. Rows without a schedule_activity_id (rare --
      // only happens for ad-hoc grid additions) are dropped.
      const linkedTaskIds = pickedIds
        .map((id) => parentActivities.find((a) => a.id === id)?.schedule_activity_id)
        .filter(Boolean);
      if (!linkedTaskIds.length) {
        toast.error("None of the picked DR rows resolve to a master schedule task.");
        setBusy(false);
        return;
      }
      const payload = {
        project_id: projectId,
        daily_report_id: containerDailyReportId,
        is_supplement: true,
        parent_daily_report_id: parentDailyReport.id,
        supplement_reason: reason.trim(),
        linked_task_ids: linkedTaskIds,
        date: parentDailyReport.report_date,
        description,
      };
      // Endpoint-specific shape adjustments.
      if (factType === "rfi") {
        payload.subject = subject;
        payload.question = description;
      }
      if (factType === "delay") {
        // Strict-enforced fields.
        payload.severity = severity;
        payload.reason_category = "other";
      }
      if (factType === "qc_issue" || factType === "safety_event") {
        payload.severity = severity;
        if (factType === "safety_event") payload.event_type = "near_miss";
      }
      const res = await api.post(cfg.endpoint, payload);
      toast.success(`${cfg.label} supplement filed`);
      onSaved?.(res.data);
      onOpenChange(false);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Filing failed");
    } finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>File supplement to DR {parentDailyReport?.report_date}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div className="bg-amber-50 border-l-4 border-amber-400 p-3 text-xs text-amber-900 flex gap-2">
            <AlertTriangle className="h-4 w-4 flex-shrink-0 mt-0.5" />
            <div>
              <strong>Filing as supplement.</strong> This DR was submitted on
              {" "}{parentDailyReport?.report_date}. New facts can't modify the
              original record. Your entry will be filed against today's DR but
              appear on the parent DR's appendix and in the chronological TIA.
            </div>
          </div>

          {step === 1 && (
            <>
              <div>
                <label className="text-xs text-slate-500 uppercase">Fact type *</label>
                <select data-testid="supplement-fact-type" value={factType} onChange={(e) => setFactType(e.target.value)}
                  className="block w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
                  {FACT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-slate-500 uppercase">Reason this is filed late *</label>
                <textarea data-testid="supplement-reason" rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
                  placeholder="e.g. sub provided rework photos this morning; original DR was already submitted"
                  className="block w-full px-2 py-1 rounded border border-slate-300 text-sm" />
              </div>
              <div className="flex justify-end">
                <button data-testid="supplement-next-1" onClick={() => setStep(2)} disabled={!reason.trim() || reason.trim().length < 5}
                  className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
                  Next <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </>
          )}

          {step === 2 && (
            <>
              <div className="text-xs text-slate-500">
                Pick affected activities from the parent DR ({parentDailyReport?.report_date}):
              </div>
              {!parentActivities.length ? (
                <div className="text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 text-xs">
                  Parent DR has no activity rows. Cannot file a supplement against it.
                </div>
              ) : (
                <div className="max-h-64 overflow-auto border border-slate-200 rounded">
                  {parentActivities.map((a) => (
                    <label key={a.id} className="flex items-center gap-2 px-2 py-1.5 border-b border-slate-100 cursor-pointer hover:bg-slate-50 text-xs">
                      <input data-testid={`supplement-activity-${a.activity_code}`} type="checkbox"
                        checked={pickedIds.includes(a.id)} onChange={() => togglePicked(a.id)} />
                      <span className="font-mono text-blue-700">{a.activity_code}</span>
                      <span className="text-slate-700 truncate">{a.activity_description}</span>
                    </label>
                  ))}
                </div>
              )}
              <div className="text-xs text-slate-500">{pickedIds.length} selected</div>
              <div className="flex justify-between">
                <button onClick={() => setStep(1)} className="h-9 px-3 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200 inline-flex items-center gap-1">
                  <ChevronLeft className="h-3.5 w-3.5" /> Back
                </button>
                <button data-testid="supplement-next-2" onClick={() => setStep(3)} disabled={!pickedIds.length}
                  className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
                  Next <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <div className="text-xs text-slate-500">Describe the {FACT_TYPES.find((t) => t.value === factType)?.label}:</div>
              {factType === "rfi" && (
                <div>
                  <label className="text-xs text-slate-500 uppercase">Subject *</label>
                  <input data-testid="supplement-subject" value={subject} onChange={(e) => setSubject(e.target.value)}
                    className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
                </div>
              )}
              <div>
                <label className="text-xs text-slate-500 uppercase">
                  {factType === "rfi" ? "Question" : "Description"} *
                </label>
                <textarea data-testid="supplement-description" rows={3} value={description} onChange={(e) => setDescription(e.target.value)}
                  className="block w-full px-2 py-1 rounded border border-slate-300 text-sm" />
              </div>
              {(factType === "delay" || factType === "qc_issue" || factType === "safety_event") && (
                <div>
                  <label className="text-xs text-slate-500 uppercase">Severity</label>
                  <select data-testid="supplement-severity" value={severity} onChange={(e) => setSeverity(e.target.value)}
                    className="block w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
                    {SEVERITY_OPTS.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>
              )}
              <div className="flex justify-between">
                <button onClick={() => setStep(2)} className="h-9 px-3 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200 inline-flex items-center gap-1">
                  <ChevronLeft className="h-3.5 w-3.5" /> Back
                </button>
                <button data-testid="supplement-submit" onClick={submit} disabled={busy}
                  className="h-9 px-3 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-50 inline-flex items-center gap-1">
                  <Check className="h-3.5 w-3.5" /> {busy ? "Filing…" : "File supplement"}
                </button>
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
