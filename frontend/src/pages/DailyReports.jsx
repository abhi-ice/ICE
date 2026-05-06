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
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";
import {
  Download, FilePlus2, RefreshCw, Cloud, Users, ClipboardList, Image as ImageIcon,
  Mic, AlertTriangle, ShieldAlert, ClipboardCheck, MessageSquare, Truck, Plus,
  Lock, CheckCircle2, ChevronLeft, Trash2, Wand2,
} from "lucide-react";

const RENOVATION_TYPES = ["renovation", "tenant_improvement"];

// ------------------------------------------------------------
//  Top-level list / open-today
// ------------------------------------------------------------
export default function DailyReports() {
  const { activeId } = useProjects();
  const [reports, setReports] = useState([]);
  const [openId, setOpenId] = useState(null);

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

  const create = async () => {
    const today = new Date().toISOString().slice(0, 10);
    try {
      const { data } = await api.post("/daily-reports", { project_id: activeId, report_date: today });
      toast.success(`Daily report opened for ${data.report_date}`);
      setOpenId(data.id);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
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
        <button data-testid="open-today-report" onClick={create}
          className="h-9 px-3 rounded bg-primary text-primary-foreground text-sm flex items-center gap-2">
          <FilePlus2 className="h-4 w-4" /> Open today&apos;s report
        </button>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead><tr><th>Date</th><th>Status</th><th>Submitted</th><th></th></tr></thead>
          <tbody>
            {reports.map((r) => (
              <tr key={r.id} data-testid={`dr-row-${r.id}`}>
                <td className="font-mono">{r.report_date}</td>
                <td><StatusPill status={r.status} /></td>
                <td className="text-xs">{r.submitted_at ? new Date(r.submitted_at).toLocaleString() : "—"}</td>
                <td>
                  <button data-testid={`open-dr-${r.id}`} onClick={() => setOpenId(r.id)} className="text-sm text-primary hover:underline">
                    Open
                  </button>
                </td>
              </tr>
            ))}
            {!reports.length && <tr><td colSpan="4" className="text-center py-6 text-sm text-muted-foreground">No reports yet.</td></tr>}
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
    try {
      await api.post(`/daily-reports/${id}/submit`, {});
      toast.success("Daily report submitted and locked.");
      reload();
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
              busy={busy} validate={validate} />

      {/* WEATHER */}
      <WeatherPanel report={report} dailyReportId={id} locked={locked} reload={reload} />

      {/* CREW SUMMARY (presence) */}
      <PresencePanel rows={presence} dailyReportId={id} locked={locked} reload={reload} />

      {/* ACTIVITY GRID */}
      <ActivityGridPanel grid={grid} available={available} reload={reload}
        dailyReportId={id} locked={locked} reportDate={report.report_date}
        presence={presence} />

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
                     reportDate={report.report_date} locked={locked} reload={reload} />
        <QcPanel rows={qc_issues} dailyReportId={id} projectId={project.id}
                 reportDate={report.report_date} locked={locked} reload={reload} />
        <RfisPanel rows={rfis} dailyReportId={id} projectId={project.id}
                   locked={locked} reload={reload} />
        <DeliveriesPanel rows={deliveries} dailyReportId={id} projectId={project.id}
                         reportDate={report.report_date} locked={locked} reload={reload} />
        <NotesPanel report={report} dailyReportId={id} locked={locked} reload={reload} />
      </div>

      {/* PHOTOS + VOICE */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <PhotosPanel rows={photos} dailyReportId={id} projectId={project.id}
                     locked={locked} reload={reload} />
        <VoicePanel rows={voice_annotations} dailyReportId={id} reload={reload} />
      </div>

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
                  onApprove, onDownload, busy, validate }) {
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

function PresencePanel({ rows, dailyReportId, locked, reload }) {
  const [draft, setDraft] = useState({ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "" });
  const total = rows.reduce((s, r) => s + (r.headcount || 0), 0);
  const totalHours = rows.reduce((s, r) => s + (r.headcount || 0) * (r.hours_per_person || 0), 0);

  const add = async () => {
    if (!draft.trade) return toast.error("Trade is required");
    try {
      await api.post(`/daily-reports/${dailyReportId}/presence`, { ...draft, daily_report_id: dailyReportId });
      setDraft({ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "" });
      reload();
    } catch (e) { toast.error(e.response?.data?.detail || "Add failed"); }
  };
  const patch = async (rid, fields) => {
    try { await api.patch(`/daily-presence/${rid}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };
  const remove = async (rid) => {
    if (!window.confirm("Remove this presence row?")) return;
    await api.delete(`/daily-presence/${rid}`); reload();
  };

  return (
    <PanelShell icon={Users} title="Crew presence" testid="dr-presence-panel" count={rows.length}
      badge={<span className="text-[10px] text-slate-500 ml-2">total: {total} workers · {totalHours.toFixed(1)} hours</span>}>
      <div className="overflow-x-auto">
        <table className="dense-table">
          <thead><tr><th>Trade</th><th>Company</th><th>Headcount</th><th>Hrs/person</th><th>Sub-total</th><th>Notes</th><th></th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} data-testid={`presence-row-${r.id}`}>
                <td>
                  <input data-testid={`presence-trade-${r.id}`} defaultValue={r.trade}
                    onBlur={(e) => patch(r.id, { trade: e.target.value })} disabled={locked}
                    className="w-32 px-2 py-1 border border-slate-200 rounded text-sm" />
                </td>
                <td><input defaultValue={r.company_name || ""} onBlur={(e) => patch(r.id, { company_name: e.target.value })} disabled={locked} className="w-44 px-2 py-1 border border-slate-200 rounded text-sm" /></td>
                <td><input type="number" defaultValue={r.headcount} onBlur={(e) => patch(r.id, { headcount: Number(e.target.value) })} disabled={locked} className="w-20 px-2 py-1 border border-slate-200 rounded text-sm tabular-nums" /></td>
                <td><input type="number" step="0.5" defaultValue={r.hours_per_person} onBlur={(e) => patch(r.id, { hours_per_person: Number(e.target.value) })} disabled={locked} className="w-20 px-2 py-1 border border-slate-200 rounded text-sm tabular-nums" /></td>
                <td className="tabular-nums text-sm">{((r.headcount || 0) * (r.hours_per_person || 0)).toFixed(1)}</td>
                <td><input defaultValue={r.notes || ""} onBlur={(e) => patch(r.id, { notes: e.target.value })} disabled={locked} className="w-44 px-2 py-1 border border-slate-200 rounded text-sm" /></td>
                <td>{!locked && <button data-testid={`presence-delete-${r.id}`} onClick={() => remove(r.id)} className="text-red-600 hover:bg-red-50 p-1 rounded"><Trash2 className="h-3.5 w-3.5" /></button>}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan="7" className="text-center py-4 text-sm text-slate-500">No presence yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {!locked && (
        <div className="mt-3 flex flex-wrap gap-2 items-end pt-3 border-t border-slate-100">
          <Field label="Trade" value={draft.trade} onChange={(v) => setDraft({ ...draft, trade: v })} testid="presence-new-trade" />
          <Field label="Company" value={draft.company_name} onChange={(v) => setDraft({ ...draft, company_name: v })} testid="presence-new-company" />
          <Field label="Headcount" type="number" value={draft.headcount} onChange={(v) => setDraft({ ...draft, headcount: Number(v) })} testid="presence-new-headcount" />
          <Field label="Hrs/person" type="number" value={draft.hours_per_person} onChange={(v) => setDraft({ ...draft, hours_per_person: Number(v) })} testid="presence-new-hours" />
          <button data-testid="presence-add" onClick={add} className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 flex items-center gap-1">
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

function ActivityGridPanel({ grid, available, reload, dailyReportId, locked, reportDate, presence = [] }) {
  const [picking, setPicking] = useState("");
  const [unplanned, setUnplanned] = useState({ description: "", trade_code: "", duration_days: 1 });
  const [refreshing, setRefreshing] = useState(false);

  const refreshFromSchedule = async () => {
    setRefreshing(true);
    try {
      const { data } = await api.post(`/daily-reports/${dailyReportId}/refresh-grid`);
      toast.success(data.inserted ? `${data.inserted} activity row(s) added from schedule.` : "Already up to date.");
      reload();
    } catch (e) { toast.error(formatErr(e, "Refresh grid")); }
    finally { setRefreshing(false); }
  };

  const updateRow = async (rowId, fields) => {
    try { await api.patch(`/daily-report-activities/${rowId}`, fields); reload(); }
    catch (e) { toast.error(formatErr(e, "Update row")); }
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
  const createUnplanned = async () => {
    if (!unplanned.description) return toast.error("Describe the unplanned activity first");
    try {
      await api.post(`/daily-reports/${dailyReportId}/unplanned-activity`, unplanned);
      setUnplanned({ description: "", trade_code: "", duration_days: 1 });
      toast.success("Unplanned activity created — flagged for PM review.");
      reload();
    } catch (e) { toast.error(formatErr(e, "Create unplanned")); }
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
              <th>Hours</th><th>Status</th><th>Notes</th><th></th>
            </tr>
          </thead>
          <tbody>
            {grid.map((r) => {
              const delta = (r.pct_complete_end_of_day || 0) - (r.pct_complete_start_of_day || 0);
              const variance = delta < 0 ? "neg" : delta === 0 ? "zero" : "pos";
              const isUnplanned = (r.activity_code || "").startsWith("AF");
              return (
                <tr key={r.id} data-testid={`dr-row-${r.activity_code}`}>
                  <td className="font-mono text-xs">
                    {r.activity_code}
                    {isUnplanned && <span className="ml-1 px-1 rounded bg-amber-100 text-amber-700 text-[9px] uppercase">field</span>}
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
                  <td><input type="text" defaultValue={r.notes || ""} onBlur={(e) => updateRow(r.id, { notes: e.target.value })} disabled={locked} placeholder="Notes…" className="w-44" /></td>
                  <td>{!locked && <button data-testid={`dr-remove-${r.activity_code}`} onClick={() => removeRow(r.id)} className="text-red-600 hover:bg-red-50 p-1 rounded text-xs">remove</button>}</td>
                </tr>
              );
            })}
            {!grid.length && <tr><td colSpan="12" className="text-center py-4 text-sm text-slate-500">No activities. Use the picker below.</td></tr>}
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
            <button data-testid="unplanned-add" onClick={createUnplanned} className="h-7 px-3 rounded bg-amber-700 text-white text-xs hover:bg-amber-800 flex items-center gap-1">
              <Plus className="h-3 w-3" /> Create unplanned activity
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
  const [resolveDate, setResolveDate] = useState(new Date().toISOString().slice(0, 10));
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
  const [resolveDate, setResolveDate] = useState(new Date().toISOString().slice(0, 10));
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

function SafetyPanel({ rows, dailyReportId, projectId, reportDate, locked, reload }) {
  // Phase C — simplified: description + type + severity. OSHA recordable
  // checkbox removed per spec.
  const [draft, setDraft] = useState({ description: "", severity: "minor", event_type: "near_miss" });
  // Phase D — copilot can push a drafted description into this form.
  useEffect(() => subscribeDescriptionInsert(({ target, text }) => {
    if (target !== "safety" || !text) return;
    setDraft((d) => ({ ...d, description: d.description ? `${d.description}\n\n${text}` : text }));
  }), []);
  const create = async () => {
    if (!draft.description) return toast.error("Description required");
    try {
      await api.post(`/safety-events`, { ...draft, project_id: projectId, daily_report_id: dailyReportId, date: reportDate });
      setDraft({ description: "", severity: "minor", event_type: "near_miss" });
      reload();
    } catch (e) { toast.error(formatErr(e, "Create safety event")); }
  };
  return (
    <PanelShell icon={ShieldAlert} title="Safety events" testid="dr-safety-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`safety-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className="pill pill-slate mr-2">{r.event_type}</span>
          <span className={`pill mr-2 ${r.severity === "critical" || r.severity === "major" ? "pill-red" : "pill-slate"}`}>{r.severity}</span>
          <span className="text-slate-700 whitespace-pre-wrap">{r.description}</span>
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No safety events today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="safety-form">
          <textarea data-testid="safety-new-desc" rows={6} placeholder="Describe the event"
            value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="w-full p-2 rounded border border-slate-200 text-sm" />
          <div className="grid grid-cols-2 gap-2">
            <select data-testid="safety-new-type" value={draft.event_type} onChange={(e) => setDraft({ ...draft, event_type: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["near_miss", "first_aid", "recordable", "lost_time", "fatality", "other"].map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
            <select data-testid="safety-new-severity" value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["minor", "moderate", "major", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <button data-testid="safety-add" onClick={create} className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1">
            <Plus className="h-3.5 w-3.5" /> Add
          </button>
        </div>
      )}
    </PanelShell>
  );
}

function QcPanel({ rows, dailyReportId, projectId, reportDate, locked, reload }) {
  // Phase C — simplified: description + severity only. Spec section
  // reference removed per spec.
  const [draft, setDraft] = useState({ description: "", severity: "minor" });
  // Phase D — copilot can push a drafted description into this form.
  useEffect(() => subscribeDescriptionInsert(({ target, text }) => {
    if (target !== "qc" || !text) return;
    setDraft((d) => ({ ...d, description: d.description ? `${d.description}\n\n${text}` : text }));
  }), []);
  const create = async () => {
    if (!draft.description) return toast.error("Description required");
    try {
      await api.post(`/qc-issues`, { ...draft, project_id: projectId, daily_report_id: dailyReportId, date: reportDate });
      setDraft({ description: "", severity: "minor" });
      reload();
    } catch (e) { toast.error(formatErr(e, "Create QC issue")); }
  };
  return (
    <PanelShell icon={ClipboardCheck} title="QC issues" testid="dr-qc-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`qc-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className={`pill mr-2 ${r.severity === "critical" || r.severity === "major" ? "pill-red" : "pill-slate"}`}>{r.severity}</span>
          <span className="text-slate-700 whitespace-pre-wrap">{r.description}</span>
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No QC issues today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="qc-form">
          <textarea data-testid="qc-new-desc" rows={6} placeholder="Describe the issue"
            value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="w-full p-2 rounded border border-slate-200 text-sm" />
          <div className="flex items-center gap-2">
            <select data-testid="qc-new-severity" value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["minor", "moderate", "major", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <button data-testid="qc-add" onClick={create} className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1">
              <Plus className="h-3.5 w-3.5" /> Add QC issue
            </button>
          </div>
        </div>
      )}
    </PanelShell>
  );
}

function RfisPanel({ rows, dailyReportId, projectId, locked, reload }) {
  const [draft, setDraft] = useState({ subject: "", description: "", priority: "normal" });
  const create = async () => {
    if (!draft.subject || !draft.description) return toast.error("Subject + question required");
    try {
      await api.post(`/rfis`, { ...draft, project_id: projectId, daily_report_id: dailyReportId });
      setDraft({ subject: "", description: "", priority: "normal" });
      reload();
    } catch (e) { toast.error(formatErr(e, "Open RFI")); }
  };
  return (
    <PanelShell icon={MessageSquare} title="RFIs" testid="dr-rfis-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`rfi-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className="font-mono mr-2 text-blue-700">{r.rfi_number}</span>
          <span className="pill pill-slate mr-2">{r.priority}</span>
          <span className="text-slate-700">{r.subject}</span>
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No RFIs raised today.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 grid grid-cols-2 gap-2">
          <input data-testid="rfi-new-subject" placeholder="Subject" value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-slate-200 text-sm" />
          <textarea data-testid="rfi-new-desc" placeholder="Question / context" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="col-span-2 px-2 py-1 rounded border border-slate-200 text-sm" rows={2} />
          <select data-testid="rfi-new-priority" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            {["low", "normal", "high", "urgent"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <button data-testid="rfi-add" onClick={create} className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 flex items-center justify-center gap-1"><Plus className="h-3.5 w-3.5" /> Open RFI</button>
        </div>
      )}
    </PanelShell>
  );
}

function DeliveriesPanel({ rows, dailyReportId, projectId, reportDate, locked, reload }) {
  const [draft, setDraft] = useState({ vendor: "", description: "", po_number: "" });
  const create = async () => {
    if (!draft.vendor || !draft.description) return toast.error("Vendor + description required");
    try {
      await api.post(`/deliveries`, { ...draft, project_id: projectId, delivery_date: reportDate });
      setDraft({ vendor: "", description: "", po_number: "" });
      reload();
    } catch (e) { toast.error(formatErr(e, "Log delivery")); }
  };
  return (
    <PanelShell icon={Truck} title="Deliveries today" testid="dr-deliveries-panel" count={rows.length}>
      {rows.map((r) => (
        <div key={r.id} data-testid={`delivery-row-${r.id}`} className="border-b border-slate-100 py-1.5 text-xs">
          <span className="font-medium mr-2">{r.vendor}</span>
          <span className="text-slate-700">{r.description}</span>
          {r.po_number && <span className="text-slate-400 ml-2 font-mono">{r.po_number}</span>}
          <span className={`pill ml-2 ${r.status === "received" ? "pill-green" : "pill-slate"}`}>{r.status}</span>
        </div>
      ))}
      {!rows.length && <div className="text-xs text-slate-400 py-2">No deliveries scheduled for {reportDate}.</div>}
      {!locked && (
        <div className="mt-3 pt-3 border-t border-slate-100 grid grid-cols-3 gap-2">
          <input data-testid="delivery-new-vendor" placeholder="Vendor" value={draft.vendor} onChange={(e) => setDraft({ ...draft, vendor: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
          <input data-testid="delivery-new-desc" placeholder="What was delivered" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-slate-200 text-sm" />
          <input data-testid="delivery-new-po" placeholder="PO #" value={draft.po_number} onChange={(e) => setDraft({ ...draft, po_number: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
          <button data-testid="delivery-add" onClick={create} className="col-span-2 h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 flex items-center justify-center gap-1"><Plus className="h-3.5 w-3.5" /> Log delivery</button>
        </div>
      )}
    </PanelShell>
  );
}

function NotesPanel({ report, dailyReportId, locked, reload }) {
  const [text, setText] = useState(report.notes || "");
  useEffect(() => { setText(report.notes || ""); }, [report.notes]);
  const save = async () => {
    try { await api.patch(`/daily-reports/${dailyReportId}`, { notes: text }); reload(); toast.success("Notes saved"); }
    catch (e) { toast.error(e.response?.data?.detail || "Save failed"); }
  };
  return (
    <PanelShell icon={ClipboardList} title="Daily notes / narrative" testid="dr-notes-panel">
      <textarea data-testid="dr-notes-textarea" disabled={locked} value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Free-text notes that go on the PDF (visitors, accidents-near-misses summary, manpower changes, etc.)"
        className="w-full h-28 px-3 py-2 rounded border border-slate-200 text-sm" />
      {!locked && (
        <div className="flex justify-end mt-2">
          <button data-testid="dr-notes-save" onClick={save} className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700">Save notes</button>
        </div>
      )}
    </PanelShell>
  );
}

function PhotosPanel({ rows, dailyReportId, projectId, locked, reload }) {
  const [busy, setBusy] = useState(false);
  const [caption, setCaption] = useState("");
  const onUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("project_id", projectId);
      fd.append("daily_report_id", dailyReportId);
      if (caption) fd.append("caption", caption);
      await api.post(`/photos/upload`, fd, { headers: { "Content-Type": "multipart/form-data" } });
      setCaption("");
      reload();
      toast.success("Photo uploaded");
    } catch (err) { toast.error(err.response?.data?.detail || "Upload failed"); }
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
          <input data-testid="photo-caption" placeholder="Caption (optional)" value={caption} onChange={(e) => setCaption(e.target.value)} className="w-full h-8 px-2 rounded border border-slate-200 text-sm" />
          <label data-testid="photo-upload-label" className="block">
            <span className="h-8 px-3 rounded bg-blue-600 text-white text-sm inline-flex items-center gap-1 hover:bg-blue-700 cursor-pointer">
              <ImageIcon className="h-3.5 w-3.5" /> {busy ? "Uploading…" : "Upload photo"}
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
  return (
    <div data-testid={`photo-${photo.id}`} className="relative block aspect-square bg-slate-100 rounded overflow-hidden border border-slate-200">
      {src ? <img src={src} alt={photo.caption || ""} className="w-full h-full object-cover" /> :
        <div className="w-full h-full grid place-items-center text-xs text-slate-400">…</div>}
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
