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
import { localTodayISO } from "../lib/dateUtil";
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";
import CoordinationEventForm from "../components/forms/CoordinationEventForm";
import SupplementWizard from "../components/forms/SupplementWizard";
import DrActivityPicker from "../components/forms/DrActivityPicker";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import {
  Download, FilePlus2, RefreshCw, Cloud, Users, ClipboardList, Image as ImageIcon,
  Mic, AlertTriangle, ShieldAlert, ClipboardCheck, MessageSquare, Truck, Plus,
  Lock, CheckCircle2, ChevronLeft, Trash2, Wand2,
} from "lucide-react";

const RENOVATION_TYPES = ["renovation", "tenant_improvement"];

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
      toast.success(existed ? `Opened existing report for ${data.report_date}`
                            : `Daily report opened for ${data.report_date}`);
      setPickerOpen(false);
      setOpenId(data.id);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const removeReport = async (r) => {
    if (r.status !== "draft") return;
    if (!window.confirm(`Delete the draft daily report for ${r.report_date}? Linked delays/QC/safety/RFIs/photos will keep their records but their daily_report_id will be cleared.`)) return;
    try {
      await api.delete(`/daily-reports/${r.id}`);
      toast.success(`Deleted daily report for ${r.report_date}`);
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
                  <td className="font-mono">{r.report_date}</td>
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

  // Validation rules to run before submit (returns {ok, errors})
  const validate = () => {
    const errors = [];
    if (!report.weather_conditions) errors.push("Weather not recorded — click Auto-fill on the Weather panel.");
    if (presence.length === 0) errors.push("Crew presence is empty — add at least one trade row.");
    if (grid.length === 0) errors.push("Activity grid is empty — add at least one activity.");
    const untouched = grid.filter((r) => (r.pct_complete_end_of_day ?? null) === r.pct_complete_start_of_day && (r.hours_worked || 0) === 0);
    if (untouched.length === grid.length && grid.length > 0) errors.push("No activity progress recorded for the day.");
    return { ok: errors.length === 0, errors };
  };

  const submit = async () => {
    const { ok, errors } = validate();
    if (!ok) {
      toast.error("Cannot submit: " + errors[0]);
      return;
    }
    setBusy(true);
    setSubmitRowErrors({});
    try {
      await api.post(`/daily-reports/${id}/submit`, {});
      toast.success("Daily report submitted and locked.");
      reload();
    } catch (e) {
      // Bucket C3a Phase 3 — server returns structured per-row errors as
      // 422 detail.row_errors. Render them inline next to each offending
      // row instead of as a single toast.
      const detail = e.response?.data?.detail;
      if (e.response?.status === 422 && detail && typeof detail === "object" && Array.isArray(detail.row_errors)) {
        const byRow = {};
        for (const err of detail.row_errors) {
          if (!byRow[err.row_id]) byRow[err.row_id] = [];
          byRow[err.row_id].push(err);
        }
        setSubmitRowErrors(byRow);
        toast.error(detail.message || "Submit blocked — fix the rows highlighted in red.");
      } else {
        toast.error(e.response?.data?.detail?.message || e.response?.data?.detail || e.message || "Submit failed");
      }
    } finally { setBusy(false); }
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
      <Dialog open={coordOpen} onOpenChange={setCoordOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Quick-log coordination event</DialogTitle></DialogHeader>
          <CoordinationEventForm
            projectId={project.id}
            linkedDailyReportId={id}
            abbreviated={true}
            onSaved={() => { setCoordOpen(false); toast.success("Coordination event logged"); }}
            onCancel={() => setCoordOpen(false)}
          />
        </DialogContent>
      </Dialog>

      {/* WEATHER */}
      <WeatherPanel report={report} dailyReportId={id} locked={locked} reload={reload} />

      {/* CREW SUMMARY (presence) */}
      <PresencePanel rows={presence} dailyReportId={id} locked={locked} reload={reload}
        projectId={project.id} grid={grid} />

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
          <div className="font-mono text-base">{report.report_date}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 uppercase tracking-wide">Status</div>
          <div className="flex items-center gap-1">
            <StatusPill status={report.status} />
            {locked && <Lock className="h-3.5 w-3.5 text-slate-500" />}
          </div>
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
          {onQuickLogCoordination && !locked && (
            <button data-testid="dr-quick-log-coord" onClick={onQuickLogCoordination}
              className="h-9 px-3 rounded border border-slate-200 bg-white text-sm flex items-center gap-1 hover:bg-slate-50">
              <MessageSquare className="h-4 w-4" /> + Coordination
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

function PresencePanel({ rows, dailyReportId, locked, reload, projectId, grid = [] }) {
  const [draft, setDraft] = useState({ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "" });
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
    try {
      await api.post(`/daily-reports/${dailyReportId}/grid`, { schedule_activity_id: picking });
      setPicking(""); reload();
    } catch (e) { toast.error(formatErr(e, "Add planned activity")); }
  };
  // Bucket D Fix E -- double-click guard for unplanned-activity create.
  const [creatingUnplanned, setCreatingUnplanned] = useState(false);
  const createUnplanned = async () => {
    if (creatingUnplanned) return;
    if (!unplanned.description) return toast.error("Describe the unplanned activity first");
    setCreatingUnplanned(true);
    try {
      await api.post(`/daily-reports/${dailyReportId}/unplanned-activity`, unplanned);
      setUnplanned({ description: "", trade_code: "", duration_days: 1 });
      toast.success("Unplanned activity created — flagged for PM review.");
      reload();
    } catch (e) { toast.error(formatErr(e, "Create unplanned")); }
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
                  <td>{!locked && <button data-testid={`dr-remove-${r.activity_code}`} onClick={() => removeRow(r.id)} className="text-red-600 hover:bg-red-50 p-1 rounded text-xs">remove</button>}</td>
                </tr>
              );
            })}
            {!grid.length && <tr><td colSpan="13" className="text-center py-4 text-sm text-slate-500">No activities. Use the picker below.</td></tr>}
          </tbody>
        </table>
      </div>

      {!locked && (
        <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4 pt-3 border-t border-slate-100">
          {/* PLANNED ADD */}
          <div className="space-y-2">
            <div className="text-xs font-medium text-slate-600">Add planned activity ({available.length} available for {reportDate})</div>
            <div className="flex gap-2">
              <select data-testid="dr-add-activity-select" value={picking} onChange={(e) => setPicking(e.target.value)} className="flex-1 h-8 px-2 rounded border border-slate-200 text-sm">
                <option value="">— Pick —</option>
                {available.map((a) => <option key={a.id} value={a.id}>[{a.activity_code}] {a.description}{a.trade_code ? ` · ${a.trade_code}` : ""}</option>)}
              </select>
              <button data-testid="dr-add-activity-button" onClick={addActivity} disabled={!picking} className="h-8 px-3 rounded bg-blue-600 text-white text-sm disabled:opacity-50 hover:bg-blue-700">Add</button>
            </div>
          </div>
          {/* UNPLANNED ADD */}
          <div className="space-y-2 bg-amber-50 border border-amber-200 rounded p-3">
            <div className="text-xs font-medium text-amber-800 flex items-center gap-1"><Wand2 className="h-3 w-3" /> Create unplanned activity (flagged for PM review)</div>
            <div className="grid grid-cols-3 gap-2">
              <input data-testid="unplanned-desc" placeholder="What was done?" value={unplanned.description} onChange={(e) => setUnplanned({ ...unplanned, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-amber-200 text-sm bg-white" />
              <input data-testid="unplanned-trade" placeholder="Trade" value={unplanned.trade_code} onChange={(e) => setUnplanned({ ...unplanned, trade_code: e.target.value })} className="h-8 px-2 rounded border border-amber-200 text-sm bg-white" />
            </div>
            <button data-testid="unplanned-add" onClick={createUnplanned} disabled={creatingUnplanned}
              className="h-7 px-3 rounded bg-amber-700 text-white text-xs hover:bg-amber-800 disabled:opacity-50 flex items-center gap-1">
              <Plus className="h-3 w-3" /> {creatingUnplanned ? "Creating…" : "Create unplanned activity"}
            </button>
          </div>
        </div>
      )}
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

// ---------- Inline event panels (delays / safety / qc / rfis / deliveries) ----------
function DelaysPanel({ rows, dailyReportId, projectId, reportDate, locked, reload }) {
  const [showForm, setShowForm] = useState(false);

  return (
    <PanelShell icon={AlertTriangle} title="Delays" testid="dr-delays-panel" count={rows.length}>
      {rows.map((r) => (
        <DelayRowInline key={r.id} delay={r} reload={reload} />
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No delays today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100">
          {!showForm ? (
            <button data-testid="delay-add-open" onClick={() => setShowForm(true)}
              className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 flex items-center gap-1">
              <Plus className="h-3.5 w-3.5" /> Log a delay
            </button>
          ) : (
            <DelayForm
              projectId={projectId}
              reportDate={reportDate}
              dailyReportId={dailyReportId}
              onSaved={() => { setShowForm(false); reload(); toast.success("Delay logged"); }}
              onCancel={() => setShowForm(false)}
            />
          )}
        </div>
      )}
    </PanelShell>
  );
}

function DelayRowInline({ delay, reload }) {
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
      </div>
      <div className="text-slate-700 whitespace-pre-wrap">{delay.description}</div>
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
  // Bucket D Task 1 -- linked_dr_activity_ids picker. OPTIONAL for
  // safety per spec (some events area-wide); empty list still saves.
  const [draft, setDraft] = useState({
    description: "", severity: "minor", event_type: "near_miss",
    linked_dr_activity_ids: [],
  });
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
    setBusy(true);
    try {
      await api.post(`/safety-events`, {
        project_id: projectId, daily_report_id: dailyReportId, date: reportDate,
        description: draft.description, severity: draft.severity, event_type: draft.event_type,
        linked_dr_activity_ids: draft.linked_dr_activity_ids,
      });
      setDraft({ description: "", severity: "minor", event_type: "near_miss",
                  linked_dr_activity_ids: [] });
      reload();
    } catch (e) { toast.error(formatErr(e, "Create safety event")); }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={ShieldAlert} title="Safety events" testid="dr-safety-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`safety-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className="pill pill-slate mr-2">{r.event_type}</span>
          <span className={`pill mr-2 ${r.severity === "critical" || r.severity === "major" ? "pill-red" : "pill-slate"}`}>{r.severity}</span>
          <span className="text-slate-700 whitespace-pre-wrap">{r.description}</span>
          <SupplementBadge row={r} />
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No safety events today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="safety-form">
          <textarea data-testid="safety-new-desc" rows={4} placeholder="Describe the event"
            value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="w-full p-2 rounded border border-slate-200 text-sm" />
          <DrActivityPicker grid={grid}
            value={draft.linked_dr_activity_ids}
            onChange={(v) => setDraft({ ...draft, linked_dr_activity_ids: v })}
            required={false} label="Affected activities (optional)"
            testidPrefix="safety-activity" compact />
          <div className="grid grid-cols-2 gap-2">
            <select data-testid="safety-new-type" value={draft.event_type} onChange={(e) => setDraft({ ...draft, event_type: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["near_miss", "first_aid", "recordable", "lost_time", "fatality", "other"].map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
            <select data-testid="safety-new-severity" value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["minor", "moderate", "major", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <button data-testid="safety-add" onClick={create} disabled={busy}
            className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
            <Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : "Add"}
          </button>
        </div>
      )}
    </PanelShell>
  );
}

function QcPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Phase 5 -- QC severity gets the rework_required tier.
  // When picked, super enters rework_duration_hours + basis;
  // calibration RAG suggests a benchmark from prior outcomes (advisory).
  // Submit -> backend creates synthetic delay + runs CPM cascade.
  const [draft, setDraft] = useState({
    description: "", severity: "minor",
    rework_duration_hours: "", rework_duration_basis: "",
    linked_dr_activity_ids: [],
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

  const create = async () => {
    if (busy) return;
    if (!draft.description) return toast.error("Description required");
    if (!draft.linked_dr_activity_ids.length) return toast.error("Pick at least one affected activity");
    if (isRework) {
      if (!draft.rework_duration_hours || Number(draft.rework_duration_hours) <= 0) {
        return toast.error("Rework hours required when severity=rework_required");
      }
      if (!draft.rework_duration_basis.trim()) {
        return toast.error("Rework basis (short note) required");
      }
    }
    setBusy(true);
    try {
      const payload = {
        project_id: projectId,
        daily_report_id: dailyReportId,
        date: reportDate,
        description: draft.description,
        severity: draft.severity,
        linked_dr_activity_ids: draft.linked_dr_activity_ids,
      };
      if (isRework) {
        payload.rework_duration_hours = Number(draft.rework_duration_hours);
        payload.rework_duration_basis = draft.rework_duration_basis.trim();
      }
      await api.post(`/qc-issues`, payload);
      setDraft({ description: "", severity: "minor",
                  rework_duration_hours: "", rework_duration_basis: "",
                  linked_dr_activity_ids: [] });
      reload();
      toast.success(isRework
        ? "QC rework filed -- synthetic delay created and CPM cascade running."
        : "QC issue logged.");
    } catch (e) { toast.error(formatErr(e, "Create QC issue")); }
    finally { setBusy(false); }
  };

  const togglePicked = (id) => {
    setDraft((d) => ({
      ...d,
      linked_dr_activity_ids: d.linked_dr_activity_ids.includes(id)
        ? d.linked_dr_activity_ids.filter((x) => x !== id)
        : [...d.linked_dr_activity_ids, id],
    }));
  };
  const activitiesAvailable = (grid || []).filter((a) => a.id);

  return (
    <PanelShell icon={ClipboardCheck} title="QC issues" testid="dr-qc-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`qc-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className={`pill mr-2 ${
            r.severity === "rework_required" || r.severity === "stop_work" ? "pill-red"
            : r.severity === "critical" || r.severity === "major" ? "pill-red"
            : "pill-slate"
          }`}>{r.severity}</span>
          <span className="text-slate-700 whitespace-pre-wrap">{r.description}</span>
          {r.linked_synthetic_delay_id && (
            <span className="ml-2 text-[10px] text-rose-700 bg-rose-50 border border-rose-200 px-1 py-0.5 rounded">
              rework {r.rework_duration_hours}h
              {r.rework_actual_hours != null && ` (actual ${r.rework_actual_hours}h)`}
            </span>
          )}
          {/* Task 3 fix D -- outcome capture button. Visible when QC has
              a synthetic delay AND no actual hours yet recorded. */}
          {r.linked_synthetic_delay_id && r.rework_actual_hours == null && (
            <ReworkOutcomeButton qcIssue={r} reload={reload} />
          )}
          <SupplementBadge row={r} />
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No QC issues today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="qc-form">
          <textarea data-testid="qc-new-desc" rows={4} placeholder="Describe the issue"
            value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="w-full p-2 rounded border border-slate-200 text-sm" />

          {/* Bucket D Phase 1 -- affected-activity multi-select. */}
          <div>
            <div className="text-[10px] uppercase text-slate-500 mb-1">Affected activities (required)</div>
            <div className="max-h-24 overflow-auto border border-slate-200 rounded">
              {activitiesAvailable.length ? activitiesAvailable.map((a) => (
                <label key={a.id} className="flex items-center gap-2 px-2 py-1 border-b border-slate-100 cursor-pointer hover:bg-slate-50 text-xs">
                  <input type="checkbox" data-testid={`qc-activity-${a.activity_code}`}
                    checked={draft.linked_dr_activity_ids.includes(a.id)}
                    onChange={() => togglePicked(a.id)} />
                  <span className="font-mono text-blue-700">{a.activity_code}</span>
                  <span className="text-slate-700 truncate">{a.activity_description}</span>
                </label>
              )) : (
                <div className="p-2 text-xs text-slate-400">No activities on this DR yet</div>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <select data-testid="qc-new-severity" value={draft.severity}
              onChange={(e) => setDraft({ ...draft, severity: e.target.value })}
              className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["observation", "minor", "moderate", "major", "critical", "rework_required", "stop_work"]
                .map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
          </div>

          {/* Bucket D Phase 5 -- rework block. */}
          {isRework && (
            <div className="bg-rose-50 border border-rose-200 rounded p-2 space-y-2" data-testid="qc-rework-block">
              <div className="text-[11px] font-semibold text-rose-900">Rework duration estimate (super)</div>
              {benchmark?.benchmark_hours != null && (
                <div className="text-[10px] text-slate-600 italic" data-testid="qc-rework-benchmark">
                  {benchmark.basis}
                </div>
              )}
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="text-[10px] text-slate-500 uppercase">Hours *</label>
                  <input data-testid="qc-rework-hours" type="number" step="0.5" min="0"
                    value={draft.rework_duration_hours}
                    onChange={(e) => setDraft({ ...draft, rework_duration_hours: e.target.value })}
                    className="w-full h-8 px-2 rounded border border-rose-300 text-sm" />
                </div>
                <div className="col-span-2">
                  <label className="text-[10px] text-slate-500 uppercase">Basis * (short note)</label>
                  <input data-testid="qc-rework-basis" type="text"
                    value={draft.rework_duration_basis}
                    onChange={(e) => setDraft({ ...draft, rework_duration_basis: e.target.value })}
                    placeholder="e.g. 2 plumbers re-rough 11A south wall, 4hrs each"
                    className="w-full h-8 px-2 rounded border border-rose-300 text-sm" />
                </div>
              </div>
              <div className="text-[10px] text-rose-800">
                On save, a synthetic delay (cause=quality_rework) will be created and the
                CPM cascade run for the affected activities.
              </div>
            </div>
          )}

          <div className="flex justify-end">
            <button data-testid="qc-add" onClick={create} disabled={busy}
              className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
              <Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : "Add QC issue"}
            </button>
          </div>
        </div>
      )}
    </PanelShell>
  );
}

function RfisPanel({ rows, dailyReportId, projectId, locked, reload, grid }) {
  // Bucket D Task 1c -- linked_dr_activity_ids picker. RFIs are
  // REQUIRED to link >=1 activity per Phase 1 (gates a task).
  const [draft, setDraft] = useState({
    subject: "", description: "", priority: "normal",
    linked_dr_activity_ids: [],
  });
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (busy) return;
    if (!draft.subject || !draft.description) return toast.error("Subject + question required");
    if (!draft.linked_dr_activity_ids.length) return toast.error("Pick at least one affected activity");
    setBusy(true);
    try {
      await api.post(`/rfis`, {
        project_id: projectId, daily_report_id: dailyReportId,
        subject: draft.subject,
        question: draft.description,  // backend alias -> description
        priority: draft.priority,
        linked_dr_activity_ids: draft.linked_dr_activity_ids,
      });
      setDraft({ subject: "", description: "", priority: "normal", linked_dr_activity_ids: [] });
      reload();
    } catch (e) { toast.error(formatErr(e, "Open RFI")); }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={MessageSquare} title="RFIs" testid="dr-rfis-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`rfi-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className="font-mono mr-2 text-blue-700">{r.rfi_number}</span>
          <span className="pill pill-slate mr-2">{r.priority}</span>
          <span className="text-slate-700">{r.subject}</span>
          <SupplementBadge row={r} />
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No RFIs raised today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
          <input data-testid="rfi-new-subject" placeholder="Subject" value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} className="w-full h-8 px-2 rounded border border-slate-200 text-sm" />
          <textarea data-testid="rfi-new-desc" placeholder="Question / context" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="w-full px-2 py-1 rounded border border-slate-200 text-sm" rows={2} />
          <DrActivityPicker grid={grid}
            value={draft.linked_dr_activity_ids}
            onChange={(v) => setDraft({ ...draft, linked_dr_activity_ids: v })}
            required={true} label="Affected activities"
            testidPrefix="rfi-activity" compact />
          <div className="flex items-center gap-2">
            <select data-testid="rfi-new-priority" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["low", "normal", "high", "urgent"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <button data-testid="rfi-add" onClick={create} disabled={busy}
              className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center gap-1"><Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : "Open RFI"}</button>
          </div>
        </div>
      )}
    </PanelShell>
  );
}

function DeliveriesPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Task 1d -- linked_dr_activity_ids picker. REQUIRED min-1
  // (deliveries gate the activities consuming them per Phase 1 spec).
  const [draft, setDraft] = useState({
    vendor: "", description: "", po_number: "",
    linked_dr_activity_ids: [],
  });
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (busy) return;
    if (!draft.vendor || !draft.description) return toast.error("Vendor + description required");
    if (!draft.linked_dr_activity_ids.length) return toast.error("Pick at least one activity consuming this delivery");
    setBusy(true);
    try {
      await api.post(`/deliveries`, {
        project_id: projectId, delivery_date: reportDate,
        daily_report_id: dailyReportId,
        vendor: draft.vendor, description: draft.description,
        po_number: draft.po_number || null,
        linked_dr_activity_ids: draft.linked_dr_activity_ids,
      });
      setDraft({ vendor: "", description: "", po_number: "", linked_dr_activity_ids: [] });
      reload();
    } catch (e) { toast.error(formatErr(e, "Log delivery")); }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={Truck} title="Deliveries today" testid="dr-deliveries-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`delivery-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className="font-medium mr-2">{r.vendor}</span>
          <span className="text-slate-700">{r.description}</span>
          {r.po_number && <span className="text-slate-400 ml-2 font-mono">{r.po_number}</span>}
          <span className={`pill ml-2 ${r.status === "received" ? "pill-green" : "pill-slate"}`}>{r.status}</span>
          <SupplementBadge row={r} />
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No deliveries scheduled for {reportDate}.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
          <div className="grid grid-cols-3 gap-2">
            <input data-testid="delivery-new-vendor" placeholder="Vendor" value={draft.vendor} onChange={(e) => setDraft({ ...draft, vendor: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
            <input data-testid="delivery-new-desc" placeholder="What was delivered" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-slate-200 text-sm" />
            <input data-testid="delivery-new-po" placeholder="PO #" value={draft.po_number} onChange={(e) => setDraft({ ...draft, po_number: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
            <button data-testid="delivery-add" onClick={create} disabled={busy}
              className="col-span-2 h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center gap-1"><Plus className="h-3.5 w-3.5" /> {busy ? "Saving…" : "Log delivery"}</button>
          </div>
          <DrActivityPicker grid={grid}
            value={draft.linked_dr_activity_ids}
            onChange={(v) => setDraft({ ...draft, linked_dr_activity_ids: v })}
            required={true} label="Activities consuming this delivery"
            testidPrefix="delivery-activity" compact />
        </div>
      )}
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
  return (
    <PanelShell icon={ClipboardList} title="Daily notes / narrative" testid="dr-notes-panel">
      <textarea data-testid="dr-notes-textarea" disabled={locked} value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Free-text notes that go on the PDF (visitors, accidents-near-misses summary, manpower changes, etc.)"
        className="w-full h-28 px-3 py-2 rounded border border-slate-200 text-sm" />
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
  // Bucket D Phase 2 -- DR upload now lands in the photo_tray
  // (status=untagged) instead of going straight to the legacy
  // /photos/upload endpoint. The tray page (sidebar) is where the
  // user opens the 3-step tagging wizard. Caption + class moved to
  // the wizard since both are tag-time decisions, not capture-time.
  const onUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("project_id", projectId);
      fd.append("capture_source", "manual_upload");
      const r = await api.post(`/photo-tray`, fd, { headers: { "Content-Type": "multipart/form-data" } });
      reload();
      if (r.data?.status === "untagged") {
        toast.success("Uploaded to photo tray. Tag it from the Photo tray page.");
      } else {
        toast.info(`Same photo already in tray (status=${r.data?.status})`);
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || "Upload failed");
    }
    finally { setBusy(false); e.target.value = ""; }
  };

  return (
    <PanelShell icon={ImageIcon} title="Photos" testid="dr-photos-panel" count={rows.length}>
      <div className="grid grid-cols-3 gap-2">
        {rows.map((p) => <AuthPhoto key={p.id} photo={p} />)}
        {!rows.length && <div className="col-span-3 text-xs text-slate-400 py-4 text-center">No photos yet.</div>}
      </div>
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
          <div className="text-[11px] text-slate-500">
            Photos upload to the <a href="/photo-tray" className="text-blue-600 underline">Photo tray</a> first.
            Tag them to specific events from there.
          </div>
          <label data-testid="photo-upload-label" className="block">
            <span className="h-8 px-3 rounded bg-blue-600 text-white text-sm inline-flex items-center gap-1 hover:bg-blue-700 cursor-pointer">
              <ImageIcon className="h-3.5 w-3.5" /> {busy ? "Uploading…" : "Upload to tray"}
            </span>
            <input data-testid="photo-upload" type="file" accept="image/*" onChange={onUpload} className="hidden" />
          </label>
        </div>
      )}
    </PanelShell>
  );
}

function AuthPhoto({ photo }) {
  const [src, setSrc] = useState(null);
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
      className="relative block aspect-square bg-slate-100 rounded overflow-hidden border border-slate-200">
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
      </div>
      {photo.caption && <div className="absolute bottom-0 left-0 right-0 bg-black/60 text-white text-[10px] px-1 py-0.5 truncate">{photo.caption}</div>}
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
// Bucket D Task 3 fix D -- outcome capture button on QC rows that
// triggered a rework cascade. Modal collects actual hours + notes,
// POSTs /qc-issues/{id}/record-rework-outcome which updates the QC,
// flips the synthetic delay to resolved, re-runs cascade + RAG.
// ------------------------------------------------------------
function ReworkOutcomeButton({ qcIssue, reload }) {
  const [open, setOpen] = useState(false);
  const [hours, setHours] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!hours || Number(hours) < 0) {
      toast.error("Actual hours required (>=0)"); return;
    }
    setBusy(true);
    try {
      await api.post(`/qc-issues/${qcIssue.id}/record-rework-outcome`, {
        rework_actual_hours: Number(hours),
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
              Estimated: {qcIssue.rework_duration_hours}h.
              Synthetic delay: <span className="font-mono">{(qcIssue.linked_synthetic_delay_id || "").slice(0, 8)}</span>.
            </div>
            <div>
              <label className="text-[11px] text-slate-500 uppercase">Actual hours *</label>
              <input data-testid="qc-rework-actual-hours" type="number" step="0.5" min="0"
                value={hours} onChange={(e) => setHours(e.target.value)}
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
// Bucket D Phase 4 -- supplements appendix on the parent DR.
// Lists every event with parent_daily_report_id == this DR.
// ------------------------------------------------------------
function SupplementsAppendix({ dailyReportId, projectId }) {
  const [data, setData] = useState(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [parentDr, setParentDr] = useState(null);
  const reload = useCallback(() => {
    if (!dailyReportId) return;
    api.get(`/daily-reports/${dailyReportId}/supplements`)
      .then(({ data }) => {
        setData(data);
        setParentDr({ id: data.daily_report_id, report_date: data.report_date });
      })
      .catch(() => setData(null));
  }, [dailyReportId]);
  useEffect(() => { reload(); }, [reload]);

  // Bucket D Phase 4 -- only render the appendix if there are
  // supplements OR the user is viewing a locked DR (so they can
  // file one). For draft DRs we hide the appendix entirely.
  const [parentLocked, setParentLocked] = useState(false);
  useEffect(() => {
    if (!dailyReportId) return;
    api.get(`/daily-reports/${dailyReportId}`)
      .then(({ data }) => setParentLocked(data?.status === "submitted" || data?.status === "approved"))
      .catch(() => setParentLocked(false));
  }, [dailyReportId]);

  const fileSupplement = async () => {
    if (!projectId) return;
    try {
      // Idempotent on (project, date) -- returns existing today's DR or creates one.
      const today = localTodayISO();
      const { data: container } = await api.post("/daily-reports", {
        project_id: projectId, report_date: today,
      });
      // Container must NOT be the same as parent (filing supplement to today's DR is nonsense).
      if (container.id === dailyReportId) {
        toast.error("Cannot file a supplement to today's DR -- edit it directly.");
        return;
      }
      // Set window state so the wizard knows the container DR id.
      window.__supplementContainerId = container.id;
      setWizardOpen(true);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Couldn't open today's DR");
    }
  };

  if (!data) return null;
  const buckets = [
    ["delays", "Delays", AlertTriangle],
    ["rfis", "RFIs", MessageSquare],
    ["qc_issues", "QC issues", ClipboardCheck],
    ["safety_events", "Safety events", ShieldAlert],
    ["coordination_events", "Coordination", MessageSquare],
    ["deliveries", "Deliveries", Truck],
    ["photos", "Photos", ImageIcon],
    ["daily_report_activities", "Activity rows", ClipboardList],
  ];
  const total = buckets.reduce((s, [k]) => s + (data[k]?.length || 0), 0);
  // If no supplements AND parent is unlocked, hide entirely.
  if (!total && !parentLocked) return null;
  return (
    <PanelShell icon={ClipboardList} title={`Supplements (${total})`} testid="dr-supplements-panel" count={total}>
      <div className="text-xs text-slate-500 mb-2 flex items-center justify-between gap-2">
        <span>
          Facts filed AFTER this report was submitted. Each supplement is linked to its container
          DR (today's, where it was filed) and renders here to keep the parent DR's record honest.
        </span>
        {parentLocked && (
          <button data-testid="file-supplement-button" onClick={fileSupplement}
            className="h-7 px-2 rounded bg-amber-600 text-white text-xs hover:bg-amber-700 inline-flex items-center gap-1 flex-shrink-0">
            <Plus className="h-3 w-3" /> File supplement
          </button>
        )}
      </div>
      <SupplementWizard open={wizardOpen} onOpenChange={setWizardOpen}
        parentDailyReport={parentDr} projectId={projectId}
        containerDailyReportId={typeof window !== "undefined" ? window.__supplementContainerId : null}
        onSaved={reload} />
      {buckets.map(([key, label, Icon]) => {
        const rows = data[key] || [];
        if (!rows.length) return null;
        return (
          <div key={key} className="mb-3">
            <div className="text-[11px] uppercase font-semibold text-slate-700 flex items-center gap-1 mb-1">
              <Icon className="h-3.5 w-3.5" /> {label} ({rows.length})
            </div>
            <ul className="space-y-1 text-xs">
              {rows.map((r) => (
                <li key={r.id} data-testid={`supplement-${key}-${r.id}`}
                  className="bg-amber-50 border-l-2 border-amber-400 px-2 py-1 rounded-r">
                  <span className="text-amber-800 font-mono text-[10px] mr-1">[SUPP]</span>
                  <span className="text-slate-700">{r.description || r.subject || r.summary || r.vendor || (r.tag_caption || r.id.slice(0, 8))}</span>
                  {r.supplement_filed_at && (
                    <span className="text-slate-400 ml-2">
                      filed {new Date(r.supplement_filed_at).toLocaleDateString()}
                    </span>
                  )}
                  {r.supplement_reason && (
                    <span className="text-slate-500 italic ml-2">— "{r.supplement_reason}"</span>
                  )}
                  {r.daily_report_id && r.daily_report_id !== dailyReportId && (
                    <a href={`/daily-reports?openId=${r.daily_report_id}`}
                      className="text-blue-600 underline ml-2 text-[10px]">
                      [container DR]
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </PanelShell>
  );
}

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
