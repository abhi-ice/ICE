import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import api, { extractApiError } from "../../lib/api";
import { toast } from "../ui/sonner";

// Open delays from EARLIER reports, shown on every later report until resolved.
export default function CarriedDelaysCard({ reportId, reportDate, className = "", onChange }) {
  const [rows, setRows] = useState([]);
  const [resolving, setResolving] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.get(`/daily-reports/${reportId}/carried-delays`).then(({ data }) => setRows(data || [])).catch(() => setRows([]));
  }, [reportId]);
  useEffect(() => { load(); }, [load]);

  const resolve = async (d) => {
    setBusy(true);
    try {
      await api.patch(`/delays/${d.id}`, { status: "resolved", resolved_date: reportDate, ...(note.trim() ? { resolved_note: note.trim() } : {}) });
      toast.success("Delay resolved");
      setResolving(null); setNote("");
      load(); onChange?.();
    } catch (e) {
      toast.error(extractApiError(e, "Couldn't resolve the delay"));
    } finally {
      setBusy(false);
    }
  };

  if (!rows.length) return null;
  return (
    <div className={`rounded-xl border border-amber-500/40 bg-amber-500/[0.07] p-4 ${className}`} data-testid="carried-delays-card">
      <div className="flex items-center gap-2 text-sm font-semibold"><AlertTriangle className="h-4 w-4 text-amber-400" />
        Open delays from earlier reports ({rows.length})</div>
      <div className="mt-3 space-y-2">
        {rows.map((d) => (
          <div key={d.id} className="rounded-lg bg-black/10 px-3 py-2.5" data-testid={`carried-delay-${d.id}`}>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <span className="font-medium">{(d.activity_names || []).join(", ") || "Delay"}</span>
              <span className="text-xs opacity-70">{d.impact_days} impact day{d.impact_days === 1 ? "" : "s"} · since {String(d.logged_date || d.date || "").slice(0, 10)} · {d.status === "in_progress" ? "In progress" : "Open"}</span>
              {resolving !== d.id && (
                <button type="button" data-testid={`carried-delay-resolve-${d.id}`} onClick={() => { setResolving(d.id); setNote(""); }}
                  className="ml-auto h-8 px-3 rounded-md border border-emerald-500/50 text-emerald-400 text-xs font-medium hover:bg-emerald-500/10 transition-colors inline-flex items-center gap-1">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Resolve</button>
              )}
            </div>
            {d.description && <div className="text-xs opacity-70 mt-1">{d.description}</div>}
            {resolving === d.id && (
              <div className="mt-2 space-y-2">
                <textarea data-testid={`carried-delay-note-${d.id}`} rows={2} value={note} onChange={(e) => setNote(e.target.value)}
                  placeholder="Comment (optional)"
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-[16px] md:text-sm focus:outline-none focus:ring-2 focus:ring-ring" />
                <div className="flex justify-end gap-2">
                  <button type="button" onClick={() => setResolving(null)} className="h-8 px-3 rounded-md border border-input text-xs">Cancel</button>
                  <button type="button" data-testid={`carried-delay-confirm-${d.id}`} disabled={busy} onClick={() => resolve(d)}
                    className="h-8 px-3 rounded-md bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-500 disabled:opacity-50 inline-flex items-center gap-1">
                    {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Mark resolved</button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
