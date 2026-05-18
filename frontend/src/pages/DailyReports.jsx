/**
 * Daily Reports — Phase 2 full rebuild.
 *
 * One page surfaces every signal a federal-construction Super needs to file:
 *   header · weather · crew presence · roster (from presence) · activity grid
 *   inline events (delays, safety, qc, rfis, deliveries) · photos · voice · renovation
 *   (only for renovation/tenant_improvement) · footer w/ submit + approve + audit log
 *
 * Copilot is given the active daily_report_id when this page is open, so it can
 * call DR-only tools: record_presence, mark_activity_status, create_unplanned_activity.
 */
import { useEffect, useState, useCallback } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { setActiveDailyReportId, subscribeDescriptionInsert } from "../lib/copilotContext";
import { localTodayISO, formatDateDisplay } from "../lib/dateUtil";
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";
import CoordinationEventForm from "../components/forms/CoordinationEventForm";
// Addition 4 -- locked-DR edit modal (reusable across event types).
import LockedEventEditModal from "../components/forms/LockedEventEditModal";
// Bucket D Addition 5 (2026-05-12) -- SupplementWizard import removed.
// The component file (frontend/src/components/forms/SupplementWizard.jsx)
// is preserved for legacy reference but no longer imported anywhere on
// this page. New events on locked DRs use the post_submit_added flow
// via LockedAddReasonBlock + the standard create endpoints.
import DrActivityPicker from "../components/forms/DrActivityPicker";
// Bucket D Corrective (2026-05-08) -- master-schedule task picker.
// Used by RFI / QC / Safety / Delivery / Coord forms below in place of
// DrActivityPicker. DrActivityPicker stays imported because nothing else
// uses it on this page yet -- safe to remove once nothing references it.
import TaskPicker from "../components/forms/TaskPicker";
// CPM Part J-6 (2026-05-15) -- per-task RFI impact input.
import PerTaskImpactsInput from "../components/forms/PerTaskImpactsInput";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import {
  Download, FilePlus2, RefreshCw, Cloud, Users, ClipboardList, Image as ImageIcon,
  Mic, AlertTriangle, ShieldAlert, ClipboardCheck, MessageSquare, Truck, Plus,
  Lock, CheckCircle2, ChevronLeft, Trash2, Wand2, Pencil, Sparkles, Loader2,
} from "lucide-react";
// Bucket F Phase 4 -- DR upload now opens the 3-step tag wizard after
// AI analysis lands (3-5s wait). Imported as a default export from
// the photos sub-package.
import TagWizard from "../components/photos/TagWizard";
// Bucket G Phase 3 -- Jigsaw pre-fill widget on the DR narrative.
import JigsawField from "../components/forms/JigsawField";

const RENOVATION_TYPES = ["renovation", "tenant_improvement"];

// Bucket D Addition 5 (2026-05-12) -- "add to locked DR" reason capture.
// Reused by every event panel + the activity grid when the user adds a
// new event/row AFTER the parent DR was submitted. The reason text is
// passed inline to the create() API call as `add_reason`; backend
// stamps post_submit_added=true + writes EVENT_ADDED_POST_SUBMIT audit
// when min-10-char threshold is met. Admin/PM bypass the reason check
// server-side but the FIELD STILL RENDERS AS REQUIRED CLIENT-SIDE for
// them so audit captures intent uniformly across roles (consistent UX,
// no role-conditional UI behavior). Submission is blocked client-side
// until the field meets the min-char threshold.
//
// Test 2D follow-up (2026-05-12): the prior implementation didn't have
// client-side enforcement -- empty submit succeeded for admin (correct
// per policy spec but inconsistent UX) and for non-admin if the form
// somehow reached the backend. The visual red-border + disabled-submit
// combo now makes the field unambiguously required at the UI layer.
const ADD_REASON_MIN = 10;
function isAddReasonValid(value) {
  return (value || "").trim().length >= ADD_REASON_MIN;
}
function LockedAddReasonBlock({ value, onChange, eventLabel = "this event", testid = "locked-add-reason" }) {
  const length = (value || "").trim().length;
  const ok = length >= ADD_REASON_MIN;
  return (
    <div className={`rounded p-2 text-xs space-y-1.5 ${
      ok
        ? "bg-amber-50 border border-amber-200 text-amber-900"
        : "bg-rose-50 border-2 border-rose-300 text-rose-900"
    }`} data-testid={`${testid}-block`}>
      <div className="flex items-start gap-1.5">
        <Lock className="h-3.5 w-3.5 mt-0.5 flex-shrink-0" />
        <div>
          <strong>Adding {eventLabel} to a locked daily report.</strong>{" "}
          <span className="font-semibold">Reason required (min {ADD_REASON_MIN} chars).</span>{" "}
          Captured in the audit chain.
        </div>
      </div>
      <textarea data-testid={testid} value={value || ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Why is this being added after the DR was submitted? (e.g. 'forgot to log discovery during demo')"
        aria-required="true"
        aria-invalid={!ok}
        className={`w-full p-2 rounded text-sm bg-white h-16 ${
          ok ? "border border-amber-300" : "border-2 border-rose-400 ring-1 ring-rose-200"
        }`} />
      <div className={`flex items-center justify-between text-[10px] ${
        ok ? "text-emerald-700" : "text-rose-700 font-semibold"
      }`}>
        <span>{ok ? "✓ ready" : "⚠ required field"}</span>
        <span>{length} / {ADD_REASON_MIN} min</span>
      </div>
    </div>
  );
}

// Helper for create() handlers: inspects an API error response and, if
// the backend returned 403 with edit_request_required, points the user
// at the edit-request flow. Returns true when handled (caller skips
// generic toast); false otherwise.
function handleLockedAddError(e, _eventType) {
  const status = e.response?.status;
  const detail = e.response?.data?.detail;
  if (
    status === 403 && detail && typeof detail === "object"
    && detail.error === "edit_request_required"
  ) {
    toast.error(
      "Self-edit window (24h) has closed. File an edit request from the "
      + "edit pencil on any existing row -- approval opens a 1-hour window "
      + "for both edits AND adds.",
    );
    return true;
  }
  return false;
}

const PHOTO_CLASSES = [
  { value: "progress", label: "Progress", color: "bg-slate-100 text-slate-700 border-slate-300" },
  { value: "discovery", label: "Discovery", color: "bg-amber-100 text-amber-800 border-amber-300" },
  { value: "damage", label: "Damage", color: "bg-rose-100 text-rose-800 border-rose-300" },
  { value: "quality", label: "Quality", color: "bg-violet-100 text-violet-800 border-violet-300" },
  { value: "safety", label: "Safety", color: "bg-orange-100 text-orange-800 border-orange-300" },
  { value: "coordination", label: "Coordination", color: "bg-sky-100 text-sky-800 border-sky-300" },
  { value: "closeout", label: "Closeout", color: "bg-emerald-100 text-emerald-800 border-emerald-300" },
];
const PHOTO_CLASS_BY_VALUE = Object.fromEntries(PHOTO_CLASSES.map((c) => [c.value, c]));

// ------------------------------------------------------------
//  Top-level list — pick a date (any past/future), open or create
// ------------------------------------------------------------
export default function DailyReports() {
  const { activeId } = useProjects();
  const [reports, setReports] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerDate, setPickerDate] = useState(localTodayISO());

  // Bucket D Phase 3 -- gallery chips + date links route here with
  // ?openId=<dr-id>; honor it on first mount so the DR opens directly.
  // Bucket D Fix F -- ALSO honor ?openEvent + ?openEventType so the
  // gallery click-through scrolls to and pulse-highlights the specific
  // event row. Stripped from the URL after first mount so refresh
  // doesn't re-trigger.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const target = params.get("openId");
    if (target) setOpenId(target);
  }, []);

  const load = useCallback(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/daily-reports`).then(({ data }) => setReports(data));
  }, [activeId]);
  useEffect(() => { load(); }, [load]);

  // Tell the copilot which DR we're scoped to.
  useEffect(() => {
    setActiveDailyReportId(openId);
    return () => setActiveDailyReportId(null);
  }, [openId]);

  // Item 8 — date-picker driven create. Backend POST is idempotent on
  // (project_id, report_date) so picking a date that already exists
  // just returns that DR; no duplicates.
  const createForDate = async () => {
    if (!pickerDate) return toast.error("Pick a date first");
    try {
      const { data } = await api.post("/daily-reports", {
        project_id: activeId, report_date: pickerDate,
      });
      const existed = reports.some((r) => r.report_date === pickerDate);
      // CPM Part G (2026-05-15) -- format report_date MM/DD/YYYY in
      // toast text. The DB-level comparison above stays ISO.
      toast.success(existed ? `Opened existing report for ${formatDateDisplay(data.report_date)}`
                            : `Daily report opened for ${formatDateDisplay(data.report_date)}`);
      setPickerOpen(false);
      setOpenId(data.id);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const removeReport = async (r) => {
    if (r.status !== "draft") return;
    if (!window.confirm(`Delete the draft daily report for ${formatDateDisplay(r.report_date)}? Linked delays/QC/safety/RFIs/photos will keep their records but their daily_report_id will be cleared.`)) return;
    try {
      await api.delete(`/daily-reports/${r.id}`);
      toast.success(`Deleted daily report for ${formatDateDisplay(r.report_date)}`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  if (openId) {
    return <DailyReportDetail id={openId} onBack={() => setOpenId(null)} />;
  }

  return (
    <div data-testid="daily-reports-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Daily reports</h1>
          <p className="text-sm text-muted-foreground">
            One page per day — weather, crew presence, activities, events, photos, voice. Copilot is scoped to the open report.
          </p>
        </div>
        <button data-testid="dr-new-button"
          onClick={() => { setPickerDate(localTodayISO()); setPickerOpen(true); }}
          className="h-9 px-3 rounded bg-primary text-primary-foreground text-sm flex items-center gap-2">
          <FilePlus2 className="h-4 w-4" /> + New daily report
        </button>
      </div>

      {pickerOpen && (
        <div data-testid="dr-new-dialog"
          className="surface p-4 border-l-4 border-blue-500 bg-blue-50/30">
          <div className="font-medium text-sm mb-2">Open or create a daily report</div>
          <div className="text-xs text-slate-600 mb-3">
            If a daily report already exists for the selected date, it will be opened. Otherwise a new draft will be created.
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-xs">
              <div className="text-slate-500 uppercase tracking-wide">Date</div>
              <input data-testid="dr-new-date" type="date" value={pickerDate}
                onChange={(e) => setPickerDate(e.target.value)}
                className="mt-1 h-9 px-2 border border-slate-200 rounded text-sm" />
            </label>
            <button data-testid="dr-new-confirm" onClick={createForDate}
              className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700">
              Open / create
            </button>
            <button data-testid="dr-new-cancel" onClick={() => setPickerOpen(false)}
              className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead><tr><th>Date</th><th>Status</th><th>Submitted</th><th className="w-32"></th></tr></thead>
          <tbody>
            {reports.map((r) => {
              const canDelete = r.status === "draft";
              return (
                <tr key={r.id} data-testid={`dr-row-${r.id}`}>
                  <td className="font-mono">{formatDateDisplay(r.report_date)}</td>
                  <td><StatusPill status={r.status} /></td>
                  <td className="text-xs">{r.submitted_at ? new Date(r.submitted_at).toLocaleString() : "—"}</td>
                  <td className="text-right">
                    <button data-testid={`open-dr-${r.id}`} onClick={() => setOpenId(r.id)} className="text-sm text-primary hover:underline mr-3">
                      Open
                    </button>
                    <button
                      data-testid={`delete-dr-${r.id}`}
                      onClick={() => removeReport(r)}
                      disabled={!canDelete}
                      title={canDelete ? "Delete draft" : "Submitted/approved reports cannot be deleted"}
                      className={`h-7 w-7 grid place-items-center rounded inline-flex align-middle ${canDelete ? "text-red-500 hover:bg-red-50" : "text-slate-300 cursor-not-allowed"}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              );
            })}
            {!reports.length && <tr><td colSpan="4" className="text-center py-6 text-sm text-muted-foreground">No reports yet — click "+ New daily report" to start one for any date.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ------------------------------------------------------------
//  Detail page (the heart of Phase 2)
// ------------------------------------------------------------
function DailyReportDetail({ id, onBack }) {
  const [bundle, setBundle] = useState(null);
  const [available, setAvailable] = useState([]);
  const [busy, setBusy] = useState(false);
  // Bucket C3a Phase 3 — per-row submit errors keyed by row id. Cleared
  // on next submit attempt or when the user successfully edits the row.
  const [submitRowErrors, setSubmitRowErrors] = useState({});
  // Bucket C3b Phase 1 — quick-log coordination event dialog.
  const [coordOpen, setCoordOpen] = useState(false);
  // Bucket D Addition 5 (2026-05-12) -- locked-DR add reason for the
  // quick-log coordination dialog (lives at DR-detail scope so the
  // header button can open it on locked DRs too).
  const [coordAddReason, setCoordAddReason] = useState("");
  // Addition 3 -- no-work-day modal state. Opened when the server rejects
  // a submit with error="empty_dr_requires_no_work_day". On confirm we
  // resubmit with is_no_work_day=true + the reason/note.
  const [noWorkModal, setNoWorkModal] = useState(null); // null | {reason, note}

  const [reminders, setReminders] = useState([]);
  const reload = useCallback(async () => {
    const [{ data: ctx }, { data: avail }, remindersRes] = await Promise.all([
      api.get(`/daily-reports/${id}/context`),
      api.get(`/daily-reports/${id}/available-tasks`),
      api.get(`/daily-reports/${id}/delay-reminders`).catch(() => ({ data: [] })),
    ]);
    setBundle(ctx);
    setAvailable(avail);
    setReminders(Array.isArray(remindersRes.data) ? remindersRes.data : []);
  }, [id]);
  useEffect(() => { reload(); }, [reload]);

  // Bucket D Fix F -- gallery click-through highlight. After the bundle
  // renders, find the matching event row by data-testid, scroll into
  // view, add the saffron pulse class. Strip the URL params so refresh
  // doesn't re-trigger.
  // Bucket D Fix G -- ALSO honor ?createEvent=<panel-id> by scrolling
  // the named panel into view + pulsing it so the user can immediately
  // hit the panel's "+ Add" button. Panel ids: dr-safety-panel,
  // dr-qc-panel, dr-rfis-panel, dr-deliveries-panel, dr-photos-panel,
  // dr-coordination (handled via quick-log dialog instead, see below).
  useEffect(() => {
    if (!bundle) return;
    const params = new URLSearchParams(window.location.search);
    const eventId = params.get("openEvent");
    const eventType = params.get("openEventType");
    const createEvent = params.get("createEvent");
    if (!eventId && !createEvent) return;
    const t = setTimeout(() => {
      let el = null;
      if (eventId && eventType) {
        const candidates = [
          `[data-testid="${eventType}-row-${eventId}"]`,
          `[data-event-row-type="${eventType}"][data-event-row-id="${eventId}"]`,
        ];
        for (const sel of candidates) {
          el = document.querySelector(sel);
          if (el) break;
        }
      } else if (createEvent) {
        // Coordination uses the quick-log dialog rather than an inline panel.
        if (createEvent === "coordination") {
          setCoordOpen(true);
          return;
        }
        const panelMap = {
          delay: "dr-reminder-banner",
          safety: "dr-safety-panel",
          qc: "dr-qc-panel",
          rfi: "dr-rfis-panel",
          delivery: "dr-deliveries-panel",
          photo: "dr-photos-panel",
        };
        const panelTestId = panelMap[createEvent];
        if (panelTestId) {
          el = document.querySelector(`[data-testid="${panelTestId}"]`);
        }
      }
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("dr-event-highlight");
        setTimeout(() => el.classList.remove("dr-event-highlight"), 2200);
      }
    }, 250);
    // Strip the navigation params so refresh doesn't re-trigger.
    const url = new URL(window.location.href);
    url.searchParams.delete("openEvent");
    url.searchParams.delete("openEventType");
    url.searchParams.delete("createEvent");
    window.history.replaceState({}, "", url.toString());
    return () => clearTimeout(t);
  }, [bundle]);

  if (!bundle) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;

  const { report, project, grid, presence, photos, voice_annotations, delays,
          safety_events, qc_issues, rfis, deliveries, audit_log, user_labels } = bundle;
  const locked = report.status === "submitted" || report.status === "approved";
  const isRenovation = RENOVATION_TYPES.includes(project.project_type || "");

  const totalHours = grid.reduce((s, r) => s + (r.hours_worked || 0), 0);
  const totalHeadcount = presence.reduce((s, p) => s + (p.headcount || 0), 0);
  const totalCrewHours = presence.reduce((s, p) => s + (p.headcount || 0) * (p.hours_per_person || 0), 0);
  const completed = grid.filter((r) => (r.pct_complete_end_of_day || 0) >= 100).length;
  const ppc = grid.length ? Math.round((completed / grid.length) * 100) : 0;

  // Bucket D Addition 5 / Test 2G follow-up (2026-05-12) -- validate()
  // relaxed. Previously this function HARD-BLOCKED submit on three
  // conditions: empty weather, empty crew presence, empty grid. That
  // prevented legitimate no-work days from reaching the backend's
  // no-work-day modal flow (which is the spec'd UX for empty DRs).
  //
  // New rule: only block on LOGICAL CONTRADICTIONS that can't be a
  // no-work day. Empty DRs flow through to the server, which returns
  // 422 with error="empty_dr_requires_no_work_day" so the client
  // opens the no-work-day modal (see _submitWith's 422 handler).
  //
  // Hard-block cases:
  //   - Crew presence empty BUT grid has recorded progress
  //     (someone progressed activities with nobody present -- has to
  //      be one or the other)
  //   - Grid rows exist AND every row is untouched (zero progress,
  //     zero hours, zero status change) -- that's a "wasted submit"
  //     not a no-work day; user should mark the rows or remove them
  //
  // Soft (non-blocking) hints would go in a future "submit checklist"
  // sidebar -- for now we trust the no-work-day modal flow to handle
  // the empty cases.
  const validate = () => {
    const errors = [];
    const hasProgress = grid.some((r) =>
      (r.pct_complete_end_of_day ?? 0) > (r.pct_complete_start_of_day ?? 0)
      || (r.hours_worked || 0) > 0
      || (r.status_at_end_of_day && r.status_at_end_of_day !== "not_started")
    );
    if (presence.length === 0 && hasProgress) {
      errors.push(
        "Crew presence is empty but activities show recorded progress. "
        + "Either add a trade row to the presence panel, or unmark the activity "
        + "progress (zero progress + no crew = no-work day, which you can submit "
        + "via the no-work-day modal).",
      );
    }
    // The "every row untouched" case: only a hard-block when grid IS
    // non-empty. Empty grid is a valid no-work day candidate.
    const untouched = grid.filter((r) =>
      (r.pct_complete_end_of_day ?? null) === r.pct_complete_start_of_day
      && (r.hours_worked || 0) === 0
      && (!r.status_at_end_of_day || r.status_at_end_of_day === "not_started")
    );
    if (grid.length > 0 && untouched.length === grid.length) {
      errors.push(
        "Activity grid has rows but none show progress. Either record EOD%, "
        + "hours, or status on at least one row, or remove the rows and submit "
        + "as a no-work day.",
      );
    }
    return { ok: errors.length === 0, errors };
  };

  // Addition 3 -- shared submit core; payload defaults to {} (normal
  // submit) but the no-work-day modal calls it with is_no_work_day +
  // reason + note populated. Returns true on success.
  const _submitWith = async (payload) => {
    setBusy(true);
    setSubmitRowErrors({});
    try {
      await api.post(`/daily-reports/${id}/submit`, payload);
      toast.success(payload.is_no_work_day
        ? "Daily report submitted as a no-work day."
        : "Daily report submitted and locked.");
      setNoWorkModal(null);
      reload();
      return true;
    } catch (e) {
      const detail = e.response?.data?.detail;
      // Addition 3 + Test 2G follow-up -- empty DR opens the no-work-
      // day modal. We accept both the legacy `error` key and the new
      // `error_code` key (Test 2G renamed it for clarity); either
      // triggers the modal.
      if (
        e.response?.status === 422
        && detail && typeof detail === "object"
        && (detail.error === "empty_dr_requires_no_work_day"
            || detail.error_code === "empty_dr_requires_no_work_day_confirmation")
      ) {
        setNoWorkModal({ reason: "", note: "" });
        toast.info(
          "This DR has no activities or events. Pick a no-work-day reason to submit.",
        );
        return false;
      }
      // Bucket C3a Phase 3 — structured per-row errors render inline.
      if (
        e.response?.status === 422
        && detail && typeof detail === "object"
        && Array.isArray(detail.row_errors)
      ) {
        const byRow = {};
        for (const err of detail.row_errors) {
          if (!byRow[err.row_id]) byRow[err.row_id] = [];
          byRow[err.row_id].push(err);
        }
        setSubmitRowErrors(byRow);
        toast.error(detail.message || "Submit blocked — fix the rows highlighted in red.");
      } else {
        toast.error(detail?.message || detail || e.message || "Submit failed");
      }
      return false;
    } finally { setBusy(false); }
  };

  const submit = async () => {
    // Test 2G follow-up (2026-05-12) -- validate() now only flags
    // logical contradictions (crew-empty-with-progress, all-rows-
    // untouched). Empty DRs intentionally pass client validation and
    // get routed to the no-work-day modal via the server's 422
    // response handler in _submitWith (see "empty_dr_requires_no_
    // work_day" / "empty_dr_requires_no_work_day_confirmation").
    const { ok, errors } = validate();
    if (!ok) {
      toast.error("Cannot submit: " + errors[0]);
      return;
    }
    await _submitWith({});
  };

  // Addition 3 -- modal confirm: resubmit with is_no_work_day=true.
  const submitNoWorkDay = async () => {
    if (!noWorkModal) return;
    if (!noWorkModal.reason) {
      toast.error("Pick a reason.");
      return;
    }
    if (noWorkModal.reason === "other" && !(noWorkModal.note || "").trim()) {
      toast.error("Note is required when reason is 'other'.");
      return;
    }
    await _submitWith({
      is_no_work_day: true,
      no_work_reason: noWorkModal.reason,
      no_work_note: (noWorkModal.note || "").trim() || null,
    });
  };

  const approve = async () => {
    setBusy(true);
    try {
      await api.post(`/daily-reports/${id}/approve`);
      toast.success("Daily report approved.");
      reload();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Approve failed");
    } finally { setBusy(false); }
  };

  // Phase F — PDF export now takes a mode: "internal" (full) or "external"
  // (omits QC, Safety, and the rolled-forward delay reminders banner).
  const downloadPdf = async (mode = "internal") => {
    const res = await api.get(`/daily-reports/${id}/pdf-rich`, {
      params: { mode },
      responseType: "blob",
    });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = `daily_report_${report.report_date}_${mode}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div data-testid="dr-detail" className="space-y-5 dr-grid">
      {/* HEADER */}
      <Header report={report} project={project} userLabels={user_labels}
              ppc={ppc} totalHours={totalHours}
              totalHeadcount={totalHeadcount} totalCrewHours={totalCrewHours}
              locked={locked}
              onBack={onBack} onSubmit={submit} onApprove={approve} onDownload={downloadPdf}
              busy={busy} validate={validate}
              onQuickLogCoordination={() => setCoordOpen(true)} />

      {/* Bucket C3b Phase 1 — quick-log coordination event from this DR.
          Pre-links the event to this daily report on save so it shows up
          on the Coordination page filtered by linked_daily_report_id. */}
      <Dialog open={coordOpen} onOpenChange={(o) => { setCoordOpen(o); if (!o) setCoordAddReason(""); }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {locked ? "Quick-log coordination event (post-submit)" : "Quick-log coordination event"}
            </DialogTitle>
          </DialogHeader>
          {locked && (
            <div className="mb-2">
              <LockedAddReasonBlock value={coordAddReason} onChange={setCoordAddReason}
                eventLabel="a coordination event" testid="coord-add-reason" />
            </div>
          )}
          <CoordinationEventForm
            projectId={project.id}
            linkedDailyReportId={id}
            abbreviated={true}
            isLocked={locked}
            lockedAddReason={locked ? coordAddReason : null}
            onSaved={() => {
              setCoordOpen(false);
              setCoordAddReason("");
              toast.success(locked
                ? "Coordination event logged (post-submit). Audit chain captured."
                : "Coordination event logged");
            }}
            onCancel={() => { setCoordOpen(false); setCoordAddReason(""); }}
          />
        </DialogContent>
      </Dialog>

      {/* Addition 3 -- no-work-day acknowledgment modal. Triggered when
          the server rejects an empty-DR submit; user picks a reason and
          confirms, modal calls submitNoWorkDay() which re-submits with
          is_no_work_day=true. */}
      <Dialog open={!!noWorkModal} onOpenChange={(o) => !o && setNoWorkModal(null)}>
        <DialogContent className="max-w-md" data-testid="no-work-day-modal">
          <DialogHeader>
            <DialogTitle>This Daily Report has no activities or events</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <p className="text-slate-600">
              Submitting a blank DR without explanation looks sloppy in
              claim defense. File this as a no-work day so the record
              shows you knew it was blank and why.
            </p>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" checked readOnly disabled
                data-testid="no-work-ack" className="mt-0.5" />
              <span><strong>File as a no-work day</strong> (required to submit a blank DR)</span>
            </label>
            <div>
              <label className="field-label">Reason *</label>
              <select data-testid="no-work-reason"
                value={noWorkModal?.reason ?? ""}
                onChange={(e) => setNoWorkModal({ ...noWorkModal, reason: e.target.value })}
                className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                <option value="">— pick a reason —</option>
                <option value="weather">Weather (storm, extreme cold, snow, etc.)</option>
                <option value="holiday">Holiday</option>
                <option value="scheduled_non_work">Scheduled non-work day</option>
                <option value="crew_unavailable">Crew unavailable</option>
                <option value="tenant_restriction">Tenant or owner access restricted</option>
                <option value="other">Other (note required)</option>
              </select>
            </div>
            <div>
              <label className="field-label">
                Note {noWorkModal?.reason === "other" && <span className="text-red-600">*</span>}
              </label>
              <textarea data-testid="no-work-note"
                value={noWorkModal?.note ?? ""}
                onChange={(e) => setNoWorkModal({ ...noWorkModal, note: e.target.value })}
                placeholder={
                  noWorkModal?.reason === "other"
                    ? "Required: describe the reason."
                    : "Optional context."
                }
                className="w-full p-2 rounded border border-slate-200 text-sm h-20" />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button data-testid="no-work-cancel" onClick={() => setNoWorkModal(null)}
                className="h-9 px-3 rounded border border-slate-200 text-sm">
                Cancel
              </button>
              <button data-testid="no-work-confirm" onClick={submitNoWorkDay}
                disabled={busy}
                className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
                {busy ? "Submitting…" : "Submit as no-work day"}
              </button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* WEATHER */}
      <WeatherPanel report={report} dailyReportId={id} locked={locked} reload={reload} />

      {/* CREW SUMMARY (presence) */}
      <PresencePanel rows={presence} dailyReportId={id} locked={locked} reload={reload}
        projectId={project.id} grid={grid} reportDate={report.report_date} />

      {/* ACTIVITY GRID */}
      <ActivityGridPanel grid={grid} available={available} reload={reload}
        dailyReportId={id} locked={locked} reportDate={report.report_date}
        presence={presence} projectId={project.id}
        submitRowErrors={submitRowErrors} />

      {/* RENOVATION (only for renovation/tenant_improvement) */}
      {isRenovation && (
        <RenovationPanel report={report} dailyReportId={id} locked={locked} reload={reload} />
      )}

      {/* OPEN DELAY REMINDERS — only renders when reminders array non-empty */}
      <ReminderBanner reminders={reminders} reload={reload} />

      {/* INLINE EVENT PANELS */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <DelaysPanel rows={delays} dailyReportId={id} projectId={project.id}
                     reportDate={report.report_date} locked={locked} reload={reload} />
        <SafetyPanel rows={safety_events} dailyReportId={id} projectId={project.id}
                     reportDate={report.report_date} locked={locked} reload={reload}
                     grid={grid} />
        <QcPanel rows={qc_issues} dailyReportId={id} projectId={project.id}
                 reportDate={report.report_date} locked={locked} reload={reload}
                 grid={grid} />
        <RfisPanel rows={rfis} dailyReportId={id} projectId={project.id}
                   locked={locked} reload={reload} grid={grid} />
        <DeliveriesPanel rows={deliveries} dailyReportId={id} projectId={project.id}
                         reportDate={report.report_date} locked={locked} reload={reload}
                         grid={grid} />
        <NotesPanel report={report} dailyReportId={id} locked={locked} reload={reload} />
      </div>

      {/* PHOTOS + VOICE */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <PhotosPanel rows={photos} dailyReportId={id} projectId={project.id}
                     locked={locked} reload={reload} />
        <VoicePanel rows={voice_annotations} dailyReportId={id} reload={reload} />
      </div>

      {/* Bucket D Phase 4 -- supplements appendix. Lists every event
          filed against THIS DR after it was submitted (i.e., events
          whose parent_daily_report_id == this DR's id). */}
      <SupplementsAppendix dailyReportId={id} projectId={project.id} />

      {/* AUDIT LOG */}
      <AuditPanel rows={audit_log} userLabels={user_labels} />
    </div>
  );
}

// ------------------------------------------------------------
//  Panels
// ------------------------------------------------------------
function StatusPill({ status }) {
  const klass = status === "approved" ? "pill-green"
    : status === "submitted" ? "pill-blue"
    : "pill-slate";
  return <span data-testid={`dr-status-${status}`} className={`pill ${klass}`}>{status}</span>;
}

function Header({ report, project, userLabels, ppc, totalHours,
                  totalHeadcount, totalCrewHours, locked, onBack, onSubmit,
                  onApprove, onDownload, busy, validate, onQuickLogCoordination }) {
  const v = validate();
  return (
    <div className="surface p-4" data-testid="dr-header">
      <div className="flex flex-wrap items-start gap-4">
        <button onClick={onBack} className="text-sm text-blue-600 hover:underline flex items-center gap-1">
          <ChevronLeft className="h-4 w-4" /> Back
        </button>
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Project</div>
          <div className="font-semibold">{project.name}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Report date</div>
          <div className="font-mono text-base">{formatDateDisplay(report.report_date)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Status</div>
          <div className="flex items-center gap-1">
            <StatusPill status={report.status} />
            {locked && <Lock className="h-3.5 w-3.5 text-slate-500" />}
          </div>
          {report.is_no_work_day && (
            <div data-testid="dr-no-work-pill"
              className="mt-1 inline-block px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-300 text-[10px] uppercase tracking-wide"
              title={report.no_work_note || "No-work day"}>
              No-work day · {(report.no_work_reason || "").replace(/_/g, " ")}
            </div>
          )}
        </div>
        <div className="hidden md:block min-w-[180px]">
          <div className="text-xs text-slate-500 uppercase tracking-wide">Trail</div>
          <div className="text-[11px] text-slate-600 leading-snug">
            <div>Prepared: {userLabels[report.prepared_by] || "—"}</div>
            <div>Submitted: {userLabels[report.submitted_by] || "—"}</div>
            <div>Approved: {userLabels[report.approved_by] || "—"}</div>
          </div>
        </div>
        <div className="ml-auto flex flex-wrap gap-3">
          <KPI label="PPC" value={`${ppc}%`} testid="kpi-ppc" />
          <KPI label="Headcount" value={totalHeadcount} testid="kpi-headcount" />
          <KPI label="Crew hours" value={totalCrewHours.toFixed(1)} testid="kpi-crew-hours" />
          <KPI label="Activity hrs" value={totalHours.toFixed(1)} testid="kpi-activity-hours" />
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-slate-100 flex flex-wrap gap-2 items-center">
        {!v.ok && !locked && (
          <div data-testid="dr-validation-warning" className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 flex items-center gap-1">
            <AlertTriangle className="h-3.5 w-3.5" />
            {v.errors.length} item{v.errors.length === 1 ? "" : "s"} blocking submit: {v.errors[0]}
          </div>
        )}
        <div className="ml-auto flex gap-2">
          {/* Bucket C3b Phase 1 — quick-log a coordination event from
              the daily report. Pre-links to this DR; user can expand
              to full form on the Coordination page later. */}
          {onQuickLogCoordination && (
            <button data-testid="dr-quick-log-coord" onClick={onQuickLogCoordination}
              className="h-9 px-3 rounded border border-slate-200 bg-white text-sm flex items-center gap-1 hover:bg-slate-50">
              <MessageSquare className="h-4 w-4" />
              {locked ? "+ Coordination (post-submit)" : "+ Coordination"}
            </button>
          )}
          <button data-testid="dr-download-pdf-internal" onClick={() => onDownload("internal")} disabled={busy}
            className="h-9 px-3 rounded bg-blue-600 text-white text-sm flex items-center gap-1 hover:bg-blue-700">
            <Download className="h-4 w-4" /> Export for Internal
          </button>
          <button data-testid="dr-download-pdf-external" onClick={() => onDownload("external")} disabled={busy}
            className="h-9 px-3 rounded border border-slate-200 bg-white text-sm flex items-center gap-1 hover:bg-slate-50">
            <Download className="h-4 w-4" /> Export for External
          </button>
          {report.status === "draft" && (
            <button data-testid="dr-submit" onClick={onSubmit} disabled={busy}
              className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
              Submit
            </button>
          )}
          {report.status === "submitted" && (
            <button data-testid="dr-approve" onClick={onApprove} disabled={busy}
              className="h-9 px-3 rounded bg-green-600 text-white text-sm hover:bg-green-700 disabled:opacity-50 flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4" /> Approve
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function KPI({ label, value, testid }) {
  return (
    <div className="px-3 py-1.5 bg-slate-50 border border-slate-200 rounded text-center min-w-[80px]" data-testid={testid}>
      <div className="text-[10px] text-slate-500 uppercase tracking-wide">{label}</div>
      <div className="font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function PanelShell({ icon: Icon, title, count, testid, children, badge }) {
  return (
    <div data-testid={testid} className="surface p-4">
      <div className="flex items-center gap-2 mb-3">
        <Icon className="h-4 w-4 text-slate-500" />
        <div className="font-medium text-sm">{title}</div>
        {typeof count === "number" && <span className="text-xs text-slate-500">({count})</span>}
        {badge}
      </div>
      {children}
    </div>
  );
}

function WeatherPanel({ report, dailyReportId, locked, reload }) {
  const [busy, setBusy] = useState(false);
  const autoFill = async () => {
    setBusy(true);
    try {
      await api.post(`/daily-reports/${dailyReportId}/weather-autofill`);
      toast.success("Weather pulled from forecast.");
      reload();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Weather provider unreachable");
    } finally { setBusy(false); }
  };
  const patch = async (fields) => {
    try { await api.patch(`/daily-reports/${dailyReportId}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };

  const fetchedAt = report.weather_fetched_at ? new Date(report.weather_fetched_at).toLocaleTimeString() : null;
  return (
    <PanelShell icon={Cloud} title="Weather" testid="dr-weather-panel"
      badge={fetchedAt && <span className="text-[10px] text-slate-400 ml-auto">fetched {fetchedAt}</span>}>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Field label="Conditions" value={report.weather_conditions || ""} onChange={(v) => patch({ weather_conditions: v })} disabled={locked} testid="weather-conditions" />
        <Field label="High °F" type="number" value={report.weather_temp_high_f ?? ""} onChange={(v) => patch({ weather_temp_high_f: v ? Number(v) : null })} disabled={locked} testid="weather-high" />
        <Field label="Low °F" type="number" value={report.weather_temp_low_f ?? ""} onChange={(v) => patch({ weather_temp_low_f: v ? Number(v) : null })} disabled={locked} testid="weather-low" />
        <Field label="Precip in" type="number" value={report.weather_precipitation_in ?? ""} onChange={(v) => patch({ weather_precipitation_in: v ? Number(v) : null })} disabled={locked} testid="weather-precip" />
        <Field label="Wind mph" type="number" value={report.weather_wind_mph ?? ""} onChange={(v) => patch({ weather_wind_mph: v ? Number(v) : null })} disabled={locked} testid="weather-wind" />
      </div>
      {!locked && (
        <button data-testid="weather-autofill" onClick={autoFill} disabled={busy}
          className="mt-3 h-8 px-3 rounded bg-slate-900 text-white text-xs flex items-center gap-1 hover:bg-slate-800 disabled:opacity-50">
          <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} /> Auto-fill from forecast
        </button>
      )}
    </PanelShell>
  );
}

const PRESENCE_SHIFTS = ["first", "second", "third", "weekend", "holiday", "off_hours"];

function PresencePanel({ rows, dailyReportId, locked, reload, projectId, grid = [], reportDate }) {
  const [draft, setDraft] = useState({ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "" });
  // Bucket H Phase 4 -- demand-vs-actual comparison. Fetch per-project
  // demand for this DR's date, group by trade, render alongside actual
  // headcount totals from presence rows.
  const [demandByTrade, setDemandByTrade] = useState(null);
  useEffect(() => {
    if (!projectId || !reportDate) { setDemandByTrade(null); return; }
    api.get(`/resource-leveling/projects/${projectId}/demand`, {
      params: { date_range_start: reportDate, date_range_end: reportDate },
    }).then((r) => {
      const matrix = r.data?.matrix || {};
      const byTrade = {};
      Object.entries(matrix).forEach(([trade, byDate]) => {
        const slot = byDate[reportDate];
        if (slot && slot.demanded > 0) byTrade[trade] = slot;
      });
      setDemandByTrade(byTrade);
    }).catch(() => setDemandByTrade(null));
  }, [projectId, reportDate]);
  const actualByTrade = rows.reduce((acc, r) => {
    const t = r.trade;
    if (!t) return acc;
    acc[t] = (acc[t] || 0) + (r.headcount || 0);
    return acc;
  }, {});
  // Bucket C3b Phase 3 — detailed roster toggle. When on, the table
  // shows zone / shift / activity columns and the add-row form gets the
  // same fields. Remembered per session via localStorage so users who
  // prefer the granular view don't have to flip it every time.
  const [showDetailed, setShowDetailed] = useState(() => {
    try { return localStorage.getItem("ice.presence.detailed") === "1"; } catch { return false; }
  });
  const [zones, setZones] = useState([]);

  useEffect(() => {
    if (!showDetailed || !projectId) return;
    api.get(`/projects/${projectId}/locations`).then(({ data }) => {
      setZones(Array.isArray(data) ? data : []);
    }).catch(() => setZones([]));
  }, [showDetailed, projectId]);

  const toggleDetailed = () => {
    const next = !showDetailed;
    setShowDetailed(next);
    try { localStorage.setItem("ice.presence.detailed", next ? "1" : "0"); } catch { /* ignore */ }
  };

  const total = rows.reduce((s, r) => s + (r.headcount || 0), 0);
  const totalHours = rows.reduce((s, r) => s + (r.headcount || 0) * (r.hours_per_person || 0), 0);

  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  const add = async () => {
    if (busy) return;
    if (!draft.trade) return toast.error("Trade is required");
    setBusy(true);
    try {
      await api.post(`/daily-reports/${dailyReportId}/presence`, { ...draft, daily_report_id: dailyReportId });
      setDraft({ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "",
                 zone_id: undefined, zone_name: undefined, shift: "first", schedule_activity_id: undefined });
      reload();
    } catch (e) { toast.error(e.response?.data?.detail || "Add failed"); }
    finally { setBusy(false); }
  };
  const patch = async (rid, fields) => {
    try { await api.patch(`/daily-presence/${rid}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };
  const remove = async (rid) => {
    if (!window.confirm("Remove this presence row?")) return;
    await api.delete(`/daily-presence/${rid}`); reload();
  };

  const taskOptions = grid.map((g) => ({
    id: g.schedule_activity_id, code: g.activity_code, desc: g.activity_description,
  })).filter((t) => t.id);

  return (
    <PanelShell icon={Users} title="Crew presence" testid="dr-presence-panel" count={rows.length}
      badge={
        <div className="ml-2 flex items-center gap-3">
          <span className="text-[10px] text-slate-500">total: {total} workers · {totalHours.toFixed(1)} hours</span>
          <button data-testid="presence-toggle-detailed" onClick={toggleDetailed}
            className="text-[10px] text-blue-600 hover:underline">
            {showDetailed ? "Hide detailed roster" : "Show detailed roster"}
          </button>
        </div>
      }>
      {/* Bucket H Phase 4 -- demand-vs-actual comparison. Only rendered
          when the project has declared trade demand on this DR's date.
          Each row shows trade / demanded / actual / delta with color
          tone. Clicking a row routes to Resource Leveling. */}
      {demandByTrade && Object.keys(demandByTrade).length > 0 && (
        <div data-testid="dr-presence-demand-vs-actual"
          className="mb-2 p-2 rounded bg-slate-50 border border-slate-200 text-xs">
          <div className="flex items-center gap-2 mb-1 text-slate-700">
            <Users className="h-3 w-3" />
            <strong>Demand vs actual today</strong>
            <Link to={`/resource-leveling?project_id=${projectId}`}
              className="ml-auto text-blue-600 hover:underline">
              Open Resource Leveling →
            </Link>
          </div>
          <table className="w-full text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="text-left">Trade</th>
                <th className="text-right">Demanded</th>
                <th className="text-right">Actual</th>
                <th className="text-right">Delta</th>
                <th className="text-left">Status</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(demandByTrade).map(([trade, info]) => {
                const actual = actualByTrade[trade] || 0;
                const delta = actual - info.demanded;
                const tone = delta >= 0 ? "text-emerald-700"
                  : delta >= -1 ? "text-amber-700"
                  : "text-rose-700 font-semibold";
                const label = delta >= 0 ? "fully staffed"
                  : delta >= -1 ? "1 short"
                  : `${Math.abs(delta)} short`;
                return (
                  <tr key={trade} data-testid={`dr-presence-trade-${trade}`} className="border-t border-slate-100">
                    <td className="py-0.5">{trade}{info.on_cp && <span className="ml-1 text-rose-600 text-[10px]">CP</span>}</td>
                    <td className="text-right tabular-nums">{info.demanded}</td>
                    <td className="text-right tabular-nums">{actual}</td>
                    <td className={`text-right tabular-nums ${tone}`}>{delta >= 0 ? `+${delta}` : delta}</td>
                    <td className={tone}>{label}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="dense-table">
          <thead>
            <tr>
              <th>Trade</th>
              <th>Company</th>
              <th>Headcount</th>
              <th>Hrs/person</th>
              <th>Sub-total</th>
              {showDetailed && <th title="C3b Phase 3 — granular layer">Zone</th>}
              {showDetailed && <th>Shift</th>}
              {showDetailed && <th>Activity</th>}
              <th>Notes</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const isAuto = (r.notes || "").startsWith("Auto-created");
              return (
                <tr key={r.id} data-testid={`presence-row-${r.id}`} className={isAuto ? "bg-blue-50/30" : undefined}>
                  <td>
                    <input data-testid={`presence-trade-${r.id}`} defaultValue={r.trade}
                      onBlur={(e) => patch(r.id, { trade: e.target.value })} disabled={locked}
                      className="w-32 px-2 py-1 border border-slate-200 rounded text-sm" />
                  </td>
                  <td><input defaultValue={r.company_name || ""} onBlur={(e) => patch(r.id, { company_name: e.target.value })} disabled={locked} className="w-44 px-2 py-1 border border-slate-200 rounded text-sm" /></td>
                  <td><input type="number" defaultValue={r.headcount} onBlur={(e) => patch(r.id, { headcount: Number(e.target.value) })} disabled={locked} className="w-20 px-2 py-1 border border-slate-200 rounded text-sm tabular-nums" /></td>
                  <td><input type="number" step="0.5" defaultValue={r.hours_per_person} onBlur={(e) => patch(r.id, { hours_per_person: Number(e.target.value) })} disabled={locked} className="w-20 px-2 py-1 border border-slate-200 rounded text-sm tabular-nums" /></td>
                  <td className="tabular-nums text-sm">{(r.total_hours ?? (r.headcount || 0) * (r.hours_per_person || 0)).toFixed(1)}</td>
                  {showDetailed && (
                    <td>
                      <select data-testid={`presence-zone-${r.id}`} defaultValue={r.zone_id || ""}
                        onChange={(e) => {
                          const z = zones.find((zz) => zz.id === e.target.value);
                          patch(r.id, { zone_id: e.target.value || null, zone_name: z?.name || null });
                        }} disabled={locked}
                        className="text-xs h-7 px-1 border border-slate-200 rounded bg-white min-w-[110px]">
                        <option value="">— no zone —</option>
                        {zones.map((z) => <option key={z.id} value={z.id}>{z.code ? `${z.code} ${z.name}` : z.name}</option>)}
                      </select>
                    </td>
                  )}
                  {showDetailed && (
                    <td>
                      <select data-testid={`presence-shift-${r.id}`} defaultValue={r.shift || "first"}
                        onChange={(e) => patch(r.id, { shift: e.target.value })} disabled={locked}
                        className="text-xs h-7 px-1 border border-slate-200 rounded bg-white">
                        {PRESENCE_SHIFTS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
                      </select>
                    </td>
                  )}
                  {showDetailed && (
                    <td>
                      <select data-testid={`presence-activity-${r.id}`} defaultValue={r.schedule_activity_id || ""}
                        onChange={(e) => patch(r.id, { schedule_activity_id: e.target.value || null })} disabled={locked}
                        className="text-xs h-7 px-1 border border-slate-200 rounded bg-white min-w-[120px]">
                        <option value="">— project-wide —</option>
                        {taskOptions.map((t) => <option key={t.id} value={t.id}>[{t.code}] {(t.desc || "").slice(0, 30)}</option>)}
                      </select>
                    </td>
                  )}
                  <td><input defaultValue={r.notes || ""} onBlur={(e) => patch(r.id, { notes: e.target.value })} disabled={locked} className="w-44 px-2 py-1 border border-slate-200 rounded text-sm" /></td>
                  <td>{!locked && <button data-testid={`presence-delete-${r.id}`} onClick={() => remove(r.id)} className="text-red-600 hover:bg-red-50 p-1 rounded"><Trash2 className="h-3.5 w-3.5" /></button>}</td>
                </tr>
              );
            })}
            {!rows.length && <tr><td colSpan={showDetailed ? "10" : "7"} className="text-center py-4 text-sm text-slate-500">No presence yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {!locked && (
        <div className="mt-3 flex flex-wrap gap-2 items-end pt-3 border-t border-slate-100">
          <Field label="Trade" value={draft.trade} onChange={(v) => setDraft({ ...draft, trade: v })} testid="presence-new-trade" />
          <Field label="Company" value={draft.company_name} onChange={(v) => setDraft({ ...draft, company_name: v })} testid="presence-new-company" />
          <Field label="Headcount" type="number" value={draft.headcount} onChange={(v) => setDraft({ ...draft, headcount: Number(v) })} testid="presence-new-headcount" />
          <Field label="Hrs/person" type="number" value={draft.hours_per_person} onChange={(v) => setDraft({ ...draft, hours_per_person: Number(v) })} testid="presence-new-hours" />
          {showDetailed && (
            <>
              <label className="text-xs flex flex-col gap-1">
                <span className="text-slate-500">Zone</span>
                <select value={draft.zone_id || ""}
                  onChange={(e) => {
                    const z = zones.find((zz) => zz.id === e.target.value);
                    setDraft({ ...draft, zone_id: e.target.value || null, zone_name: z?.name || null });
                  }}
                  className="h-8 px-2 border border-slate-200 rounded text-sm bg-white">
                  <option value="">— no zone —</option>
                  {zones.map((z) => <option key={z.id} value={z.id}>{z.code ? `${z.code} ${z.name}` : z.name}</option>)}
                </select>
              </label>
              <label className="text-xs flex flex-col gap-1">
                <span className="text-slate-500">Shift</span>
                <select value={draft.shift || "first"}
                  onChange={(e) => setDraft({ ...draft, shift: e.target.value })}
                  className="h-8 px-2 border border-slate-200 rounded text-sm bg-white">
                  {PRESENCE_SHIFTS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
                </select>
              </label>
              <label className="text-xs flex flex-col gap-1">
                <span className="text-slate-500">Activity</span>
                <select value={draft.schedule_activity_id || ""}
                  onChange={(e) => setDraft({ ...draft, schedule_activity_id: e.target.value || null })}
                  className="h-8 px-2 border border-slate-200 rounded text-sm bg-white">
                  <option value="">— project-wide —</option>
                  {taskOptions.map((t) => <option key={t.id} value={t.id}>[{t.code}] {(t.desc || "").slice(0, 30)}</option>)}
                </select>
              </label>
            </>
          )}
          <button data-testid="presence-add" onClick={add} disabled={busy} className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1">
            <Plus className="h-3.5 w-3.5" /> Add trade
          </button>
        </div>
      )}
    </PanelShell>
  );
}

// Phase E — Crew column on the activity grid: trade dropdown sourced from
// the Crew presence panel + an auto-filled-but-editable headcount input.
// Picking a trade sets linked_crew_id AND crew_count (server PATCH does both).
function CrewSelector({ row, presence, locked, updateRow }) {
  const onPickTrade = (e) => {
    const presenceId = e.target.value || null;
    const picked = presence.find((p) => p.id === presenceId);
    const fields = { linked_crew_id: presenceId };
    // Auto-fill headcount from picked presence row (super can override after)
    if (picked && typeof picked.headcount === "number") {
      fields.crew_count = picked.headcount;
    } else if (!presenceId) {
      // Cleared selection — leave crew_count as-is so the user doesn't lose
      // a manual override they typed earlier.
    }
    updateRow(row.id, fields);
  };
  const onChangeHeadcount = (e) => {
    updateRow(row.id, { crew_count: Number(e.target.value) });
  };
  return (
    <div className="flex flex-col gap-1 min-w-[140px]">
      <select
        data-testid={`dr-crew-trade-${row.activity_code}`}
        value={row.linked_crew_id || ""}
        onChange={onPickTrade}
        disabled={locked}
        className="h-7 text-[11px] px-1 border border-slate-200 rounded bg-white"
      >
        <option value="">— no crew assigned —</option>
        {presence.map((p) => (
          <option key={p.id} value={p.id}>
            {p.trade}{p.company_name ? ` — ${p.company_name}` : ""}
          </option>
        ))}
      </select>
      <input
        data-testid={`dr-crew-${row.activity_code}`}
        type="number"
        defaultValue={row.crew_count}
        key={`hc-${row.linked_crew_id || "none"}-${row.crew_count}`}
        onBlur={onChangeHeadcount}
        disabled={locked}
        title="Headcount on this activity (auto-filled from picked crew, but editable)"
        className="w-full h-6 text-xs px-1 border border-slate-200 rounded tabular-nums"
      />
    </div>
  );
}

// Bucket C3a Phase 2 — controlled constraint vocabulary; mirrors the
// backend ConstraintType Literal in models.py.
const CONSTRAINT_TYPES = [
  { value: "not_constrained", label: "(none — not constrained)" },
  { value: "awaiting_rfi_response", label: "Awaiting RFI response" },
  { value: "awaiting_submittal_return", label: "Awaiting submittal return" },
  { value: "awaiting_bulletin_proposal", label: "Awaiting bulletin proposal" },
  { value: "awaiting_bulletin_approval", label: "Awaiting bulletin approval" },
  { value: "awaiting_ae_direction", label: "Awaiting A/E direction" },
  { value: "awaiting_shutdown_window", label: "Awaiting shutdown window" },
  { value: "awaiting_tenant_access", label: "Awaiting tenant access" },
  { value: "awaiting_clearance", label: "Awaiting clearance" },
  { value: "awaiting_predecessor_completion", label: "Awaiting predecessor completion" },
  { value: "concealed_condition_under_investigation", label: "Concealed condition under investigation" },
  { value: "weather_impact", label: "Weather impact" },
  { value: "labor_unavailability", label: "Labor unavailability" },
  { value: "material_not_on_site", label: "Material not on site" },
  { value: "other", label: "Other (free text)" },
];

// Followup-fix 1 (2026-05-07): full set per backend ConstraintType validator.
// The backend requires constraint_reference_entity_id for ANY type that's
// not 'not_constrained' and not 'other'. The earlier subset of 6 types
// dead-ended 7 types in the UI (user could pick the type, no Reference
// dropdown rendered, save 422'd permanently with no recovery path).
const CONSTRAINT_TYPES_NEEDING_REF = new Set([
  "awaiting_rfi_response", "awaiting_submittal_return",
  "awaiting_bulletin_proposal", "awaiting_bulletin_approval",
  "awaiting_ae_direction", "awaiting_shutdown_window",
  "awaiting_tenant_access", "awaiting_clearance",
  "awaiting_predecessor_completion",
  "concealed_condition_under_investigation",
  "weather_impact", "labor_unavailability", "material_not_on_site",
]);

// Bucket C3a Phase 2 — Constraint cell on the activity grid. Stacks
// type dropdown + (reference dropdown OR other-text) + impact-hours
// vertically inside a single cell.
//
// Followup-fix 1 (2026-05-07): defer the save when the user changes
// constraint_type to one that needs a follow-up (reference for the
// awaiting_*/concealed_*/weather/labor/material types, description
// for 'other') AND the row doesn't yet carry a valid follow-up. The
// pending type lives in local state until the follow-up is provided;
// then both fields are sent in one PATCH so backend cross-field
// validation passes. Without this, the type-change PATCH 422'd before
// the user could fill in the follow-up.
//
// Reference options eager-load on first render of a needs-ref state
// so the dropdown is immediately usable instead of requiring focus.
function ConstraintCell({ row, projectId, locked, updateRow }) {
  const taskId = row.schedule_activity_id;
  const persistedType = row.constraint_type || "not_constrained";

  const [pendingType, setPendingType] = useState(null);
  const [pendingDesc, setPendingDesc] = useState(null);

  // Reset pending state when the underlying row identity / persisted
  // value changes (e.g., another tab edited, or we just reloaded after
  // a successful save).
  useEffect(() => {
    setPendingType(null);
    setPendingDesc(null);
  }, [row.id, persistedType, row.constraint_reference_entity_id, row.constraint_other_description]);

  const ct = pendingType || persistedType;
  const needsRef = CONSTRAINT_TYPES_NEEDING_REF.has(ct);
  const isOther = ct === "other";
  const hasValidRef = !!row.constraint_reference_entity_id;
  const hasValidDesc = !!(row.constraint_other_description && String(row.constraint_other_description).trim());
  const pending = !!pendingType || pendingDesc !== null;

  const [refOptions, setRefOptions] = useState([]);
  const [refLoaded, setRefLoaded] = useState(false);
  const [refLoading, setRefLoading] = useState(false);

  const ensureRefs = async () => {
    if (refLoaded || refLoading || !projectId || !taskId) return;
    setRefLoading(true);
    try {
      const { data } = await api.get(`/projects/${projectId}/gating-entities`, {
        params: { task_id: taskId, open_only: true },
      });
      setRefOptions(Array.isArray(data) ? data : []);
    } catch (e) {
      setRefOptions([]);
    } finally { setRefLoaded(true); setRefLoading(false); }
  };

  // Eager-load gating options the moment the user picks a needs-ref
  // type so the dropdown is ready without requiring a second focus.
  useEffect(() => { if (needsRef) ensureRefs(); /* eslint-disable-next-line */ }, [needsRef, taskId, projectId]);

  const setType = (newType) => {
    // Path A — clearing the constraint. Always saves immediately;
    // backend nulls the dependents.
    if (newType === "not_constrained") {
      setPendingType(null);
      setPendingDesc(null);
      updateRow(row.id, {
        constraint_type: "not_constrained",
        constraint_reference_entity_id: null,
        constraint_other_description: null,
      });
      return;
    }
    // Path B — switching to 'other'. Save now if the row already has
    // a non-empty description (e.g., user is just re-confirming the
    // type); else hold pending until the description blur.
    if (newType === "other") {
      if (hasValidDesc) {
        setPendingType(null);
        updateRow(row.id, {
          constraint_type: "other",
          constraint_reference_entity_id: null,
        });
      } else {
        setPendingType("other");
      }
      return;
    }
    // Path C — switching to a needs-ref type. Save now if the row
    // already has a reference (re-confirm); else hold pending until
    // the user picks a reference.
    if (CONSTRAINT_TYPES_NEEDING_REF.has(newType)) {
      if (hasValidRef) {
        setPendingType(null);
        updateRow(row.id, {
          constraint_type: newType,
          constraint_other_description: null,
        });
      } else {
        setPendingType(newType);
      }
      return;
    }
    // Fallthrough (shouldn't happen given the enum) — save raw.
    setPendingType(null);
    updateRow(row.id, { constraint_type: newType });
  };

  const setRef = (refId) => {
    if (!refId) return;
    const typeToSend = pendingType || persistedType;
    setPendingType(null);
    updateRow(row.id, {
      constraint_type: typeToSend,
      constraint_reference_entity_id: refId,
      constraint_other_description: null,
    });
  };

  const commitDesc = (raw) => {
    const desc = (raw || "").trim();
    setPendingDesc(null);
    if (!desc && pendingType !== "other") {
      // Description cleared on an already-persisted 'other' row; fall
      // back to backend validation path by sending null + current type
      // -- backend will 422 if type stays 'other'. That's the right
      // signal since the row would be invalid.
      updateRow(row.id, { constraint_other_description: null });
      return;
    }
    if (!desc) {
      // 'other' with empty desc -- don't fire a save, surface inline
      // hint instead. Restore pending so the input renders.
      setPendingType("other");
      return;
    }
    setPendingType(null);
    updateRow(row.id, {
      constraint_type: "other",
      constraint_other_description: desc,
      constraint_reference_entity_id: null,
    });
  };

  return (
    <div className="flex flex-col gap-1 min-w-[200px]" data-testid={`dr-constraint-${row.activity_code}`}>
      <select
        data-testid={`dr-constraint-type-${row.activity_code}`}
        value={ct}
        onChange={(e) => setType(e.target.value)}
        onFocus={ensureRefs}
        disabled={locked}
        className={`h-7 text-[11px] px-1 border rounded ${row.constraint_auto_filled ? "border-blue-300 bg-blue-50" : pending ? "border-amber-300 bg-amber-50" : "border-slate-200 bg-white"}`}
        title={row.constraint_auto_filled
          ? "Auto-filled from open gating entity. Override to take ownership."
          : pending ? "Pending — pick reference or fill description to save."
          : undefined}
      >
        {CONSTRAINT_TYPES.map((c) => (
          <option key={c.value} value={c.value}>{c.label}</option>
        ))}
      </select>

      {needsRef && (
        <select
          data-testid={`dr-constraint-ref-${row.activity_code}`}
          value={row.constraint_reference_entity_id || ""}
          onChange={(e) => setRef(e.target.value)}
          onFocus={ensureRefs}
          disabled={locked}
          className={`h-7 text-[11px] px-1 border rounded bg-white ${pending && !hasValidRef ? "border-amber-400" : "border-slate-200"}`}
        >
          <option value="" disabled={refLoaded && refOptions.length === 0}>
            {refLoading ? "— loading… —"
              : refLoaded
                ? (refOptions.length === 0
                    ? "No open gating entities for this task"
                    : `— pick (${refOptions.length} open) —`)
                : "— loading… —"}
          </option>
          {refOptions.map((o) => (
            <option key={o.id} value={o.id}>
              {o.entity_number ? `${o.entity_number}: ` : ""}{(o.display_label || "").slice(0, 60)}
              {typeof o.current_age_working_days === "number" ? ` · ${o.current_age_working_days}wd` : ""}
              {o.is_over_sla ? " · OVER SLA" : ""}
            </option>
          ))}
        </select>
      )}

      {isOther && (
        <input
          data-testid={`dr-constraint-other-${row.activity_code}`}
          type="text"
          defaultValue={row.constraint_other_description || ""}
          onChange={(e) => setPendingDesc(e.target.value)}
          onBlur={(e) => commitDesc(e.target.value)}
          disabled={locked}
          placeholder="Describe the constraint"
          className={`h-7 text-[11px] px-1 border rounded ${pending && !hasValidDesc ? "border-amber-400 bg-amber-50" : "border-slate-200"}`}
        />
      )}

      {pending && (
        <div className="text-[10px] text-amber-700" data-testid={`dr-constraint-pending-${row.activity_code}`}>
          {needsRef && !hasValidRef ? "Pick reference to save"
            : isOther && !hasValidDesc ? "Type description to save"
            : "Pending…"}
        </div>
      )}

      {ct !== "not_constrained" && (
        <input
          data-testid={`dr-constraint-hours-${row.activity_code}`}
          type="number"
          min="0"
          step="0.5"
          defaultValue={row.production_impact_hours ?? ""}
          onBlur={(e) => updateRow(row.id, { production_impact_hours: e.target.value === "" ? null : Number(e.target.value) })}
          disabled={locked}
          placeholder="impact hrs"
          title="Crew-hours lost on this activity due to the constraint"
          className="h-6 text-[11px] px-1 border border-slate-200 rounded tabular-nums"
        />
      )}
    </div>
  );
}

function ActivityGridPanel({ grid, available, reload, dailyReportId, locked, reportDate, presence = [], projectId, submitRowErrors = {} }) {
  // Phase 2 — alias so the ConstraintCell call site reads cleanly.
  const dailyReportProjectId = projectId;
  const [picking, setPicking] = useState("");
  const [unplanned, setUnplanned] = useState({ description: "", trade_code: "", duration_days: 1 });
  const [refreshing, setRefreshing] = useState(false);
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture
  // for activity grid rows (both planned + unplanned). Reused across the
  // two add forms below so a single reason captures both kinds.
  const [addReason, setAddReason] = useState("");
  // Bucket C1 Phase 1 — rows that the backend rejected with 409 because
  // the linked task was modified externally between grid load and PATCH.
  // Cleared by reload() (which pulls a fresh task_updated_at via /context).
  const [staleRows, setStaleRows] = useState(() => new Set());

  const refreshFromSchedule = async () => {
    setRefreshing(true);
    try {
      const { data } = await api.post(`/daily-reports/${dailyReportId}/refresh-grid`);
      toast.success(data.inserted ? `${data.inserted} activity row(s) added from schedule.` : "Already up to date.");
      setStaleRows(new Set());
      reload();
    } catch (e) { toast.error(formatErr(e, "Refresh grid")); }
    finally { setRefreshing(false); }
  };

  const updateRow = async (rowId, fields) => {
    // Bucket C1 Phase 1 — attach the optimistic-lock token captured
    // from /context. If the linked task has moved underneath us, the
    // backend rejects with 409 and we mark the row stale so the user
    // sees a "Schedule updated — refresh row" hint.
    //
    // Bucket D Addition 5 (2026-05-12) -- on a LOCKED DR, the inline
    // grid inputs route through this updater. The PATCH endpoint now
    // requires an edit_reason on locked DRs; quick inline edits won't
    // carry one, so this code path is intended for DRAFT DRs only.
    // Locked-DR edits go through the row's "edit" button which opens
    // LockedEventEditModal with the edit_reason capture (see the
    // table-row `locked && <button>edit</button>` chunk further down).
    const row = grid.find((r) => r.id === rowId) || {};
    const payload = { ...fields };
    if (row.task_updated_at) payload.task_last_modified_at = row.task_updated_at;
    try {
      await api.patch(`/daily-report-activities/${rowId}`, payload);
      // Successful write; clear any stale flag and pull fresh data.
      if (staleRows.has(rowId)) {
        const next = new Set(staleRows); next.delete(rowId); setStaleRows(next);
      }
      reload();
    } catch (e) {
      if (e.response?.status === 409) {
        const next = new Set(staleRows); next.add(rowId); setStaleRows(next);
        toast.error("Schedule was updated externally — click Refresh on the row to pull the latest before re-entering.");
      } else {
        toast.error(formatErr(e, "Update row"));
      }
    }
  };
  const refreshStaleRow = () => {
    // Easiest reliable refresh: pull /context again. Clears the
    // stale set and gets a fresh task_updated_at for every row.
    setStaleRows(new Set());
    reload();
  };
  const removeRow = async (rowId) => {
    if (!window.confirm("Remove from grid?")) return;
    try { await api.delete(`/daily-report-activities/${rowId}`); reload(); }
    catch (e) { toast.error(formatErr(e, "Remove row")); }
  };
  const addActivity = async () => {
    if (!picking) return;
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    try {
      await api.post(`/daily-reports/${dailyReportId}/grid`, {
        schedule_activity_id: picking,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setPicking("");
      if (locked) { setAddReason(""); toast.success("Activity row added (post-submit). Audit chain captured."); }
      reload();
    } catch (e) {
      if (!handleLockedAddError(e, "activity")) {
        toast.error(formatErr(e, "Add planned activity"));
      }
    }
  };
  // Bucket D Fix E -- double-click guard for unplanned-activity create.
  const [creatingUnplanned, setCreatingUnplanned] = useState(false);
  const createUnplanned = async () => {
    if (creatingUnplanned) return;
    if (!unplanned.description) return toast.error("Describe the unplanned activity first");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setCreatingUnplanned(true);
    try {
      await api.post(`/daily-reports/${dailyReportId}/unplanned-activity`, {
        ...unplanned,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setUnplanned({ description: "", trade_code: "", duration_days: 1 });
      if (locked) { setAddReason(""); toast.success("Unplanned activity created (post-submit). Audit chain captured."); }
      else toast.success("Unplanned activity created — flagged for PM review.");
      reload();
    } catch (e) {
      if (!handleLockedAddError(e, "activity")) {
        toast.error(formatErr(e, "Create unplanned"));
      }
    }
    finally { setCreatingUnplanned(false); }
  };

  const STATUS_OPTS = ["not_started", "in_progress", "blocked", "complete"];
  return (
    <PanelShell icon={ClipboardList} title="Activity grid" testid="dr-activity-grid" count={grid.length}
      badge={
        <div className="ml-auto flex items-center gap-2">
          <span className="text-[10px] text-slate-500">date: {reportDate}</span>
          {!locked && (
            <button data-testid="dr-refresh-grid" onClick={refreshFromSchedule} disabled={refreshing}
              className="h-7 px-2 rounded border border-slate-200 bg-white text-[11px] hover:bg-slate-50 flex items-center gap-1 disabled:opacity-50">
              <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} /> Refresh from schedule
            </button>
          )}
        </div>
      }>
      <div className="overflow-x-auto">
        <table className="dense-table">
          <thead>
            <tr>
              <th>Code</th><th>Description</th><th>Loc</th><th>Trade</th>
              <th>Crew</th><th>SOD %</th><th>EOD %</th><th>Δ</th>
              <th>Hours</th><th>Status</th>
              <th title="Bucket C3a — what's blocking this row, if anything. Required on submit when EOD% = SOD%.">Constraint</th>
              <th>Notes</th><th></th>
            </tr>
          </thead>
          <tbody>
            {grid.map((r) => {
              const delta = (r.pct_complete_end_of_day || 0) - (r.pct_complete_start_of_day || 0);
              const variance = delta < 0 ? "neg" : delta === 0 ? "zero" : "pos";
              const isUnplanned = (r.activity_code || "").startsWith("AF");
              const isStale = staleRows.has(r.id);
              const isProvisional = r.sync_state === "provisional";
              const rowErrs = submitRowErrors[r.id] || [];
              const hasErr = rowErrs.length > 0;
              const rowClass = isStale ? "bg-amber-50/40"
                : hasErr ? "bg-red-50/40" : undefined;
              return (
                <tr key={r.id} data-testid={`dr-row-${r.activity_code}`} className={rowClass}>
                  <td className="font-mono text-xs">
                    {r.activity_code}
                    {isUnplanned && <span className="ml-1 px-1 rounded bg-amber-100 text-amber-700 text-[9px] uppercase">field</span>}
                    {isProvisional && !isStale && (
                      <span title="Provisional sync — confirmed on submit"
                        className="ml-1 inline-block w-1.5 h-1.5 rounded-full bg-blue-400 align-middle" />
                    )}
                    {isStale && (
                      <button data-testid={`dr-row-refresh-${r.activity_code}`}
                        onClick={refreshStaleRow}
                        title="Schedule was updated externally — click to refresh row"
                        className="ml-1 inline-flex items-center gap-1 text-[10px] px-1 rounded bg-amber-100 text-amber-800 border border-amber-200 hover:bg-amber-200">
                        <RefreshCw className="h-2.5 w-2.5" /> refresh
                      </button>
                    )}
                    {hasErr && (
                      <div data-testid={`dr-row-err-${r.activity_code}`}
                        className="mt-1 text-[10px] text-red-700 normal-case font-sans">
                        {rowErrs.map((e, i) => <div key={i}>⚠ {e.message}</div>)}
                      </div>
                    )}
                  </td>
                  <td className="truncate max-w-[260px]">{r.activity_description}</td>
                  <td className="text-xs">{r.location_name || "—"}</td>
                  <td className="text-xs">{r.trade_code || "—"}</td>
                  <td>
                    <CrewSelector row={r} presence={presence} locked={locked} updateRow={updateRow} />
                  </td>
                  <td className="text-xs tabular-nums text-slate-500">{r.pct_complete_start_of_day || 0}</td>
                  <td><input data-testid={`dr-pct-${r.activity_code}`} type="number" min="0" max="100" defaultValue={r.pct_complete_end_of_day} onBlur={(e) => updateRow(r.id, { pct_complete_end_of_day: Number(e.target.value) })} disabled={locked} className="w-14 tabular-nums" /></td>
                  <td className={`text-xs tabular-nums font-medium ${variance === "neg" ? "text-red-600" : variance === "pos" ? "text-emerald-600" : "text-slate-400"}`}>
                    {delta > 0 ? "+" : ""}{delta}
                  </td>
                  <td><input type="number" defaultValue={r.hours_worked} onBlur={(e) => updateRow(r.id, { hours_worked: Number(e.target.value) })} disabled={locked} className="w-14" /></td>
                  <td>
                    <select data-testid={`dr-status-${r.activity_code}`} defaultValue={r.status_at_end_of_day || "not_started"} onChange={(e) => updateRow(r.id, { status_at_end_of_day: e.target.value })} disabled={locked} className="text-xs h-7 border border-slate-200 rounded px-1">
                      {STATUS_OPTS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
                    </select>
                  </td>
                  <td>
                    <ConstraintCell row={r} projectId={dailyReportProjectId} locked={locked} updateRow={updateRow} />
                  </td>
                  <td><input type="text" defaultValue={r.notes || ""} onBlur={(e) => updateRow(r.id, { notes: e.target.value })} disabled={locked} placeholder="Notes…" className="w-44" /></td>
                  <td>
                    {!locked && <button data-testid={`dr-remove-${r.activity_code}`} onClick={() => removeRow(r.id)} className="text-red-600 hover:bg-red-50 p-1 rounded text-xs">remove</button>}
                    {/* Bucket D Addition 5 (2026-05-12) -- per-row Edit
                        button on locked DRs. Opens LockedEventEditModal
                        targeting the row's PATCH endpoint with the
                        editable activity-grid fields. The PATCH path
                        on the backend fires
                        ACTIVITY_GRID_EDITED_POST_SUBMIT (distinct from
                        EVENT_EDITED_POST_SUBMIT) when the parent DR
                        is locked. */}
                    {locked && <ActivityRowLockedEditButton row={r} dailyReportId={dailyReportId} reload={reload} />}
                  </td>
                </tr>
              );
            })}
            {!grid.length && <tr><td colSpan="13" className="text-center py-4 text-sm text-slate-500">No activities. Use the picker below.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="mt-4 pt-3 border-t border-slate-100 space-y-3">
        {locked && (
          <LockedAddReasonBlock value={addReason} onChange={setAddReason}
            eventLabel="an activity row" testid="activity-add-reason" />
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* PLANNED ADD */}
          <div className="space-y-2">
            <div className="text-xs font-medium text-slate-600">Add planned activity ({available.length} available for {reportDate})</div>
            <div className="flex gap-2">
              <select data-testid="dr-add-activity-select" value={picking} onChange={(e) => setPicking(e.target.value)} className="flex-1 h-8 px-2 rounded border border-slate-200 text-sm">
                <option value="">— Pick —</option>
                {available.map((a) => <option key={a.id} value={a.id}>[{a.activity_code}] {a.description}{a.trade_code ? ` · ${a.trade_code}` : ""}</option>)}
              </select>
              <button data-testid="dr-add-activity-button" onClick={addActivity}
                disabled={!picking || (locked && !isAddReasonValid(addReason))}
                title={locked && !isAddReasonValid(addReason)
                  ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
                  : undefined}
                className="h-8 px-3 rounded bg-blue-600 text-white text-sm disabled:opacity-50 disabled:cursor-not-allowed hover:bg-blue-700">
                {locked ? "Add (post-submit)" : "Add"}
              </button>
            </div>
          </div>
          {/* UNPLANNED ADD */}
          <div className="space-y-2 bg-amber-50 border border-amber-200 rounded p-3">
            <div className="text-xs font-medium text-amber-800 flex items-center gap-1"><Wand2 className="h-3 w-3" /> Create unplanned activity (flagged for PM review)</div>
            <div className="grid grid-cols-3 gap-2">
              <input data-testid="unplanned-desc" placeholder="What was done?" value={unplanned.description} onChange={(e) => setUnplanned({ ...unplanned, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-amber-200 text-sm bg-white" />
              <input data-testid="unplanned-trade" placeholder="Trade" value={unplanned.trade_code} onChange={(e) => setUnplanned({ ...unplanned, trade_code: e.target.value })} className="h-8 px-2 rounded border border-amber-200 text-sm bg-white" />
            </div>
            <button data-testid="unplanned-add" onClick={createUnplanned}
              disabled={creatingUnplanned || (locked && !isAddReasonValid(addReason))}
              title={locked && !isAddReasonValid(addReason)
                ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
                : undefined}
              className="h-7 px-3 rounded bg-amber-700 text-white text-xs hover:bg-amber-800 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1">
              <Plus className="h-3 w-3" /> {creatingUnplanned ? "Creating…" : (locked ? "Create unplanned (post-submit)" : "Create unplanned activity")}
            </button>
          </div>
        </div>
      </div>
    </PanelShell>
  );
}

function RenovationPanel({ report, dailyReportId, locked, reload }) {
  const patch = async (fields) => {
    try { await api.patch(`/daily-reports/${dailyReportId}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };
  return (
    <PanelShell icon={ShieldAlert} title="Renovation / occupied building" testid="dr-renovation-panel">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <CheckField label="Building still occupied during work" value={!!report.occupancy_active} onChange={(v) => patch({ occupancy_active: v })} disabled={locked} testid="ren-occupancy" />
        <CheckField label="Dust / containment controls in place" value={!!report.dust_controls_in_place} onChange={(v) => patch({ dust_controls_in_place: v })} disabled={locked} testid="ren-dust" />
        <CheckField label="After-hours work (noise / vibration window)" value={!!report.after_hours_work} onChange={(v) => patch({ after_hours_work: v })} disabled={locked} testid="ren-after-hours" />
        <Field label="Occupied areas active today" value={report.occupied_areas || ""} onChange={(v) => patch({ occupied_areas: v })} disabled={locked} testid="ren-occupied-areas" />
        <Field label="Noise complaints" type="number" value={report.noise_complaints_count ?? 0} onChange={(v) => patch({ noise_complaints_count: Number(v) })} disabled={locked} testid="ren-noise-count" />
        <Field label="Dust violations" type="number" value={report.dust_violations_count ?? 0} onChange={(v) => patch({ dust_violations_count: Number(v) })} disabled={locked} testid="ren-dust-count" />
      </div>
    </PanelShell>
  );
}

// Bucket D Addition 5 (2026-05-12) -- per-row Edit button on the
// activity grid when the parent DR is locked. Renders an amber
// "edit" link that opens LockedEventEditModal targeting
// /daily-report-activities/{rowId} with the SOD%/EOD%/hours/status/
// notes/constraint fields. The modal already handles the edit_reason
// prompt + 403 needs_request flip into edit-request mode. Backend
// PATCH fires ACTIVITY_GRID_EDITED_POST_SUBMIT (instead of
// EVENT_EDITED_POST_SUBMIT) so counsel can filter grid edits
// separately in the Audit Log dropdown.
function ActivityRowLockedEditButton({ row, dailyReportId, reload }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button data-testid={`dr-row-edit-locked-${row.activity_code}`}
        onClick={() => setOpen(true)}
        className="text-amber-700 hover:bg-amber-50 p-1 rounded text-xs">
        edit
      </button>
      {/* Bug #51 (2026-05-13) -- post-submit modal field set was
          previously {EOD%, hours, status, notes}, missing SOD% and
          Constraint that are editable pre-submit. Adding both so the
          post-submit edit path is field-symmetric with pre-submit.
          Δ is intentionally NOT added: it's derived (EOD% - SOD%),
          not stored or patchable, and the LockedEventEditModal's
          FieldEditor has no display-only field type.

          Bug #52 (2026-05-13) -- Constraint editing post-submit is
          covered by the constraint_type dropdown below. The pre-submit
          ConstraintCell's reference / description dependents are NOT
          replicated here (the modal's flat field renderer can't host
          the conditional follow-on inputs). Behavior:
            - Switching to 'not_constrained' works: backend auto-clears
              constraint_reference_entity_id + _other_description.
            - Switching to 'other' works IFF the row already has a
              non-empty constraint_other_description (set pre-submit
              or via a prior edit). Otherwise backend 422s with a clear
              message; user must first set a description via the
              pre-submit grid or via direct API.
            - Switching to a needs-ref type (awaiting_*, weather_impact,
              etc.) works IFF the row already has a ref. Same fallback.
          Constraint changes route through the same backend handler as
          pre-submit edits, so the cascade hooks fire identically. */}
      <LockedEventEditModal
        open={open} onOpenChange={setOpen}
        eventType="activity" eventId={row.id} dailyReportId={dailyReportId}
        patchUrl={`/daily-report-activities/${row.id}`}
        title={`Edit grid row [${row.activity_code}] (post-submit)`}
        fields={[
          { key: "pct_complete_start_of_day", label: "SOD %", type: "number" },
          { key: "pct_complete_end_of_day", label: "EOD %", type: "number" },
          { key: "hours_worked", label: "Hours worked", type: "number" },
          { key: "status_at_end_of_day", label: "Status", type: "select",
            options: [
              { value: "not_started", label: "not started" },
              { value: "in_progress", label: "in progress" },
              { value: "blocked", label: "blocked" },
              { value: "complete", label: "complete" },
            ] },
          { key: "constraint_type", label: "Constraint", type: "select",
            options: CONSTRAINT_TYPES },
          { key: "notes", label: "Notes", type: "textarea" },
        ]}
        currentValues={{
          pct_complete_start_of_day: row.pct_complete_start_of_day,
          pct_complete_end_of_day: row.pct_complete_end_of_day,
          hours_worked: row.hours_worked,
          status_at_end_of_day: row.status_at_end_of_day,
          constraint_type: row.constraint_type || "not_constrained",
          notes: row.notes,
        }}
        onSaved={reload}
      />
    </>
  );
}

// ---------- Inline event panels (delays / safety / qc / rfis / deliveries) ----------
function DelaysPanel({ rows, dailyReportId, projectId, reportDate, locked, reload }) {
  const [showForm, setShowForm] = useState(false);
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");

  return (
    <PanelShell icon={AlertTriangle} title="Delays" testid="dr-delays-panel" count={rows.length}>
      {rows.map((r) => (
        <DelayRowInline key={r.id} delay={r} reload={reload}
          locked={locked} dailyReportId={dailyReportId} />
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No delays today.</div>}
      <div className="mt-3 pt-3 border-t border-slate-100">
        {!showForm ? (
          <button data-testid="delay-add-open" onClick={() => setShowForm(true)}
            className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 flex items-center gap-1">
            <Plus className="h-3.5 w-3.5" /> {locked ? "Log a delay (post-submit)" : "Log a delay"}
          </button>
        ) : (
          <div className="space-y-2">
            {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
              eventLabel="a delay" testid="delay-add-reason" />}
            <DelayForm
              projectId={projectId}
              reportDate={reportDate}
              dailyReportId={dailyReportId}
              isLocked={locked}
              lockedAddReason={locked ? addReason : null}
              onSaved={() => {
                setShowForm(false);
                setAddReason("");
                reload();
                toast.success(locked ? "Delay logged (post-submit). Audit chain captured." : "Delay logged");
              }}
              onCancel={() => { setShowForm(false); setAddReason(""); }}
            />
          </div>
        )}
      </div>
    </PanelShell>
  );
}

function DelayRowInline({ delay, reload, locked, dailyReportId }) {
  const [resolving, setResolving] = useState(false);
  const [resolveDate, setResolveDate] = useState(localTodayISO());
  const [resolveNote, setResolveNote] = useState("");
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);

  const resolve = async () => {
    try {
      await api.patch(`/delays/${delay.id}`, {
        status: "resolved",
        resolved_date: resolveDate,
        resolved_note: resolveNote || null,
      });
      setResolving(false);
      reload();
      toast.success("Delay marked resolved");
    } catch (e) { toast.error(formatErr(e, "Resolve delay")); }
  };

  const sevColor = delay.severity === "critical" || delay.severity === "major" ? "pill-red" : "pill-slate";
  const statusColor = delay.status === "resolved" ? "pill-green" : delay.status === "in_progress" ? "pill-amber" : "pill-slate";
  const linkedCount = (delay.linked_task_ids || []).length;
  return (
    <div data-testid={`delay-row-${delay.id}`} className="border-b border-slate-100 py-2 text-xs space-y-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={`pill ${sevColor}`}>{delay.severity}</span>
        <span className={`pill ${statusColor}`}>{(delay.status || "open").replace(/_/g, " ")}</span>
        {delay.reason_category && <span className="text-slate-400">{delay.reason_category}</span>}
        {linkedCount > 0 && <span className="text-slate-500">· {linkedCount} task{linkedCount === 1 ? "" : "s"}</span>}
        {delay.duration_days != null && <span className="text-slate-500">· {delay.duration_days}d open</span>}
        <SupplementBadge row={delay} />
        {delay.status !== "resolved" && (
          <button data-testid={`delay-resolve-open-${delay.id}`} onClick={() => setResolving((v) => !v)}
            className="ml-auto text-blue-600 hover:underline">
            {resolving ? "cancel" : "mark resolved"}
          </button>
        )}
        {locked && (
          <button data-testid={`delay-edit-locked-${delay.id}`} onClick={() => setEditOpen(true)}
            className={`${delay.status !== "resolved" ? "ml-2" : "ml-auto"} text-amber-700 hover:underline`}>
            edit
          </button>
        )}
        {delay.post_submit_edited && (
          <span className="text-[10px] text-amber-700"
            title={`Last edit: ${(delay.last_edit_reason || "").slice(0, 80)}`}>
            (edited {delay.edit_count || 0}× post-submit)
          </span>
        )}
      </div>
      <div className="text-slate-700 whitespace-pre-wrap">{delay.description}</div>
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="delay" eventId={delay.id} dailyReportId={dailyReportId}
          patchUrl={`/delays/${delay.id}`}
          fields={[
            { key: "description", label: "Description", type: "textarea" },
            { key: "severity", label: "Severity", type: "select",
              options: [
                { value: "minor", label: "minor" }, { value: "moderate", label: "moderate" },
                { value: "major", label: "major" }, { value: "critical", label: "critical" },
              ] },
            { key: "reason_category", label: "Reason category" },
            { key: "owner_attribution", label: "Owner attribution", type: "select",
              options: [
                { value: "none", label: "none" }, { value: "owner", label: "owner" },
                { value: "gc", label: "gc" }, { value: "design", label: "design" },
                { value: "force_majeure", label: "force majeure" },
              ] },
          ]}
          currentValues={{
            description: delay.description, severity: delay.severity,
            reason_category: delay.reason_category, owner_attribution: delay.owner_attribution,
          }}
          onSaved={reload}
        />
      )}
      {resolving && (
        <div className="mt-2 p-2 bg-slate-50 rounded grid grid-cols-2 gap-2">
          <input type="date" value={resolveDate} onChange={(e) => setResolveDate(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <input type="text" placeholder="Resolution note (optional)" value={resolveNote}
            onChange={(e) => setResolveNote(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <button data-testid={`delay-resolve-confirm-${delay.id}`} onClick={resolve}
            className="col-span-2 h-7 px-3 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-700">
            Mark resolved
          </button>
        </div>
      )}
    </div>
  );
}

function ReminderBanner({ reminders, reload }) {
  if (!reminders?.length) return null;
  return (
    <div data-testid="dr-reminder-banner" className="surface p-4 border-l-4 border-amber-400 bg-amber-50/40">
      <div className="flex items-center gap-2 mb-2">
        <AlertTriangle className="h-4 w-4 text-amber-600" />
        <div className="font-medium text-sm text-amber-900">Open delay reminders</div>
        <span className="text-xs text-amber-700">({reminders.length})</span>
        <span className="text-[11px] text-slate-500 ml-auto">delays from earlier days that touch tasks scheduled today</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {reminders.map((r) => (
          <ReminderCard key={r.id} delay={r} reload={reload} />
        ))}
      </div>
    </div>
  );
}

function ReminderCard({ delay, reload }) {
  const [resolving, setResolving] = useState(false);
  const [resolveDate, setResolveDate] = useState(localTodayISO());
  const [resolveNote, setResolveNote] = useState("");

  const resolve = async () => {
    try {
      await api.patch(`/delays/${delay.id}`, {
        status: "resolved",
        resolved_date: resolveDate,
        resolved_note: resolveNote || null,
      });
      setResolving(false);
      reload();
      toast.success("Delay marked resolved");
    } catch (e) { toast.error(formatErr(e, "Resolve delay")); }
  };

  const sevColor = delay.severity === "critical" || delay.severity === "major" ? "pill-red" : "pill-slate";
  const linked = delay.linked_task_ids || [];
  const firstLine = (delay.description || "").split("\n")[0].slice(0, 140);
  return (
    <div data-testid={`reminder-card-${delay.id}`} className="bg-white rounded border border-amber-200 p-2 text-xs space-y-1">
      <div className="flex items-center gap-1.5">
        <span className={`pill ${sevColor}`}>{delay.severity}</span>
        {delay.reason_category && <span className="text-slate-400">{delay.reason_category}</span>}
        {delay.duration_days != null && <span className="text-slate-500">· {delay.duration_days}d open</span>}
        {!resolving && (
          <button data-testid={`reminder-resolve-open-${delay.id}`} onClick={() => setResolving(true)}
            className="ml-auto text-blue-600 hover:underline">mark resolved</button>
        )}
      </div>
      <div className="text-slate-800 truncate">{firstLine}</div>
      {linked.length > 0 && (
        <div className="text-[10px] text-slate-500">tasks: {linked.length} linked</div>
      )}
      {resolving && (
        <div className="mt-1 p-2 bg-amber-50 rounded grid grid-cols-2 gap-2">
          <input type="date" value={resolveDate} onChange={(e) => setResolveDate(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <input type="text" placeholder="Resolution note" value={resolveNote}
            onChange={(e) => setResolveNote(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <button onClick={() => setResolving(false)} className="h-7 px-2 rounded border border-slate-200 text-xs">Cancel</button>
          <button data-testid={`reminder-resolve-confirm-${delay.id}`} onClick={resolve}
            className="h-7 px-2 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-700">Save</button>
        </div>
      )}
    </div>
  );
}

function SafetyPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Phase C — simplified: description + type + severity. OSHA recordable
  // checkbox removed per spec.
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) picker. OPTIONAL for safety per spec (some events area-wide);
  // empty list still saves.
  const [draft, setDraft] = useState({
    description: "", severity: "minor", event_type: "near_miss",
    linked_task_ids: [],
  });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  // Phase D — copilot can push a drafted description into this form.
  useEffect(() => subscribeDescriptionInsert(({ target, text }) => {
    if (target !== "safety" || !text) return;
    setDraft((d) => ({ ...d, description: d.description ? `${d.description}\n\n${text}` : text }));
  }), []);
  const create = async () => {
    if (busy) return;
    if (!draft.description) return toast.error("Description required");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      await api.post(`/safety-events`, {
        project_id: projectId, daily_report_id: dailyReportId, date: reportDate,
        description: draft.description, severity: draft.severity, event_type: draft.event_type,
        linked_task_ids: draft.linked_task_ids,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setDraft({ description: "", severity: "minor", event_type: "near_miss",
                  linked_task_ids: [] });
      setAddReason("");
      reload();
      if (locked) toast.success("Safety event added (post-submit). Audit chain captured.");
    } catch (e) {
      if (!handleLockedAddError(e, "safety")) {
        toast.error(formatErr(e, "Create safety event"));
      }
    }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={ShieldAlert} title="Safety events" testid="dr-safety-panel" count={rows.length}>
      {rows.map((r) => (
        <SafetyRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No safety events today.</div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="safety-form">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a safety event" testid="safety-add-reason" />}
        <textarea data-testid="safety-new-desc" rows={4} placeholder="Describe the event"
          value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          className="w-full p-2 rounded border border-slate-200 text-sm" />
        <TaskPicker projectId={projectId}
          value={draft.linked_task_ids}
          onChange={(v) => setDraft({ ...draft, linked_task_ids: v })}
          required={false} label="Linked tasks (optional)"
          testidPrefix="safety-activity" />
        <div className="grid grid-cols-2 gap-2">
          <select data-testid="safety-new-type" value={draft.event_type} onChange={(e) => setDraft({ ...draft, event_type: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            {["near_miss", "first_aid", "recordable", "lost_time", "fatality", "other"].map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
          </select>
          <select data-testid="safety-new-severity" value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            {["minor", "moderate", "major", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <button data-testid="safety-add" onClick={create}
          disabled={busy || (locked && !isAddReasonValid(addReason))}
          title={locked && !isAddReasonValid(addReason)
            ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
            : undefined}
          className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1">
          <Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : locked ? "Add (post-submit)" : "Add"}
        </button>
      </div>
    </PanelShell>
  );
}

function SafetyRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`safety-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className="pill pill-slate mr-2">{row.event_type}</span>
      <span className={`pill mr-2 ${row.severity === "critical" || row.severity === "major" ? "pill-red" : "pill-slate"}`}>{row.severity}</span>
      <span className="text-slate-700 whitespace-pre-wrap">{row.description}</span>
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`safety-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-[10px] text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="safety" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/safety-events/${row.id}`}
          fields={[
            { key: "description", label: "Description", type: "textarea" },
            { key: "severity", label: "Severity", type: "select",
              options: ["minor", "moderate", "major", "critical"].map((s) => ({ value: s, label: s })) },
            { key: "event_type", label: "Event type", type: "select",
              options: ["near_miss", "first_aid", "recordable", "lost_time", "fatality"].map((s) => ({ value: s, label: s.replace(/_/g, " ") })) },
          ]}
          currentValues={{
            description: row.description, severity: row.severity, event_type: row.event_type,
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}

function QcPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Phase 5 -- QC severity gets the rework_required tier.
  // When picked, super enters rework_duration_days + basis;
  // calibration RAG suggests a benchmark from prior outcomes (advisory).
  // Submit -> backend creates synthetic delay + runs CPM cascade.
  // QC rework days (2026-05-17) -- rework duration is entered in
  // WORKING DAYS (0.5-day increments), consistent with delays / RFIs.
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) replaces the old per-DR linked_dr_activity_ids picker. The
  // synthetic delay and CPM cascade walk these task ids directly.
  const [draft, setDraft] = useState({
    description: "", severity: "minor",
    rework_duration_days: "", rework_duration_basis: "",
    linked_task_ids: [],
  });
  const [benchmark, setBenchmark] = useState(null);
  // Phase D — copilot can push a drafted description into this form.
  useEffect(() => subscribeDescriptionInsert(({ target, text }) => {
    if (target !== "qc" || !text) return;
    setDraft((d) => ({ ...d, description: d.description ? `${d.description}\n\n${text}` : text }));
  }), []);

  useEffect(() => {
    if (draft.severity !== "rework_required" || !projectId) { setBenchmark(null); return; }
    api.get(`/projects/${projectId}/qc-issues/rework-benchmark`)
      .then(({ data }) => setBenchmark(data))
      .catch(() => setBenchmark(null));
  }, [draft.severity, projectId]);

  const isRework = draft.severity === "rework_required";
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);

  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  const create = async () => {
    if (busy) return;
    if (!draft.description) return toast.error("Description required");
    if (!draft.linked_task_ids.length) return toast.error("Pick at least one linked task");
    if (isRework) {
      if (!draft.rework_duration_days || Number(draft.rework_duration_days) <= 0) {
        return toast.error("Rework duration (days) required when severity=rework_required");
      }
      if (!draft.rework_duration_basis.trim()) {
        return toast.error("Rework basis (short note) required");
      }
    }
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      const payload = {
        project_id: projectId,
        daily_report_id: dailyReportId,
        date: reportDate,
        description: draft.description,
        severity: draft.severity,
        linked_task_ids: draft.linked_task_ids,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      };
      if (isRework) {
        payload.rework_duration_days = Number(draft.rework_duration_days);
        payload.rework_duration_basis = draft.rework_duration_basis.trim();
      }
      await api.post(`/qc-issues`, payload);
      setDraft({ description: "", severity: "minor",
                  rework_duration_days: "", rework_duration_basis: "",
                  linked_task_ids: [] });
      setAddReason("");
      reload();
      toast.success(isRework
        ? "QC rework filed -- synthetic delay created and CPM cascade running."
        : (locked ? "QC issue added (post-submit). Audit chain captured." : "QC issue logged."));
    } catch (e) {
      if (!handleLockedAddError(e, "qc")) {
        toast.error(formatErr(e, "Create QC issue"));
      }
    }
    finally { setBusy(false); }
  };

  return (
    <PanelShell icon={ClipboardCheck} title="QC issues" testid="dr-qc-panel" count={rows.length}>
      {rows.map((r) => (
        <QcRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No QC issues today.</div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="qc-form">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a QC issue" testid="qc-add-reason" />}
        <div className="space-y-2">
          <textarea data-testid="qc-new-desc" rows={4} placeholder="Describe the issue"
            value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="w-full p-2 rounded border border-slate-200 text-sm" />

          {/* Bucket D Corrective -- master-schedule task picker. */}
          <TaskPicker projectId={projectId}
            value={draft.linked_task_ids}
            onChange={(v) => setDraft({ ...draft, linked_task_ids: v })}
            required={true} label="Linked tasks (required)"
            testidPrefix="qc-activity" />

          <div className="flex items-center gap-2">
            <select data-testid="qc-new-severity" value={draft.severity}
              onChange={(e) => setDraft({ ...draft, severity: e.target.value })}
              className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["observation", "minor", "moderate", "major", "critical", "rework_required", "stop_work"]
                .map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
          </div>

          {/* Bucket D Phase 5 -- rework block.
              QC rework days (2026-05-17) -- duration entered in
              working days, not hours. */}
          {isRework && (
            <div className="bg-rose-50 border border-rose-200 rounded p-2 space-y-2" data-testid="qc-rework-block">
              <div className="text-[11px] font-semibold text-rose-900">Rework duration estimate (super)</div>
              {benchmark?.benchmark_days != null && (
                <div className="text-[10px] text-slate-600 italic" data-testid="qc-rework-benchmark">
                  {benchmark.basis}
                </div>
              )}
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="text-[10px] text-slate-500 uppercase">Rework duration (days) *</label>
                  <input data-testid="qc-rework-days" type="number" step="0.5" min="0"
                    value={draft.rework_duration_days}
                    onChange={(e) => setDraft({ ...draft, rework_duration_days: e.target.value })}
                    className="w-full h-8 px-2 rounded border border-rose-300 text-sm" />
                </div>
                <div className="col-span-2">
                  <label className="text-[10px] text-slate-500 uppercase">Basis * (short note)</label>
                  <input data-testid="qc-rework-basis" type="text"
                    value={draft.rework_duration_basis}
                    onChange={(e) => setDraft({ ...draft, rework_duration_basis: e.target.value })}
                    placeholder="e.g. re-rough 11A south wall, 2 plumbers ~1 day"
                    className="w-full h-8 px-2 rounded border border-rose-300 text-sm" />
                </div>
              </div>
              {/* QC rework days (2026-05-17) -- silent-zero guard. The
                  CPM cascade applies WHOLE-day impacts, so a sub-1-day
                  rework rounds to 0 working days of schedule shift.
                  Warn the super (NON-BLOCKING -- save still proceeds)
                  so a half-day entry is not a silent no-op. */}
              {Number(draft.rework_duration_days) > 0
                && Number(draft.rework_duration_days) <= 0.5 && (
                <div data-testid="qc-rework-subday-warning"
                  className="text-[10px] text-amber-900 bg-amber-50 border border-amber-300 rounded px-2 py-1">
                  A {draft.rework_duration_days}-day rework will be recorded on the
                  QC issue but will not shift the schedule — the cascade currently
                  applies whole-day impacts (a sub-1-day rework rounds to 0).
                </div>
              )}
              <div className="text-[10px] text-rose-800">
                Duration is in working days (0.5-day increments). On save, a
                synthetic delay (cause=quality_rework) of that many days is
                created and the CPM cascade run for the affected activities.
              </div>
            </div>
          )}

          <div className="flex justify-end">
            <button data-testid="qc-add" onClick={create}
              disabled={busy || (locked && !isAddReasonValid(addReason))}
              title={locked && !isAddReasonValid(addReason)
                ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
                : undefined}
              className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1">
              <Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : locked ? "Add QC (post-submit)" : "Add QC issue"}
            </button>
          </div>
        </div>
      </div>
    </PanelShell>
  );
}

function QcRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`qc-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className={`pill mr-2 ${
        row.severity === "rework_required" || row.severity === "stop_work" ? "pill-red"
        : row.severity === "critical" || row.severity === "major" ? "pill-red"
        : "pill-slate"
      }`}>{row.severity}</span>
      <span className="text-slate-700 whitespace-pre-wrap">{row.description}</span>
      {row.linked_synthetic_delay_id && (
        <span className="ml-2 text-[10px] text-rose-700 bg-rose-50 border border-rose-200 px-1 py-0.5 rounded">
          rework {row.rework_duration_days}d
          {row.rework_actual_days != null && ` (actual ${row.rework_actual_days}d)`}
        </span>
      )}
      {/* Task 3 fix D -- outcome capture button. Visible when QC has
          a synthetic delay AND no actual rework yet recorded. */}
      {row.linked_synthetic_delay_id && row.rework_actual_days == null && (
        <ReworkOutcomeButton qcIssue={row} reload={reload} />
      )}
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`qc-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-[10px] text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="qc" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/qc-issues/${row.id}`}
          fields={[
            { key: "description", label: "Description", type: "textarea" },
            { key: "severity", label: "Severity", type: "select",
              options: ["observation", "minor", "moderate", "major", "critical", "rework_required", "stop_work"]
                .map((s) => ({ value: s, label: s.replace(/_/g, " ") })) },
            { key: "status", label: "Status", type: "select",
              options: ["open", "in_progress", "resolved", "closed"].map((s) => ({ value: s, label: s.replace(/_/g, " ") })) },
            { key: "resolution_notes", label: "Resolution notes", type: "textarea" },
          ]}
          currentValues={{
            description: row.description, severity: row.severity,
            status: row.status, resolution_notes: row.resolution_notes,
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}


function RfisPanel({ rows, dailyReportId, projectId, locked, reload, grid }) {
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) picker. RFIs are REQUIRED to link >=1 task (gates a CPM
  // node via the gating_entities back-pointer).
  // CPM Part J-6 (2026-05-15) -- per_task_impacts captures the super's
  // estimated schedule impact per linked task. Optional on create;
  // empty array means "no impact captured yet" (backend treats the
  // RFI as a binary blocker via count_open_blockers, doesn't push
  // Slip on the linked tasks).
  const [draft, setDraft] = useState({
    subject: "", description: "", priority: "normal",
    linked_task_ids: [],
    per_task_impacts: [],
  });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (busy) return;
    if (!draft.subject || !draft.description) return toast.error("Subject + question required");
    if (!draft.linked_task_ids.length) return toast.error("Pick at least one linked task");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      // CPM Part J-6 -- include per_task_impacts (cleaned by
      // PerTaskImpactsInput: empty / 0 inputs already pruned, only
      // positive integers + task_ids remain). Empty array is fine;
      // backend treats it as "no impact captured yet".
      await api.post(`/rfis`, {
        project_id: projectId, daily_report_id: dailyReportId,
        subject: draft.subject,
        question: draft.description,  // backend alias -> description
        priority: draft.priority,
        linked_task_ids: draft.linked_task_ids,
        per_task_impacts: draft.per_task_impacts || [],
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setDraft({
        subject: "", description: "", priority: "normal",
        linked_task_ids: [], per_task_impacts: [],
      });
      setAddReason("");
      reload();
      if (locked) toast.success("RFI added (post-submit). Audit chain captured.");
    } catch (e) {
      if (!handleLockedAddError(e, "rfi")) {
        toast.error(formatErr(e, "Open RFI"));
      }
    }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={MessageSquare} title="RFIs" testid="dr-rfis-panel" count={rows.length}>
      {rows.map((r) => (
        <RfiRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No RFIs raised today.</div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="an RFI" testid="rfi-add-reason" />}
        <input data-testid="rfi-new-subject" placeholder="Subject" value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} className="w-full h-8 px-2 rounded border border-slate-200 text-sm" />
        <textarea data-testid="rfi-new-desc" placeholder="Question / context" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="w-full px-2 py-1 rounded border border-slate-200 text-sm" rows={2} />
        <TaskPicker projectId={projectId}
          value={draft.linked_task_ids}
          onChange={(v) => setDraft((d) => ({
            // CPM Part J-6 -- when super removes a task from linked
            // tasks, prune its per_task_impacts entry so the cleaned
            // array PerTaskImpactsInput later receives doesn't carry
            // stale rows. (PerTaskImpactsInput's own auto-sync useEffect
            // catches this defensively too, but pruning at the source
            // avoids a transient inconsistent state.)
            ...d,
            linked_task_ids: v,
            per_task_impacts: (d.per_task_impacts || []).filter(
              (imp) => (v || []).includes(imp.task_id),
            ),
          }))}
          required={true} label="Linked tasks"
          testidPrefix="rfi-activity" />
        {/* CPM Part J-6 -- per-task schedule-impact input. Only
            renders when there are linked tasks. Empty array (super
            hasn't entered any impact_days yet) is a valid state and
            keeps the RFI as a binary blocker only. */}
        <PerTaskImpactsInput
          linkedTaskIds={draft.linked_task_ids}
          value={draft.per_task_impacts}
          onChange={(v) => setDraft((d) => ({ ...d, per_task_impacts: v }))}
          projectId={projectId}
          testidPrefix="rfi-impacts"
        />
        <div className="flex items-center gap-2">
          <select data-testid="rfi-new-priority" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            {["low", "normal", "high", "urgent"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <button data-testid="rfi-add" onClick={create}
            disabled={busy || (locked && !isAddReasonValid(addReason))}
            title={locked && !isAddReasonValid(addReason)
              ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
              : undefined}
            className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-1"><Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : locked ? "Open RFI (post-submit)" : "Open RFI"}</button>
        </div>
      </div>
    </PanelShell>
  );
}

function RfiRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`rfi-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className="font-mono mr-2 text-blue-700">{row.rfi_number}</span>
      <span className="pill pill-slate mr-2">{row.priority}</span>
      <span className="text-slate-700">{row.subject}</span>
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`rfi-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-[10px] text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="rfi" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/rfis/${row.id}`}
          fields={[
            { key: "subject", label: "Subject" },
            { key: "description", label: "Question / context", type: "textarea" },
            { key: "status", label: "Status", type: "select",
              options: ["draft", "open", "responded", "closed"].map((s) => ({ value: s, label: s })) },
            { key: "response_text", label: "Response (when status=responded)", type: "textarea" },
            // CPM Part J-6 (2026-05-15) -- per_task_impacts editor.
            // Linked tasks are FIXED in this modal (the modal only
            // edits impact magnitudes; linkage edits live on the
            // standalone /rfis page). Empty value renders zero inputs;
            // user adding a value emits the cleaned array on submit.
            { key: "per_task_impacts", label: "Schedule impact (optional)",
              type: "per_task_impacts",
              linkedTaskIds: row.linked_task_ids || [],
              projectId: row.project_id },
          ]}
          currentValues={{
            subject: row.subject, description: row.description,
            status: row.status, response_text: row.response_text,
            per_task_impacts: row.per_task_impacts || [],
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}


function DeliveriesPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) picker. REQUIRED min-1 -- deliveries gate the tasks
  // consuming them so the cascade can attribute slip if a delivery is
  // late.
  const [draft, setDraft] = useState({
    vendor: "", description: "", po_number: "",
    linked_task_ids: [],
  });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (busy) return;
    if (!draft.vendor || !draft.description) return toast.error("Vendor + description required");
    if (!draft.linked_task_ids.length) return toast.error("Pick at least one task consuming this delivery");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      await api.post(`/deliveries`, {
        project_id: projectId, delivery_date: reportDate,
        daily_report_id: dailyReportId,
        vendor: draft.vendor, description: draft.description,
        po_number: draft.po_number || null,
        linked_task_ids: draft.linked_task_ids,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setDraft({ vendor: "", description: "", po_number: "", linked_task_ids: [] });
      setAddReason("");
      reload();
      if (locked) toast.success("Delivery added (post-submit). Audit chain captured.");
    } catch (e) {
      if (!handleLockedAddError(e, "delivery")) {
        toast.error(formatErr(e, "Log delivery"));
      }
    }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={Truck} title="Deliveries today" testid="dr-deliveries-panel" count={rows.length}>
      {rows.map((r) => (
        <DeliveryRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No deliveries scheduled for {reportDate}.</div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a delivery" testid="delivery-add-reason" />}
        <div className="grid grid-cols-3 gap-2">
          <input data-testid="delivery-new-vendor" placeholder="Vendor" value={draft.vendor} onChange={(e) => setDraft({ ...draft, vendor: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
          <input data-testid="delivery-new-desc" placeholder="What was delivered" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-slate-200 text-sm" />
          <input data-testid="delivery-new-po" placeholder="PO #" value={draft.po_number} onChange={(e) => setDraft({ ...draft, po_number: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
          <button data-testid="delivery-add" onClick={create}
            disabled={busy || (locked && !isAddReasonValid(addReason))}
            title={locked && !isAddReasonValid(addReason)
              ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
              : undefined}
            className="col-span-2 h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-1"><Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : locked ? "Log delivery (post-submit)" : "Log delivery"}</button>
        </div>
        <TaskPicker projectId={projectId}
          value={draft.linked_task_ids}
          onChange={(v) => setDraft({ ...draft, linked_task_ids: v })}
          required={true} label="Tasks consuming this delivery"
          testidPrefix="delivery-activity" />
      </div>
    </PanelShell>
  );
}

function NotesPanel({ report, dailyReportId, locked, reload }) {
  const [text, setText] = useState(report.notes || "");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  useEffect(() => { setText(report.notes || ""); }, [report.notes]);
  const save = async () => {
    if (busy) return;
    setBusy(true);
    try { await api.patch(`/daily-reports/${dailyReportId}`, { notes: text }); reload(); toast.success("Notes saved"); }
    catch (e) { toast.error(e.response?.data?.detail || "Save failed"); }
    finally { setBusy(false); }
  };
  // Bucket G Phase 3 (2026-05-11) -- Jigsaw pre-fill on the DR narrative.
  const userRole = (typeof window !== "undefined" && JSON.parse(localStorage.getItem("ice.user") || "null")?.role || "").toLowerCase();
  return (
    <PanelShell icon={ClipboardList} title="Daily notes / narrative" testid="dr-notes-panel">
      <JigsawField entityType="daily_report" entityId={dailyReportId}
        fieldPath="notes" userRole={userRole} fieldLabel="notes"
        onConfirm={(v) => setText(String(v ?? ""))}>
        <textarea data-testid="dr-notes-textarea" disabled={locked} value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Free-text notes that go on the PDF (visitors, accidents-near-misses summary, manpower changes, etc.)"
          className="w-full h-28 px-3 py-2 rounded border border-slate-200 text-sm" />
      </JigsawField>
      {!locked && (
        <div className="flex justify-end mt-2">
          <button data-testid="dr-notes-save" onClick={save} disabled={busy}
            className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
            {busy ? "Saving…" : "Save notes"}
          </button>
        </div>
      )}
    </PanelShell>
  );
}

function PhotosPanel({ rows, dailyReportId, projectId, locked, reload }) {
  const [busy, setBusy] = useState(false);
  // Bucket F Phase 4 -- after upload, we poll the tray's AI analysis
  // for up to ~6s while showing "Preparing tag suggestions…", then
  // open the tag wizard with the AI defaults pre-loaded. The wizard
  // starts at step 2 since the DR is already known from the page.
  const [aiPrepStatus, setAiPrepStatus] = useState(null); // null | 'preparing' | 'ready' | 'skipped'
  const [pendingTray, setPendingTray] = useState(null);
  const [openWizard, setOpenWizard] = useState(false);
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason
  // capture for photo uploads. Forwarded through to TagWizard which
  // includes it on the /photo-tags POST.
  const [addReason, setAddReason] = useState("");

  // Poll the tray's /ai-analysis until status leaves "pending"/"analyzing"
  // or we hit the time cap. We never fail the upload on AI errors;
  // worst case the wizard opens without AI defaults and the user tags
  // manually.
  const waitForAnalysis = async (trayId) => {
    const MAX_ITERS = 12; // 12 * 500ms = 6s
    for (let i = 0; i < MAX_ITERS; i++) {
      try {
        const r = await api.get(`/photo-tray/${trayId}/ai-analysis`);
        const s = r.data?.ai_analysis_status;
        if (s === "complete" || s === "failed" || s === "skipped"
            || s === "budget_paused" || s === "rejected_by_user") {
          return r.data;
        }
      } catch {
        // Non-fatal -- keep waiting; loop will eventually time out.
      }
      // Don't sleep on the last iteration.
      if (i < MAX_ITERS - 1) {
        await new Promise((res) => setTimeout(res, 500));
      }
    }
    return null;
  };

  // Bucket D Phase 2 -- DR upload now lands in the photo_tray
  // (status=untagged) instead of going straight to the legacy
  // /photos/upload endpoint. The tray page (sidebar) is where the
  // user opens the 3-step tagging wizard. Caption + class moved to
  // the wizard since both are tag-time decisions, not capture-time.
  //
  // Bucket F Phase 4 -- after upload, we don't redirect the user to
  // the tray page; we open the tag wizard inline with AI defaults
  // pre-loaded.
  const onUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    // Bucket D Addition 5 -- locked-DR uploads require add_reason BEFORE
    // we even hit the upload endpoint, so the user knows the gate up
    // front. The reason itself is enforced on the tag-create call.
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) before uploading to a locked DR.`);
      e.target.value = "";
      return;
    }
    setBusy(true);
    setAiPrepStatus(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("project_id", projectId);
      fd.append("capture_source", "manual_upload");
      const r = await api.post(`/photo-tray`, fd, { headers: { "Content-Type": "multipart/form-data" } });
      reload();
      const trayRow = r.data;
      if (!trayRow?.id) {
        toast.error("Upload returned no tray row");
        return;
      }
      // Existing dedup branch: same photo already uploaded -> the
      // server returns the prior tray row (any status). Surface a
      // friendlier message but still proceed to wizard if untagged.
      if (trayRow.status && trayRow.status !== "untagged") {
        toast.info(`Same photo already in tray (status=${trayRow.status}). Opening wizard.`);
      }
      // If CUI / AI disabled, the analysis status is already "skipped"
      // -- don't wait; just open the wizard immediately.
      const initialStatus = trayRow.ai_analysis_status;
      if (initialStatus === "complete" || initialStatus === "skipped"
          || initialStatus === "budget_paused"
          || initialStatus === "rejected_by_user"
          || initialStatus === "failed") {
        setPendingTray(trayRow);
        setAiPrepStatus(initialStatus === "complete" ? "ready" : "skipped");
        setOpenWizard(true);
        return;
      }
      // Otherwise: wait + show inline spinner.
      setAiPrepStatus("preparing");
      const finalRow = await waitForAnalysis(trayRow.id);
      // Merge the latest analysis fields back onto the tray row so
      // the wizard picks them up via its `tray` prop.
      const merged = finalRow ? { ...trayRow, ...finalRow } : trayRow;
      setPendingTray(merged);
      setAiPrepStatus(finalRow?.ai_analysis_status === "complete" ? "ready" : "skipped");
      setOpenWizard(true);
    } catch (err) {
      toast.error(err.response?.data?.detail || "Upload failed");
    }
    finally { setBusy(false); e.target.value = ""; }
  };

  return (
    <PanelShell icon={ImageIcon} title="Photos" testid="dr-photos-panel" count={rows.length}>
      <div className="grid grid-cols-3 gap-2">
        {rows.map((p) => (
          <AuthPhoto key={p.id} photo={p}
            locked={locked} dailyReportId={dailyReportId} reload={reload} />
        ))}
        {!rows.length && <div className="col-span-3 text-xs text-slate-400 py-4 text-center">No photos yet.</div>}
      </div>
      {/* Bucket F Phase 4 -- "Preparing tag suggestions…" spinner
          renders inline below the gallery while the upload waits for
          AI analysis to land. Cap is 6s; the user can keep working
          on the page in the meantime since the wizard hasn't opened
          yet. */}
      {aiPrepStatus === "preparing" && (
        <div data-testid="dr-photos-ai-preparing"
          className="mt-3 px-3 py-2 rounded bg-blue-50 border border-blue-200 text-blue-800 text-xs inline-flex items-center gap-2">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <Sparkles className="h-3.5 w-3.5" />
          Preparing tag suggestions…
        </div>
      )}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a photo" testid="photo-add-reason" />}
        <div className="text-[11px] text-slate-500">
          Photos upload to the <a href="/photo-tray" className="text-blue-600 underline">Photo tray</a> first.
          After upload, we'll open the tag wizard with AI suggestions ready.
        </div>
        <label data-testid="photo-upload-label" className="block">
          <span className="h-8 px-3 rounded bg-blue-600 text-white text-sm inline-flex items-center gap-1 hover:bg-blue-700 cursor-pointer">
            <ImageIcon className="h-3.5 w-3.5" />
            {busy ? "Uploading…" : (locked ? "Upload + tag (post-submit)" : "Upload + tag")}
          </span>
          <input data-testid="photo-upload" type="file" accept="image/*" onChange={onUpload} className="hidden" />
        </label>
      </div>
      {/* Auto-opened tag wizard. DR is pre-locked so the wizard starts
          at step 2 (event-type picker) and inherits AI suggestions. */}
      {pendingTray && (
        <TagWizard
          open={openWizard}
          onOpenChange={(v) => {
            setOpenWizard(v);
            if (!v) {
              setPendingTray(null);
              setAiPrepStatus(null);
              // Reset reason after the wizard closes so the next
              // locked-DR upload requires a fresh reason capture.
              if (locked) setAddReason("");
            }
          }}
          tray={pendingTray}
          projectId={projectId}
          initialDailyReportId={dailyReportId}
          isLocked={locked}
          lockedAddReason={locked ? addReason : null}
          onTagged={() => {
            reload();
            setOpenWizard(false);
            setPendingTray(null);
            setAiPrepStatus(null);
          }}
        />
      )}
    </PanelShell>
  );
}

function AuthPhoto({ photo, locked, dailyReportId, reload }) {
  const [src, setSrc] = useState(null);
  // Addition 4 follow-up -- locked-DR edit modal toggle for the photo
  // card. Pencil icon overlays in the top-right when locked; opens
  // LockedEventEditModal pre-filled with caption + photo_class.
  const [editOpen, setEditOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let blobUrl = null;
    api.get(`/photos/${photo.id}/file`, { responseType: "blob" })
      .then((res) => { if (cancelled) return; blobUrl = URL.createObjectURL(res.data); setSrc(blobUrl); })
      .catch(() => { /* ignore — show placeholder */ });
    return () => { cancelled = true; if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [photo.id]);
  const cls = PHOTO_CLASS_BY_VALUE[photo.photo_class] || PHOTO_CLASS_BY_VALUE.progress;
  return (
    <div data-testid={`photo-${photo.id}`} data-event-row-id={photo.id} data-event-row-type="photo"
      className="relative block aspect-square bg-slate-100 rounded overflow-hidden border border-slate-200 group">
      {src ? <img src={src} alt={photo.caption || ""} className="w-full h-full object-cover" /> :
        <div className="w-full h-full grid place-items-center text-xs text-slate-400">…</div>}
      <div className="absolute top-1 left-1 flex items-center gap-1">
        <span data-testid={`photo-class-${photo.id}`} className={`text-[9px] leading-none px-1.5 py-0.5 rounded border ${cls.color}`}>
          {cls.label}
        </span>
        {photo.evidentiary_flag && (
          <span data-testid={`photo-evidentiary-${photo.id}`} title="Evidentiary — locked record" className="bg-black/70 text-white rounded p-0.5">
            <Lock className="h-2.5 w-2.5" />
          </span>
        )}
        {photo.timestamp_mismatch_flag && (
          <span data-testid={`photo-tsmismatch-${photo.id}`} title="Device vs. server timestamp mismatch" className="bg-amber-500 text-white rounded p-0.5">
            <AlertTriangle className="h-2.5 w-2.5" />
          </span>
        )}
        {/* Bucket D Task 2 -- supplement marker overlaid on the
            photo card. Saffron pill matches SupplementBadge style. */}
        {photo.is_supplement && (
          <span data-testid={`photo-supplement-${photo.id}`}
            title={`Supplement to DR ${photo.parent_daily_report_date || photo.parent_daily_report_id?.slice(0, 8) || ""}`}
            className="bg-amber-200 text-amber-900 rounded px-1 text-[8px] font-mono uppercase">
            SUPP
          </span>
        )}
        {/* Addition 4 follow-up -- post-submit-edited annotation.
            Saffron square stays visible; the edit-pencil only shows
            on hover when the parent DR is locked. */}
        {photo.post_submit_edited && (
          <span data-testid={`photo-edited-${photo.id}`}
            title={`Edited ${photo.edit_count || 0}x post-submit. ${photo.last_edit_reason ? `Last reason: ${photo.last_edit_reason}` : ""}`}
            className="bg-amber-300 text-amber-900 rounded px-1 text-[8px] font-mono uppercase">
            EDITED {photo.edit_count || 0}x
          </span>
        )}
        {/* Bucket F Phase 3 pulled-forward (Risk #4) -- AI lineage
            badge. Stamps when the tag row's created_via_ai_suggestion
            is true. Renders alongside the post-submit-edited badge so
            counsel + supers can see both lineage facts at a glance. */}
        {photo.created_via_ai_suggestion && (
          <span data-testid={`photo-ai-${photo.id}`}
            title={
              photo.ai_confidence_at_tag_time != null
                ? `Tagged from AI suggestion at ${Math.round(photo.ai_confidence_at_tag_time * 100)}% confidence`
                : "Tagged from AI suggestion"
            }
            className="bg-blue-100 text-blue-800 border border-blue-200 rounded px-1 text-[8px] font-mono uppercase inline-flex items-center gap-0.5">
            <Sparkles className="h-2 w-2" /> AI
          </span>
        )}
      </div>
      {locked && (
        <button data-testid={`photo-edit-locked-${photo.id}`}
          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setEditOpen(true); }}
          title="Edit photo (post-submit -- captures audit trail)"
          className="absolute top-1 right-1 bg-amber-600/90 hover:bg-amber-700 text-white rounded p-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Pencil className="h-2.5 w-2.5" />
        </button>
      )}
      {photo.caption && <div className="absolute bottom-0 left-0 right-0 bg-black/60 text-white text-[10px] px-1 py-0.5 truncate">{photo.caption}</div>}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="photo" eventId={photo.id} dailyReportId={dailyReportId}
          patchUrl={`/photos/${photo.id}`}
          fields={[
            { key: "caption", label: "Caption", type: "textarea" },
            { key: "photo_class", label: "Photo class", type: "select",
              options: ["progress", "discovery", "damage", "quality", "safety", "coordination", "closeout"]
                .map((c) => ({ value: c, label: c })) },
          ]}
          currentValues={{
            caption: photo.caption || "", photo_class: photo.photo_class || "progress",
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}

function VoicePanel({ rows }) {
  return (
    <PanelShell icon={Mic} title="Voice annotations" testid="dr-voice-panel" count={rows.length}>
      <div className="space-y-2">
        {rows.map((v) => (
          <div key={v.id} data-testid={`voice-${v.id}`} className="bg-slate-50 border border-slate-200 rounded p-2 text-xs">
            <div className="flex items-center gap-2 mb-1">
              <span className="pill pill-slate">{v.kind}</span>
              <span className="text-slate-400">{new Date(v.created_at).toLocaleTimeString()}</span>
            </div>
            <div className="text-slate-700">{v.transcript || <em className="text-slate-400">(no transcript)</em>}</div>
          </div>
        ))}
        {!rows.length && <div className="text-xs text-slate-400 py-4 text-center">
          No voice memos linked to this report. Use the floating mic to record one — then link it to this DR via the result sheet.
        </div>}
      </div>
    </PanelShell>
  );
}

function AuditPanel({ rows, userLabels }) {
  return (
    <PanelShell icon={ClipboardList} title="Audit trail" testid="dr-audit-panel" count={rows.length}>
      <div className="space-y-1 max-h-60 overflow-y-auto">
        {rows.slice(0, 30).map((a) => (
          <div key={a.id} data-testid={`audit-${a.id}`} className="text-[11px] text-slate-600 flex gap-2 border-b border-slate-100 py-1">
            <span className="font-mono text-slate-400 w-32 shrink-0">{new Date(a.created_at).toLocaleTimeString()}</span>
            <span className="pill pill-slate">{a.op}</span>
            <span className="font-mono">{a.table_name}</span>
            <span className="text-slate-500">by {userLabels[a.actor_id] || (a.actor_id || "—").slice(0, 8)}</span>
          </div>
        ))}
        {!rows.length && <div className="text-xs text-slate-400 text-center py-3">No audit entries yet.</div>}
      </div>
    </PanelShell>
  );
}

// ------------------------------------------------------------
function DeliveryRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`delivery-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className="font-medium mr-2">{row.vendor}</span>
      <span className="text-slate-700">{row.description}</span>
      {row.po_number && <span className="text-slate-400 ml-2 font-mono">{row.po_number}</span>}
      <span className={`pill ml-2 ${row.status === "received" ? "pill-green" : "pill-slate"}`}>{row.status}</span>
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`delivery-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-[10px] text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="delivery" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/deliveries/${row.id}`}
          fields={[
            { key: "vendor", label: "Vendor" },
            { key: "description", label: "Description", type: "textarea" },
            { key: "po_number", label: "PO number" },
            { key: "delivery_date", label: "Delivery date", type: "text" },
            { key: "status", label: "Status", type: "select",
              options: ["scheduled", "received", "delayed", "cancelled"].map((s) => ({ value: s, label: s })) },
          ]}
          currentValues={{
            vendor: row.vendor, description: row.description,
            po_number: row.po_number, delivery_date: row.delivery_date,
            status: row.status,
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}


// Bucket D Task 3 fix D -- outcome capture button on QC rows that
// triggered a rework cascade. Modal collects actual rework days +
// notes, POSTs /qc-issues/{id}/record-rework-outcome which updates
// the QC, flips the synthetic delay to resolved, re-runs cascade+RAG.
// QC rework days (2026-05-17) -- actual rework captured in WORKING
// DAYS (was hours), consistent with the days-based estimate.
// ------------------------------------------------------------
function ReworkOutcomeButton({ qcIssue, reload }) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (days === "" || Number(days) < 0) {
      toast.error("Actual rework (days) required (>=0)"); return;
    }
    setBusy(true);
    try {
      await api.post(`/qc-issues/${qcIssue.id}/record-rework-outcome`, {
        rework_actual_days: Number(days),
        rework_outcome_notes: notes || null,
      });
      toast.success("Rework outcome recorded -- cascade re-running");
      setOpen(false);
      reload();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Outcome capture failed");
    } finally { setBusy(false); }
  };
  return (
    <>
      <button data-testid={`qc-rework-outcome-${qcIssue.id}`} onClick={() => setOpen(true)}
        className="ml-2 text-[10px] px-2 py-0.5 rounded bg-emerald-600 text-white hover:bg-emerald-700">
        Record outcome
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>Record rework outcome</DialogTitle></DialogHeader>
          <div className="space-y-3 text-sm">
            <div className="text-xs text-slate-500">
              QC: "{(qcIssue.description || "").slice(0, 80)}…"
              <br />
              Estimated: {qcIssue.rework_duration_days}d.
              Synthetic delay: <span className="font-mono">{(qcIssue.linked_synthetic_delay_id || "").slice(0, 8)}</span>.
            </div>
            <div>
              <label className="text-[11px] text-slate-500 uppercase">Actual rework (days) *</label>
              <input data-testid="qc-rework-actual-days" type="number" step="0.5" min="0"
                value={days} onChange={(e) => setDays(e.target.value)}
                className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
            </div>
            <div>
              <label className="text-[11px] text-slate-500 uppercase">Notes</label>
              <textarea data-testid="qc-rework-outcome-notes" rows={3}
                value={notes} onChange={(e) => setNotes(e.target.value)}
                placeholder="What went better or worse than expected?"
                className="block w-full px-2 py-1 rounded border border-slate-300 text-sm" />
            </div>
            <div className="flex justify-end gap-2">
              <button onClick={() => setOpen(false)} className="h-9 px-3 rounded bg-slate-200 text-slate-700 text-sm hover:bg-slate-300">Cancel</button>
              <button data-testid="qc-rework-outcome-submit" onClick={submit} disabled={busy}
                className="h-9 px-3 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-50">
                {busy ? "Recording…" : "Record outcome"}
              </button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ------------------------------------------------------------
// Bucket D Task 2 -- per-row supplement badge. Container DR panels
// display this next to event-row titles when the row is is_supplement
// =true, so the user can tell at a glance which rows are filed back
// against an older locked DR. Backend enriches parent_daily_report_
// date in the /context bundle (Task 2) so this renders without an
// extra fetch.
// ------------------------------------------------------------
function SupplementBadge({ row }) {
  if (!row?.is_supplement) return null;
  const parentDate = row.parent_daily_report_date || row.parent_daily_report_id?.slice(0, 8);
  return (
    <span data-testid={`supplement-badge-${row.id}`}
      title="Filed after the parent daily report was submitted"
      className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded bg-amber-200 text-amber-900 text-[9px] font-mono uppercase tracking-wide">
      SUPP→{parentDate || "?"}
    </span>
  );
}

// ------------------------------------------------------------
// Bucket D Addition 5 (2026-05-12) -- Supplements section HIDDEN.
//
// The supplement flow (file new events against a "container DR"
// pointing back to the locked parent) was replaced by the
// post_submit_added flag (file events directly on the locked DR
// with an add_reason). Existing supplement records stay in the DB
// for audit + TIA preservation, but the UI no longer renders them
// here -- they appear inline on each event panel like any other
// row, with their SUPP→<date> badge per SupplementBadge above.
//
// The function below is preserved (commented-out call site in the
// DR detail render) for forensic recovery if we ever need to expose
// the legacy supplement list as a debug surface. The early return
// keeps it from rendering anything in the meantime.
function SupplementsAppendix(_props) {
  return null;
}
// Legacy SupplementsAppendix implementation removed (2026-05-12). The
// supplement flow is replaced by post_submit_added on each event
// collection. Pre-existing supplement records still surface inline on
// their event panels with the SUPP→<date> badge (see SupplementBadge
// above) and are preserved in the DB / audit chain. Git history holds
// the legacy implementation if it ever needs to be revived as a debug
// surface.

// ------------------------------------------------------------
//  Tiny field helpers
// ------------------------------------------------------------
function Field({ label, value, onChange, type = "text", disabled, testid }) {
  return (
    <label className="text-xs flex flex-col gap-1">
      <span className="text-slate-500 uppercase tracking-wide">{label}</span>
      <input data-testid={testid} type={type} defaultValue={value} disabled={disabled}
        onBlur={(e) => onChange(e.target.value)}
        className="h-8 px-2 rounded border border-slate-200 bg-white text-sm" />
    </label>
  );
}
function CheckField({ label, value, onChange, disabled, testid }) {
  return (
    <label className="text-xs flex items-center gap-2 bg-slate-50 border border-slate-200 rounded px-2 py-1.5 cursor-pointer">
      <input data-testid={testid} type="checkbox" checked={value} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

// Surface backend errors as helpfully as possible — never "Load failed".
function formatErr(e, action = "Action") {
  const r = e.response;
  if (!r) return `${action} failed: ${e.message || "network error"}`;
  const detail = r.data?.detail;
  if (Array.isArray(detail)) {
    const lines = detail.map((d) => `${(d.loc || []).slice(-1)[0] || "?"}: ${d.msg || d}`);
    return `${action} failed (${r.status}): ${lines.join("; ").slice(0, 240)}`;
  }
  if (typeof detail === "string") return `${action} failed (${r.status}): ${detail}`;
  return `${action} failed (${r.status})`;
}
