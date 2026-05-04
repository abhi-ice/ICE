// Generic list page used by RFIs, Delays, Safety, QC, Work Items, Change Orders.
// Keeps things uniform & dense.
import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";

const CONFIG = {
  rfis: {
    title: "RFIs", path: "rfis", testid: "rfis-page",
    columns: [
      { key: "rfi_number", label: "Number", className: "font-mono text-xs" },
      { key: "subject", label: "Subject" },
      { key: "to_party", label: "To" },
      { key: "priority", label: "Priority" },
      { key: "status", label: "Status", pill: true },
      { key: "date_initiated", label: "Initiated", className: "font-mono text-xs" },
      { key: "date_required", label: "Required", className: "font-mono text-xs" },
    ],
  },
  delays: {
    title: "Delays", path: "delays", testid: "delays-page",
    columns: [
      { key: "date", label: "Date", className: "font-mono text-xs" },
      { key: "description", label: "Description" },
      { key: "severity", label: "Severity", pill: true },
      { key: "delay_minutes", label: "Min" },
      { key: "reason_category", label: "Category" },
      { key: "status", label: "Status", pill: true },
    ],
  },
  "safety-events": {
    title: "Safety events", path: "safety-events", testid: "safety-page",
    columns: [
      { key: "date", label: "Date", className: "font-mono text-xs" },
      { key: "event_type", label: "Type" },
      { key: "description", label: "Description" },
      { key: "severity", label: "Severity", pill: true },
      { key: "osha_recordable", label: "OSHA", render: (v) => (v ? "Yes" : "No") },
      { key: "status", label: "Status", pill: true },
    ],
  },
  "qc-issues": {
    title: "QC issues", path: "qc-issues", testid: "qc-page",
    columns: [
      { key: "date", label: "Date", className: "font-mono text-xs" },
      { key: "description", label: "Description" },
      { key: "severity", label: "Severity", pill: true },
      { key: "spec_section_ref", label: "Spec ref", className: "font-mono text-xs" },
      { key: "status", label: "Status", pill: true },
    ],
  },
  "work-items": {
    title: "Field issues", path: "work-items", testid: "work-items-page",
    columns: [
      { key: "title", label: "Title" },
      { key: "priority", label: "Priority", pill: true },
      { key: "status", label: "Status", pill: true },
      { key: "estimated_cost_impact", label: "Est. cost", render: (v) => (v ? `$${v.toLocaleString()}` : "—") },
      { key: "estimated_time_impact_days", label: "Est. days" },
      { key: "target_resolution_date", label: "Target", className: "font-mono text-xs" },
    ],
  },
  "change-orders": {
    title: "Change orders", path: "change-orders", testid: "co-page",
    columns: [
      { key: "co_number", label: "Number", className: "font-mono text-xs" },
      { key: "title", label: "Title" },
      { key: "type", label: "Type" },
      { key: "amount_usd", label: "Amount", render: (v) => `$${(v ?? 0).toLocaleString()}` },
      { key: "time_extension_days", label: "Days" },
      { key: "status", label: "Status", pill: true },
    ],
  },
  photos: {
    title: "Photos", path: "photos", testid: "photos-page",
    columns: [
      { key: "captured_at", label: "Captured", className: "font-mono text-xs", render: (v) => new Date(v).toLocaleString() },
      { key: "caption", label: "Caption" },
      { key: "tags", label: "Tags", render: (v) => (Array.isArray(v) ? v.join(", ") : "") },
      { key: "storage_path", label: "Path", className: "font-mono text-xs truncate max-w-[280px]" },
    ],
  },
  drawings: {
    title: "Drawings", path: "drawings", testid: "drawings-page",
    columns: [
      { key: "sheet_number", label: "Sheet", className: "font-mono text-xs" },
      { key: "title", label: "Title" },
      { key: "discipline", label: "Disc." },
      { key: "drawing_set_name", label: "Set" },
      { key: "revision_count", label: "Rev" },
      { key: "status", label: "Status", pill: true },
    ],
  },
  specs: {
    title: "Specifications", path: "specs", testid: "specs-page",
    columns: [
      { key: "name", label: "Name" },
      { key: "revision_label", label: "Revision" },
      { key: "pdf_page_count", label: "Pages" },
      { key: "total_sections", label: "Sections" },
      { key: "processing_status", label: "Status", pill: true },
    ],
  },
  locations: {
    title: "Locations", path: "locations", testid: "locations-page",
    columns: [
      { key: "code", label: "Code", className: "font-mono text-xs" },
      { key: "name", label: "Name" },
      { key: "location_type", label: "Type" },
      { key: "area_sqft", label: "Area sqft", render: (v) => (v ? v.toLocaleString() : "—") },
    ],
  },
  deliveries: {
    title: "Deliveries", path: "deliveries", testid: "deliveries-page",
    columns: [
      { key: "delivery_date", label: "Date", className: "font-mono text-xs" },
      { key: "vendor", label: "Vendor" },
      { key: "description", label: "Description" },
      { key: "status", label: "Status", pill: true },
    ],
  },
  submittals: {
    title: "Submittals", path: "submittals", testid: "submittals-page",
    columns: [
      { key: "spec_section", label: "Spec section", className: "font-mono text-xs" },
      { key: "title", label: "Title" },
      { key: "type", label: "Type" },
      { key: "status", label: "Status", pill: true },
    ],
  },
};

export default function ProjectScopedList({ kind }) {
  const cfg = CONFIG[kind];
  const { activeId, projects } = useProjects();
  const [rows, setRows] = useState([]);
  const project = projects.find((p) => p.id === activeId);

  useEffect(() => {
    if (!activeId || !cfg) return;
    api.get(`/projects/${activeId}/${cfg.path}`).then(({ data }) => setRows(data)).catch(() => setRows([]));
  }, [activeId, cfg]);

  if (!cfg) return <div>Unknown</div>;

  return (
    <div data-testid={cfg.testid} className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{cfg.title}</h1>
        <p className="text-sm text-muted-foreground">{project?.name}</p>
      </div>
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead><tr>{cfg.columns.map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.id || i} data-testid={`${cfg.testid}-row-${i}`}>
                {cfg.columns.map((c) => {
                  const v = r[c.key];
                  if (c.pill) return <td key={c.key}><span className={`pill ${pillTone(v)}`}>{v ?? "—"}</span></td>;
                  if (c.render) return <td key={c.key} className={c.className}>{c.render(v)}</td>;
                  return <td key={c.key} className={c.className}>{v ?? "—"}</td>;
                })}
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={cfg.columns.length} className="text-center py-6 text-sm text-muted-foreground">No items.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function pillTone(v) {
  if (!v) return "pill-slate";
  const s = String(v).toLowerCase();
  if (["complete", "answered", "approved", "resolved", "closed", "verified"].includes(s)) return "pill-green";
  if (["in_progress", "in_review", "investigating", "sent"].includes(s)) return "pill-blue";
  if (["high", "urgent", "major", "critical", "rejected"].includes(s)) return "pill-red";
  if (["medium", "moderate", "pending", "draft", "open"].includes(s) || s === "normal") return "pill-amber";
  return "pill-slate";
}
