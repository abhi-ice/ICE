import { useEffect, useState } from "react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";

export default function Approvals() {
  const [items, setItems] = useState([]);
  const [filter, setFilter] = useState("pending");

  const load = () => api.get(`/approvals?status_filter=${filter}`).then(({ data }) => setItems(data));
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [filter]);

  const decide = async (id, decision) => {
    await api.post(`/approvals/${id}/decide`, { decision, notes: null });
    toast.success(`Marked ${decision}`);
    load();
  };

  return (
    <div data-testid="approvals-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Approvals</h1>
        <div className="flex gap-2">
          {["pending", "approved", "rejected"].map((s) => (
            <button key={s} data-testid={`approvals-filter-${s}`} onClick={() => setFilter(s)} className={`h-8 px-3 rounded border text-sm capitalize ${filter === s ? "bg-primary text-primary-foreground border-primary" : "border-border"}`}>
              {s}
            </button>
          ))}
        </div>
      </div>
      <div className="space-y-3">
        {items.map((a) => (
          <div key={a.id} data-testid={`approval-card-${a.id}`} className="bg-card border border-border rounded-lg p-4">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-mono">{a.id.slice(0, 8)}</span>
                  <span>•</span>
                  <span className="capitalize">{a.kind.replace(/_/g, " ")}</span>
                  <span>•</span>
                  <span>{a.subject_entity_type}: {a.subject_entity_id}</span>
                </div>
                <pre className="mt-2 text-xs bg-muted/50 p-2 rounded overflow-x-auto">
                  {JSON.stringify(a.proposed_payload, null, 2)}
                </pre>
                {a.notes && <div className="mt-2 text-sm">{a.notes}</div>}
                <div className="mt-2 text-xs text-muted-foreground">
                  Proposed at {new Date(a.proposed_at).toLocaleString()}
                </div>
              </div>
              <div className="flex flex-col gap-2 shrink-0">
                <span className={`pill ${a.status === "pending" ? "pill-amber" : a.status === "approved" ? "pill-green" : "pill-red"}`}>{a.status}</span>
                {a.status === "pending" && (
                  <>
                    <button data-testid={`approve-${a.id}`} onClick={() => decide(a.id, "approved")} className="h-8 px-3 rounded bg-emerald-600 text-white text-sm">Approve</button>
                    <button data-testid={`reject-${a.id}`} onClick={() => decide(a.id, "rejected")} className="h-8 px-3 rounded bg-red-600 text-white text-sm">Reject</button>
                  </>
                )}
              </div>
            </div>
          </div>
        ))}
        {!items.length && <div className="text-sm text-muted-foreground">No {filter} approvals.</div>}
      </div>
    </div>
  );
}
