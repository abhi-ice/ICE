// EntitySidePanel — slide-out card showing a linked entity. Used by the
// drawing-viewer pin click-through (master prompt §13: cross-functional
// linkage everywhere).
import { useEffect, useState } from "react";
import { X, ExternalLink } from "lucide-react";
import api from "../../lib/api";

const ENTITY_LABELS = {
  task: { title: "Activity", route: "/tasks" },
  rfi: { title: "RFI", route: "/rfis" },
  work_item: { title: "Field issue", route: "/work-items" },
  delay: { title: "Delay", route: "/delays" },
  safety_event: { title: "Safety event", route: "/safety-events" },
  qc_issue: { title: "QC issue", route: "/qc-issues" },
  photo: { title: "Photo", route: "/photos" },
  delivery: { title: "Delivery", route: "/deliveries" },
  change_order: { title: "Change order", route: "/change-orders" },
  submittal: { title: "Submittal", route: "/submittals" },
};

export default function EntitySidePanel({ entityType, entityId, onClose }) {
  const [entity, setEntity] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const meta = ENTITY_LABELS[entityType] || { title: entityType, route: "/" };

  useEffect(() => {
    if (!entityType || !entityId) return;
    setLoading(true);
    setErr(null);
    api.get(`/entities/${entityType}/${entityId}`)
      .then(({ data }) => setEntity(data.entity))
      .catch((e) => setErr(e.response?.data?.detail || e.message))
      .finally(() => setLoading(false));
  }, [entityType, entityId]);

  if (!entityType || !entityId) return null;

  const renderRow = (label, value) => (
    <div className="grid grid-cols-3 gap-2 py-1 border-b border-slate-100 text-xs">
      <div className="text-slate-500">{label}</div>
      <div className="col-span-2 break-words">{value ?? "—"}</div>
    </div>
  );

  return (
    <div data-testid="entity-side-panel" className="fixed right-0 top-0 h-screen w-full sm:w-[380px] bg-white border-l border-slate-200 z-50 shadow-2xl flex flex-col">
      <div className="h-12 px-4 border-b border-slate-200 flex items-center gap-2">
        <span className="pill pill-blue">{meta.title}</span>
        {entity?.activity_code && <span className="font-mono text-xs">{entity.activity_code}</span>}
        {entity?.rfi_number && <span className="font-mono text-xs">{entity.rfi_number}</span>}
        {entity?.co_number && <span className="font-mono text-xs">{entity.co_number}</span>}
        <button onClick={onClose} className="ml-auto h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><X className="h-4 w-4" /></button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 scrollbar-soft">
        {loading && <div className="text-sm text-slate-500">Loading…</div>}
        {err && <div className="text-sm text-red-600">{String(err)}</div>}
        {entity && (
          <div className="space-y-3">
            <div className="space-y-1">
              {entity.title && <div className="font-medium text-base">{entity.title}</div>}
              {entity.subject && <div className="font-medium text-base">{entity.subject}</div>}
              {entity.description && (
                <div className="text-sm text-slate-700 whitespace-pre-wrap">{entity.description}</div>
              )}
            </div>

            <div className="border-t border-slate-200 pt-3 space-y-1">
              {entityType === "task" && (
                <>
                  {renderRow("Status", entity.status)}
                  {renderRow("Plan start", entity.planned_start)}
                  {renderRow("Plan finish", entity.planned_finish)}
                  {renderRow("Duration", entity.duration_days ? `${entity.duration_days} days` : null)}
                  {renderRow("% complete", `${entity.pct_complete ?? 0}%`)}
                  {renderRow("Trade", entity.trade_code)}
                  {renderRow("Critical path", entity.is_critical_path ? "Yes" : "No")}
                </>
              )}
              {entityType === "rfi" && (
                <>
                  {renderRow("Status", entity.status)}
                  {renderRow("Priority", entity.priority)}
                  {renderRow("To", entity.to_party)}
                  {renderRow("Initiated", entity.date_initiated)}
                  {renderRow("Required", entity.date_required)}
                  {renderRow("Cost impact", entity.cost_impact_estimated_usd ? `$${entity.cost_impact_estimated_usd.toLocaleString()}` : null)}
                </>
              )}
              {entityType === "work_item" && (
                <>
                  {renderRow("Status", entity.status)}
                  {renderRow("Priority", entity.priority)}
                  {renderRow("Est. cost", entity.estimated_cost_impact ? `$${entity.estimated_cost_impact.toLocaleString()}` : null)}
                  {renderRow("Est. days", entity.estimated_time_impact_days)}
                  {renderRow("Target", entity.target_resolution_date)}
                </>
              )}
              {(entityType === "delay" || entityType === "safety_event" || entityType === "qc_issue") && (
                <>
                  {renderRow("Status", entity.status)}
                  {renderRow("Severity", entity.severity)}
                  {renderRow("Date", entity.date)}
                  {entityType === "safety_event" && renderRow("Event type", entity.event_type)}
                  {entityType === "delay" && renderRow("Minutes", entity.delay_minutes)}
                  {entityType === "qc_issue" && renderRow("Spec ref", entity.spec_section_ref)}
                </>
              )}
              {entityType === "submittal" && (
                <>
                  {renderRow("Status", entity.status)}
                  {renderRow("Type", entity.type)}
                  {renderRow("Spec section", entity.spec_section_code)}
                  {renderRow("Sub responsible", entity.sub_responsible)}
                </>
              )}
            </div>
          </div>
        )}
      </div>
      <div className="p-3 border-t border-slate-200">
        <a
          href={meta.route}
          data-testid="entity-side-panel-open-list"
          className="w-full h-9 grid place-items-center rounded border border-slate-200 hover:bg-slate-50 text-sm flex-row gap-1 inline-flex"
        >
          <ExternalLink className="h-3.5 w-3.5" /> Open in {meta.title} list
        </a>
      </div>
    </div>
  );
}
