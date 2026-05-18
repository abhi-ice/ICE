// Bucket C3c Phase 2 — RFI-to-CPM cascade dashboard. Surfaces every open
// gating entity (RFI/Submittal/etc.) with projected schedule slip at
// N=5/10/20 working days, sorted by worst critical-path impact.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, RefreshCw, ChevronRight } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

const SEVERITY_TONE = {
  green: "bg-emerald-50 border-l-4 border-emerald-400",
  yellow: "bg-amber-50 border-l-4 border-amber-400",
  orange: "bg-orange-50 border-l-4 border-orange-500",
  red: "bg-rose-50 border-l-4 border-rose-500",
};

const SEVERITY_PILL = {
  green: "bg-emerald-100 text-emerald-800",
  yellow: "bg-amber-100 text-amber-800",
  orange: "bg-orange-100 text-orange-800",
  red: "bg-rose-100 text-rose-800",
};

const SLA_TONE = {
  under_sla: "bg-emerald-100 text-emerald-700",
  at_sla: "bg-amber-100 text-amber-700",
  over_sla: "bg-rose-100 text-rose-700",
};

function fmtDays(n) {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "number" ? n : parseFloat(n);
  if (!Number.isFinite(v)) return "—";
  return v.toFixed(v % 1 === 0 ? 0 : 1);
}

export default function CascadeDashboard() {
  const { activeId } = useProjects();
  const [rows, setRows] = useState([]);
  const [lastComputed, setLastComputed] = useState(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // Bucket H Phase 4 -- projected gap side panel data.
  const [projectedGaps, setProjectedGaps] = useState({ topCells: [], summary: null });

  const load = async () => {
    if (!activeId) { setRows([]); return; }
    setLoading(true);
    try {
      const r = await api.get(`/projects/${activeId}/cascade-dashboard`);
      setRows(r.data?.rows || []);
      setLastComputed(r.data?.last_computed_at || null);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load cascade dashboard");
    } finally { setLoading(false); }
  };

  // Bucket H Phase 4 -- projected gap side panel. Pulls the resource-
  // leveling dashboard for the project + 30-day window and surfaces
  // the worst (severity, CP) cells. The cascade dashboard's central
  // claim is "this open delay will push tasks N days forward"; the
  // side panel answers "and those pushed tasks will produce these
  // resource shortages."
  const loadGaps = async () => {
    if (!activeId) { setProjectedGaps({ topCells: [], summary: null }); return; }
    try {
      const today = new Date();
      const end = new Date();
      end.setDate(today.getDate() + 30);
      const r = await api.get(`/resource-leveling/dashboard`, {
        params: {
          date_range_start: today.toISOString().slice(0, 10),
          date_range_end: end.toISOString().slice(0, 10),
          project_id: activeId,
        },
      });
      const cells = r.data?.cells || {};
      const sevRank = { major: 3, moderate: 2, minor: 1 };
      const flat = [];
      Object.entries(cells).forEach(([trade, byDate]) => {
        Object.entries(byDate).forEach(([dateIso, cell]) => {
          if (!["minor", "moderate", "major"].includes(cell.severity)) return;
          flat.push({ trade, date: dateIso, ...cell });
        });
      });
      flat.sort((a, b) => {
        if (a.is_critical_path !== b.is_critical_path) {
          return a.is_critical_path ? -1 : 1;
        }
        return (sevRank[b.severity] || 0) - (sevRank[a.severity] || 0);
      });
      setProjectedGaps({
        topCells: flat.slice(0, 8),
        summary: r.data?.summary || null,
      });
    } catch {
      setProjectedGaps({ topCells: [], summary: null });
    }
  };

  useEffect(() => { load(); loadGaps(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  const refresh = async () => {
    if (!activeId) return;
    setRefreshing(true);
    try {
      const r = await api.post(`/projects/${activeId}/cascade-dashboard/refresh`);
      if (r.data?.mode === "background") {
        toast.info(`Recomputing ${r.data.open_gates} gates in background — refresh again in a minute.`);
      } else {
        toast.success(`Recomputed ${r.data?.computed || 0} gates`);
      }
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Refresh failed");
    } finally { setRefreshing(false); }
  };

  if (!activeId) return <div className="p-6 text-sm text-slate-500">Pick a project to view cascade dashboard.</div>;

  return (
    <div className="p-6" data-testid="cascade-dashboard">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800">Open gates by projected critical-path impact</h1>
          </div>
          <div className="text-xs text-slate-500 mt-1">
            {lastComputed
              ? <>Last computed {new Date(lastComputed).toLocaleString()}</>
              : <>No snapshots yet — click Refresh to compute.</>}
          </div>
        </div>
        <button
          data-testid="cascade-refresh"
          onClick={refresh}
          disabled={refreshing}
          className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} />
          {refreshing ? "Recomputing…" : "Refresh"}
        </button>
      </header>

      {/* Bucket H Phase 4 -- projected gap side panel. Surfaces when
          the project has at least one shortage in the next 30 days so
          PMs reading the cascade view see the downstream resource
          impact alongside the CP-slip projection. */}
      {projectedGaps.topCells.length > 0 && (
        <div data-testid="cascade-projected-gap-panel"
          className="mb-4 p-3 rounded border border-amber-200 bg-amber-50">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm font-medium text-amber-900">
              Projected resource gaps (next 30 days)
            </div>
            <Link to={`/resource-leveling?project_id=${activeId}`}
              className="text-xs text-blue-600 hover:underline">
              Open Resource Leveling →
            </Link>
          </div>
          <div className="text-xs text-amber-900 mb-2">
            {projectedGaps.summary?.shortage_cells || 0} shortage cell{(projectedGaps.summary?.shortage_cells || 0) === 1 ? "" : "s"}
            {projectedGaps.summary?.cp_shortage_cells > 0 && (
              <span className="ml-1 text-rose-700 font-semibold">
                · {projectedGaps.summary.cp_shortage_cells} on critical path
              </span>
            )}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-1">
            {projectedGaps.topCells.map((cell) => {
              const tone = cell.is_critical_path
                ? "bg-rose-100 border-rose-300 text-rose-900"
                : cell.severity === "major"
                  ? "bg-rose-50 border-rose-200 text-rose-800"
                  : cell.severity === "moderate"
                    ? "bg-orange-100 border-orange-300 text-orange-900"
                    : "bg-amber-100 border-amber-300 text-amber-900";
              return (
                <Link key={cell.id} to={`/resource-leveling?tab=gaps&trade=${encodeURIComponent(cell.trade)}`}
                  data-testid={`cascade-gap-${cell.id}`}
                  className={`block p-2 rounded border text-xs ${tone}`}>
                  <div className="font-medium">{cell.trade}</div>
                  <div className="text-xs font-mono">
                    {cell.date} · supply {cell.supply} / demand {cell.demand} · gap +{cell.gap}
                  </div>
                  <div className="text-xs capitalize">
                    {cell.severity}{cell.is_critical_path ? " · CP" : ""}
                  </div>
                </Link>
              );
            })}
          </div>
        </div>
      )}

      {loading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : !rows.length ? (
        <div className="text-sm text-slate-500 border border-dashed border-slate-300 rounded p-8 text-center">
          No open gating entities. Cascade snapshots appear here when an RFI / submittal / field issue is open and gates a task.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse" data-testid="cascade-table">
            <thead>
              <tr className="bg-slate-100 text-slate-600">
                <th className="text-left p-2 border border-slate-200">Gate</th>
                <th className="text-left p-2 border border-slate-200">Type</th>
                <th className="text-right p-2 border border-slate-200">Direct</th>
                <th className="text-right p-2 border border-slate-200">Transitive</th>
                <th className="text-right p-2 border border-slate-200">Open age (wd)</th>
                <th className="text-left p-2 border border-slate-200">SLA</th>
                <th className="text-right p-2 border border-slate-200">Slip @ N5</th>
                <th className="text-right p-2 border border-slate-200">Slip @ N10</th>
                <th className="text-right p-2 border border-slate-200">Slip @ N20</th>
                <th className="text-right p-2 border border-slate-200">CP impact @ N10</th>
                <th className="text-left p-2 border border-slate-200">Severity</th>
                <th className="p-2 border border-slate-200"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} data-testid={`cascade-row-${r.gating_entity_id}`} className={SEVERITY_TONE[r.severity_band] || ""}>
                  <td className="p-2 border border-slate-200">
                    <Link to={`/gating-entities/${r.gating_entity_id}`} className="text-slate-800 hover:text-blue-600 font-medium">
                      {r.entity_label || r.gating_entity_id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="p-2 border border-slate-200 text-slate-600">{r.entity_type}</td>
                  <td className="p-2 border border-slate-200 text-right">{(r.directly_gated_activity_ids || []).length}</td>
                  <td className="p-2 border border-slate-200 text-right">{(r.transitively_gated_activity_ids || []).length}</td>
                  <td className="p-2 border border-slate-200 text-right">{r.open_age_working_days ?? "—"}</td>
                  <td className="p-2 border border-slate-200">
                    <span className={`px-1.5 py-0.5 rounded text-xs ${SLA_TONE[r.sla_status] || "bg-slate-100"}`}>
                      {r.sla_status || "—"}
                    </span>
                  </td>
                  <td className="p-2 border border-slate-200 text-right">{fmtDays(r.projected_slip_at_n5_days)}</td>
                  <td className="p-2 border border-slate-200 text-right">{fmtDays(r.projected_slip_at_n10_days)}</td>
                  <td className="p-2 border border-slate-200 text-right">{fmtDays(r.projected_slip_at_n20_days)}</td>
                  <td className="p-2 border border-slate-200 text-right font-medium">{fmtDays(r.projected_cp_impact_at_n10_days)}</td>
                  <td className="p-2 border border-slate-200">
                    <span className={`px-1.5 py-0.5 rounded text-xs uppercase ${SEVERITY_PILL[r.severity_band] || "bg-slate-100"}`}>
                      {r.severity_band || "?"}
                    </span>
                  </td>
                  <td className="p-2 border border-slate-200 text-slate-400">
                    <Link to={`/gating-entities/${r.gating_entity_id}`}><ChevronRight className="h-4 w-4" /></Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
