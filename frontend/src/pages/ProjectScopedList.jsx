// ProjectScopedList — generic list page for entities scoped to the active
// project. Now full CRUD via CrudList component. Delays use a custom page
// (DelayList) because the Phase A multi-task picker doesn't fit CrudList.
//
// Bucket D Phase 3 -- entities now carry linked_dr_activity_ids; the
// gallery view renders affected-activity chips and accepts an
// affected_activity_id query param. Click-through goes to the parent
// Daily Report (via daily_report_id) so users land where the event
// was logged.
import CrudList from "../components/CrudList";
import DelayList from "./DelayList";
import { useProjects } from "../lib/projects";
import { Link } from "react-router-dom";
import GalleryActivityChips from "../components/photos/GalleryActivityChips";

const TRADE_OPTIONS = [
  "general", "concrete", "masonry", "steel", "framing", "drywall",
  "electrical", "plumbing", "hvac", "fire_protection", "roofing",
  "flooring", "painting", "ceilings", "landscaping", "civil",
].map((t) => ({ value: t, label: t.replace(/_/g, " ") }));

const RFI_STATUS = ["draft", "sent", "in_review", "answered", "closed"].map((s) => ({ value: s, label: s.replace(/_/g, " ") }));
const PRIORITY = ["low", "normal", "high", "critical"].map((p) => ({ value: p, label: p }));
const SEVERITY_LMH = ["low", "moderate", "high"].map((s) => ({ value: s, label: s }));
const SEVERITY_QC = ["minor", "major", "critical"].map((s) => ({ value: s, label: s }));
// Phase C — backend Severity enum is minor/moderate/major/critical for both QC + Safety.
const SEVERITY_FULL = ["minor", "moderate", "major", "critical"].map((s) => ({ value: s, label: s }));
const DELAY_REASON = [
  { value: "weather", label: "Weather" },
  { value: "rfi_pending", label: "RFI pending" },
  { value: "submittal_pending", label: "Submittal pending" },
  { value: "design_change", label: "Design change" },
  { value: "site_condition", label: "Site condition" },
  { value: "manpower", label: "Manpower" },
  { value: "other", label: "Other" },
];
const SAFETY_TYPES = [
  { value: "near_miss", label: "Near miss" },
  { value: "first_aid", label: "First aid" },
  { value: "recordable", label: "Recordable" },
  { value: "lost_time", label: "Lost time" },
  { value: "fatality", label: "Fatality" },
  { value: "other", label: "Other" },
];

// Bucket D Phase 3 -- gallery rework. Event kinds get an "Affected
// activities" chip column AND an affected_activity_id filter. The
// galleryEventKind flag tells ProjectScopedList to surface the
// activity-id filter input + chip column rendering.
const KIND_CONFIG = {
  rfis: {
    title: "RFIs",
    subtitle: "Requests for information — questions to the design team that block work.",
    help: "Open an RFI when a drawing or spec is unclear, conflicting, or missing detail. Each RFI gets a unique number, links to spec sections / drawings affected, and is tracked through draft → sent → answered → closed. The aging dashboard surfaces RFIs open more than 5 days.",
    list: (p) => `/projects/${p}/rfis`,
    create: "/rfis",
    galleryEventKind: true,
    columns: [
      // Bucket C3a Phase 4 — number cell links to the gating-entity
      // detail page (schedule + SLA + affected daily reports). Falls
      // back to plain text when the row hasn't been backfilled yet.
      { key: "rfi_number", label: "Number", render: (r) => r.gating_entity_id
          ? <Link to={`/gating-entities/${r.gating_entity_id}`} className="text-blue-700 font-mono hover:underline">{r.rfi_number}</Link>
          : <span className="font-mono">{r.rfi_number}</span> },
      { key: "subject", label: "Subject" },
      { key: "trade_code", label: "Trade" },
      { key: "status", label: "Status", render: (r) => <span className="pill pill-blue capitalize">{r.status}</span> },
      { key: "date_initiated", label: "Initiated" },
      // Bucket D Phase 3 -- chip column. Rendered via the page wrapper
      // since Tile/CrudList knows nothing about projectId; we inject a
      // closure over projectId at mount time below.
      { key: "linked_dr_activity_ids", label: "Affected activities", _isChips: true },
    ],
    form: [
      { key: "subject", label: "Subject", required: true },
      { key: "question", label: "Question (full text)", type: "textarea", required: true },
      { key: "trade_code", label: "Trade", type: "select", options: TRADE_OPTIONS },
      { key: "status", label: "Status", type: "select", options: RFI_STATUS, default: "draft" },
      { key: "answer", label: "Answer (if available)", type: "textarea" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  deliveries: {
    title: "Deliveries",
    subtitle: "Material/equipment deliveries — vendor, date, PO.",
    help: "Track inbound deliveries so you know what shows up and when. Deliveries can be linked to procurement submittals + tasks.",
    list: (p) => `/projects/${p}/deliveries`,
    create: "/deliveries",
    galleryEventKind: true,
    columns: [
      { key: "vendor", label: "Vendor" },
      { key: "description", label: "Description" },
      { key: "delivery_date", label: "Date" },
      { key: "po_number", label: "PO" },
      { key: "status", label: "Status" },
      { key: "linked_dr_activity_ids", label: "Activities consuming", _isChips: true },
    ],
    form: [
      { key: "vendor", label: "Vendor", required: true },
      { key: "description", label: "What is being delivered", type: "textarea", required: true },
      { key: "delivery_date", label: "Delivery date", type: "date" },
      { key: "po_number", label: "PO number" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  "work-items": {
    title: "Field issues",
    subtitle: "Open field issues with priority, owner, and resolution.",
    help: "Field issues are problems found in the field — punch list items, deficiencies, hazards. Each has priority + owner; closed items feed the knowledge base for future projects.",
    list: (p) => `/projects/${p}/work-items`,
    create: "/work-items",
    columns: [
      // Bucket C3a Phase 4 — title links to gating-entity detail.
      { key: "title", label: "Title", render: (r) => r.gating_entity_id
          ? <Link to={`/gating-entities/${r.gating_entity_id}`} className="text-blue-700 hover:underline">{r.title}</Link>
          : r.title },
      { key: "priority", label: "Priority", render: (r) => <span className={`pill capitalize ${r.priority === "high" || r.priority === "critical" ? "pill-amber" : "pill-slate"}`}>{r.priority}</span> },
      { key: "category", label: "Category" },
      { key: "status", label: "Status", render: (r) => <span className="pill pill-slate">{r.status}</span> },
      { key: "owned_by", label: "Owner" },
    ],
    form: [
      { key: "title", label: "Title", required: true },
      { key: "description", label: "Description", type: "textarea", required: true },
      { key: "priority", label: "Priority", type: "select", options: PRIORITY, default: "normal" },
      { key: "category", label: "Category" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  "change-orders": {
    title: "Change orders",
    subtitle: "Approved or pending scope changes with cost / schedule impact.",
    help: "Change orders are formal scope changes after contract award — usually triggered by an RFI answer that adds work. Each tracks cost delta and schedule delta in days.",
    list: (p) => `/projects/${p}/change-orders`,
    create: "/change-orders",
    columns: [
      { key: "co_number", label: "Number" },
      { key: "title", label: "Title" },
      { key: "cost_delta_usd", label: "Cost delta", render: (r) => r.cost_delta_usd ? `$${r.cost_delta_usd.toLocaleString()}` : "—" },
      { key: "schedule_delta_days", label: "Days", render: (r) => r.schedule_delta_days ?? "—" },
      { key: "status", label: "Status" },
    ],
    form: [
      { key: "title", label: "Title", required: true },
      { key: "description", label: "Description", type: "textarea" },
      { key: "cost_delta_usd", label: "Cost delta (USD)", type: "number" },
      { key: "schedule_delta_days", label: "Schedule delta (days)", type: "number" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  delays: {
    title: "Delays",
    subtitle: "Logged schedule delays with reason category and minute impact.",
    help: "Log every delay so the platform can roll up weather/manpower/RFI-pending impact at the end of each week. Severity drives the recovery agent.",
    list: (p) => `/projects/${p}/delays`,
    create: "/delays",
    columns: [
      { key: "date", label: "Date" },
      { key: "description", label: "Description" },
      { key: "reason_category", label: "Reason" },
      { key: "severity", label: "Severity" },
      { key: "delay_minutes", label: "Minutes" },
    ],
    form: [
      { key: "date", label: "Date", type: "date" },
      { key: "description", label: "Description", type: "textarea", required: true },
      { key: "reason_category", label: "Reason", type: "select", options: DELAY_REASON, required: true },
      { key: "severity", label: "Severity", type: "select", options: SEVERITY_LMH, default: "moderate" },
      { key: "delay_minutes", label: "Delay minutes", type: "number" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  "safety-events": {
    // Phase C — simplified to description + type + severity. OSHA
    // recordable removed. Severity dropdown is now minor/moderate/major/critical.
    title: "Safety events",
    subtitle: "Near misses, first aid, recordables, lost-time. Severity drives investigation pace.",
    help: "Log every safety event — even near misses — so we can spot patterns. Investigation status moves the event from open to closed.",
    list: (p) => `/projects/${p}/safety-events`,
    create: "/safety-events",
    galleryEventKind: true,
    columns: [
      { key: "date", label: "Date" },
      { key: "event_type", label: "Type" },
      { key: "description", label: "Description" },
      { key: "severity", label: "Severity" },
      { key: "status", label: "Status" },
      { key: "linked_dr_activity_ids", label: "Affected activities", _isChips: true },
    ],
    form: [
      { key: "date", label: "Date", type: "date" },
      { key: "description", label: "Describe the event", type: "textarea", required: true },
      { key: "event_type", label: "Type", type: "select", options: SAFETY_TYPES, default: "near_miss" },
      { key: "severity", label: "Severity", type: "select", options: SEVERITY_FULL, default: "minor" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  "qc-issues": {
    // Phase C — simplified to description + severity. Spec section field removed.
    title: "QC issues",
    subtitle: "Quality-control deficiencies — wrong material, sloppy workmanship, missing inspection.",
    help: "Quality-control issues are deviations from spec. Status moves from open to closed when remediated.",
    list: (p) => `/projects/${p}/qc-issues`,
    create: "/qc-issues",
    galleryEventKind: true,
    columns: [
      { key: "date", label: "Date" },
      { key: "description", label: "Description" },
      { key: "severity", label: "Severity" },
      { key: "status", label: "Status" },
      { key: "linked_dr_activity_ids", label: "Affected activities", _isChips: true },
    ],
    form: [
      { key: "date", label: "Date", type: "date" },
      { key: "description", label: "Describe the issue", type: "textarea", required: true },
      { key: "severity", label: "Severity", type: "select", options: SEVERITY_FULL, default: "minor" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  locations: {
    title: "Locations",
    subtitle: "Rooms, zones, corridors organised under floors.",
    help: "Locations are the physical spaces in your project. Hierarchy: Building → Floor → Zone → Room. Tasks can be tagged with a location for spatial planning. Use the Polygon editor to draw the room boundary on the floor plan, or click 'AI auto-detect rooms' to extract them all from a plan PDF.",
    list: (p) => `/projects/${p}/locations`,
    create: "/locations",
    columns: [
      { key: "code", label: "Code" },
      { key: "name", label: "Name" },
      { key: "location_type", label: "Type" },
      { key: "area_sqft", label: "Area (sq ft)" },
    ],
    form: [
      { key: "code", label: "Code", required: true },
      { key: "name", label: "Name", required: true },
      { key: "location_type", label: "Type", type: "select", options: ["room", "zone", "corridor", "stair", "mech_room", "exterior"].map((t) => ({ value: t, label: t })), default: "room" },
      { key: "floor_id", label: "Floor ID (UUID)", required: true },
      { key: "area_sqft", label: "Area (sq ft)", type: "number" },
    ],
    buildPayload: (form, projectId) => ({ project_id: projectId, ...form }),
  },
  drawings: {
    title: "Drawings",
    subtitle: "Plan PDFs uploaded to the project.",
    help: "Drawings are individual plan PDFs — typically one per sheet. Upload them on the Drawings page (top right) since drawings need a binary file. The AI copilot can list them but uploading is via the Drawings UI.",
    list: (p) => `/projects/${p}/drawings`,
    create: null, // upload via /drawings page
    columns: [
      { key: "sheet_number", label: "Sheet" },
      { key: "title", label: "Title" },
      { key: "discipline", label: "Discipline" },
    ],
    form: [],
  },
};

import { useUrlState } from "../lib/useUrlState";
import { useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
import { Button } from "../components/ui/button";
import { navigateToTodaysDrForCreate } from "../lib/todaysDr";
import { toast } from "../components/ui/sonner";
import { useState as useReactState } from "react";

// Bucket D Fix G -- map sidebar `kind` to the createEvent param the DR
// page understands (which it uses to pick the panel to scroll to).
const KIND_TO_CREATE_EVENT = {
  rfis: "rfi", "qc-issues": "qc", "safety-events": "safety",
  deliveries: "delivery",
};

export default function ProjectScopedList({ kind }) {
  const { activeId } = useProjects();
  const navigate = useNavigate();
  // Bucket D Fix H -- gallery filter mirrored to URL so refresh /
  // back-button preserves the filter state.
  const [urlFilters, setUrlFilters] = useUrlState({ affected_activity_id: "" });
  const affectedActivityId = urlFilters.affected_activity_id;
  const setAffectedActivityId = (v) => setUrlFilters({ affected_activity_id: v || "" });
  // Bucket D Fix G -- "+ New" button busy guard while resolving today's DR.
  const [routing, setRouting] = useReactState(false);
  const routeToTodaysDr = async () => {
    if (routing) return;
    const createEvent = KIND_TO_CREATE_EVENT[kind];
    if (!createEvent) return;
    setRouting(true);
    try {
      const { url, locked, dr } = await navigateToTodaysDrForCreate(activeId, createEvent);
      if (locked) {
        toast.info(`Today's DR (${dr.report_date}) is locked. Use Supplements panel on the parent DR to add ${createEvent}.`);
        navigate(`/daily-reports?openId=${dr.id}`);
      } else {
        navigate(url);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Couldn't resolve today's DR");
    } finally { setRouting(false); }
  };
  if (kind === "delays") return <DelayList />;
  const cfg = KIND_CONFIG[kind];
  if (!cfg) {
    return <div className="text-sm text-slate-500">Unknown list: {kind}</div>;
  }
  if (!activeId) {
    return <div className="text-sm text-slate-500">Select a project from the top bar to view {cfg.title}.</div>;
  }
  // Map kind → backend entity name for /crud/{entity}/{id} fallback
  const entityName = {
    rfis: "rfis", deliveries: "deliveries", "work-items": "work-items",
    "change-orders": "change-orders", delays: "delays",
    "safety-events": "safety-events", "qc-issues": "qc-issues",
    locations: "locations",
  }[kind];

  // Bucket D Phase 3 -- decorate gallery columns with chip render + DR
  // click-through. The _isChips marker on a column means render via
  // GalleryActivityChips against linked_dr_activity_ids on the row.
  // Date column gets a click-through to the parent DR.
  const decoratedColumns = cfg.columns.map((c) => {
    if (c._isChips) {
      return {
        ...c,
        render: (r) => (
          <GalleryActivityChips projectId={activeId}
            activityIds={r.linked_dr_activity_ids || []} />
        ),
      };
    }
    if (c.key === "date" && cfg.galleryEventKind) {
      // Bucket D Fix F -- include openEvent + openEventType so the
      // parent DR scrolls to + pulse-highlights this row. Map the
      // sidebar `kind` to the row-testid prefix used inside the DR.
      const eventTypeMap = {
        "qc-issues": "qc", "safety-events": "safety",
        "rfis": "rfi", "deliveries": "delivery",
      };
      const eventType = eventTypeMap[kind] || kind;
      return {
        ...c,
        render: (r) => r.daily_report_id ? (
          <Link to={`/daily-reports?openId=${r.daily_report_id}&openEvent=${r.id}&openEventType=${eventType}`}
            className="text-blue-700 hover:underline font-mono text-xs">
            {r.date}
          </Link>
        ) : <span className="font-mono text-xs">{r.date || "—"}</span>,
      };
    }
    return c;
  });

  // Append the affected-activity query param when set so the backend
  // does the filter (avoids loading all rows then filtering client-side).
  const listUrl = cfg.list(activeId)
    + (affectedActivityId ? `?affected_activity_id=${encodeURIComponent(affectedActivityId)}` : "");

  return (
    <div className="space-y-3">
      {cfg.galleryEventKind && (
        <div className="bg-amber-50 border border-amber-200 rounded p-3 text-xs text-amber-900">
          <strong>Gallery view.</strong> {cfg.title} live on their parent daily report; this page
          aggregates across all DRs in the project. Click a date or affected-activity chip to
          jump to the parent DR.
          <div className="mt-2 flex items-center gap-2">
            <label className="uppercase tracking-wide text-[10px]">Filter by activity id</label>
            <input data-testid={`${kind}-affected-activity-filter`}
              value={affectedActivityId}
              onChange={(e) => setAffectedActivityId(e.target.value)}
              placeholder="paste a daily_report_activity.id"
              className="h-7 px-2 rounded border border-amber-300 bg-white text-xs flex-1" />
            {affectedActivityId && (
              <button onClick={() => setAffectedActivityId("")}
                className="h-7 px-2 rounded bg-slate-200 text-slate-700 text-xs hover:bg-slate-300">
                Clear
              </button>
            )}
          </div>
        </div>
      )}
      <CrudList
        title={cfg.title}
        subtitle={cfg.subtitle}
        help={cfg.help}
        listEndpoint={listUrl}
        createEndpoint={cfg.create}
        patchEndpoint={cfg.create && entityName ? (r) => `/crud/${entityName}/${r.id}` : null}
        deleteEndpoint={cfg.create && entityName ? (r) => `/crud/${entityName}/${r.id}` : null}
        testidPrefix={kind}
        columns={decoratedColumns}
        formFields={cfg.form}
        buildPayload={cfg.buildPayload ? (form, ed) => cfg.buildPayload(form, activeId) : null}
        customAddButton={cfg.galleryEventKind && KIND_TO_CREATE_EVENT[kind] ? (
          <Button data-testid={`${kind}-add-via-dr`} onClick={routeToTodaysDr}
            disabled={routing} className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 gap-1">
            <Plus className="h-4 w-4" /> {routing ? "Opening today's DR…" : "+ New on today's DR"}
          </Button>
        ) : undefined}
      />
    </div>
  );
}
