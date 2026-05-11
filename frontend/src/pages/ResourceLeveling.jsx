// Bucket H Phase 3 (2026-05-08) -- Resource Leveling dashboard.
//
// Three tabs: Heat Map (trades x dates), Gap List (severity-sorted),
// Crew Utilization (per-crew rollup).
//
// Production-ready: empty / loading / error states, mobile responsive,
// first-run guided flow, over-commit warnings on reallocation commit.
//
// Per-project RBAC handled server-side -- this page just renders
// whatever the user can see.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Users, RefreshCw, Plus, AlertTriangle, AlertCircle, CheckCircle2,
  Calendar, Filter, ChevronRight, X, Info, ShieldCheck, FileBarChart,
} from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { useUrlState } from "../lib/useUrlState";

// ---------- color tones ---------------------------------------------
const SEVERITY_CELL = {
  none:     "bg-emerald-50 text-emerald-800 border-emerald-200",
  surplus:  "bg-sky-50 text-sky-800 border-sky-200",
  minor:    "bg-amber-50 text-amber-800 border-amber-200",
  moderate: "bg-orange-100 text-orange-900 border-orange-300",
  major:    "bg-rose-100 text-rose-900 border-rose-300",
};
const SEVERITY_CELL_CP = {
  // CP cells get a thicker, darker border per spec.
  minor:    "bg-amber-50 text-amber-900 border-2 border-amber-500",
  moderate: "bg-orange-100 text-orange-900 border-2 border-orange-600",
  major:    "bg-rose-100 text-rose-900 border-2 border-rose-700",
};
const SEVERITY_PILL = {
  none:     "bg-emerald-100 text-emerald-800",
  surplus:  "bg-sky-100 text-sky-800",
  minor:    "bg-amber-200 text-amber-900",
  moderate: "bg-orange-200 text-orange-900",
  major:    "bg-rose-200 text-rose-900",
};

const TABS = [
  { key: "heatmap", label: "Heat map", icon: Calendar },
  { key: "gaps",    label: "Gap list", icon: AlertTriangle },
  { key: "crews",   label: "Crew utilization", icon: Users },
];

// ---------- root component ------------------------------------------
export default function ResourceLeveling() {
  // URL state mirrors filters so a deep-link to a heat-map view is
  // shareable. days is a number stored as string in the URL.
  const [filters, setFilters] = useUrlState({
    tab: "heatmap",
    days: "14",
    trade: "",          // CSV
    project_id: "",     // CSV
  });

  const days = useMemo(() => {
    const n = parseInt(filters.days, 10);
    return Number.isFinite(n) && n >= 1 && n <= 180 ? n : 14;
  }, [filters.days]);

  const [dashboard, setDashboard] = useState(null);    // {trades, dates, cells, summary}
  const [gaps, setGaps] = useState(null);              // {total, rows}
  const [crews, setCrews] = useState(null);            // [{crew, summary}]
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [recomputing, setRecomputing] = useState(false);

  // First-run flow state: derived from presence of crews + tasks data
  // on the tenant. We probe via /resources/crews + /projects.
  const [firstRun, setFirstRun] = useState({
    checked: false, hasCrews: false, hasProjects: false, hasComputed: false,
  });

  // Modal states.
  const [drillCell, setDrillCell] = useState(null);
  const [crewCalendar, setCrewCalendar] = useState(null);   // {crew, days}
  const [crewCalendarOpen, setCrewCalendarOpen] = useState(false);
  const [reallocOpen, setReallocOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [overCommitWarning, setOverCommitWarning] = useState(null);

  // ---- Load dashboard / gaps / crews -----
  const loadAll = async () => {
    setLoading(true); setError(null);
    const params = { date_range_start: todayISO(),
                      date_range_end: futureISO(days) };
    if (filters.trade) params.trade = filters.trade;
    if (filters.project_id) params.project_id = filters.project_id;
    try {
      // Phase 4 fix #1 -- crew utilization rollup served by the backend
      // (real numbers: avg_utilization_pct, over_committed_days, etc.).
      const [d, g, u] = await Promise.all([
        api.get(`/resource-leveling/dashboard`, { params }),
        api.get(`/resource-leveling/gaps`, { params: { ...params, limit: 100 } }),
        api.get(`/resource-leveling/crew-utilization-rollup`, {
          params: { date_range_start: params.date_range_start,
                     date_range_end: params.date_range_end },
        }),
      ]);
      setDashboard(d.data);
      setGaps(g.data);
      setCrews(Array.isArray(u.data) ? u.data : []);
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "Load failed");
      // Don't blow away partial data per spec Phase 3.3 error spec.
    } finally { setLoading(false); }
  };

  // Probe first-run state once.
  const probeFirstRun = async () => {
    try {
      const [crewsRes, projectsRes] = await Promise.all([
        api.get(`/resources/crews`),
        api.get(`/projects`),
      ]);
      const crewCount = Array.isArray(crewsRes.data) ? crewsRes.data.length : 0;
      const projCount = Array.isArray(projectsRes.data) ? projectsRes.data.length : 0;
      setFirstRun({
        checked: true,
        hasCrews: crewCount > 0,
        hasProjects: projCount > 0,
        hasComputed: false,    // updated after first /dashboard load
      });
    } catch {
      setFirstRun({ checked: true, hasCrews: false, hasProjects: false, hasComputed: false });
    }
  };

  useEffect(() => { probeFirstRun(); }, []);
  useEffect(() => { loadAll(); /* eslint-disable-next-line */ }, [days, filters.trade, filters.project_id]);

  // Update firstRun.hasComputed once we get the dashboard payload.
  useEffect(() => {
    if (dashboard && dashboard.summary?.total_cells > 0 && firstRun.checked) {
      setFirstRun((p) => ({ ...p, hasComputed: true }));
    }
  }, [dashboard, firstRun.checked]);

  const showFirstRun = firstRun.checked && (!firstRun.hasCrews || !firstRun.hasProjects);

  // ---- Recompute trigger -----
  const onRecompute = async () => {
    if (recomputing) return;
    setRecomputing(true);
    try {
      const r = await api.post(`/resource-leveling/recompute`, {
        date_range_start: todayISO(),
        date_range_end: futureISO(days),
      });
      if (r.data?.status === "scheduled") {
        toast.info("Recompute scheduled in the background. Refresh in ~30s.");
      } else {
        toast.success("Recompute complete.");
      }
      await loadAll();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Recompute failed");
    } finally { setRecomputing(false); }
  };

  // ---- Cell click -> drill-down modal -----
  const onCellClick = async (trade, dateIso, cell) => {
    setDrillCell({ trade, date: dateIso, ...cell, loading: true });
    try {
      // Pull suggestions for this gap.
      const r = await api.get(
        `/resource-leveling/suggestions/${encodeURIComponent(cell.id)}`,
      );
      setDrillCell({ trade, date: dateIso, ...cell, loading: false,
                     suggestions: r.data?.suggestions || [],
                     gap: r.data?.gap });
    } catch (e) {
      setDrillCell({ trade, date: dateIso, ...cell, loading: false,
                     error: e.response?.data?.detail || "Couldn't load suggestions" });
    }
  };

  const onCrewRowClick = async (crewId) => {
    try {
      const r = await api.get(
        `/resource-leveling/crews/${crewId}/calendar`,
        { params: { date_range_start: todayISO(),
                     date_range_end: futureISO(days) } },
      );
      setCrewCalendar(r.data);
      setCrewCalendarOpen(true);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Couldn't load crew calendar");
    }
  };

  // ---- Render -----
  return (
    <div data-testid="resource-leveling-page" className="p-4 sm:p-6 max-w-[1400px] mx-auto">
      {/* Header */}
      <header className="mb-4">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-2">
            <Users className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800">Resource Leveling</h1>
            <button data-testid="rl-help-btn" onClick={() => setHelpOpen(true)}
              className="ml-2 text-slate-400 hover:text-slate-700"
              title="How to read this dashboard">
              <Info className="h-4 w-4" />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <Link to="/resources/crews"
              data-testid="rl-add-crew"
              className="h-8 px-3 rounded bg-slate-200 hover:bg-slate-300 text-slate-700 text-sm inline-flex items-center gap-1">
              <Plus className="h-3.5 w-3.5" /> Add crew
            </Link>
            <button data-testid="rl-refresh" onClick={onRecompute} disabled={recomputing}
              className="h-8 px-3 rounded bg-blue-600 hover:bg-blue-700 text-white text-sm disabled:opacity-50 inline-flex items-center gap-1">
              <RefreshCw className={`h-3.5 w-3.5 ${recomputing ? "animate-spin" : ""}`} />
              {recomputing ? "Recomputing…" : "Refresh"}
            </button>
          </div>
        </div>

        {/* Filter row */}
        <div className="flex flex-wrap items-end gap-3 p-3 bg-slate-50 border border-slate-200 rounded">
          <Filter className="h-4 w-4 text-slate-500 mb-2" />
          <div>
            <label className="text-[10px] uppercase text-slate-500 block">Window (days)</label>
            <select data-testid="rl-filter-days" value={String(days)}
              onChange={(e) => setFilters({ ...filters, days: e.target.value })}
              className="h-8 px-2 rounded border border-slate-300 text-sm bg-white">
              <option value="7">7</option>
              <option value="14">14</option>
              <option value="30">30</option>
              <option value="60">60</option>
              <option value="90">90</option>
              <option value="180">180</option>
            </select>
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500 block">Trade (csv)</label>
            <input data-testid="rl-filter-trade" value={filters.trade}
              onChange={(e) => setFilters({ ...filters, trade: e.target.value })}
              placeholder="Plumber,Electrician"
              className="h-8 px-2 rounded border border-slate-300 text-sm w-48" />
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500 block">Project id (csv)</label>
            <input data-testid="rl-filter-project" value={filters.project_id}
              onChange={(e) => setFilters({ ...filters, project_id: e.target.value })}
              placeholder="project id"
              className="h-8 px-2 rounded border border-slate-300 text-sm w-48" />
          </div>
          {(filters.trade || filters.project_id) && (
            <button onClick={() => setFilters({ ...filters, trade: "", project_id: "" })}
              data-testid="rl-filter-clear"
              className="h-8 px-3 rounded bg-slate-200 text-slate-700 text-sm hover:bg-slate-300">
              Clear
            </button>
          )}
        </div>
      </header>

      {/* First-run guided flow */}
      {showFirstRun && (
        <div data-testid="rl-first-run" className="mb-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
          <FirstRunCard
            step="1" title="Add crews" done={firstRun.hasCrews}
            cta="Open Resources" to="/resources/crews"
            subtitle="Define your crews and their default headcount." />
          <FirstRunCard
            step="2" title="Import a schedule" done={firstRun.hasProjects}
            cta="Open Imports" to="/imports"
            subtitle="Upload your master schedule so demand can be computed." />
          <FirstRunCard
            step="3" title="Compute leveling"
            done={firstRun.hasComputed}
            cta="Click Refresh" to={null}
            subtitle="Click Refresh in the header to compute supply, demand, and gaps." />
        </div>
      )}

      {/* Error banner (non-blocking) */}
      {error && (
        <div data-testid="rl-error" className="mb-3 p-3 bg-rose-50 border border-rose-200 rounded text-sm flex items-center gap-2">
          <AlertCircle className="h-4 w-4 text-rose-600 shrink-0" />
          <div className="flex-1 text-rose-900">{error}</div>
          <button onClick={loadAll}
            className="h-7 px-3 rounded bg-rose-600 text-white text-xs hover:bg-rose-700">
            Retry
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-slate-200 mb-3">
        {TABS.map((t) => {
          const Icon = t.icon;
          const active = filters.tab === t.key;
          return (
            <button key={t.key} data-testid={`rl-tab-${t.key}`}
              onClick={() => setFilters({ ...filters, tab: t.key })}
              className={`h-9 px-3 text-sm inline-flex items-center gap-1.5 border-b-2 -mb-px transition-colors ${
                active ? "border-blue-600 text-blue-700 font-medium"
                       : "border-transparent text-slate-600 hover:text-slate-900"
              }`}>
              <Icon className="h-3.5 w-3.5" /> {t.label}
            </button>
          );
        })}
        {dashboard?.summary && (
          <div className="ml-auto self-end pb-2 text-xs text-slate-500 flex items-center gap-3">
            <span data-testid="rl-summary-cells">{dashboard.summary.total_cells} cells</span>
            <span className="text-rose-700">{dashboard.summary.shortage_cells} shortage</span>
            <span className="text-sky-700">{dashboard.summary.surplus_cells} surplus</span>
            {dashboard.summary.cp_shortage_cells > 0 && (
              <span className="text-rose-700 font-semibold">
                {dashboard.summary.cp_shortage_cells} CP shortage
              </span>
            )}
          </div>
        )}
      </div>

      {/* Tab content */}
      {loading && !dashboard ? (
        <div data-testid="rl-loading" className="space-y-2">
          <SkeletonBlock height="2.5rem" />
          <SkeletonBlock height="14rem" />
        </div>
      ) : (filters.tab === "heatmap" ? (
        <HeatMapTab dashboard={dashboard} onCellClick={onCellClick}
          showFirstRun={showFirstRun} loading={loading} />
      ) : filters.tab === "gaps" ? (
        <GapListTab gaps={gaps} onGapClick={(g) => onCellClick(g.trade, g.date, g)}
          loading={loading} />
      ) : (
        <CrewUtilTab crews={crews} onCrewClick={onCrewRowClick}
          onReallocate={() => setReallocOpen(true)}
          loading={loading} />
      ))}

      {/* Drill-down modal (heat-map cell or gap row click) */}
      <Dialog open={!!drillCell} onOpenChange={(o) => !o && setDrillCell(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{drillCell?.trade} · {drillCell?.date}</DialogTitle>
          </DialogHeader>
          {drillCell && (
            <DrillDown cell={drillCell} onReallocate={() => {
              setReallocOpen(true);
              // Don't auto-close; user may want to read suggestions first.
            }} />
          )}
        </DialogContent>
      </Dialog>

      {/* Crew calendar modal */}
      <Dialog open={crewCalendarOpen} onOpenChange={setCrewCalendarOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{crewCalendar?.crew?.name || "Crew calendar"}</DialogTitle>
          </DialogHeader>
          {crewCalendar && <CrewCalendar data={crewCalendar} />}
        </DialogContent>
      </Dialog>

      {/* Reallocation modal */}
      <Dialog open={reallocOpen} onOpenChange={setReallocOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Commit reallocation</DialogTitle></DialogHeader>
          <ReallocationForm onClose={() => setReallocOpen(false)}
            onCommitted={(result) => {
              if (result?.over_commits?.length) {
                setOverCommitWarning(result);
              }
              loadAll();
            }} />
        </DialogContent>
      </Dialog>

      {/* Over-commit warning banner -- floats above dashboard until dismissed */}
      {overCommitWarning && (
        <OverCommitBanner result={overCommitWarning}
          onDismiss={() => setOverCommitWarning(null)} />
      )}

      {/* Help side panel */}
      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>How to read this dashboard</DialogTitle></DialogHeader>
          <HelpContent />
        </DialogContent>
      </Dialog>
    </div>
  );
}

// =====================================================================
// First-run guided card
// =====================================================================

function FirstRunCard({ step, title, subtitle, done, cta, to }) {
  return (
    <div data-testid={`rl-firstrun-step-${step}`}
      className={`p-3 rounded border ${
        done ? "bg-emerald-50 border-emerald-300" : "bg-white border-slate-300"
      }`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`w-6 h-6 rounded-full grid place-items-center text-xs font-bold ${
          done ? "bg-emerald-500 text-white" : "bg-slate-300 text-slate-700"
        }`}>
          {done ? <CheckCircle2 className="h-4 w-4" /> : step}
        </span>
        <div className="font-medium text-sm">{title}</div>
      </div>
      <div className="text-xs text-slate-600 mb-2">{subtitle}</div>
      {!done && to && (
        <Link to={to} className="text-xs text-blue-600 hover:underline inline-flex items-center gap-1">
          {cta} <ChevronRight className="h-3 w-3" />
        </Link>
      )}
      {!done && !to && (
        <div className="text-xs text-slate-500 italic">{cta}</div>
      )}
    </div>
  );
}

// =====================================================================
// Heat Map tab
// =====================================================================

function HeatMapTab({ dashboard, onCellClick, showFirstRun, loading }) {
  if (!dashboard) return null;
  const { trades, dates, cells } = dashboard;
  if (!trades.length || !dates.length) {
    return (
      <EmptyState testid="rl-empty-heatmap"
        title="No resource data yet"
        message={showFirstRun
          ? "Complete the steps above to start computing supply, demand, and gaps."
          : "No supply or demand in the selected window. Try widening the window or adding more crews/scheduled tasks."}
      />
    );
  }
  return (
    <>
      {/* Desktop heat map (horizontal scroll) */}
      <div data-testid="rl-heatmap-desktop" className="hidden sm:block overflow-x-auto bg-white border border-slate-200 rounded">
        <table className="text-xs min-w-full">
          <thead className="bg-slate-50">
            <tr>
              <th className="text-left p-2 sticky left-0 bg-slate-50 z-10 font-medium border-r border-slate-200">Trade</th>
              {dates.map((d) => (
                <th key={d} className="p-1 text-center font-mono text-[10px] text-slate-500 whitespace-nowrap">
                  {d.slice(5)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t} className="border-t border-slate-100">
                <td className="p-2 sticky left-0 bg-white border-r border-slate-200 font-medium text-slate-700 whitespace-nowrap">{t}</td>
                {dates.map((d) => {
                  const cell = (cells[t] || {})[d];
                  if (!cell) return <td key={d} className="p-0.5"><div className="h-7 w-12 rounded bg-slate-50 border border-slate-100" /></td>;
                  const isCP = cell.is_critical_path && ["minor","moderate","major"].includes(cell.severity);
                  const cls = isCP ? SEVERITY_CELL_CP[cell.severity] : SEVERITY_CELL[cell.severity];
                  return (
                    <td key={d} className="p-0.5">
                      <button
                        data-testid={`rl-cell-${t}-${d}`}
                        onClick={() => onCellClick(t, d, cell)}
                        title={`${t} · ${d}\nsupply ${cell.supply} / demand ${cell.demand} / gap ${cell.gap}${cell.is_critical_path ? " · on CP" : ""}`}
                        className={`h-7 w-12 rounded border ${cls} hover:ring-2 hover:ring-blue-400 transition`}>
                        <span className="block text-[10px] leading-tight">{cell.supply}/{cell.demand}</span>
                        <span className="block text-[8px] leading-tight">g{cell.gap >= 0 ? "+" : ""}{cell.gap}</span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile card-list */}
      <div data-testid="rl-heatmap-mobile" className="sm:hidden space-y-2">
        {trades.map((t) => (
          <details key={t} className="bg-white border border-slate-200 rounded">
            <summary className="p-3 font-medium text-sm cursor-pointer flex items-center justify-between">
              <span>{t}</span>
              <ChevronRight className="h-4 w-4" />
            </summary>
            <div className="border-t border-slate-100 p-2 grid grid-cols-3 gap-1">
              {dates.map((d) => {
                const cell = (cells[t] || {})[d];
                if (!cell) return null;
                const isCP = cell.is_critical_path && ["minor","moderate","major"].includes(cell.severity);
                const cls = isCP ? SEVERITY_CELL_CP[cell.severity] : SEVERITY_CELL[cell.severity];
                return (
                  <button key={d}
                    onClick={() => onCellClick(t, d, cell)}
                    className={`p-1 rounded border ${cls} text-left`}>
                    <div className="text-[10px] font-mono text-slate-600">{d.slice(5)}</div>
                    <div className="text-[10px]">s{cell.supply}/d{cell.demand}/g{cell.gap}</div>
                  </button>
                );
              })}
            </div>
          </details>
        ))}
      </div>
    </>
  );
}

// =====================================================================
// Gap List tab
// =====================================================================

function GapListTab({ gaps, onGapClick, loading }) {
  if (!gaps) return null;
  if (!gaps.rows || !gaps.rows.length) {
    return (
      <EmptyState testid="rl-empty-gaps"
        title="No open shortages"
        message="Either you have enough crew capacity for the selected window, or no demand has been declared yet." />
    );
  }
  return (
    <div data-testid="rl-gap-list" className="bg-white border border-slate-200 rounded overflow-x-auto">
      <table className="text-sm w-full">
        <thead className="bg-slate-50 border-b border-slate-200">
          <tr>
            <th className="p-2 text-left">Date</th>
            <th className="p-2 text-left">Trade</th>
            <th className="p-2 text-right">Demand</th>
            <th className="p-2 text-right">Supply</th>
            <th className="p-2 text-right">Gap</th>
            <th className="p-2 text-left">Severity</th>
            <th className="p-2 text-left">CP?</th>
            <th className="p-2 text-left">Projects</th>
            <th className="p-2"></th>
          </tr>
        </thead>
        <tbody>
          {gaps.rows.map((r) => (
            <tr key={r.id} data-testid={`rl-gap-${r.id}`} className="border-b border-slate-100 hover:bg-slate-50">
              <td className="p-2 font-mono text-xs">{r.date}</td>
              <td className="p-2 font-medium">{r.trade}</td>
              <td className="p-2 text-right">{r.demand}</td>
              <td className="p-2 text-right">{r.supply}</td>
              <td className="p-2 text-right font-mono">
                <span className={r.gap > 0 ? "text-rose-700 font-semibold" : "text-emerald-700"}>
                  {r.gap >= 0 ? "+" : ""}{r.gap}
                </span>
              </td>
              <td className="p-2">
                <span className={`pill ${SEVERITY_PILL[r.severity]} capitalize text-xs px-2 py-0.5 rounded`}>
                  {r.severity}
                </span>
              </td>
              <td className="p-2">
                {r.is_critical_path && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-100 text-rose-800 font-mono">CP</span>
                )}
              </td>
              <td className="p-2 text-xs text-slate-600">
                {(r.contributing_project_ids || []).length || "—"}
              </td>
              <td className="p-2 text-right">
                <button onClick={() => onGapClick(r)}
                  data-testid={`rl-gap-action-${r.id}`}
                  className="h-7 px-2 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
                  Suggest fix
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="p-2 text-xs text-slate-500 border-t border-slate-100">
        Showing {gaps.rows.length} of {gaps.total} gap{gaps.total === 1 ? "" : "s"}.
      </div>
    </div>
  );
}

// =====================================================================
// Crew Utilization tab
// =====================================================================

function CrewUtilTab({ crews, onCrewClick, onReallocate, loading }) {
  if (!crews || !crews.length) {
    return (
      <EmptyState testid="rl-empty-crews"
        title="No crews defined"
        message="Add crews on the Resources page to populate utilization."
        cta={{ to: "/resources/crews", label: "Add crew" }} />
    );
  }
  return (
    <div data-testid="rl-crew-list" className="bg-white border border-slate-200 rounded overflow-x-auto">
      <div className="p-2 border-b border-slate-100 flex items-center justify-end">
        <button onClick={onReallocate}
          data-testid="rl-reallocate-trigger"
          className="h-7 px-3 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
          + Commit reallocation
        </button>
      </div>
      <table className="text-sm w-full">
        <thead className="bg-slate-50 border-b border-slate-200">
          <tr>
            <th className="p-2 text-left">Crew</th>
            <th className="p-2 text-left">Trade</th>
            <th className="p-2 text-left">Company</th>
            <th className="p-2 text-right">Headcount</th>
            <th className="p-2 text-right">Avg util %</th>
            <th className="p-2 text-right">Committed h</th>
            <th className="p-2 text-right">Available h</th>
            <th className="p-2 text-right">Over-committed days</th>
            <th className="p-2 text-right">Under-utilized days</th>
            <th className="p-2"></th>
          </tr>
        </thead>
        <tbody>
          {crews.map((row) => (
            <tr key={row.crew.id} data-testid={`rl-crew-${row.crew.id}`}
              className="border-b border-slate-100 hover:bg-slate-50 cursor-pointer"
              onClick={() => onCrewClick(row.crew.id)}>
              <td className="p-2 font-medium">{row.crew.name}</td>
              <td className="p-2 text-slate-600">{row.crew.trade}</td>
              <td className="p-2 text-slate-600">{row.crew.company_name || "—"}</td>
              <td className="p-2 text-right">{row.crew.default_headcount ?? row.crew.target_size}</td>
              <td className="p-2 text-right font-mono">
                <UtilizationCell pct={row.avg_utilization_pct} />
              </td>
              <td className="p-2 text-right font-mono text-xs">{row.total_committed_hours?.toFixed(0) ?? 0}</td>
              <td className="p-2 text-right font-mono text-xs">{row.total_available_hours?.toFixed(0) ?? 0}</td>
              <td className="p-2 text-right">
                {row.over_committed_days > 0
                  ? <span className="text-rose-700 font-semibold">{row.over_committed_days}</span>
                  : <span className="text-slate-400">0</span>}
              </td>
              <td className="p-2 text-right">
                {row.under_utilized_days > 0
                  ? <span className="text-amber-700">{row.under_utilized_days}</span>
                  : <span className="text-slate-400">0</span>}
              </td>
              <td className="p-2 text-right">
                <ChevronRight className="h-4 w-4 text-slate-400 inline" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UtilizationCell({ pct }) {
  const v = Number(pct || 0);
  let tone = "text-slate-600";
  if (v > 100) tone = "text-rose-700 font-semibold";
  else if (v > 85) tone = "text-amber-700 font-semibold";
  else if (v >= 50) tone = "text-emerald-700";
  else if (v > 0) tone = "text-amber-600";
  return <span className={tone}>{v.toFixed(0)}%</span>;
}

// =====================================================================
// Drill-down content (used by the heat-map cell modal)
// =====================================================================

function DrillDown({ cell, onReallocate }) {
  return (
    <div className="space-y-3 text-sm">
      <div className="grid grid-cols-3 gap-2">
        <Metric label="Supply" value={cell.supply} />
        <Metric label="Demand" value={cell.demand} />
        <Metric label="Gap" value={cell.gap >= 0 ? "+" + cell.gap : String(cell.gap)}
          tone={cell.gap > 0 ? "rose" : cell.gap < 0 ? "sky" : "emerald"} />
      </div>
      {cell.is_critical_path && (
        <div className="p-2 bg-rose-50 border border-rose-200 rounded text-xs text-rose-900 font-medium inline-flex items-center gap-1">
          <AlertTriangle className="h-3.5 w-3.5" />
          Demand sits on the critical path — resolve first.
        </div>
      )}
      {cell.contributing_project_ids?.length > 0 && (
        <div>
          <div className="text-[10px] uppercase text-slate-500 mb-1">Contributing projects</div>
          <div className="text-xs space-y-0.5">
            {cell.contributing_project_ids.map((pid) => (
              <div key={pid} className="font-mono text-slate-600">{pid.slice(0, 8)}…</div>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="text-[10px] uppercase text-slate-500 mb-1">Resolution suggestions</div>
        {cell.loading ? (
          <SkeletonBlock height="3rem" />
        ) : cell.error ? (
          <div className="text-xs text-rose-700">{cell.error}</div>
        ) : !cell.suggestions || !cell.suggestions.length ? (
          <div className="text-xs text-slate-500 italic">No automatic suggestions for this gap.</div>
        ) : (
          <div className="space-y-2">
            {cell.suggestions.map((s, i) => (
              <div key={i} data-testid={`rl-suggestion-${i}`}
                className={`p-2 rounded border text-xs ${
                  s.priority === "high"
                    ? "bg-rose-50 border-rose-200"
                    : "bg-slate-50 border-slate-200"
                }`}>
                <div className="font-medium mb-0.5">
                  <span className="text-[9px] uppercase tracking-wide mr-1 px-1 py-0.5 rounded bg-slate-200 text-slate-700">
                    {s.action_type.replace(/_/g, " ")}
                  </span>
                  {s.title}
                </div>
                <div className="text-slate-700">{s.rationale}</div>
                {(s.from_project_id || s.to_project_id) && (
                  <div className="mt-1 text-[10px] text-slate-500 font-mono">
                    {s.from_project_id ? `from ${s.from_project_id.slice(0, 8)}…` : ""}
                    {s.to_project_id ? ` → to ${s.to_project_id.slice(0, 8)}…` : ""}
                  </div>
                )}
              </div>
            ))}
            <button onClick={onReallocate}
              data-testid="rl-drill-reallocate"
              className="h-7 px-3 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
              Commit a reallocation
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// =====================================================================
// Crew calendar modal content
// =====================================================================

function CrewCalendar({ data }) {
  const { crew, days } = data;
  return (
    <div className="text-sm">
      <div className="mb-2 text-xs text-slate-500">
        <span className="font-mono">{crew.trade}</span>
        {crew.company_name && <span className="ml-2">· {crew.company_name}</span>}
      </div>
      <div className="bg-white border border-slate-200 rounded overflow-x-auto">
        <table className="text-xs w-full">
          <thead className="bg-slate-50">
            <tr>
              <th className="p-2 text-left">Date</th>
              <th className="p-2 text-right">Total</th>
              <th className="p-2 text-right">Committed</th>
              <th className="p-2 text-right">Available</th>
              <th className="p-2 text-left">Holiday?</th>
              <th className="p-2 text-left">Source</th>
            </tr>
          </thead>
          <tbody>
            {days.map((d) => (
              <tr key={d.date} className="border-b border-slate-100">
                <td className="p-2 font-mono">{d.date}</td>
                <td className="p-2 text-right">{d.total_headcount}</td>
                <td className="p-2 text-right">{d.committed_headcount}</td>
                <td className="p-2 text-right">{d.available_headcount}</td>
                <td className="p-2">{d.is_holiday ? "yes" : ""}</td>
                <td className="p-2 text-[10px] uppercase text-slate-500">{d.computed_from}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// =====================================================================
// Reallocation form
// =====================================================================

function ReallocationForm({ onClose, onCommitted }) {
  const [form, setForm] = useState({
    crew_id: "", from_project_id: "", to_project_id: "",
    date_range_start: todayISO(), date_range_end: futureISO(1),
    headcount: 1, reason: "",
  });
  const [crews, setCrews] = useState([]);
  const [projects, setProjects] = useState([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.get(`/resources/crews`).then((r) => setCrews(Array.isArray(r.data) ? r.data : []));
    api.get(`/projects`).then((r) => setProjects(Array.isArray(r.data) ? r.data : []));
  }, []);
  const submit = async () => {
    if (busy) return;
    if (!form.crew_id || !form.to_project_id) {
      toast.error("Crew and target project are required"); return;
    }
    if (form.headcount < 1) {
      toast.error("Headcount must be at least 1"); return;
    }
    // Bucket H Phase 5 (5.6) -- confirmation before commit.
    // Reallocation writes to crew_capacity audit-logged; review the
    // summary before sending. Browser-native confirm is enough for
    // this destructive flow; over-commits surface in a separate banner.
    const crewName = crews.find((c) => c.id === form.crew_id)?.name || "this crew";
    const toName = projects.find((p) => p.id === form.to_project_id)?.name || "the target project";
    const fromName = form.from_project_id
      ? (projects.find((p) => p.id === form.from_project_id)?.name || "another project")
      : "the unassigned pool";
    const dayCount = (() => {
      try {
        const a = new Date(form.date_range_start);
        const b = new Date(form.date_range_end);
        return Math.max(1, Math.round((b - a) / 86400000) + 1);
      } catch { return 1; }
    })();
    const msg = `Reallocate ${form.headcount} from "${crewName}" to "${toName}" for ${dayCount} day${dayCount === 1 ? "" : "s"} (from "${fromName}")?\n\nThis writes to the audit log and updates committed headcount on each affected date.`;
    if (!window.confirm(msg)) return;
    setBusy(true);
    try {
      const r = await api.post(`/resource-leveling/commit-reallocation`, {
        ...form,
        from_project_id: form.from_project_id || null,
        reason: form.reason || null,
      });
      toast.success("Reallocation committed");
      onCommitted?.(r.data);
      onClose();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Reallocation failed");
    } finally { setBusy(false); }
  };
  return (
    <div className="space-y-3 text-sm">
      <div>
        <label className="field-label">Crew *</label>
        <select data-testid="rl-realloc-crew" value={form.crew_id}
          onChange={(e) => setForm({ ...form, crew_id: e.target.value })}
          className="w-full h-9 px-2 rounded border border-slate-300 bg-white text-sm">
          <option value="">— select crew —</option>
          {crews.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.trade})</option>)}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="field-label">From project</label>
          <select data-testid="rl-realloc-from" value={form.from_project_id}
            onChange={(e) => setForm({ ...form, from_project_id: e.target.value })}
            className="w-full h-9 px-2 rounded border border-slate-300 bg-white text-sm">
            <option value="">(pool / unassigned)</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
        <div>
          <label className="field-label">To project *</label>
          <select data-testid="rl-realloc-to" value={form.to_project_id}
            onChange={(e) => setForm({ ...form, to_project_id: e.target.value })}
            className="w-full h-9 px-2 rounded border border-slate-300 bg-white text-sm">
            <option value="">— select target project —</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <div>
          <label className="field-label">Start date</label>
          <input type="date" value={form.date_range_start}
            onChange={(e) => setForm({ ...form, date_range_start: e.target.value })}
            className="w-full h-9 px-2 rounded border border-slate-300 text-sm" />
        </div>
        <div>
          <label className="field-label">End date</label>
          <input type="date" value={form.date_range_end}
            onChange={(e) => setForm({ ...form, date_range_end: e.target.value })}
            className="w-full h-9 px-2 rounded border border-slate-300 text-sm" />
        </div>
        <div>
          <label className="field-label">Headcount *</label>
          <input data-testid="rl-realloc-headcount" type="number" min="1"
            value={form.headcount}
            onChange={(e) => setForm({ ...form, headcount: parseInt(e.target.value, 10) || 0 })}
            className="w-full h-9 px-2 rounded border border-slate-300 text-sm" />
        </div>
      </div>
      <div>
        <label className="field-label">Reason</label>
        <input data-testid="rl-realloc-reason" value={form.reason}
          onChange={(e) => setForm({ ...form, reason: e.target.value })}
          placeholder="e.g. cover plumbing pull on critical path"
          className="w-full h-9 px-2 rounded border border-slate-300 text-sm" />
      </div>
      <div className="pt-2 border-t border-slate-100 flex justify-end gap-2">
        <button onClick={onClose}
          className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
          Cancel
        </button>
        <Button data-testid="rl-realloc-commit" onClick={submit} disabled={busy}
          className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50">
          {busy ? "Committing…" : "Commit"}
        </Button>
      </div>
    </div>
  );
}

// =====================================================================
// Over-commit warning banner (Phase 3 addition #2)
// =====================================================================

function OverCommitBanner({ result, onDismiss }) {
  return (
    <div data-testid="rl-overcommit-banner"
      className="fixed bottom-4 right-4 max-w-md p-4 rounded shadow-lg border-l-4 border-amber-500 bg-amber-50 text-amber-900 z-50">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-5 w-5 shrink-0 mt-0.5" />
        <div className="flex-1">
          <div className="font-semibold mb-1">
            Reallocation committed with over-commits
          </div>
          <div className="text-xs mb-2">
            Crew {result.crew_id?.slice(0, 8)}… is now over its total headcount on
            the following dates. Audit log captured the move; review supply and
            reduce committed headcount where possible.
          </div>
          <div className="space-y-1 text-xs font-mono">
            {(result.over_commits || []).slice(0, 8).map((oc) => (
              <div key={oc.date} className="px-2 py-1 bg-white/60 rounded">
                {oc.date}: total {oc.total} · would-be committed {oc.would_be_committed}
                {" "}({oc.would_be_committed - oc.total} over)
              </div>
            ))}
            {(result.over_commits || []).length > 8 && (
              <div className="text-amber-700">
                +{result.over_commits.length - 8} more
              </div>
            )}
          </div>
        </div>
        <button onClick={onDismiss}
          data-testid="rl-overcommit-dismiss"
          className="text-amber-900 hover:text-amber-700">
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

// =====================================================================
// Help panel content
// =====================================================================

function HelpContent() {
  return (
    <div className="text-sm space-y-3 text-slate-800">
      <p>
        <strong>Resource Leveling</strong> shows where you have enough crew
        capacity to do the work demanded by your schedules — and where you
        don't.
      </p>
      <div>
        <strong>Heat map</strong>: trade rows × date columns. Each cell shows
        supply/demand. Color codes:
      </div>
      <div className="grid grid-cols-2 gap-1 text-xs">
        <div className="p-1 bg-emerald-50 border border-emerald-200 rounded">balanced / no demand</div>
        <div className="p-1 bg-sky-50 border border-sky-200 rounded">surplus (supply &gt; demand)</div>
        <div className="p-1 bg-amber-50 border border-amber-200 rounded">minor shortage (&lt;20%)</div>
        <div className="p-1 bg-orange-100 border border-orange-300 rounded">moderate (20-50%)</div>
        <div className="p-1 bg-rose-100 border border-rose-300 rounded">major (&gt;50%)</div>
        <div className="p-1 bg-rose-100 border-2 border-rose-700 rounded">on critical path</div>
      </div>
      <div>
        <strong>Gap list</strong>: every shortage in the window, sorted by
        severity. Click "Suggest fix" to see resolution options.
      </div>
      <div>
        <strong>Crew utilization</strong>: per-crew rollup. Over-committed
        days mean the crew is booked beyond its headcount across projects.
      </div>
      <div>
        <strong>Permissions</strong>: admin sees all projects; PM sees
        projects they manage; super sees projects they're a member of with
        role pm/super/foreman; viewer / foreman are read-only.
      </div>
      <div>
        <strong>Auto-recompute</strong>: refresh kicks off a tenant-wide
        recompute. Schedule imports, crew CRUD, and CPM cascade also
        trigger background recomputes (Phase 4).
      </div>
    </div>
  );
}

// =====================================================================
// Bits and bobs
// =====================================================================

function Metric({ label, value, tone = "slate" }) {
  const tones = {
    slate: "bg-slate-50 border-slate-200 text-slate-800",
    rose: "bg-rose-50 border-rose-200 text-rose-900",
    sky: "bg-sky-50 border-sky-200 text-sky-900",
    emerald: "bg-emerald-50 border-emerald-200 text-emerald-900",
  };
  return (
    <div className={`p-2 rounded border ${tones[tone]} text-center`}>
      <div className="text-[10px] uppercase tracking-wide opacity-75">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function EmptyState({ testid, title, message, cta }) {
  return (
    <div data-testid={testid} className="p-8 bg-white border border-dashed border-slate-300 rounded text-center">
      <ShieldCheck className="h-8 w-8 text-slate-300 mx-auto mb-2" />
      <div className="font-medium text-slate-800 mb-1">{title}</div>
      <div className="text-xs text-slate-500 max-w-md mx-auto mb-3">{message}</div>
      {cta && (
        <Link to={cta.to}
          className="inline-flex items-center gap-1 h-8 px-3 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
          <Plus className="h-3.5 w-3.5" /> {cta.label}
        </Link>
      )}
    </div>
  );
}

function SkeletonBlock({ height = "1rem" }) {
  return (
    <div className="bg-slate-100 rounded animate-pulse" style={{ height }} />
  );
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

function futureISO(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

// Phase 4 fix #1 -- buildCrewUtilization placeholder removed. The
// real rollup comes from GET /resource-leveling/crew-utilization-
// rollup; see CrewUtilTab above for the row shape.
