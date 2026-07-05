// Bucket C3a Phase 4 — generic detail page for any gating entity
// (RFI / Submittal / WorkItem / future Bulletin / etc). Reachable via
// /gating-entities/:id from the entity list pages — those pages keep
// their existing CRUD UX, this page adds the schedule / SLA / affected-
// daily-reports lens on top.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import BackButton from "../components/BackButton";
import { ChevronLeft, AlertTriangle, Clock, FileText, Calendar } from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
// CPM Part G (2026-05-15) -- submitted_at, target_response_at,
// report_date, projected_finish_date displayed MM/DD/YYYY via the
// lib/dateUtil chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";

export default function GatingEntityDetail() {
  const { id } = useParams();
  const [data, setData] = useState(null);
  const [affected, setAffected] = useState(null);

  const refresh = async () => {
    try {
      const [d, a] = await Promise.all([
        api.get(`/gating-entities/${id}`).then((r) => r.data),
        api.get(`/gating-entities/${id}/affected-daily-reports`).then((r) => r.data),
      ]);
      setData(d);
      setAffected(a);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [id]);

  if (!data) return <div className="p-4 text-sm text-slate-500">Loading gating entity…</div>;

  const ge = data.gating_entity;
  const u = data.underlying || {};
  const tasks = data.linked_tasks || [];
  const overSla = ge.is_over_sla;
  const statusColor = ge.status === "responded" || ge.status === "closed" ? "pill-green"
    : ge.status === "under_review" || ge.status === "awaiting_clarification" ? "pill-amber"
    : "pill-blue";

  // Choose a human entity-type label.
  const typeLabel = ({
    rfi: "RFI", submittal: "Submittal", field_issue: "Field Issue",
    bulletin: "Bulletin", coordination_request: "Coordination Request",
    mop: "MOP", hot_work_permit: "Hot Work Permit",
  })[ge.entity_type] || ge.entity_type;

  // Best-effort title — each entity type stores its summary in a
  // different field. Fall back to the denormalized display_label.
  const subject = u?.subject || u?.title || ge.display_label || ge.entity_id;

  return (
    <div data-testid="gating-entity-detail-page" className="space-y-4">
      {/* pt.37: Option E back-button pill — destination matches the
          gating entity's source list. */}
      <BackButton
        to={ge.entity_type === "rfi" ? "/rfis"
          : ge.entity_type === "submittal" ? "/submittals"
          : ge.entity_type === "field_issue" ? "/work-items" : "/"}
        label={ge.entity_type === "rfi" ? "RFIs"
          : ge.entity_type === "submittal" ? "Submittals"
          : ge.entity_type === "field_issue" ? "Field issues" : "Dashboard"}
      />

      {/* Header */}
      <div className="surface p-4">
        <div className="flex items-start gap-3">
          <FileText className="h-5 w-5 text-slate-500 mt-1" />
          <div className="flex-1">
            <div className="text-xs text-slate-500">{typeLabel}{ge.entity_number ? ` · ${ge.entity_number}` : ""}</div>
            <div className="font-semibold text-base mt-1">{subject}</div>
            {u?.description && (
              <div className="text-sm text-slate-700 mt-2 whitespace-pre-wrap">{u.description}</div>
            )}
          </div>
          <div className="flex flex-col gap-1 items-end">
            <span className={`pill ${statusColor}`}>{(ge.status || "").replace(/_/g, " ")}</span>
            {overSla && (
              <span className="pill pill-red inline-flex items-center gap-1">
                <AlertTriangle className="h-3 w-3" /> over SLA
              </span>
            )}
          </div>
        </div>

        <div className="mt-3 pt-3 border-t border-slate-100 grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
          <Metric label="Submitted" value={formatDateDisplay(ge.submitted_at)} />
          <Metric label="Target response" value={formatDateDisplay(ge.target_response_at)} />
          <Metric label="SLA (working days)" value={ge.sla_working_days} />
          <Metric label="Working days open" value={ge.current_age_working_days ?? "—"} />
          <Metric label="Calendar days open" value={ge.current_age_calendar_days ?? "—"} />
        </div>
        <div className="mt-2 text-xs text-slate-500">
          Owner attribution: <b>{ge.owner_attribution}</b> · Cause class: <b>{(ge.cause_class || "").replace(/_/g, " ")}</b>
        </div>
      </div>

      {/* Linked tasks */}
      <div className="surface p-4">
        <div className="font-medium text-sm mb-3">Construction tasks gated by this entity</div>
        {tasks.length === 0 && <div className="text-xs text-slate-400">No linked tasks.</div>}
        <div className="space-y-1">
          {tasks.map((t) => {
            const slip = t.projected_finish_date && t.planned_finish && t.projected_finish_date > t.planned_finish;
            const csClass = t.computed_status === "blocked" ? "pill-red"
              : t.computed_status === "behind" ? "pill-amber"
              : t.computed_status === "complete" ? "pill-green" : "pill-slate";
            return (
              <div key={t.id} className="flex items-center gap-2 text-xs border-b border-slate-100 py-1.5">
                <span className="flex-1 truncate">{t.description}</span>
                <span className="text-slate-500 tabular-nums">{t.pct_complete ?? 0}%</span>
                {t.computed_status && <span className={`pill ${csClass} capitalize`}>{(t.computed_status || "").replace(/_/g, " ")}</span>}
                {t.is_critical_path && <span className="pill pill-red">CP</span>}
                {slip && (
                  <span className="text-amber-700 font-medium">→ {formatDateDisplay(t.projected_finish_date)}</span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Affected daily reports */}
      <div className="surface p-4">
        <div className="flex items-center gap-2 mb-3">
          <Calendar className="h-4 w-4 text-slate-500" />
          <div className="font-medium text-sm">Affected daily reports</div>
          <div className="ml-auto text-xs text-slate-500">
            {affected ? `${affected.row_count} row(s) · ${affected.total_production_impact_hours}h impact total` : "loading…"}
          </div>
        </div>
        {affected && affected.row_count === 0 && (
          <div className="text-xs text-slate-400">No daily-report rows have logged a constraint pointing at this entity yet.</div>
        )}
        {affected && affected.row_count > 0 && (
          <div className="overflow-x-auto">
            <table className="dense-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Activity</th>
                  <th>Constraint</th>
                  <th title="Crew-hours lost on this activity due to this constraint">Impact (h)</th>
                  <th>Progress</th>
                  <th>DR status</th>
                </tr>
              </thead>
              <tbody>
                {affected.rows.map((r) => (
                  <tr key={r.id}>
                    <td className="font-mono text-xs">{formatDateDisplay(r.report_date)}</td>
                    <td className="text-xs">
                      <span className="text-slate-600">{r.activity_description}</span>
                    </td>
                    <td className="text-xs">
                      {(r.constraint_type || "—").replace(/_/g, " ")}
                      {r.constraint_auto_filled && (
                        <span className="ml-1 text-xs text-blue-700/80">(auto)</span>
                      )}
                    </td>
                    <td className="text-xs tabular-nums">{r.production_impact_hours ?? "—"}</td>
                    <td className="text-xs tabular-nums text-slate-500">
                      {r.pct_complete_start_of_day ?? 0}% → {r.pct_complete_end_of_day ?? 0}%
                    </td>
                    <td className="text-xs">
                      <span className={`pill ${r.dr_status === "submitted" ? "pill-blue" : r.dr_status === "approved" ? "pill-green" : "pill-slate"}`}>
                        {r.dr_status || "—"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div className="bg-slate-50 rounded px-2 py-1.5 border border-slate-100">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-sm tabular-nums">{value ?? "—"}</div>
    </div>
  );
}
