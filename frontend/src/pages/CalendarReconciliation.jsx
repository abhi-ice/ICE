// Bucket C3c Phase 3 — Working-day vs calendar-day reconciliation page.
// Renders one row per date with calendar attributes + production /
// weather / delay aggregates pulled from daily reports.
import { useEffect, useState, useCallback } from "react";
import { CalendarClock, Download, Search } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

function isoDaysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function isoToday() {
  return new Date().toISOString().slice(0, 10);
}

function rowTone(r) {
  if (r.is_building_closed) return "bg-rose-50";
  if (!r.is_working_day) return "bg-slate-50 text-slate-500";
  if (r.is_restricted_access) return "bg-amber-50";
  if ((r.weather_impact_hours || 0) > 0) return "bg-sky-50";
  return "";
}

export default function CalendarReconciliation() {
  const { activeId } = useProjects();
  const [start, setStart] = useState(isoDaysAgo(30));
  const [end, setEnd] = useState(isoToday());
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!activeId) { setData(null); return; }
    setLoading(true);
    try {
      const r = await api.get(`/projects/${activeId}/calendar-reconciliation`, {
        params: { date_range_start: start, date_range_end: end },
      });
      setData(r.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load reconciliation");
    } finally { setLoading(false); }
  }, [activeId, start, end]);

  useEffect(() => { load(); }, [load]);

  const downloadCsv = async () => {
    if (!activeId) return;
    try {
      const r = await api.get(`/projects/${activeId}/calendar-reconciliation/csv`, {
        params: { date_range_start: start, date_range_end: end },
        responseType: "blob",
      });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = `calendar_reconciliation_${start}_${end}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.response?.data?.detail || "CSV export failed");
    }
  };

  if (!activeId) return <div className="p-6 text-sm text-slate-500">Pick a project.</div>;

  return (
    <div className="p-6" data-testid="calendar-reconciliation">
      <header className="mb-4 flex items-center gap-2">
        <CalendarClock className="h-5 w-5 text-slate-700" />
        <h1 className="text-xl font-semibold text-slate-800">Calendar reconciliation</h1>
      </header>

      <div className="bg-white border border-slate-200 rounded p-3 mb-4 flex items-end gap-3 flex-wrap">
        <div>
          <label className="text-xs text-slate-500 uppercase">From</label>
          <input data-testid="reconciliation-start" type="date" value={start} onChange={(e) => setStart(e.target.value)}
            className="block h-9 px-2 rounded border border-slate-300 text-sm" />
        </div>
        <div>
          <label className="text-xs text-slate-500 uppercase">To</label>
          <input data-testid="reconciliation-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)}
            className="block h-9 px-2 rounded border border-slate-300 text-sm" />
        </div>
        <button data-testid="reconciliation-load" onClick={load} disabled={loading}
          className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
          <Search className="h-3.5 w-3.5" /> {loading ? "Loading…" : "Refresh"}
        </button>
        <button data-testid="reconciliation-csv" onClick={downloadCsv}
          className="h-9 px-3 rounded bg-slate-700 text-white text-sm hover:bg-slate-800 inline-flex items-center gap-1">
          <Download className="h-3.5 w-3.5" /> Download CSV
        </button>
      </div>

      {data && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mb-4">
            <Stat label="Calendar days" n={data.calendar_days} />
            <Stat label="Working days" n={data.working_days} />
            <Stat label="Production hours" n={data.total_production_hours} />
            <Stat label="Weather impact (h)" n={data.total_weather_impact_hours} />
            <Stat label="Delay (h)" n={data.total_delay_hours} />
          </div>

          <div className="overflow-x-auto bg-white border border-slate-200 rounded">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="bg-slate-100 text-slate-600">
                  <th className="text-left p-2 border border-slate-200">Date</th>
                  <th className="text-center p-2 border border-slate-200">Working</th>
                  <th className="text-center p-2 border border-slate-200">Holiday</th>
                  <th className="text-center p-2 border border-slate-200">Closed</th>
                  <th className="text-center p-2 border border-slate-200">Restricted</th>
                  <th className="text-left p-2 border border-slate-200">Weather</th>
                  <th className="text-right p-2 border border-slate-200">Weather (h)</th>
                  <th className="text-right p-2 border border-slate-200">Delay (h)</th>
                  <th className="text-right p-2 border border-slate-200">Production (h)</th>
                  <th className="text-right p-2 border border-slate-200">Headcount</th>
                  <th className="text-left p-2 border border-slate-200">Notes</th>
                </tr>
              </thead>
              <tbody>
                {(data.rows || []).map((r) => (
                  <tr key={r.date} data-testid={`reconciliation-row-${r.date}`} className={rowTone(r)}>
                    <td className="p-2 border border-slate-200 font-mono">{r.date}</td>
                    <td className="p-2 border border-slate-200 text-center">{r.is_working_day ? "✓" : ""}</td>
                    <td className="p-2 border border-slate-200 text-center">{r.is_holiday ? "✓" : ""}</td>
                    <td className="p-2 border border-slate-200 text-center">{r.is_building_closed ? "✓" : ""}</td>
                    <td className="p-2 border border-slate-200 text-center">{r.is_restricted_access ? "✓" : ""}</td>
                    <td className="p-2 border border-slate-200">{r.weather_conditions || ""}</td>
                    <td className="p-2 border border-slate-200 text-right">{r.weather_impact_hours || 0}</td>
                    <td className="p-2 border border-slate-200 text-right">{r.delay_hours || 0}</td>
                    <td className="p-2 border border-slate-200 text-right">{r.production_hours || 0}</td>
                    <td className="p-2 border border-slate-200 text-right">{r.headcount_total || 0}</td>
                    <td className="p-2 border border-slate-200 text-slate-500">{r.notes || ""}</td>
                  </tr>
                ))}
                {!(data.rows || []).length && (
                  <tr><td colSpan={11} className="p-4 text-center text-slate-400">No data in range.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, n }) {
  return (
    <div className="bg-white border border-slate-200 rounded p-3">
      <div className="text-2xl font-semibold text-slate-800">{n ?? 0}</div>
      <div className="text-xs text-slate-500 uppercase tracking-wide">{label}</div>
    </div>
  );
}
