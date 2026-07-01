// Bucket E Phase 3 (2026-05-11) -- Submittals page rebuild with tabs.
//
// Three tabs (no new sidebar entries per spec):
//   - Register: extended table with SLA pill, aging, urgency, must-
//     approve-by, reviewer; per-row actions (submit-to-ae, record
//     event, pause/resume, assign reviewer, view timeline).
//   - Aging: 5-bucket dashboard, per-discipline rollup, top 10
//     longest-open, must_approve_by miss count, breach trend chart.
//   - Review Events: chronological chain-of-custody timeline.
//
// Mobile: register collapses to per-row cards via CSS; aging buckets
// stack vertically. Form fields wrap with JigsawField on edit-mode
// (entity_id known) so ai_submittal_classifier suggestions surface
// inline.
import { useEffect, useMemo, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import {
  Plus, Wand2, Link2, Sparkles, FileSignature, ClipboardList,
  History, AlertTriangle, Clock, Pause, Play, UserCheck,
  ChevronRight, RefreshCw, Loader2, Send, ListChecks, X,
} from "lucide-react";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import { getUser } from "../lib/auth";
import JigsawField from "../components/forms/JigsawField";
import FieldLabel from "../components/forms/FieldLabel";
// CPM Part G (2026-05-15) -- submittal SLA dates (submitted_to_ae_at,
// a_e_review_due_at, must_approve_by) rendered MM/DD/YYYY via the
// chokepoint helper. The fields are datetimes in storage but the
// display cells already truncated to the date portion via
// .toLocaleDateString(); formatDateDisplay does the same truncation
// (slice(0,10)) plus the format conversion in one place.
import { formatDateDisplay } from "../lib/dateUtil";
// CPM Part K-6 (2026-05-15) -- hybrid-mode per-task impact input.
// Reuses the J-6 component with mode="submittal" for source pills +
// reset-to-suggestion + within-SLA banner.
import PerTaskImpactsInput from "../components/forms/PerTaskImpactsInput";

const STATUS_OPTIONS = [
  "not_yet_submitted", "submitted", "in_review", "approved",
  "approved_as_noted", "rejected", "revise_resubmit", "closed",
];

const TYPE_OPTIONS = [
  "product_data", "shop_drawing", "sample", "mock_up",
  "certifications", "test_reports", "o_m_manual",
  "as_built", "closeout", "other",
];

const REVIEW_EVENT_TYPES = [
  "submitted_to_ae", "ae_review_started", "ae_review_completed",
  "forwarded_to_sub", "returned_from_sub", "approved",
  "approved_as_noted", "rejected", "revise_resubmit", "closed",
];

// SLA pill palette -- reuses the Jigsaw confidence vocabulary so
// users read the visual language consistently across the platform.
const SLA_PILL_TONE = {
  on_track:    "bg-emerald-100 text-emerald-900 border-emerald-300",
  approaching: "bg-amber-100 text-amber-900 border-amber-300",
  breached:    "bg-rose-100 text-rose-900 border-rose-300",
  paused:      "bg-slate-200 text-slate-700 border-slate-300",
  n_a:         "bg-slate-100 text-slate-500 border-slate-200",
};

const SLA_PILL_LABEL = {
  on_track:    "On track",
  approaching: "Approaching",
  breached:    "Breached",
  paused:      "Paused",
  n_a:         "—",
};

export default function Submittals() {
  const [tab, setTab] = useState("register");
  return (
    <div data-testid="submittals-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <FileSignature className="h-5 w-5" />
        <h1 className="text-2xl font-semibold tracking-tight">Submittals</h1>
      </div>

      {/* max-md:grid grid-cols-3 -- three tabs won't fit one 318px row with
          their icons, so on phones we split the row into equal thirds and hide
          the icons (label-only) instead of horizontally scrolling + clipping
          "Review Events". Desktop keeps the icon+label flex row (max-md gated). */}
      <div className="flex border-b border-slate-200 text-sm overflow-x-auto max-md:grid max-md:grid-cols-3 max-md:overflow-visible"
        data-testid="submittals-tabs">
        <Tab tab="register" active={tab} onClick={setTab} label="Register"
          Icon={ListChecks} testid="submittals-tab-register" />
        <Tab tab="aging" active={tab} onClick={setTab} label="Aging"
          Icon={Clock} testid="submittals-tab-aging" />
        <Tab tab="events" active={tab} onClick={setTab} label="Review Events"
          Icon={History} testid="submittals-tab-events" />
      </div>

      {tab === "register" && <RegisterTab />}
      {tab === "aging"    && <AgingTab />}
      {tab === "events"   && <ReviewEventsTab />}
    </div>
  );
}


function Tab({ tab, active, onClick, label, Icon, testid }) {
  const isActive = tab === active;
  return (
    <button data-testid={testid} onClick={() => onClick(tab)}
      className={`px-3 py-2 -mb-px border-b-2 inline-flex items-center gap-1.5 whitespace-nowrap max-md:justify-center max-md:px-1 max-md:text-xs ${
        isActive
          ? "border-blue-600 text-blue-900 font-medium"
          : "border-transparent text-slate-600 hover:text-slate-900"
      }`}>
      <Icon className="h-3.5 w-3.5 max-md:hidden" /> {label}
    </button>
  );
}


// =====================================================================
// Register tab -- extended table with SLA pills + per-row actions.
// =====================================================================
function RegisterTab() {
  const { activeId, projects } = useProjects();
  const project = projects.find((p) => p.id === activeId);
  const [list, setList] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);   // submittal row or null
  const [form, setForm] = useState({
    title: "", spec_section_code: "", type: "product_data",
    sub_responsible: "", discipline: "",
    procurement_lead_time_days: "", sla_days_override: "",
  });
  const [sortBy, setSortBy] = useState("submittal_number");
  const [filterDiscipline, setFilterDiscipline] = useState("");
  const [filterSlaStatus, setFilterSlaStatus] = useState("");
  const userRole = (getUser()?.role || "").toLowerCase();
  const isAdminOrPm = ["admin", "pm"].includes(userRole);

  const load = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      const [s, t] = await Promise.all([
        api.get(`/projects/${activeId}/submittals`),
        api.get(`/projects/${activeId}/tasks`),
      ]);
      setList(s.data || []);
      setTasks((t.data || []).filter((x) => x.status !== "deleted"));
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    } finally {
      setLoading(false);
    }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [activeId]);

  const taskById = useMemo(
    () => Object.fromEntries(tasks.map((t) => [t.id, t])),
    [tasks],
  );
  const disciplines = useMemo(() => {
    const s = new Set(list.map((r) => r.discipline).filter(Boolean));
    return Array.from(s).sort();
  }, [list]);

  const filtered = useMemo(() => {
    let r = [...list];
    if (filterDiscipline) r = r.filter((x) => x.discipline === filterDiscipline);
    if (filterSlaStatus)  r = r.filter((x) => (x.sla_status || "n_a") === filterSlaStatus);
    // Sort.
    r.sort((a, b) => {
      const av = a[sortBy]; const bv = b[sortBy];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") return av - bv;
      return String(av).localeCompare(String(bv));
    });
    return r;
  }, [list, filterDiscipline, filterSlaStatus, sortBy]);

  const create = async () => {
    try {
      const payload = {
        project_id: activeId,
        title: form.title,
        spec_section_code: form.spec_section_code || undefined,
        type: form.type,
        sub_responsible: form.sub_responsible || undefined,
        discipline: form.discipline || undefined,
      };
      if (form.procurement_lead_time_days !== "") {
        payload.procurement_lead_time_days = Number(form.procurement_lead_time_days);
      }
      if (form.sla_days_override !== "") {
        payload.sla_days_override = Number(form.sla_days_override);
      }
      await api.post("/submittals", payload);
      toast.success("Submittal created");
      setOpen(false);
      setForm({
        title: "", spec_section_code: "", type: "product_data",
        sub_responsible: "", discipline: "",
        procurement_lead_time_days: "", sla_days_override: "",
      });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Create failed");
    }
  };

  const submitToAe = async (s) => {
    try {
      await api.post(`/submittals/${s.id}/submit-to-ae`);
      toast.success(`${s.submittal_number} sent to A/E`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Submit failed");
    }
  };

  const pauseResume = async (s) => {
    if (s.sla_status === "paused") {
      try {
        await api.post(`/submittals/${s.id}/resume-sla`);
        toast.success(`${s.submittal_number} SLA resumed`);
        load();
      } catch (e) {
        toast.error(e.response?.data?.detail || "Resume failed");
      }
    } else {
      const reason = window.prompt(
        `Pause SLA on ${s.submittal_number}? Enter reason (5+ chars):`,
      );
      if (!reason || reason.trim().length < 5) {
        if (reason !== null) toast.error("Reason must be at least 5 characters");
        return;
      }
      try {
        await api.post(`/submittals/${s.id}/pause-sla`, { reason: reason.trim() });
        toast.success(`${s.submittal_number} SLA paused`);
        load();
      } catch (e) {
        toast.error(e.response?.data?.detail || "Pause failed");
      }
    }
  };

  return (
    <div className="space-y-3" data-testid="submittals-register">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-sm text-slate-500">
          {project?.name} · {list.length} total
          {list.length > 0 && (
            <> · {list.filter((s) => s.sla_status === "breached").length} breached
              · {list.filter((s) => s.urgency_flag).length} urgent</>
          )}
        </p>
        <div className="flex items-center gap-2 flex-wrap">
          <select data-testid="register-filter-discipline"
            value={filterDiscipline}
            onChange={(e) => setFilterDiscipline(e.target.value)}
            className="h-8 max-md:min-h-[44px] px-2 rounded border border-slate-200 text-xs bg-white">
            <option value="">All disciplines</option>
            {disciplines.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <select data-testid="register-filter-sla"
            value={filterSlaStatus}
            onChange={(e) => setFilterSlaStatus(e.target.value)}
            className="h-8 max-md:min-h-[44px] px-2 rounded border border-slate-200 text-xs bg-white">
            <option value="">All SLA statuses</option>
            {Object.keys(SLA_PILL_LABEL).map((k) =>
              <option key={k} value={k}>{SLA_PILL_LABEL[k]}</option>)}
          </select>
          <select data-testid="register-sort"
            value={sortBy} onChange={(e) => setSortBy(e.target.value)}
            className="h-8 max-md:min-h-[44px] px-2 rounded border border-slate-200 text-xs bg-white">
            <option value="submittal_number">Sort: # asc</option>
            <option value="aging_days">Sort: aging desc</option>
            <option value="a_e_review_due_at">Sort: due date</option>
            <option value="title">Sort: title</option>
          </select>
          <button onClick={load} disabled={loading}
            data-testid="register-refresh"
            className="h-8 max-md:min-h-[44px] max-md:min-w-[44px] px-2 rounded border border-slate-200 bg-white text-xs inline-flex items-center gap-1 hover:bg-slate-50">
            <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
          </button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              {/* pt.40: primary sbtn, no Plus icon, meaningful label. */}
              <button data-testid="new-submittal" className="sbtn sbtn-sm sbtn-primary">
                New submittal
              </button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader><DialogTitle>New submittal</DialogTitle></DialogHeader>
              <div className="space-y-3">
                <div>
                  <FieldLabel required className="field-label">Title</FieldLabel>
                  <input data-testid="sub-title" value={form.title}
                    onChange={(e) => setForm({ ...form, title: e.target.value })}
                    className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <FieldLabel optional className="field-label">Spec section code</FieldLabel>
                    <input value={form.spec_section_code}
                      onChange={(e) => setForm({ ...form, spec_section_code: e.target.value })}
                      placeholder="03 30 00"
                      className="w-full h-9 px-3 rounded border border-slate-200 font-mono text-sm" />
                  </div>
                  <div>
                    <FieldLabel optional className="field-label">Discipline</FieldLabel>
                    <input data-testid="sub-discipline" value={form.discipline}
                      onChange={(e) => setForm({ ...form, discipline: e.target.value })}
                      placeholder="Plumbing / Mechanical / Electrical / …"
                      className="w-full h-9 px-3 rounded border border-slate-200" />
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="field-label">Type</label>
                    <select data-testid="sub-type" value={form.type}
                      onChange={(e) => setForm({ ...form, type: e.target.value })}
                      className="w-full h-9 px-2 rounded border border-slate-200 text-sm bg-white">
                      {TYPE_OPTIONS.map((t) =>
                        <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                    </select>
                  </div>
                  <div>
                    <FieldLabel optional className="field-label">Sub responsible</FieldLabel>
                    <input value={form.sub_responsible}
                      onChange={(e) => setForm({ ...form, sub_responsible: e.target.value })}
                      className="w-full h-9 px-3 rounded border border-slate-200" />
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <FieldLabel optional className="field-label">Procurement lead time (days)</FieldLabel>
                    <input data-testid="sub-lead-time" type="number" min="0"
                      value={form.procurement_lead_time_days}
                      onChange={(e) => setForm({ ...form, procurement_lead_time_days: e.target.value })}
                      placeholder="21"
                      className="w-full h-9 px-3 rounded border border-slate-200" />
                  </div>
                  {isAdminOrPm && (
                    <div>
                      <FieldLabel optional className="field-label" title="Admin-only override">
                        SLA override (days)
                      </FieldLabel>
                      <input data-testid="sub-sla-override" type="number" min="1" max="365"
                        value={form.sla_days_override}
                        onChange={(e) => setForm({ ...form, sla_days_override: e.target.value })}
                        placeholder="default 14"
                        className="w-full h-9 px-3 rounded border border-slate-200" />
                    </div>
                  )}
                </div>
                <Button data-testid="sub-create" onClick={create} disabled={!form.title}
                  className="bg-blue-600 hover:bg-blue-700">Create</Button>
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {loading && filtered.length === 0 ? (
        <div className="space-y-2" data-testid="register-loading">
          {[1, 2, 3].map((i) =>
            <div key={i} className="h-10 bg-slate-100 rounded animate-pulse" />)}
        </div>
      ) : !filtered.length ? (
        <div data-testid="register-empty"
          className="p-6 text-center border border-dashed border-slate-300 rounded text-sm text-slate-500">
          <FileSignature className="h-5 w-5 mx-auto mb-1 text-slate-400" />
          <div className="font-medium">No submittals match these filters.</div>
          <div className="text-xs mt-1">
            Use "New submittal" above or upload a spec PDF — submittals auto-generate
            from "1.4 SUBMITTALS" paragraphs.
          </div>
        </div>
      ) : (
        <>
          {/* Desktop register table */}
          <div className="surface overflow-x-auto hidden md:block"
            data-testid="register-table">
            <table className="dense-table">
              <thead><tr>
                <th>Number</th><th>Title</th><th>Discipline</th><th>Type</th>
                <th>Rev</th><th>Submitted</th><th>Due</th><th>Aging</th>
                <th>SLA</th><th>Must approve by</th>
                <th>Reviewer</th><th>Status</th><th>Actions</th>
              </tr></thead>
              <tbody>
                {filtered.map((s) =>
                  <RegisterRow key={s.id} s={s}
                    taskById={taskById} userRole={userRole}
                    isAdminOrPm={isAdminOrPm} onChanged={load}
                    onSubmitToAe={submitToAe} onPauseResume={pauseResume}
                    onOpenEdit={() => setEditing(s)} />
                )}
              </tbody>
            </table>
          </div>
          {/* Mobile cards */}
          <div className="md:hidden space-y-2"
            data-testid="register-mobile-cards">
            {filtered.map((s) =>
              <RegisterCard key={s.id} s={s} userRole={userRole}
                isAdminOrPm={isAdminOrPm} onChanged={load}
                onSubmitToAe={submitToAe} onPauseResume={pauseResume}
                onOpenEdit={() => setEditing(s)} />
            )}
          </div>
        </>
      )}

      {editing && (
        <EditSubmittalDialog submittal={editing}
          onClose={() => setEditing(null)} onSaved={load}
          userRole={userRole} isAdminOrPm={isAdminOrPm} />
      )}
    </div>
  );
}


function SlaPill({ status }) {
  const tone = SLA_PILL_TONE[status] || SLA_PILL_TONE.n_a;
  const label = SLA_PILL_LABEL[status] || "—";
  return (
    <span data-testid={`sla-pill-${status || "n_a"}`}
      className={`inline-flex items-center px-1.5 py-0.5 rounded border text-xs ${tone}`}>
      {label}
    </span>
  );
}


function RegisterRow({ s, taskById, isAdminOrPm, onSubmitToAe, onPauseResume, onOpenEdit }) {
  return (
    <tr data-testid={`sub-row-${s.submittal_number}`}>
      <td className="font-mono text-xs">
        {s.gating_entity_id
          ? <RouterLink to={`/gating-entities/${s.gating_entity_id}`}
              className="text-blue-700 hover:underline">{s.submittal_number}</RouterLink>
          : s.submittal_number}
      </td>
      <td className="truncate max-w-[260px]">
        {s.title}
        {s.auto_generated && (
          <span className="ml-2 inline-flex items-center gap-1 pill pill-blue text-xs">
            <Wand2 className="h-2.5 w-2.5" /> auto
          </span>
        )}
      </td>
      <td className="text-xs">{s.discipline || "—"}</td>
      <td className="text-xs">{(s.type || "").replace(/_/g, " ")}</td>
      <td className="text-xs tabular-nums">{s.review_cycle_count || 0}</td>
      <td className="text-xs font-mono">
        {formatDateDisplay(s.submitted_to_ae_at)}
      </td>
      <td className="text-xs font-mono">
        {formatDateDisplay(s.a_e_review_due_at)}
      </td>
      <td className="text-xs tabular-nums">
        {s.aging_days != null
          ? <>{s.aging_days}d <span className="text-slate-400">({s.aging_working_days || 0}wd)</span></>
          : "—"}
      </td>
      <td><SlaPill status={s.sla_status} /></td>
      <td className="text-xs font-mono">
        {s.must_approve_by
          ? <span className={s.urgency_flag ? "text-rose-700 font-medium" : ""}>
              {formatDateDisplay(s.must_approve_by)}
              {s.urgency_flag && (
                <AlertTriangle className="inline h-3 w-3 ml-1 text-rose-600" />
              )}
            </span>
          : "—"}
      </td>
      <td className="text-xs">
        {s.assigned_reviewer_id
          ? <code className="text-xs">{String(s.assigned_reviewer_id).slice(0, 8)}</code>
          : "—"}
      </td>
      <td className="text-xs">{(s.status || "").replace(/_/g, " ")}</td>
      <td>
        <div className="flex items-center gap-1">
          {s.status === "not_yet_submitted" && (
            <button data-testid={`sub-submit-${s.submittal_number}`}
              onClick={() => onSubmitToAe(s)} title="Submit to A/E"
              className="h-6 px-1 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
              <Send className="h-3 w-3" />
            </button>
          )}
          {isAdminOrPm && s.submitted_to_ae_at && (
            <button data-testid={`sub-pause-${s.submittal_number}`}
              onClick={() => onPauseResume(s)}
              title={s.sla_status === "paused" ? "Resume SLA" : "Pause SLA"}
              className="h-6 px-1 rounded border border-slate-200 bg-white text-slate-700 text-xs hover:bg-slate-50">
              {s.sla_status === "paused"
                ? <Play className="h-3 w-3" />
                : <Pause className="h-3 w-3" />}
            </button>
          )}
          <button data-testid={`sub-edit-${s.submittal_number}`}
            onClick={onOpenEdit} title="Edit + assign reviewer + record event"
            className="h-6 px-1 rounded border border-slate-200 bg-white text-slate-700 text-xs hover:bg-slate-50">
            <ChevronRight className="h-3 w-3" />
          </button>
        </div>
      </td>
    </tr>
  );
}


function RegisterCard({ s, isAdminOrPm, onSubmitToAe, onPauseResume, onOpenEdit }) {
  return (
    <div data-testid={`sub-card-${s.submittal_number}`}
      className="bg-white border border-slate-200 rounded p-2 text-xs space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono font-semibold">{s.submittal_number}</span>
        <SlaPill status={s.sla_status} />
      </div>
      <div className="font-medium">{s.title}</div>
      <div className="text-slate-500 text-xs">
        {s.discipline || "—"} · {(s.type || "").replace(/_/g, " ")} · rev {s.review_cycle_count || 0}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-1 text-xs text-slate-600">
        <div>Submitted: {formatDateDisplay(s.submitted_to_ae_at)}</div>
        <div>Due: {formatDateDisplay(s.a_e_review_due_at)}</div>
        <div>Aging: {s.aging_days != null ? `${s.aging_days}d` : "—"}</div>
        <div className={s.urgency_flag ? "text-rose-700" : ""}>
          Must approve: {formatDateDisplay(s.must_approve_by)}
        </div>
      </div>
      <div className="flex items-center gap-1 pt-1 border-t border-slate-100">
        {s.status === "not_yet_submitted" && (
          <button onClick={() => onSubmitToAe(s)}
            data-testid={`sub-card-submit-${s.submittal_number}`}
            className="h-7 max-md:min-h-[44px] px-2 rounded bg-blue-600 text-white text-xs inline-flex items-center gap-1 hover:bg-blue-700">
            <Send className="h-3 w-3" /> Submit
          </button>
        )}
        {isAdminOrPm && s.submitted_to_ae_at && (
          <button onClick={() => onPauseResume(s)}
            className="h-7 max-md:min-h-[44px] px-2 rounded border border-slate-200 bg-white text-slate-700 text-xs inline-flex items-center gap-1">
            {s.sla_status === "paused"
              ? <><Play className="h-3 w-3" /> Resume</>
              : <><Pause className="h-3 w-3" /> Pause</>}
          </button>
        )}
        <button onClick={onOpenEdit}
          className="h-7 max-md:min-h-[44px] px-2 rounded border border-slate-200 bg-white text-slate-700 text-xs inline-flex items-center gap-1 ml-auto">
          <ChevronRight className="h-3 w-3" /> Edit
        </button>
      </div>
    </div>
  );
}


function EditSubmittalDialog({ submittal, onClose, onSaved, userRole, isAdminOrPm }) {
  const [form, setForm] = useState({
    title: submittal.title || "",
    type: submittal.type || "product_data",
    discipline: submittal.discipline || "",
    procurement_lead_time_days: submittal.procurement_lead_time_days ?? "",
    sla_days_override: submittal.sla_days_override ?? "",
    sla_uses_calendar_days_override:
      submittal.sla_uses_calendar_days_override ?? null,
    sub_responsible: submittal.sub_responsible || "",
  });
  // CPM Part K-6 (2026-05-15) -- per_task_impacts state. Kept
  // separate from `form` because PerTaskImpactsInput owns array
  // semantics (deep equality compare on save). Initial value comes
  // from the row directly; auto-suggested entries arrive via K-2
  // server-side stamp when SLA breaches.
  const [perTaskImpacts, setPerTaskImpacts] = useState(
    Array.isArray(submittal.per_task_impacts) ? submittal.per_task_impacts : []
  );
  const [reviewerId, setReviewerId] = useState(submittal.assigned_reviewer_id || "");
  const [eventType, setEventType] = useState("");
  const [eventNotes, setEventNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState([]);
  const [tab, setTab] = useState("edit");

  // CPM Part K-6 -- linked task ids for the hybrid input. Submittals
  // link via singular schedule_activity_id; the component takes an
  // array so we wrap. Empty array if no link.
  const linkedTaskIds = submittal.schedule_activity_id
    ? [submittal.schedule_activity_id] : [];

  const loadEvents = async () => {
    try {
      const { data } = await api.get(`/submittals/${submittal.id}/review-events`);
      setEvents(data?.events || []);
    } catch { /* non-fatal */ }
  };
  useEffect(() => { if (tab === "timeline") loadEvents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const save = async () => {
    setBusy(true);
    try {
      const payload = { ...form };
      if (payload.procurement_lead_time_days === "")
        delete payload.procurement_lead_time_days;
      else payload.procurement_lead_time_days = Number(payload.procurement_lead_time_days);
      if (payload.sla_days_override === "")
        delete payload.sla_days_override;
      else payload.sla_days_override = Number(payload.sla_days_override);
      if (payload.sla_uses_calendar_days_override === null)
        delete payload.sla_uses_calendar_days_override;

      // CPM Part K-6 -- include per_task_impacts only when it changed
      // from the initial row state. JSON.stringify deep-compare mirrors
      // the LockedEventEditModal pattern (J-6). When unchanged we
      // omit it so the backend treats this as a non-impacts PATCH
      // (skips the K-3 verdict-stamp pass + auto_recompute call).
      const initialImpacts = Array.isArray(submittal.per_task_impacts)
        ? submittal.per_task_impacts : [];
      const impactsChanged = JSON.stringify(initialImpacts) !== JSON.stringify(perTaskImpacts);
      if (impactsChanged) {
        // Send the array as-is; backend validator (K-1
        // _normalize_submittal_per_task_impacts) enforces shape +
        // source enum. K-3 re-stamps verdict keys post-recompute.
        payload.per_task_impacts = perTaskImpacts;
      }

      await api.patch(`/submittals/${submittal.id}`, payload);
      toast.success("Saved");
      onSaved?.();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    } finally { setBusy(false); }
  };

  const assign = async () => {
    if (!reviewerId.trim()) return;
    setBusy(true);
    try {
      await api.post(`/submittals/${submittal.id}/assign-reviewer`, {
        reviewer_user_id: reviewerId.trim(),
      });
      toast.success("Reviewer assigned");
      onSaved?.();
    } catch (e) {
      toast.error(e.response?.data?.detail?.reason
        || e.response?.data?.detail || "Assign failed");
    } finally { setBusy(false); }
  };

  const recordEvent = async () => {
    if (!eventType) {
      toast.error("Pick an event type");
      return;
    }
    setBusy(true);
    try {
      await api.post(`/submittals/${submittal.id}/record-review-event`, {
        submittal_id: submittal.id,
        event_type: eventType,
        notes: eventNotes || null,
      });
      toast.success("Event recorded");
      setEventType(""); setEventNotes("");
      onSaved?.();
      if (tab === "timeline") loadEvents();
    } catch (e) {
      toast.error(e.response?.data?.detail?.reason
        || e.response?.data?.detail || "Record failed");
    } finally { setBusy(false); }
  };

  return (
    <Dialog open={true} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {submittal.submittal_number} — {submittal.title}
          </DialogTitle>
        </DialogHeader>
        <div className="flex border-b border-slate-200 text-sm overflow-x-auto">
          <button onClick={() => setTab("edit")}
            data-testid="edit-tab-fields"
            className={`px-3 py-2 -mb-px border-b-2 whitespace-nowrap ${tab === "edit"
              ? "border-blue-600 text-blue-900 font-medium"
              : "border-transparent text-slate-600"}`}>
            Fields
          </button>
          <button onClick={() => setTab("event")}
            data-testid="edit-tab-event"
            className={`px-3 py-2 -mb-px border-b-2 whitespace-nowrap ${tab === "event"
              ? "border-blue-600 text-blue-900 font-medium"
              : "border-transparent text-slate-600"}`}>
            Record event
          </button>
          {isAdminOrPm && (
            <button onClick={() => setTab("reviewer")}
              data-testid="edit-tab-reviewer"
              className={`px-3 py-2 -mb-px border-b-2 whitespace-nowrap ${tab === "reviewer"
                ? "border-blue-600 text-blue-900 font-medium"
                : "border-transparent text-slate-600"}`}>
              Assign reviewer
            </button>
          )}
          <button onClick={() => setTab("timeline")}
            data-testid="edit-tab-timeline"
            className={`px-3 py-2 -mb-px border-b-2 whitespace-nowrap ${tab === "timeline"
              ? "border-blue-600 text-blue-900 font-medium"
              : "border-transparent text-slate-600"}`}>
            Timeline
          </button>
        </div>

        {tab === "edit" && (
          <div className="space-y-3 text-sm">
            <div>
              <label className="field-label">Title</label>
              <JigsawField entityType="submittal" entityId={submittal.id}
                fieldPath="title" userRole={userRole} fieldLabel="title"
                onConfirm={(v) => setForm({ ...form, title: String(v || form.title) })}>
                <input value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                  className="w-full h-9 px-3 rounded border border-slate-200" />
              </JigsawField>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="field-label">Type</label>
                <JigsawField entityType="submittal" entityId={submittal.id}
                  fieldPath="type" userRole={userRole} fieldLabel="type"
                  onConfirm={(v) => setForm({ ...form, type: String(v || form.type) })}>
                  <select value={form.type}
                    onChange={(e) => setForm({ ...form, type: e.target.value })}
                    className="w-full h-9 px-2 rounded border border-slate-200 text-sm bg-white">
                    {TYPE_OPTIONS.map((t) =>
                      <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                  </select>
                </JigsawField>
              </div>
              <div>
                <FieldLabel optional className="field-label">Discipline</FieldLabel>
                <JigsawField entityType="submittal" entityId={submittal.id}
                  fieldPath="discipline" userRole={userRole} fieldLabel="discipline"
                  onConfirm={(v) => setForm({ ...form, discipline: String(v || "") })}>
                  <input value={form.discipline}
                    onChange={(e) => setForm({ ...form, discipline: e.target.value })}
                    className="w-full h-9 px-3 rounded border border-slate-200" />
                </JigsawField>
              </div>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <FieldLabel optional className="field-label">Procurement lead time (days)</FieldLabel>
                <input type="number" min="0" value={form.procurement_lead_time_days}
                  onChange={(e) => setForm({ ...form, procurement_lead_time_days: e.target.value })}
                  className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
              <div>
                <FieldLabel optional className="field-label">Sub responsible</FieldLabel>
                <input value={form.sub_responsible}
                  onChange={(e) => setForm({ ...form, sub_responsible: e.target.value })}
                  className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
            </div>
            {isAdminOrPm && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 p-2 bg-amber-50 border border-amber-200 rounded">
                <div>
                  <FieldLabel optional className="field-label text-amber-900">SLA override (days)</FieldLabel>
                  <input type="number" min="1" max="365"
                    value={form.sla_days_override}
                    onChange={(e) => setForm({ ...form, sla_days_override: e.target.value })}
                    placeholder="14"
                    className="w-full h-9 px-3 rounded border border-amber-300 bg-white" />
                </div>
                <div>
                  <label className="field-label text-amber-900">Use calendar days?</label>
                  <select value={form.sla_uses_calendar_days_override === null ? ""
                    : String(form.sla_uses_calendar_days_override)}
                    onChange={(e) => setForm({ ...form,
                      sla_uses_calendar_days_override:
                        e.target.value === "" ? null : e.target.value === "true"
                    })}
                    className="w-full h-9 px-2 rounded border border-amber-300 bg-white text-sm">
                    <option value="">Inherit project setting</option>
                    <option value="false">No (working days)</option>
                    <option value="true">Yes (calendar days)</option>
                  </select>
                </div>
              </div>
            )}

            {/* CPM Part K-6 (2026-05-15) -- hybrid-mode per-task impacts.
                Only renders when this submittal has a linked task (the
                schedule_activity_id field). For unlinked submittals
                there's nothing to capture an impact against; the panel
                hides entirely. When the submittal is within SLA the
                component shows a banner explaining the math isn't
                active yet, but inputs remain (super may pre-fill). */}
            {linkedTaskIds.length > 0 && (
              <div>
                <label className="field-label">Schedule impact (per task)</label>
                <PerTaskImpactsInput
                  mode="submittal"
                  slaStatus={submittal.sla_status}
                  linkedTaskIds={linkedTaskIds}
                  value={perTaskImpacts}
                  onChange={setPerTaskImpacts}
                  projectId={submittal.project_id}
                  testidPrefix="sub-impacts"
                />
              </div>
            )}

            <div className="flex justify-end">
              <Button onClick={save} disabled={busy}
                data-testid="edit-save"
                className="bg-blue-600 hover:bg-blue-700 max-md:min-h-[44px]">
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Save"}
              </Button>
            </div>
          </div>
        )}

        {tab === "event" && (
          <div className="space-y-3 text-sm">
            <div>
              <FieldLabel required className="field-label">Event type</FieldLabel>
              <select value={eventType}
                onChange={(e) => setEventType(e.target.value)}
                data-testid="event-type"
                className="w-full h-9 px-2 rounded border border-slate-200 bg-white text-sm">
                <option value="">— select —</option>
                {REVIEW_EVENT_TYPES.map((t) =>
                  <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
              </select>
            </div>
            <div>
              <FieldLabel optional className="field-label">Notes</FieldLabel>
              <textarea value={eventNotes}
                onChange={(e) => setEventNotes(e.target.value)}
                rows={3} className="w-full p-2 rounded border border-slate-200 text-sm" />
            </div>
            <div className="flex justify-end">
              <Button onClick={recordEvent} disabled={busy || !eventType}
                data-testid="event-record"
                className="bg-blue-600 hover:bg-blue-700 max-md:min-h-[44px]">
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Record event"}
              </Button>
            </div>
          </div>
        )}

        {tab === "reviewer" && isAdminOrPm && (
          <div className="space-y-3 text-sm">
            <div>
              <FieldLabel required className="field-label">Reviewer user_id</FieldLabel>
              <input value={reviewerId}
                onChange={(e) => setReviewerId(e.target.value)}
                placeholder="user_id (admin / submittal_tech / pm)"
                data-testid="reviewer-id"
                className="w-full h-9 px-3 rounded border border-slate-200" />
            </div>
            <div className="flex justify-end">
              <Button onClick={assign} disabled={busy || !reviewerId.trim()}
                data-testid="reviewer-assign"
                className="bg-blue-600 hover:bg-blue-700 max-md:min-h-[44px]">
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Assign"}
              </Button>
            </div>
          </div>
        )}

        {tab === "timeline" && (
          <div className="space-y-2 text-xs"
            data-testid="edit-timeline">
            {events.length === 0 ? (
              <div className="text-slate-500 italic">No events yet.</div>
            ) : events.map((ev) => (
              <div key={ev.id}
                className="border border-slate-200 rounded p-2 bg-white">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{ev.event_type.replace(/_/g, " ")}</span>
                  <span className="text-slate-400 font-mono">
                    {new Date(ev.performed_at).toLocaleString()}
                  </span>
                </div>
                <div className="text-slate-600 mt-0.5">
                  {ev.status_before} → {ev.status_after}
                  {ev.calendar_days_elapsed != null && (
                    <span className="ml-2 text-slate-500">
                      · Δ {ev.calendar_days_elapsed}d cal /
                      {" "}{ev.working_days_elapsed ?? 0}wd
                    </span>
                  )}
                </div>
                {ev.notes && <div className="text-slate-500 italic mt-0.5">"{ev.notes}"</div>}
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}


// =====================================================================
// Aging tab.
// =====================================================================
function AgingTab() {
  const { activeId } = useProjects();
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [days, setDays] = useState(90);

  const load = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      const { data } = await api.get(
        `/projects/${activeId}/submittals/aging-summary`,
        { params: { days } },
      );
      setSummary(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Aging load failed");
      setSummary(null);
    } finally { setLoading(false); }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [activeId, days]);

  if (!activeId) return <div className="text-sm text-slate-500">Select a project.</div>;
  if (loading && !summary) {
    return (
      <div className="space-y-2" data-testid="aging-loading">
        {[1, 2, 3, 4, 5].map((i) =>
          <div key={i} className="h-12 bg-slate-100 rounded animate-pulse" />)}
      </div>
    );
  }
  if (!summary) return null;

  const maxBucket = Math.max(1, ...summary.buckets.map((b) => b.count));
  return (
    <div className="space-y-3" data-testid="aging-dashboard">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-medium">Window:</span>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}
            className="h-8 px-2 rounded border border-slate-200 bg-white">
            <option value={30}>30 days</option>
            <option value={60}>60 days</option>
            <option value={90}>90 days</option>
          </select>
          <button onClick={load} className="h-8 px-2 rounded border border-slate-200 bg-white">
            <RefreshCw className="h-3 w-3" />
          </button>
        </div>
        <div className="text-xs text-slate-500">
          {summary.open_count} open · {summary.breach_count} breached ·{" "}
          {summary.must_approve_by_misses} approaching procurement miss
        </div>
      </div>

      {/* 5 buckets */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2"
        data-testid="aging-buckets">
        {summary.buckets.map((b) => (
          <div key={b.label}
            data-testid={`aging-bucket-${b.label}`}
            className="bg-white border border-slate-200 rounded p-3">
            <div className="text-xs text-slate-500">{b.label}</div>
            <div className="text-2xl font-semibold tabular-nums mt-0.5">{b.count}</div>
            <div className="h-1.5 bg-slate-100 rounded-full mt-2 overflow-hidden">
              <div
                style={{ width: `${(b.count / maxBucket) * 100}%` }}
                className={
                  b.label === "30+d" ? "h-full bg-rose-500" :
                  b.label === "22-30d" ? "h-full bg-amber-500" :
                  "h-full bg-sky-500"
                }
              />
            </div>
          </div>
        ))}
      </div>

      {/* Per-discipline rollup */}
      {summary.by_discipline?.length > 0 && (
        <div className="bg-white border border-slate-200 rounded p-3"
          data-testid="aging-by-discipline">
          <div className="text-sm font-medium mb-2">By discipline</div>
          <div className="space-y-1">
            {summary.by_discipline.map((d) => (
              <div key={d.discipline}
                className="flex items-center gap-2 text-xs border-b border-slate-100 py-1">
                <span className="flex-1 truncate">{d.discipline}</span>
                <span className="text-slate-500">open {d.open_count}</span>
                <span className="text-rose-700">breached {d.breached_count}</span>
                <span className="text-slate-500">avg {d.avg_aging_days}d</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Top 10 longest-open */}
      {summary.top_10_longest_open?.length > 0 && (
        <div className="bg-white border border-slate-200 rounded p-3"
          data-testid="aging-top10">
          <div className="text-sm font-medium mb-2">Top 10 longest-open</div>
          <div className="space-y-1">
            {summary.top_10_longest_open.map((s) => (
              <div key={s.id}
                className="flex items-center gap-2 text-xs border-b border-slate-100 py-1">
                <code className="font-mono w-20 truncate">{s.submittal_number}</code>
                <span className="flex-1 truncate">{s.title}</span>
                <SlaPill status={s.sla_status} />
                <span className="text-slate-500 tabular-nums">{s.aging_days}d</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Breach trend */}
      {summary.breach_trend?.length > 0 && (
        <div className="bg-white border border-slate-200 rounded p-3"
          data-testid="aging-breach-trend">
          <div className="text-sm font-medium mb-2">
            Breach trend ({summary.window_days}d)
          </div>
          <div className="flex items-end gap-0.5 h-16">
            {(() => {
              const maxC = Math.max(1, ...summary.breach_trend.map((d) => d.count));
              return summary.breach_trend.map((d) => (
                <div key={d.date} title={`${d.date}: ${d.count} breach(es)`}
                  className="flex-1 bg-rose-500/70 hover:bg-rose-700 min-w-[2px]"
                  style={{ height: `${(d.count / maxC) * 100}%` }} />
              ));
            })()}
          </div>
          <div className="flex justify-between text-xs text-slate-400 mt-1">
            <span>{summary.breach_trend[0]?.date}</span>
            <span>{summary.breach_trend[summary.breach_trend.length - 1]?.date}</span>
          </div>
        </div>
      )}
    </div>
  );
}


// =====================================================================
// Review Events timeline tab.
// =====================================================================
function ReviewEventsTab() {
  const { activeId } = useProjects();
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      // Pull events via the per-submittal endpoint -- list submittals
      // first then fan out. For projects with many submittals this is
      // O(N) per refresh; acceptable for the page-level dashboard.
      const { data: subs } = await api.get(`/projects/${activeId}/submittals`);
      const all = [];
      // Cap at 100 submittals to bound latency.
      const candidates = (subs || []).slice(0, 100);
      const results = await Promise.all(
        candidates.map((s) =>
          api.get(`/submittals/${s.id}/review-events`)
            .then((r) => ({ s, events: r.data?.events || [] }))
            .catch(() => ({ s, events: [] })),
        ),
      );
      for (const { s, events: evs } of results) {
        for (const ev of evs) {
          all.push({ ...ev, submittal_number: s.submittal_number, submittal_title: s.title });
        }
      }
      all.sort((a, b) => (b.performed_at || "").localeCompare(a.performed_at || ""));
      setEvents(all);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Event load failed");
    } finally { setLoading(false); }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [activeId]);

  if (!activeId) return <div className="text-sm text-slate-500">Select a project.</div>;

  return (
    <div className="space-y-2" data-testid="review-events-tab">
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-500">
          {events.length} event(s) across project submittals.
        </span>
        <button onClick={load} className="h-7 px-2 rounded border border-slate-200 bg-white text-xs inline-flex items-center gap-1">
          <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>
      {loading && events.length === 0 ? (
        <div className="space-y-1">
          {[1, 2, 3, 4].map((i) =>
            <div key={i} className="h-12 bg-slate-100 rounded animate-pulse" />)}
        </div>
      ) : events.length === 0 ? (
        <div className="p-6 text-center border border-dashed border-slate-300 rounded text-sm text-slate-500"
          data-testid="events-empty">
          <History className="h-5 w-5 mx-auto mb-1 text-slate-400" />
          <div>No review events recorded yet.</div>
        </div>
      ) : (
        <div className="space-y-1">
          {events.slice(0, 200).map((ev) => (
            <div key={ev.id}
              data-testid={`event-row-${ev.id}`}
              className="border border-slate-200 rounded p-2 bg-white text-xs">
              <div className="flex items-center gap-2">
                <code className="font-mono">{ev.submittal_number}</code>
                <span className="font-medium">{ev.event_type.replace(/_/g, " ")}</span>
                {ev.event_type === "sla_breached" && (
                  <AlertTriangle className="h-3 w-3 text-rose-600" />
                )}
                <span className="ml-auto text-slate-400 font-mono">
                  {new Date(ev.performed_at).toLocaleString()}
                </span>
              </div>
              <div className="text-slate-600 mt-0.5">
                {ev.submittal_title}
                {ev.calendar_days_elapsed != null && (
                  <span className="ml-2 text-slate-500">
                    · Δ {ev.calendar_days_elapsed}d cal /
                    {" "}{ev.working_days_elapsed ?? 0}wd
                  </span>
                )}
              </div>
              {ev.notes && <div className="text-slate-500 italic mt-0.5">"{ev.notes}"</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
