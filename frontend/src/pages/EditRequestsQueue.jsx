// Addition 4 -- Admin/PM approval queue for edit requests filed by
// supers who need to fix events on locked DRs past the 24-hour self-
// edit window. Each request shows: requester, DR date, event type +
// id, reason, time-since-filed, Approve/Deny buttons.
//
// Non-admin users see this page filtered to their OWN requests
// (read-only -- approve/deny disabled).
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Check, X, Clock, ListChecks, RefreshCw } from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
import { Button } from "../components/ui/button";

const STATUS_FILTERS = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "denied", label: "Denied" },
  { value: "", label: "All" },
];

const EVENT_LABEL = {
  delay: "Delay", rfi: "RFI", qc: "QC issue", safety: "Safety event",
  photo: "Photo", coordination: "Coordination", delivery: "Delivery",
  activity: "Grid activity",
};

function _ageLabel(iso) {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  const mins = Math.round(ms / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(ms / 3_600_000);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(ms / 86_400_000);
  return `${days}d ago`;
}

export default function EditRequestsQueue() {
  const [rows, setRows] = useState([]);
  const [statusFilter, setStatusFilter] = useState("pending");
  const [loading, setLoading] = useState(false);
  // Reviewer-side: track per-row note input + busy state.
  const [noteByRow, setNoteByRow] = useState({});
  const [busyById, setBusyById] = useState(new Set());

  const reload = async () => {
    setLoading(true);
    try {
      const params = {};
      if (statusFilter) params.status = statusFilter;
      const { data } = await api.get("/edit-requests", { params });
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    } finally { setLoading(false); }
  };
  useEffect(() => { reload(); /* eslint-disable-next-line */ }, [statusFilter]);

  const review = async (row, action) => {
    const id = row.id;
    if (busyById.has(id)) return;
    setBusyById((p) => new Set(p).add(id));
    try {
      await api.post(`/edit-requests/${id}/${action}`, {
        review_note: (noteByRow[id] || "").trim() || null,
      });
      toast.success(action === "approve"
        ? "Request approved. 1-hour edit window opened for the requester."
        : "Request denied.");
      reload();
    } catch (e) {
      toast.error(e.response?.data?.detail?.message || e.response?.data?.detail || `${action} failed`);
    } finally {
      setBusyById((p) => { const n = new Set(p); n.delete(id); return n; });
    }
  };

  return (
    <div className="p-6" data-testid="edit-requests-page">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ListChecks className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800">Edit requests</h1>
          </div>
          <p className="text-sm text-slate-500">
            Supers file these to ask Admin or PM for edit access to events
            on locked DRs more than 24 hours after submit. Approving opens a
            1-hour edit window for that user on that DR.
          </p>
        </div>
        <button data-testid="edit-requests-reload" onClick={reload}
          className="h-9 px-3 rounded bg-slate-200 text-slate-700 text-sm hover:bg-slate-300 inline-flex items-center gap-1">
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </header>

      <div className="flex items-center gap-2 mb-3">
        {STATUS_FILTERS.map((f) => (
          <button key={f.value} onClick={() => setStatusFilter(f.value)}
            data-testid={`edit-requests-filter-${f.value || "all"}`}
            className={`h-8 px-3 rounded text-xs ${
              statusFilter === f.value
                ? "bg-blue-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}>
            {f.label}
          </button>
        ))}
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead>
            <tr>
              <th>Filed</th>
              <th>Requester</th>
              <th>DR / event</th>
              <th>Reason</th>
              <th>Status</th>
              <th>Reviewer notes / actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const isPending = r.status === "pending";
              const expired = (
                r.status === "approved"
                && r.edit_window_expires_at
                && new Date(r.edit_window_expires_at).getTime() < Date.now()
              );
              const statusPill = isPending ? "pill-amber"
                : r.status === "approved" && !expired ? "pill-green"
                : r.status === "approved" ? "pill-slate"  // expired
                : "pill-red";
              const statusText = expired ? "expired" : r.status;
              return (
                <tr key={r.id} data-testid={`edit-request-row-${r.id}`}>
                  <td className="text-xs whitespace-nowrap">
                    <span title={r.requested_at}>{_ageLabel(r.requested_at)}</span>
                  </td>
                  <td className="font-mono text-xs">{r.requested_by?.slice(0, 8) || "—"}</td>
                  <td className="text-xs">
                    <Link to={`/daily-reports?openId=${r.daily_report_id}`}
                      className="text-blue-700 hover:underline">
                      {EVENT_LABEL[r.event_type] || r.event_type}
                    </Link>
                    <div className="text-xs text-slate-400 font-mono">
                      DR {r.daily_report_id?.slice(0, 8)} · ev {r.event_id?.slice(0, 8)}
                    </div>
                  </td>
                  <td className="text-xs max-w-[320px]"
                    title={r.reason}>
                    {r.reason}
                  </td>
                  <td><span className={`pill ${statusPill} capitalize`}>{statusText}</span></td>
                  <td>
                    {isPending ? (
                      <div className="flex flex-col gap-1">
                        <textarea data-testid={`edit-request-note-${r.id}`}
                          value={noteByRow[r.id] || ""}
                          onChange={(e) => setNoteByRow({ ...noteByRow, [r.id]: e.target.value })}
                          placeholder="Optional note (encouraged on deny)"
                          className="w-full p-1 rounded border border-slate-200 text-xs h-12" />
                        <div className="flex gap-1">
                          <Button data-testid={`edit-request-approve-${r.id}`}
                            onClick={() => review(r, "approve")}
                            disabled={busyById.has(r.id)}
                            className="h-7 px-2 bg-emerald-600 hover:bg-emerald-700 text-white text-xs gap-1">
                            <Check className="h-3 w-3" /> Approve
                          </Button>
                          <Button data-testid={`edit-request-deny-${r.id}`}
                            onClick={() => review(r, "deny")}
                            disabled={busyById.has(r.id)}
                            className="h-7 px-2 bg-rose-600 hover:bg-rose-700 text-white text-xs gap-1">
                            <X className="h-3 w-3" /> Deny
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="text-xs text-slate-500 space-y-0.5">
                        <div>By {r.reviewed_by?.slice(0, 8) || "—"} · {_ageLabel(r.reviewed_at)}</div>
                        {r.review_note && <div className="italic">"{r.review_note}"</div>}
                        {r.status === "approved" && (
                          <div className="font-mono text-xs">
                            <Clock className="inline h-3 w-3 mr-0.5" />
                            window {expired ? "expired" : "expires"} {_ageLabel(r.edit_window_expires_at)}
                          </div>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
            {!loading && rows.length === 0 && (
              <tr><td colSpan="6" className="text-center py-8 text-sm text-slate-500">
                {statusFilter === "pending" ? "No pending requests." : "No requests match this filter."}
              </td></tr>
            )}
            {loading && rows.length === 0 && (
              <tr><td colSpan="6" className="text-center py-8 text-sm text-slate-400">Loading…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
