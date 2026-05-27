// Bucket C3c Phase 4 — History of TIA generations for the active project.
// Click a row to open the detail / download page (Phase 5 will add
// privilege flag/unflag controls on the detail).
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { History, FileText, ChevronLeft, ShieldAlert, ShieldOff } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
// CPM Part G (2026-05-15) -- date_range columns rendered MM/DD/YYYY.
// generated_at stays as toLocaleString (it's a datetime with time).
import { formatDateDisplay } from "../lib/dateUtil";

export default function TIAGenerationsList() {
  const { activeId } = useProjects();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!activeId) return;
    setLoading(true);
    api.get(`/projects/${activeId}/tia/generations`)
      .then((r) => setRows(r.data || []))
      .catch((e) => toast.error(e.response?.data?.detail || "Failed to load"))
      .finally(() => setLoading(false));
  }, [activeId]);

  if (!activeId) return <div className="text-sm text-slate-500">Pick a project.</div>;

  return (
    /* pt.35: dropped outer p-6 -- AppLayout main already pads p-6. */
    <div data-testid="tia-history">
      <Link to="/tia" className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 mb-3">
        <ChevronLeft className="h-3 w-3" /> Back to generator
      </Link>
      <header className="mb-4 flex items-center gap-2">
        <History className="h-5 w-5 text-slate-700" />
        <h1 className="text-xl font-semibold text-slate-800">TIA generation history</h1>
      </header>

      {loading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : !rows.length ? (
        <div className="text-sm text-slate-500 border border-dashed border-slate-300 rounded p-8 text-center">
          No TIA generations yet for this project.
        </div>
      ) : (
        <div className="overflow-x-auto bg-white border border-slate-200 rounded">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="bg-slate-100 text-slate-600">
                <th className="text-left p-2 border border-slate-200">Generated</th>
                <th className="text-left p-2 border border-slate-200">Date range</th>
                <th className="text-right p-2 border border-slate-200">Items</th>
                <th className="text-right p-2 border border-slate-200">Downloads</th>
                <th className="text-left p-2 border border-slate-200">Status</th>
                <th className="text-left p-2 border border-slate-200">Privilege</th>
                <th className="text-left p-2 border border-slate-200">ID</th>
                <th className="p-2 border border-slate-200"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} data-testid={`tia-history-${r.id}`}>
                  <td className="p-2 border border-slate-200">{new Date(r.generated_at).toLocaleString()}</td>
                  <td className="p-2 border border-slate-200 text-slate-600">{formatDateDisplay(r.date_range_start)} → {formatDateDisplay(r.date_range_end)}</td>
                  <td className="p-2 border border-slate-200 text-right">{r.total_evidence_items}</td>
                  <td className="p-2 border border-slate-200 text-right">{(r.download_log || []).length}</td>
                  <td className="p-2 border border-slate-200">
                    <span className={`px-1.5 py-0.5 rounded text-xs ${
                      r.status === "complete" ? "bg-emerald-100 text-emerald-800"
                        : r.status === "failed" ? "bg-rose-100 text-rose-800"
                        : "bg-amber-100 text-amber-800"
                    }`}>{r.status}</span>
                  </td>
                  <td className="p-2 border border-slate-200">
                    {r.counsel_privileged_work_product
                      ? <span className="inline-flex items-center gap-1 text-amber-700"><ShieldAlert className="h-3 w-3" /> privileged</span>
                      : <span className="inline-flex items-center gap-1 text-slate-500"><ShieldOff className="h-3 w-3" /> open</span>}
                  </td>
                  <td className="p-2 border border-slate-200 font-mono text-xs text-slate-400">{r.id.slice(0, 8)}</td>
                  <td className="p-2 border border-slate-200">
                    <Link to={`/tia/generations/${r.id}`} className="text-blue-600 hover:underline inline-flex items-center gap-1">
                      <FileText className="h-3 w-3" /> Open
                    </Link>
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
