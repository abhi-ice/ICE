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

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

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
    <div className="p-6 max-w-7xl mx-auto" data-testid="cascade-dashboard">
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
                    <span className={`px-1.5 py-0.5 rounded text-[10px] ${SLA_TONE[r.sla_status] || "bg-slate-100"}`}>
                      {r.sla_status || "—"}
                    </span>
                  </td>
                  <td className="p-2 border border-slate-200 text-right">{fmtDays(r.projected_slip_at_n5_days)}</td>
                  <td className="p-2 border border-slate-200 text-right">{fmtDays(r.projected_slip_at_n10_days)}</td>
                  <td className="p-2 border border-slate-200 text-right">{fmtDays(r.projected_slip_at_n20_days)}</td>
                  <td className="p-2 border border-slate-200 text-right font-medium">{fmtDays(r.projected_cp_impact_at_n10_days)}</td>
                  <td className="p-2 border border-slate-200">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase ${SEVERITY_PILL[r.severity_band] || "bg-slate-100"}`}>
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
