import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import TopHorizontalScroll from "../components/TopHorizontalScroll";
import { Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
// CPM Part F (2026-05-15) -- shared display helpers for the
// Impacted Start / Impacted Finish / Slip columns.
// CPM Part A frontend follow-up (2026-05-15) -- formatPredecessors
// renders dict-shape predecessors {code, type, lag_days} produced by
// migration 018 (was: bare-string codes pre-018). Without this the
// Predecessors column shows "[object Object]" via the bare .join.
import { formatDateDisplay, slipDays, formatPredecessors, localISODate } from "../lib/dateUtil";
// Bug #50 (2026-05-13): added FileText (submittal) icon for the
// extended blocker tooltip. Bug #53 (2026-05-13): dropped ClipboardCheck
// (qc_issue) and ShieldAlert (safety_event) imports since those types
// are no longer surfaced as blockers.
import { Plus, Pencil, Trash2, AlertTriangle, MessageSquare, AlertCircle, Users, Sparkles, Check, Loader2, X, FileText } from "lucide-react";
import ExportDropdown from "../components/ExportDropdown";
import { confirmAction } from "../lib/confirmAction";
import PageHelp from "../components/PageHelp";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";
import FieldLabel from "../components/forms/FieldLabel";

// N1 (2026-05-18) -- the AI-assigned work_category (Feature F1) shown
// as a small muted badge on the activity-code sub-line of the Tasks
// page name cell. DISPLAY ONLY -- F1 still computes / stores the field.
const WORK_CATEGORY_LABELS = {
  preconstruction_paperwork: "Preconstruction paperwork",
  construction_paperwork: "Construction paperwork",
  field_work: "Field work",
  closeout_paperwork: "Closeout paperwork",
};
const WORK_CATEGORY_TINT = {
  preconstruction_paperwork: "bg-violet-50 text-violet-700 border-violet-200",
  construction_paperwork: "bg-sky-50 text-sky-700 border-sky-200",
  field_work: "bg-emerald-50 text-emerald-700 border-emerald-200",
  closeout_paperwork: "bg-amber-50 text-amber-800 border-amber-200",
};

function WorkCategoryBadge({ category }) {
  const label = WORK_CATEGORY_LABELS[category];
  if (!label) return null;  // null / legacy / unknown work_category -> no badge
  return (
    <span
      data-testid={`task-work-category-${category}`}
      title={`Work category: ${label}`}
      className={`inline-flex items-center px-1.5 py-0 rounded border text-xs font-medium whitespace-nowrap ${WORK_CATEGORY_TINT[category] || "bg-slate-100 text-slate-600 border-slate-200"}`}
    >
      {label}
    </span>
  );
}

const EMPTY = {
  activity_code: "", description: "", wbs: "", trade_code: "",
  planned_start: "", planned_finish: "", duration_days: "",
  predecessors: "", is_milestone: false,
};

// MS Project file pipeline (2026-07-02) -- plain-language labels for the
// stored .mpp's origin, shown on the banner's file-status line.
const FILE_SOURCE_LABELS = {
  upload: "uploaded",
  worker_push: "saved by sync",
  generated: "generated from ICE tasks",
};

// Bucket H Phase 4 -- per-task gap indicator. Fetches the resource-
// leveling dashboard for the active project + 60-day window and
// builds a task_id -> worst-severity-cell-attribution map.
//
// Phase 5 (pulled-forward fix #2): refetches every 5 minutes while
// the Tasks tab is visible. Pauses on visibilitychange=hidden so a
// backgrounded tab doesn't ping the backend, and triggers an
// immediate refetch on return-to-foreground so the user sees fresh
// numbers when they switch back from another window.
const GAP_POLL_INTERVAL_MS = 5 * 60 * 1000;   // 5 minutes

function useTaskGapIndicator(projectId) {
  const [byTaskId, setByTaskId] = useState({});
  useEffect(() => {
    if (!projectId) { setByTaskId({}); return undefined; }
    let cancelled = false;
    const fetchOnce = async () => {
      const today = new Date();
      const end = new Date();
      end.setDate(today.getDate() + 60);
      const startIso = localISODate(today);
      const endIso = localISODate(end);
      try {
        const r = await api.get(`/resource-leveling/dashboard`, {
          params: { date_range_start: startIso, date_range_end: endIso,
                     project_id: projectId },
        });
        if (cancelled) return;
        const out = {};
        const severityRank = { major: 3, moderate: 2, minor: 1 };
        const cells = r.data?.cells || {};
        Object.entries(cells).forEach(([trade, byDate]) => {
          Object.entries(byDate).forEach(([dateIso, cell]) => {
            if (!["minor", "moderate", "major"].includes(cell.severity)) return;
            for (const tid of (cell.contributing_task_ids || [])) {
              const existing = out[tid];
              const cur = severityRank[cell.severity] || 0;
              const prev = existing ? (severityRank[existing.severity] || 0) : 0;
              if (cur > prev) {
                out[tid] = {
                  severity: cell.severity,
                  trade, date: dateIso,
                  is_critical_path: !!cell.is_critical_path,
                  gap: cell.gap, gap_id: cell.id,
                };
              }
            }
          });
        });
        setByTaskId(out);
      } catch {
        if (!cancelled) setByTaskId({});
      }
    };
    // Initial fetch.
    fetchOnce();
    // Polling: recurring setTimeout (re-armed after each fetch) so we
    // don't stack pending requests on a slow network. Skipped when
    // document.hidden -- visibilitychange handler resumes on focus.
    let timerId = null;
    const scheduleNext = () => {
      if (cancelled) return;
      timerId = setTimeout(async () => {
        if (cancelled || document.hidden) return;
        await fetchOnce();
        scheduleNext();
      }, GAP_POLL_INTERVAL_MS);
    };
    scheduleNext();
    const onVisibilityChange = () => {
      if (document.hidden) return;
      if (timerId) clearTimeout(timerId);
      fetchOnce().then(scheduleNext);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      if (timerId) clearTimeout(timerId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [projectId]);
  return byTaskId;
}

function TaskGapIcon({ gap }) {
  if (!gap) return null;
  const tone = gap.is_critical_path
    ? "text-rose-700 bg-rose-100 border-rose-300"
    : gap.severity === "major"
      ? "text-rose-700 bg-rose-50 border-rose-200"
      : gap.severity === "moderate"
        ? "text-orange-800 bg-orange-50 border-orange-200"
        : "text-amber-800 bg-amber-50 border-amber-200";
  return (
    <Link to={`/resource-leveling?tab=gaps&trade=${encodeURIComponent(gap.trade)}`}
      title={`${gap.trade} shortage on ${gap.date} (gap ${gap.gap}${gap.is_critical_path ? ", on CP" : ""}). Click to open Resource Leveling.`}
      data-testid={`task-gap-icon-${gap.gap_id}`}
      className={`inline-flex items-center gap-0.5 px-1 py-0.5 rounded border text-xs font-mono ${tone}`}>
      <Users className="h-3 w-3" />
      <span className="hidden md:inline">{gap.severity[0].toUpperCase()}</span>
    </Link>
  );
}

export default function Tasks() {
  const { activeId, projects } = useProjects();
  const [tasks, setTasks] = useState([]);
  const [crews, setCrews] = useState([]);
  const [locs, setLocs] = useState([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(EMPTY);
  // Bucket C1 Phase 3 — only one blocker popover open at a time.
  const [openBlockerId, setOpenBlockerId] = useState(null);
  // Bucket H Phase 4 -- per-task resource-leveling gap indicator.
  const gapByTaskId = useTaskGapIndicator(activeId);
  const project = projects.find((p) => p.id === activeId);
  // Ref for the wide tasks table scroll container — wired to a sticky
  // top horizontal scrollbar so users can drag the table sideways
  // without scrolling to the bottom of the viewport first.
  const tableScrollRef = useRef(null);
  // CPM Part F (2026-05-15) -- sort + filter affordances for the new
  // Impacted Start / Impacted Finish / Slip columns. Defaults preserve
  // the pre-Part-F behavior (no sort, no filter -- tasks render in
  // server order, which is by activity_code).
  const [sortBySlipDesc, setSortBySlipDesc] = useState(false);
  const [showOnlyImpacted, setShowOnlyImpacted] = useState(false);

  // CPM Part F (2026-05-15) -- derive the display list. Memoized so
  // toggling sort / filter doesn't recompute on every keystroke.
  // Slip is computed from planned_finish vs projected_finish_date
  // via the shared slipDays helper, which returns 0 (not NaN) on
  // missing dates -- the "Show only impacted" filter correctly hides
  // tasks with no projection at all.
  const displayTasks = useMemo(() => {
    const enriched = tasks.map((t) => ({
      ...t,
      _slip: slipDays(t.planned_finish, t.projected_finish_date),
    }));
    const filtered = showOnlyImpacted
      ? enriched.filter((t) => t._slip > 0)
      : enriched;
    if (sortBySlipDesc) {
      // Stable sort -- equal-slip tasks stay in their original order
      // (which is activity_code from the server). Slip descending so
      // worst-hit tasks land at the top, mirroring the Part F spec.
      return [...filtered].sort((a, b) => b._slip - a._slip);
    }
    return filtered;
  }, [tasks, sortBySlipDesc, showOnlyImpacted]);

  const load = () => activeId && api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data));
  useEffect(() => {
    if (!activeId) return;
    load();
    api.get("/crews").then(({ data }) => setCrews(data));
    api.get(`/projects/${activeId}/locations`).then(({ data }) => setLocs(data));
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [activeId]);

  // CPM Mode 2 (2026-06-29) -- MS Project schedule sync state for this project.
  const isMode2 = project?.schedule_mode === "mode2";
  const [syncState, setSyncState] = useState(null);
  const activeIdRef = useRef(activeId);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  const refreshSyncState = useCallback(() => {
    // Reset (not just skip) on project change so a previous project's
    // sync-state (file line, active job) never bleeds into this one.
    if (!activeId || !isMode2) { setSyncState(null); return; }
    const requestedFor = activeId;
    api.get(`/projects/${requestedFor}/ms-project/sync-state`)
      // A response that raced a project switch must not land on the new
      // project — the closure's id is compared against the current one.
      .then(({ data }) => setSyncState((prev) =>
        requestedFor === activeIdRef.current ? data : prev))
      .catch(() => {});
  }, [activeId, isMode2]);
  useEffect(() => { refreshSyncState(); }, [refreshSyncState]);

  // MS Project file pipeline (2026-07-02) -- while the worker holds a job
  // (sync-state.active_job), poll every 10s; the moment it clears, reload
  // the task list once (the recalculated dates just landed) and stop.
  const activeJobId = syncState?.active_job?.id || null;
  const sawActiveJobRef = useRef(false);
  useEffect(() => { sawActiveJobRef.current = false; }, [activeId]);
  useEffect(() => {
    if (activeJobId) {
      sawActiveJobRef.current = true;
      const timer = setInterval(refreshSyncState, 10000);
      return () => clearInterval(timer);
    }
    if (sawActiveJobRef.current) {
      sawActiveJobRef.current = false;
      load();
    }
    return undefined;
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [activeJobId, refreshSyncState]);

  // Mode 2 recompute is asynchronous: MS Project recalculates out-of-band via
  // the worker. Poll the job to completion, then refresh the table.
  const pollRecompute = async (jobId, tries = 0) => {
    if (!jobId || tries > 60) { load(); return; }
    try {
      const { data } = await api.get(`/projects/${activeId}/recompute-status/${jobId}`);
      const s = data?.status;
      if (s === "done") {
        const n = data.summary?.ingested_tasks;
        toast.success(`MS Project recompute applied${n != null ? ` — ${n} task(s) updated` : ""}.`);
        load(); refreshSyncState(); return;
      }
      if (s === "conflict") {
        toast.error("MS Project was changed outside ICE — use “Sync from MS Project” to reconcile.");
        load(); refreshSyncState(); return;
      }
      if (s === "error" || s === "parked") {
        toast.error("MS Project recompute failed — check the worker, or open the plan in Project.");
        load(); return;
      }
    } catch { /* backend asleep / transient — keep polling */ }
    setTimeout(() => pollRecompute(jobId, tries + 1), 3000);
  };

  const recompute = async () => {
    const { data } = await api.post(`/projects/${activeId}/critical-path/recompute`);
    // CPM Mode 2: a Mode-2 project returns {status:"queued"} — MS Project's
    // engine recomputes via the worker; poll to completion. Mode-1 keeps the
    // synchronous {critical_count, project_finish} path below, unchanged.
    if (data?.status === "queued") {
      const f = data.stale?.project_finish;
      toast.info(`Sent to MS Project — recalculating…${f ? ` (showing as of finish ${f})` : ""}`);
      await pollRecompute(data.job_id);
      return;
    }
    // services/cpm.py:56 returns {error: "cycle_detected"} on cycle without
    // a project_finish; guard so the toast doesn't render "Finish: undefined".
    if (data?.error === "cycle_detected") {
      toast.error("Schedule has a cycle in predecessor topology — cannot compute CPM. Fix the cycle and retry.");
      load();
      return;
    }
    if (data?.error) {
      toast.error(`CPM recompute failed: ${data.error}`);
      load();
      return;
    }
    toast.success(`CPM updated. ${data.critical_count} on critical path. Finish: ${data.project_finish}`);
    load();
  };

  const syncFromProject = async () => {
    try {
      await api.post(`/projects/${activeId}/ms-project/pull`);
      toast.info("Sync from MS Project queued — changes will arrive as a schedule proposal to review.");
      setTimeout(refreshSyncState, 4000);
    } catch {
      toast.error("Could not start MS Project sync.");
    }
  };

  // MS Project file pipeline (2026-07-02) -- the .mpp lives in ICE now.
  // Upload stores a new version (rev+1) and auto-queues the first sync;
  // Download streams the current version; Generate asks the worker to build
  // a brand-new file from the ICE task list (only offered when none exists).
  const mppFileInputRef = useRef(null);
  const [mppBusy, setMppBusy] = useState(false);

  const uploadMppFile = async (file) => {
    if (!file || mppBusy) return;
    setMppBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const { data } = await api.post(`/projects/${activeId}/ms-project/file`, fd);
      toast.success(data?.auto_job ? "File uploaded — first sync queued." : "File uploaded.");
      refreshSyncState();
    } catch (e) {
      toast.error(e.response?.data?.detail || "File upload failed.");
    } finally {
      setMppBusy(false);
      // Clear so picking the same file again re-fires onChange.
      if (mppFileInputRef.current) mppFileInputRef.current.value = "";
    }
  };

  const downloadMppFile = async () => {
    try {
      const res = await api.get(`/projects/${activeId}/ms-project/file`, { responseType: "blob" });
      // Blob -> anchor click -> revoke URL (same pattern as ExportDropdown).
      const blobUrl = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = syncState?.file?.name || "schedule.mpp";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(blobUrl);
    } catch {
      toast.error("Could not download the file.");
    }
  };

  const generateMppFile = async () => {
    if (mppBusy) return;
    setMppBusy(true);
    try {
      await api.post(`/projects/${activeId}/ms-project/generate-file`);
      toast.success("Building a Microsoft Project file from the ICE tasks — it will appear here when ready.");
      refreshSyncState();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not start the file build.");
    } finally {
      setMppBusy(false);
    }
  };

  const openNew = () => { setEditing(null); setForm(EMPTY); setOpen(true); };
  const openEdit = (t) => {
    setEditing(t);
    setForm({
      activity_code: t.activity_code,
      description: t.description,
      wbs: t.wbs || "",
      trade_code: t.trade_code || "",
      planned_start: t.planned_start || "",
      planned_finish: t.planned_finish || "",
      duration_days: t.duration_days ?? "",
      predecessors: (t.predecessors || []).join(", "),
      is_milestone: !!t.is_milestone,
      crew_id: t.crew_id || "",
      location_id: t.location_id || "",
    });
    setOpen(true);
  };

  const submit = async () => {
    const payload = {
      activity_code: form.activity_code.trim(),
      description: form.description.trim(),
      wbs: form.wbs || null,
      trade_code: form.trade_code || null,
      planned_start: form.planned_start || null,
      planned_finish: form.planned_finish || null,
      duration_days: form.duration_days === "" ? null : Number(form.duration_days),
      predecessors: form.predecessors ? form.predecessors.split(",").map((s) => s.trim()).filter(Boolean) : [],
      is_milestone: !!form.is_milestone,
      crew_id: form.crew_id || null,
      location_id: form.location_id || null,
    };
    Object.keys(payload).forEach((k) => { if (payload[k] === null) delete payload[k]; });
    try {
      if (editing) {
        // task update can't change activity_code; predecessors are now patchable
        const { activity_code, ...patchable } = payload;
        await api.patch(`/tasks/${editing.id}`, patchable);
        toast.success("Activity updated — CPM recomputing in background.");
      } else {
        await api.post("/tasks", { project_id: activeId, ...payload });
        toast.success("Activity created");
      }
      setOpen(false);
      load();
    } catch (e) {
      toast.error(JSON.stringify(e.response?.data?.detail || e.message));
    }
  };

  const remove = async (t) => {
    // pt.37: themed modal (was window.confirm).
    const ok = await confirmAction({
      title: `Delete activity ${t.activity_code}?`,
      message: t.description ? `"${t.description}" will be removed from the schedule.` : undefined,
      confirmText: "Delete activity",
      icon: "trash",
    });
    if (!ok) return;
    await api.delete(`/tasks/${t.id}`);
    toast.success("Deleted");
    load();
  };

  return (
    <div data-testid="tasks-page" className="space-y-4">
      {/* Bucket G Phase 4 (2026-05-11) -- schedule-import suggestions
          banner. Renders when this project has pending AI-trade-
          inference suggestions on schedule_activity rows (the BG task
          on /schedule-proposals/{id}/approve drops these in bulk).
          PM can route to the Review Queue or bulk-accept all in one
          click; saves ~30 min on a 50-task import. */}
      {activeId && (
        <ScheduleAiSuggestionsBanner projectId={activeId} />
      )}
      {/* CPM Mode 2 (2026-06-29) -- MS Project engine banner. Rendered ONLY for
          a project on schedule_mode="mode2"; Mode-1 projects never see it (and
          never fetch sync-state). Shows the engine source, a standing
          out-of-band-edit conflict, and a "Sync from MS Project" action. */}
      {isMode2 && (
        <div
          data-testid="msproject-mode-banner"
          className={`mb-3 rounded-md border px-3 py-2 text-sm ${
            syncState?.conflict
              ? "border-rose-300 bg-rose-50 text-rose-800"
              : "border-sky-200 bg-sky-50 text-sky-800"
          }`}
        >
          <div className="flex items-center gap-3 max-md:flex-wrap">
            <span className="font-medium">MS Project engine</span>
            {syncState?.conflict ? (
              <span data-testid="msproject-conflict">
                ⚠ MS Project was changed outside ICE — sync to reconcile.
              </span>
            ) : (
              <span className="text-sky-700/80">
                Dates &amp; critical path are computed by Microsoft Project.
              </span>
            )}
            {/* MS Project file pipeline (2026-07-02) -- worker-job indicator.
                Amber while a job is pending/claimed; red suffix when the
                worker hasn't checked in recently. */}
            {syncState?.active_job && (
              <span
                data-testid="msp-active-job"
                className="pill bg-amber-100 text-amber-900 border border-amber-300"
              >
                Recalculating in Microsoft Project…
                {!syncState.worker_online && (
                  <span className="text-rose-700 font-semibold">{" — worker offline"}</span>
                )}
              </span>
            )}
            <button
              data-testid="msproject-sync"
              onClick={syncFromProject}
              className="ml-auto sbtn sbtn-sm sbtn-secondary max-md:mt-1"
            >
              Sync from MS Project
            </button>
          </div>
          {/* MS Project file pipeline (2026-07-02) -- the .mpp is stored in
              ICE (no shared-folder placement). File-status line + actions. */}
          <div className={`mt-2 pt-2 border-t flex items-center gap-2 max-md:flex-wrap ${
            syncState?.conflict ? "border-rose-200" : "border-sky-200"
          }`}>
            <span data-testid="msp-file-status" className="text-xs">
              {syncState?.file
                ? `${syncState.file.name} · v${syncState.file.rev} · updated ${formatDateDisplay(syncState.file.updated_at)} · ${FILE_SOURCE_LABELS[syncState.file.source] || syncState.file.source}`
                : "No file yet"}
            </span>
            <input
              ref={mppFileInputRef}
              type="file"
              accept=".mpp"
              className="hidden"
              onChange={(e) => uploadMppFile(e.target.files?.[0] || null)}
            />
            <span className="ml-auto flex items-center gap-2 max-md:ml-0 max-md:flex-wrap">
              <button
                data-testid="msp-file-upload"
                onClick={() => mppFileInputRef.current?.click()}
                disabled={mppBusy}
                className="sbtn sbtn-sm sbtn-secondary"
              >
                Upload file
              </button>
              {syncState?.file && (
                <button
                  data-testid="msp-file-download"
                  onClick={downloadMppFile}
                  className="sbtn sbtn-sm sbtn-secondary"
                >
                  Download
                </button>
              )}
              {/* Hidden while a job is in flight: generating against a file
                  that's about to change would race the upload it fences. */}
              {!syncState?.file && !syncState?.active_job && tasks.length > 0 && (
                <button
                  data-testid="msp-file-generate"
                  onClick={generateMppFile}
                  disabled={mppBusy}
                  title="Build a new Microsoft Project file from the activities already in ICE."
                  className="sbtn sbtn-sm sbtn-secondary"
                >
                  Generate from ICE tasks
                </button>
              )}
            </span>
          </div>
        </div>
      )}
      <div className="flex items-center justify-between max-md:flex-col max-md:items-start max-md:gap-2">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight inline-flex items-baseline">
            Schedule activities
            {/* pt.38: legend (CP / CP* / Float / Rem Float definitions)
                moved from the bottom of the page into this PageHelp
                popover -- it was eating ~50px of vertical space the
                schedule table could use. Power users don't need the
                legend every visit; new users get it one click away. */}
            <PageHelp title="Schedule activities — column glossary">
              <div className="space-y-2">
                <div>
                  <span className="inline-block px-1.5 py-0.5 text-[10px] rounded bg-rose-100 text-rose-800 border border-rose-300 font-semibold mr-1.5">CP</span>
                  <b>Critical Path</b> — zero structural float, any slip delays the project finish.
                </div>
                <div>
                  <span className="inline-block px-1.5 py-0.5 text-[10px] rounded bg-amber-100 text-amber-900 border border-amber-300 font-semibold mr-1.5">CP*</span>
                  <b>Effectively critical</b> — all float consumed by delays/cascade; driving the project finish.
                </div>
                <div>
                  <b>Float (slack)</b> — working days an activity can slip without delaying the project. High floats (e.g. 40+) usually mean the source schedule has no predecessor links between activities.
                </div>
                <div>
                  <b>Rem Float</b> — structural float minus working-day cascade slip. Negative (red) = task is past its slack and driving the finish.
                </div>
              </div>
            </PageHelp>
          </h1>
          <p className="text-sm text-slate-500">{project?.name}</p>
        </div>
        <div className="flex gap-2 max-md:flex-wrap">
          {/* Bug #42 (2026-05-12) -- Tasks export dropdown. Same audit-
              honest pattern as the Audit Log CSV / Photo Evidence Pack
              from Bucket K -- single-click download, server writes
              TASKS_EXPORTED audit before returning the file. */}
          <ExportDropdown
            label="Export"
            testidPrefix="tasks-export"
            buildUrl={(fmt) => `/projects/${activeId}/tasks/export?format=${fmt}`}
            filenameHint={(fmt) => `tasks_${(activeId || "").slice(0, 8)}.${fmt}`}
            disabled={!activeId}
          />
          {/* pt.40: page-header tier = sbtn-md (32px), no Plus icon. */}
          <button data-testid="cpm-recompute" onClick={recompute} className="sbtn sbtn-md sbtn-secondary">
            Recompute CPM
          </button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <button data-testid="new-activity" onClick={openNew} className="sbtn sbtn-md sbtn-primary">
                New activity
              </button>
            </DialogTrigger>
            <DialogContent className="max-w-xl">
              <DialogHeader><DialogTitle>{editing ? `Edit ${editing.activity_code}` : "New activity"}</DialogTitle></DialogHeader>
              {/* max-md:grid-cols-2 -- pair the short fields (code/WBS/dates/
                  trade) 2-up on phones; sm:=640px never fires below 767.98px.
                  col-span-2 children stay full-width. Desktop unchanged. */}
              <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
                <div>
                  <FieldLabel required className="field-label">Activity code</FieldLabel>
                  <input data-testid="ac-code" value={form.activity_code} disabled={!!editing} onChange={(e) => setForm({ ...form, activity_code: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200 disabled:bg-slate-50" />
                </div>
                <div>
                  <label className="field-label">WBS</label>
                  <input value={form.wbs} onChange={(e) => setForm({ ...form, wbs: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div className="col-span-2">
                  <FieldLabel required className="field-label">Description</FieldLabel>
                  <input data-testid="ac-desc" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Planned start</label>
                  <input type="date" value={form.planned_start} onChange={(e) => setForm({ ...form, planned_start: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Planned finish</label>
                  <input type="date" value={form.planned_finish} onChange={(e) => setForm({ ...form, planned_finish: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Duration (days)</label>
                  <input type="number" value={form.duration_days} onChange={(e) => setForm({ ...form, duration_days: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Trade</label>
                  <input value={form.trade_code} onChange={(e) => setForm({ ...form, trade_code: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Crew</label>
                  <select value={form.crew_id || ""} onChange={(e) => setForm({ ...form, crew_id: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    <option value="">None</option>
                    {crews.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.trade})</option>)}
                  </select>
                </div>
                <div>
                  <label className="field-label">Location</label>
                  <select value={form.location_id || ""} onChange={(e) => setForm({ ...form, location_id: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    <option value="">None</option>
                    {locs.map((l) => <option key={l.id} value={l.id}>{l.code} {l.name}</option>)}
                  </select>
                </div>
                <div className="col-span-2">
                  <label className="field-label">Predecessors (comma-separated activity_codes)</label>
                  <input data-testid="task-predecessors" value={form.predecessors} onChange={(e) => setForm({ ...form, predecessors: e.target.value })} placeholder="e.g. 1.2, 1.5" className="w-full h-9 px-3 rounded border border-slate-200 font-mono text-sm" />
                  <div className="text-xs text-slate-400 mt-1">Comma-separated activity codes from this project. CPM is recomputed automatically.</div>
                </div>
                <label className="col-span-2 flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={!!form.is_milestone} onChange={(e) => setForm({ ...form, is_milestone: e.target.checked })} />
                  Milestone
                </label>
              </div>
              <Button data-testid="ac-submit" onClick={submit} disabled={!form.activity_code || !form.description} className="bg-blue-600 hover:bg-blue-700">
                {editing ? "Save changes" : "Create activity"}
              </Button>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {/* CPM Part F (2026-05-15) -- impact-column controls. Two
          affordances: sort by slip descending (worst-hit at top),
          and filter to slip > 0 (hide tasks with no projection
          shift). Both toggle on/off independently. The slip-count
          summary is purely informational. */}
      {tasks.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-slate-600 max-md:flex-wrap">
          <button
            data-testid="tasks-sort-by-slip"
            onClick={() => setSortBySlipDesc((v) => !v)}
            className={`h-7 max-md:min-h-[44px] px-2 rounded border text-xs ${sortBySlipDesc ? "bg-rose-50 border-rose-300 text-rose-700" : "bg-white border-slate-200"}`}
            title="Sort by Slip descending so the worst-hit tasks land at the top."
          >
            {sortBySlipDesc ? "✓ Sorted by slip ↓" : "Sort by slip ↓"}
          </button>
          {/* max-md:w-full + 44px row -- on phones the checkbox gets its own
              full-width, thumb-tappable row instead of wrapping awkwardly under
              the Sort button. Desktop keeps it inline. */}
          <label className="inline-flex items-center gap-1 cursor-pointer max-md:w-full max-md:min-h-[44px]">
            <input
              data-testid="tasks-show-only-impacted"
              type="checkbox"
              checked={showOnlyImpacted}
              onChange={(e) => setShowOnlyImpacted(e.target.checked)}
            />
            Show only impacted (slip &gt; 0)
          </label>
          <span className="text-slate-400">
            ·{" "}
            {tasks.filter((t) => slipDays(t.planned_finish, t.projected_finish_date) > 0).length}{" "}
            impacted of {tasks.length}
          </span>
        </div>
      )}

      <div className="surface overflow-hidden">
        <TopHorizontalScroll targetRef={tableScrollRef} />
        <div
          ref={tableScrollRef}
          className="overflow-auto scrollbar-soft scrollbar-no-x max-h-[calc(100vh-220px)]"
          data-testid="tasks-table-scroll"
        >
        <table className="dense-table dense-table-frozen min-w-max">
          <thead>
            <tr>
              <th className="w-10 col-pin-1">#</th>
              <th className="col-pin-2">Description</th>
              <th>Resource</th>
              <th>Predecessors</th>
              <th>Trade</th>
              <th>Plan start</th>
              <th>Plan finish</th>
              {/* CPM Part F -- Impacted Start / Finish / Slip. The
                  Impacted dates come from compute_projected_finish_dates
                  (Bucket C1 Phase 4 + Part F extension); Slip is
                  derived client-side via slipDays(planned_finish,
                  projected_finish_date). All three are populated
                  on first event (delay / RFI / SLA flip / mitigation
                  choose) that fires auto_recompute_after_event. */}
              <th title="Projected start date when an open delay or RFI pushes the task forward via CPM walk-forward.">Impacted Start</th>
              <th title="Projected finish date — planned_finish + accumulated open-delay shift through CPM predecessors.">Impacted Finish</th>
              <th title="Working days of slip: working_days_between(planned_finish, projected_finish_date). Em-dash when zero. Light-red row tint when > 0. (CPM Bug 3.B: working days, was calendar — a 5-working-day delay now shows +5, not +7.)">Slip (wd)</th>
              <th>Dur (d)</th>
              <th>%</th>
              <th title="Computed status: complete > blocked > behind > on_track. Updates on every EOD% edit, delay open/close, and read.">Status</th>
              <th title="Open blockers (delays / RFIs / SLA-breached submittals / field-issues linked to this task). Click to see them. QC issues + safety events stay linked for the gallery but don't surface here.">Blockers</th>
              <th title="Critical Path — these activities have zero float, any slip pushes the project finish.">CP</th>
              <th title="Float (a.k.a. slack): how many working days an activity can slip without delaying the project finish. Structural metric — does NOT decrease from logged delays. (CPM Part L: working days, was calendar days pre-Part-L.)">Float (d)</th>
              <th title="Remaining float: structural float minus the sum of impact days from currently-open blockers (delays + RFIs + over-SLA submittals) on this task. Updates live as events are added or resolved. Goes UP when delays close. Light-red row tint when 0 (any new event pushes CP). CPM Part L-2.">Rem Float (d)</th>
              <th title="Resource gap. Surfaces when the master schedule's trade demand exceeds crew supply on at least one day in this task's date range.">Gap</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {displayTasks.map((t, idx) => {
              const slip = t._slip;
              // CPM Part F -- row tint: light red when slip > 0. The
              // existing pink critical-path tint (bg-red-50/40) wins
              // on critical-path tasks; for non-CP tasks a slip > 0
              // applies a slightly darker rose tint so the user can
              // tell "this is on the CP" (pink) from "this has
              // projected slip" (rose).
              // CPM Bug 3.E (2026-05-16) -- remaining_float_days is
              // now SIGNED (may be negative). A task with
              // remaining <= 0 has consumed all its float (by direct
              // delay OR inherited cascade) and is at/over the cliff
              // -- any further slip drives the project finish. Tint
              // those rows darker than the slip-rose tint so PMs can
              // scan for tasks with no slack left.
              const slipTint = slip > 0 ? "row-slip" : "";
              const structural = t.total_float_days;
              const remaining = t.remaining_float_days;
              const cliffTint = (
                remaining != null && remaining <= 0
              ) ? "row-cliff" : "";
              // pt.74 (2026-05-29): row tints moved from raw Tailwind
              // bg-red-50/bg-rose-50/bg-rose-100 to the dedicated
              // .row-cp / .row-slip / .row-cliff classes (index.css).
              // The Tailwind classes were invisible in LIGHT theme
              // (the `.dense-table-frozen tbody tr { background: surface }`
              // rule out-specified them; dark only worked via !important
              // overrides) and never reached the sticky #/Description
              // columns (their surface gradient painted over the tint).
              // The new classes are opaque + theme-aware, win with
              // !important, and let the pinned cells inherit the tint so
              // the WHOLE row tints in both themes while still masking
              // scrolled-under content on the sticky columns.
              const rowClass = t.is_critical_path
                ? "row-cp"
                : (cliffTint || slipTint);
              return (
              <tr key={t.id} data-testid={`task-row-${t.activity_code}`} className={rowClass}>
                <td className={`tabular-nums text-xs text-slate-500 col-pin-1 ${rowClass || "bg-white"}`}>{idx + 1}</td>
                {/* pt.46: dropped `truncate` so long descriptions like
                    "Selective Demo — Walls, Ceilings, Casework" wrap
                    within the column instead of getting cut off with
                    no ellipsis. max-w stays the same, height grows. */}
                <td className={`max-w-[280px] col-pin-2 ${rowClass || "bg-white"}`}>
                  <div className="font-medium text-sm whitespace-normal break-words">{t.description}</div>
                  {/* N1: activity-code sub-line + work_category badge */}
                  <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 mt-0.5 whitespace-normal">
                    <span className="text-xs text-slate-400 font-mono whitespace-nowrap">
                      {t.activity_code}{t.is_milestone ? " · milestone" : ""}
                    </span>
                    <WorkCategoryBadge category={t.work_category} />
                  </div>
                </td>
                <td className="text-xs">
                  {t.resource_names && t.resource_names.length
                    ? t.resource_names.join(", ")
                    : <span className="text-slate-400">—</span>}
                </td>
                <td className="text-xs font-mono">
                  {/* CPM Part A frontend follow-up (2026-05-15) --
                      formatPredecessors handles BOTH legacy bare-string
                      codes ("1.5") and post-018 dict shape ({code, type,
                      lag_days}). FS+0 collapses to bare code; non-FS or
                      non-zero-lag renders the MS-Project shorthand
                      "22FS-3d" / "13SS+2d". Returns "—" for empty/null. */}
                  {t.predecessors && t.predecessors.length
                    ? formatPredecessors(t.predecessors)
                    : <span className="text-slate-400">—</span>}
                </td>
                <td className="text-xs">{t.trade_code || "—"}</td>
                {/* CPM Part G (2026-05-15) -- existing Plan start /
                    Plan finish columns now route through
                    formatDateDisplay so they render MM/DD/YYYY in
                    the same chokepoint as Impacted Start / Impacted
                    Finish below. The Tasks page form's <input
                    type="date"> inputs (lines 305-315 area) keep
                    using planned_start / planned_finish as ISO
                    strings -- the HTML date input REQUIRES ISO
                    YYYY-MM-DD; this is the storage layer, not the
                    display layer, and Part G leaves storage alone. */}
                <td className="font-mono text-xs">{formatDateDisplay(t.planned_start)}</td>
                <td className="font-mono text-xs">{formatDateDisplay(t.planned_finish)}</td>
                {/* CPM Part F -- Impacted Start. formatDateDisplay
                    returns "—" on null/empty so fresh tasks (no
                    projection yet) render the em-dash. Part G has
                    flipped the helper output to MM/DD/YYYY app-wide
                    via this same chokepoint. */}
                <td
                  data-testid={`task-impacted-start-${t.activity_code}`}
                  className="font-mono text-xs"
                >
                  {formatDateDisplay(t.projected_start_date)}
                </td>
                <td
                  data-testid={`task-impacted-finish-${t.activity_code}`}
                  className="font-mono text-xs"
                >
                  {formatDateDisplay(t.projected_finish_date)}
                </td>
                <td
                  data-testid={`task-slip-${t.activity_code}`}
                  className="tabular-nums text-xs"
                  title={slip > 0
                    ? `Projected slip +${slip} working days vs planned_finish`
                    : "No projected slip"}
                >
                  {slip > 0
                    ? <span className="text-rose-700 font-semibold">+{slip}</span>
                    : <span className="text-slate-300">—</span>}
                </td>
                <td className="tabular-nums text-xs">{t.duration_days}</td>
                <td className="tabular-nums">{t.pct_complete}%</td>
                <td><ComputedStatusPill task={t} /></td>
                <td>
                  <BlockerCell
                    task={t}
                    open={openBlockerId === t.id}
                    onToggle={() => setOpenBlockerId(openBlockerId === t.id ? null : t.id)}
                  />
                </td>
                {/* CPM Bug 3.F (2026-05-16) -- CP column shows TWO
                    badges. "CP" = structural critical path
                    (is_critical_path, zero structural float).
                    "CP*" = effectively critical (is_currently_
                    critical) -- the task has consumed all its float
                    by direct delay or inherited cascade and now
                    drives the project finish, even though it is NOT
                    on the structural critical path. Structural CP
                    tasks are trivially also currently-critical, so
                    "CP" alone is shown for them (no redundant CP*). */}
                <td>
                  {t.is_critical_path ? (
                    <span className="pill pill-red" title="Critical Path — zero structural float; any slip pushes the project finish.">CP</span>
                  ) : t.is_currently_critical ? (
                    <span
                      className="pill bg-amber-100 text-amber-900 border border-amber-300"
                      title="Effectively critical — all float consumed by delays/cascade. NOT on the structural critical path, but currently driving the project finish."
                    >CP*</span>
                  ) : null}
                </td>
                <td className="tabular-nums text-xs" title="Structural working-day float — does NOT decrease from logged events">
                  {t.total_float_days ?? "—"}
                </td>
                {/* CPM Bug 3.E (2026-05-16) -- live remaining float,
                    SIGNED. Derived from the cascade's projected
                    finish: structural − working-day cascade slip.
                    Negative => task is that many working days past
                    its slack, driving the project finish. Em-dash
                    when null (not yet computed). RED text when <= 0
                    (at or past the cliff); normal when > 0. */}
                <td
                  data-testid={`task-remaining-float-${t.activity_code}`}
                  className="tabular-nums text-xs"
                  title={
                    remaining == null
                      ? "Remaining float not yet computed"
                      : remaining > 0
                        ? `${remaining} working days of float remaining of ${structural ?? "?"} structural`
                        : remaining === 0
                          ? "Zero float remaining — at the edge; any further slip drives the project finish"
                          : `All float consumed — this task is ${Math.abs(remaining)} working days past its slack and is driving the project finish.`
                  }
                >
                  {remaining == null ? (
                    <span className="text-slate-300">—</span>
                  ) : remaining <= 0 ? (
                    <span className="text-rose-700 font-semibold">{remaining}</span>
                  ) : (
                    <span className="text-slate-700">{remaining}</span>
                  )}
                </td>
                <td><TaskGapIcon gap={gapByTaskId[t.id]} /></td>
                <td className="text-right">
                  <button data-testid={`edit-task-${t.activity_code}`} onClick={() => openEdit(t)} title="Edit" className="h-7 w-7 max-md:min-h-[44px] max-md:min-w-[44px] grid place-items-center rounded hover:bg-slate-100">
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button data-testid={`delete-task-${t.activity_code}`} onClick={() => remove(t)} title="Delete" className="h-7 w-7 max-md:min-h-[44px] max-md:min-w-[44px] grid place-items-center rounded hover:bg-red-50 text-red-600">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
              );
            })}
            {!displayTasks.length && tasks.length > 0 && (
              <tr><td colSpan="19" className="text-center py-8 text-sm text-slate-500">
                No activities match the current filter. Toggle &ldquo;Show only impacted&rdquo; off to see all.
              </td></tr>
            )}
            {!tasks.length && <tr><td colSpan="19" className="text-center py-8 text-sm text-slate-500">No activities. Use the &ldquo;New activity&rdquo; button or import a schedule.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      {/* pt.38: removed the bottom CP/CP-star/Float/Rem-Float legend.
          Same content now lives in the PageHelp (?) on the title --
          gives the schedule table the full vertical real estate it
          deserves as the page's primary content. */}
    </div>
  );
}

// Bucket C1 Phase 2 — Status pill driven by computed_status, with a small
// blue dot when the latest sync was provisional (i.e. the daily report
// that generated it hasn't been submitted yet).
//
// Followup-fix 3 (2026-05-07): also surface "at risk" when
// projected_finish_date > planned_finish even if computed_status is
// still on_track (mirrors the same fix on Lookahead.jsx so the two
// surfaces agree).
function ComputedStatusPill({ task }) {
  const cs = task.computed_status || "on_track";
  const projShifted = task.projected_finish_date && task.planned_finish &&
    task.projected_finish_date > task.planned_finish;
  const blocked = cs === "blocked";
  const atRisk = !blocked && cs !== "behind" && cs !== "complete" && projShifted;
  const klass = cs === "complete" ? "pill-green"
    : blocked ? "pill-red"
    : cs === "behind" || atRisk ? "pill-amber"
    : "pill-slate";
  const label = blocked ? "blocked"
    : cs === "complete" ? "complete"
    : cs === "behind" ? "behind"
    : atRisk ? "at risk"
    : cs.replace(/_/g, " ");
  const isProvisional = task.last_synced_from_dr?.sync_state === "provisional";
  const lifecycle = task.status; // not_started / in_progress / complete / on_hold etc
  const projNote = atRisk ? ` · projected slip to ${task.projected_finish_date}` : "";
  const title = `Computed status (lifecycle: ${lifecycle || "—"})${projNote}${isProvisional ? " · provisional from open daily report" : ""}`;
  return (
    <span title={title} data-testid={`task-computed-${task.activity_code}`} className={`pill ${klass} inline-flex items-center gap-1`}>
      {label}
      {isProvisional && <span className="inline-block w-1.5 h-1.5 rounded-full bg-blue-500" />}
    </span>
  );
}

// Bucket C1 Phase 3 — Blocker count with click popover listing the open
// delays / RFIs / submittals / field-issues linked to this task.
// open_blocker_summary is denormalized server-side
// (services/task_status.count_open_blockers).
//
// Bug #50 (2026-05-13): aggregation extended to RFIs via linked_task_ids
// and SLA-breached submittals. Bug #53 (2026-05-13) narrowed Bug #50:
// QC issues and safety events are NOT blockers (Sid's call) -- they're
// internal quality / safety flags. The linkage stays, but they don't
// appear in the count badge or tooltip. If a backend row still carries
// a stale 'qc_issue' / 'safety_event' summary entry from before Bug #53
// rolled out, the count-letter logic below silently ignores it; the
// next compute_and_persist_status pass will rewrite the summary.
//
// Mini-letters in the count badge: d (delay), r (rfi), s (submittal),
// w (work_item). Severity-to-color reuses the red/slate scheme;
// submittal rows (always 'critical' or 'high' per the server) render red.
function BlockerCell({ task, open, onToggle }) {
  const summary = task.open_blocker_summary || [];
  const count = task.open_blocker_count || 0;
  // Hide the Copilot FAB while this blocker bottom-sheet is open on phones so
  // it doesn't clip the sheet corner (same z-trap as the shared BlockerPopover).
  // Hook must run before the early return below (Rules of Hooks).
  useEffect(() => {
    document.body.classList.toggle("blocker-sheet-open", open);
    return () => document.body.classList.remove("blocker-sheet-open");
  }, [open]);
  if (!count) return <span className="text-slate-300 text-xs">—</span>;
  const counts = summary.reduce((acc, s) => {
    acc[s.entity_type] = (acc[s.entity_type] || 0) + 1;
    return acc;
  }, {});
  return (
    <div className="relative inline-block">
      <button
        data-testid={`task-blockers-${task.activity_code}`}
        onClick={onToggle}
        className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border border-red-200 bg-red-50 text-red-700 hover:bg-red-100"
      >
        <AlertTriangle className="h-3 w-3" /> {count}
        <span className="text-xs text-red-500/70">
          {[
            counts.delay && `${counts.delay}d`,
            counts.rfi && `${counts.rfi}r`,
            // Bug #53: qc_issue + safety_event removed from blocker
            // aggregation. Letters dropped to match.
            counts.submittal && `${counts.submittal}s`,
            counts.work_item && `${counts.work_item}w`,
          ].filter(Boolean).join(" ")}
        </span>
      </button>
      {open && (
        <div
          data-testid={`task-blockers-popover-${task.activity_code}`}
          className="absolute z-20 right-0 mt-1 w-72 bg-white border border-slate-200 rounded shadow-lg p-2 text-xs max-md:fixed max-md:inset-x-3 max-md:bottom-3 max-md:top-auto max-md:w-auto max-md:z-50 max-md:max-h-[55vh] max-md:overflow-y-auto max-md:shadow-2xl"
        >
          <div className="font-medium mb-1 text-slate-700">Open blockers ({count})</div>
          <div className="space-y-1 max-h-56 overflow-y-auto">
            {summary
              // Bug #53 (2026-05-13): filter out any qc_issue /
              // safety_event entries that may still be cached on a
              // task row from before Bug #53 rolled out. The next
              // compute_and_persist_status pass will rewrite the
              // summary without them; until then we hide them here
              // so the UI matches the new policy immediately.
              .filter((s) => s.entity_type !== "qc_issue" && s.entity_type !== "safety_event")
              .map((s, i) => {
              const Icon = s.entity_type === "delay" ? AlertTriangle
                : s.entity_type === "rfi" ? MessageSquare
                : s.entity_type === "submittal" ? FileText
                : s.entity_type === "work_item" ? AlertCircle
                : AlertCircle;
              const sevColor = s.severity === "critical" || s.severity === "major" || s.severity === "high"
                ? "text-red-600" : "text-slate-500";
              const link = s.entity_type === "delay" ? `/delays/${s.entity_id}`
                : s.entity_type === "rfi" ? `/rfis`
                : s.entity_type === "submittal" ? `/submittals`
                : s.entity_type === "work_item" ? `/work-items`
                : null;
              const inner = (
                <div className="flex items-start gap-1.5 py-1 border-b border-slate-100 last:border-0">
                  <Icon className={`h-3 w-3 mt-0.5 ${sevColor} shrink-0`} />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-slate-500">{s.entity_type.replace(/_/g, " ")} · {s.severity || "—"}</div>
                    <div className="truncate text-slate-800">{s.label || s.entity_id?.slice(0, 8)}</div>
                  </div>
                </div>
              );
              return link ? (
                <Link key={i} to={link} className="block hover:bg-slate-50 -mx-1 px-1 rounded">{inner}</Link>
              ) : (
                <div key={i}>{inner}</div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}


// =====================================================================
// Bucket G Phase 4 -- schedule-import AI suggestions banner.
//
// Surfaces a sticky toast-style banner when the project has pending
// AI trade-inference suggestions (entity_type=schedule_activity,
// source=ai_schedule_analyzer). Two affordances:
//   - "Review all" routes to the Review Queue inside the Approvals
//     page with the filters pre-applied.
//   - "Bulk accept defaults" hits the bulk-confirm endpoint with
//     up to 50 ids at a time, chunking until the queue is empty.
//
// Dismissable per-session (sessionStorage). Re-polls on tab focus.
// =====================================================================
const TASKS_SUGGESTIONS_DISMISS_KEY = "ice.tasks.ai_suggestions.dismissed";

function ScheduleAiSuggestionsBanner({ projectId }) {
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(() => {
    try { return sessionStorage.getItem(TASKS_SUGGESTIONS_DISMISS_KEY) === "1"; }
    catch { return false; }
  });

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      // Hit the per-user pending feed with explicit filters so we
      // only see the schedule-activity rows that ai_schedule_analyzer
      // produced. The endpoint server-side filters by role+status;
      // we then client-filter by source since the server endpoint
      // doesn't expose a source-filter param.
      const { data } = await api.get("/users/current/pending-suggestions", {
        params: {
          project_id: projectId,
          entity_type: "schedule_activity",
          limit: 200,
        },
      });
      const list = (data?.rows || [])
        .filter((r) => r.suggested_by === "ai_schedule_analyzer");
      setRows(list);
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    load();
    const onVis = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [load]);

  const reviewAll = () => {
    // The Approvals page reads the active tab from local state; we
    // deep-link via a query param so it lands on the Pending
    // suggestions tab with the entity_type filter pre-applied. The
    // Phase 5 polish could wire URL state directly into Approvals.
    navigate(
      "/approvals?tab=suggestions&entity_type=schedule_activity"
      + `&source=ai_schedule_analyzer&project=${projectId}`,
    );
  };

  const bulkAcceptDefaults = async () => {
    if (busy || rows.length === 0) return;
    setBusy(true);
    let chunk = 0;
    let confirmed = 0;
    let failed = 0;
    try {
      // Chunked at 50 per call (matches backend BULK_CAP). Stop on
      // first hard error so the user can investigate.
      const remaining = [...rows];
      while (remaining.length) {
        const ids = remaining.splice(0, 50).map((r) => r.id);
        chunk += 1;
        const { data } = await api.post("/field-suggestions/bulk-confirm", {
          suggestion_ids: ids,
        });
        confirmed += (data?.success_count || 0);
        failed += (data?.failure_count || 0);
      }
      if (confirmed > 0) {
        toast.success(
          `Confirmed ${confirmed} AI suggestion(s) across ${chunk} chunk(s).`
          + (failed > 0 ? ` ${failed} skipped (role / race).` : ""),
        );
      } else if (failed > 0) {
        toast.info(`${failed} suggestion(s) skipped (role / race / not-found).`);
      }
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Bulk accept failed");
    } finally {
      setBusy(false);
    }
  };

  const dismiss = () => {
    try { sessionStorage.setItem(TASKS_SUGGESTIONS_DISMISS_KEY, "1"); }
    catch { /* sessionStorage unavailable -- no-op */ }
    setDismissed(true);
  };

  if (loading && rows.length === 0) return null;
  if (dismissed) return null;
  if (rows.length === 0) return null;

  return (
    <div
      data-testid="tasks-ai-suggestions-banner"
      className="bg-blue-50 border border-blue-300 rounded p-3 text-sm text-blue-900 flex items-start gap-2"
    >
      <Sparkles className="h-4 w-4 mt-0.5 shrink-0 text-blue-700" />
      <div className="flex-1">
        <div className="font-medium">
          {rows.length} schedule activit{rows.length === 1 ? "y has" : "ies have"} AI
          trade/headcount suggestions.
        </div>
        <div className="text-xs mt-0.5">
          Confidence 70% across the rule-based analyzer. Review each one or
          accept the defaults in one click -- you save roughly{" "}
          {Math.ceil(rows.length * 0.6)} minute(s) vs. per-row review.
        </div>
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <button
            data-testid="tasks-ai-banner-review"
            onClick={reviewAll}
            className="h-8 max-md:min-h-[44px] px-3 rounded bg-white border border-blue-300 text-blue-900 text-xs hover:bg-blue-100"
          >
            Review all
          </button>
          <button
            data-testid="tasks-ai-banner-bulk-accept"
            onClick={bulkAcceptDefaults}
            disabled={busy}
            className="h-8 max-md:min-h-[44px] px-3 rounded bg-emerald-600 text-white text-xs inline-flex items-center gap-1 hover:bg-emerald-700 disabled:opacity-50"
          >
            {busy
              ? <Loader2 className="h-3 w-3 animate-spin" />
              : <Check className="h-3 w-3" />}
            Bulk accept defaults
          </button>
          <button
            data-testid="tasks-ai-banner-refresh"
            onClick={load}
            disabled={busy || loading}
            className="h-8 max-md:min-h-[44px] px-2 rounded border border-blue-300 bg-white text-blue-700 text-xs"
            title="Re-check"
          >
            <Loader2 className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>
      <button
        data-testid="tasks-ai-banner-dismiss"
        onClick={dismiss}
        title="Hide until next session"
        className="text-blue-700 hover:text-blue-900 p-1"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
