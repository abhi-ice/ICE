// Bucket C3b Phase 1 — coordination event detail / edit / delete page.
// Linked-entity rendering is intentionally minimal in Phase 1 — Phase 2
// will add reverse links from the linked entities (Affected Daily
// Reports section already covers DR linkage).
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, Pencil, Trash2 } from "lucide-react";
import api from "../lib/api";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import CoordinationEventForm from "../components/forms/CoordinationEventForm";

const TOPIC_LABELS = {
  tenant_access: "Tenant access", building_coordination: "Building coordination",
  a_e_clarification: "A/E clarification", owner_direction: "Owner direction",
  sub_coordination: "Sub coordination", regulatory: "Regulatory",
  safety: "Safety", quality: "Quality", scope: "Scope", payment: "Payment",
  other: "Other",
};

export default function CoordinationEventDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [ev, setEv] = useState(null);
  const [editing, setEditing] = useState(false);

  const refresh = async () => {
    try {
      const { data } = await api.get(`/coordination-events/${id}`);
      setEv(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [id]);

  if (!ev) return <div className="p-4 text-sm text-slate-500">Loading…</div>;

  const remove = async () => {
    if (!window.confirm("Delete this coordination event? This is a soft-delete; the audit trail keeps the record.")) return;
    try {
      await api.delete(`/coordination-events/${id}`);
      toast.success("Coordination event deleted");
      nav("/coordination");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  const outClass = ev.outcome === "agreement" ? "pill-green"
    : ev.outcome === "disagreement" || ev.outcome === "escalation_required" ? "pill-red"
    : ev.outcome === "pending" ? "pill-amber" : "pill-slate";

  const linkedSections = [
    ["RFIs", ev.linked_rfi_ids],
    ["Field issues", ev.linked_field_issue_ids],
    ["Submittals", ev.linked_submittal_ids],
    ["Bulletins", ev.linked_bulletin_ids],
    ["Activities", ev.linked_activity_ids],
    ["Gating entities", ev.linked_gating_entity_ids],
  ].filter(([, arr]) => Array.isArray(arr) && arr.length > 0);

  return (
    <div data-testid="coordination-event-detail-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <Link to="/coordination" className="text-sm text-blue-600 hover:underline flex items-center gap-1">
          <ChevronLeft className="h-4 w-4" /> Back to coordination log
        </Link>
      </div>

      <div className="surface p-4">
        <div className="flex items-start gap-3">
          <div className="flex-1">
            <div className="text-xs text-slate-500 uppercase tracking-wide">
              {TOPIC_LABELS[ev.topic] || ev.topic} · {ev.channel.replace(/_/g, " ")} · {ev.direction}
            </div>
            <div className="text-base font-semibold mt-1">{ev.summary}</div>
            <div className="text-xs text-slate-500 mt-1">
              {(ev.occurred_at || "").slice(0, 16).replace("T", " ")} · with <b>{ev.other_party_name}</b>
              {ev.other_party_org && ` (${ev.other_party_org})`}
              {ev.other_party_role && ` · ${ev.other_party_role}`}
            </div>
            {(ev.other_party_email || ev.other_party_phone) && (
              <div className="text-xs text-slate-500">
                {ev.other_party_email && <span className="mr-3">{ev.other_party_email}</span>}
                {ev.other_party_phone && <span>{ev.other_party_phone}</span>}
              </div>
            )}
          </div>
          <div className="flex flex-col gap-1 items-end">
            <span className={`pill ${outClass} capitalize`}>{ev.outcome.replace(/_/g, " ")}</span>
            {ev.voice_extracted && (
              <span className="pill pill-amber text-[10px]">voice draft</span>
            )}
          </div>
          <div className="flex flex-col gap-1">
            <Button data-testid="coord-edit" onClick={() => setEditing(true)} variant="outline" className="gap-1">
              <Pencil className="h-3.5 w-3.5" /> Edit
            </Button>
            <Button data-testid="coord-delete" onClick={remove} variant="outline" className="gap-1 text-red-600 border-red-200 hover:bg-red-50">
              <Trash2 className="h-3.5 w-3.5" /> Delete
            </Button>
          </div>
        </div>

        {ev.detail_text && (
          <div className="mt-3 pt-3 border-t border-slate-100">
            <div className="text-xs text-slate-500 uppercase tracking-wide mb-1">Detail</div>
            <div className="text-sm whitespace-pre-wrap text-slate-800">{ev.detail_text}</div>
          </div>
        )}

        {ev.outcome_notes && (
          <div className="mt-3 pt-3 border-t border-slate-100">
            <div className="text-xs text-slate-500 uppercase tracking-wide mb-1">Outcome notes</div>
            <div className="text-sm whitespace-pre-wrap text-slate-800">{ev.outcome_notes}</div>
          </div>
        )}
      </div>

      {linkedSections.length > 0 && (
        <div className="surface p-4">
          <div className="font-medium text-sm mb-2">Linked entities</div>
          {linkedSections.map(([label, arr]) => (
            <div key={label} className="text-xs mb-1">
              <span className="uppercase tracking-wide text-slate-500 mr-2">{label}:</span>
              <span className="font-mono text-slate-700">{arr.length} reference{arr.length === 1 ? "" : "s"}</span>
            </div>
          ))}
          {ev.linked_daily_report_id && (
            <div className="text-xs mb-1">
              <span className="uppercase tracking-wide text-slate-500 mr-2">Daily report:</span>
              <span className="font-mono">{ev.linked_daily_report_id.slice(0, 8)}</span>
            </div>
          )}
        </div>
      )}

      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Edit coordination event</DialogTitle></DialogHeader>
          <CoordinationEventForm
            projectId={ev.project_id}
            initial={ev}
            onSaved={() => { setEditing(false); refresh(); toast.success("Saved"); }}
            onCancel={() => setEditing(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}
