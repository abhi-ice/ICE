// Resources — generic CRUD pages for crews/equipment/people/certifications.
// Each one uses the CrudList component with a config below.
import CrudList from "../components/CrudList";
import { useProjects } from "../lib/projects";

const TRADE_OPTIONS = [
  "general", "concrete", "masonry", "steel", "framing", "drywall",
  "electrical", "plumbing", "hvac", "fire_protection", "roofing",
  "flooring", "painting", "ceilings", "landscaping", "civil",
].map((t) => ({ value: t, label: t.replace(/_/g, " ") }));

const PEOPLE_ROLE_OPTIONS = ["worker", "foreman", "superintendent", "pm", "engineer", "qc", "safety"].map((r) => ({ value: r, label: r.toUpperCase() }));

const EQUIPMENT_TYPES = [
  "excavator", "loader", "crane", "scaffold", "lift", "compressor",
  "pump", "generator", "welder", "saw", "drill", "general",
].map((t) => ({ value: t, label: t }));


export function CrewsPage() {
  return (
    <CrudList
      title="Crews"
      subtitle="Self-perform and external crews. Each crew rolls up to a trade and has a target size."
      help="A crew is a group of workers organised by trade (e.g. 'Concrete Crew A — General contractor'). Tasks consume crews for cost forecasting and conflict detection. Use the AI copilot to add quickly: 'Create a new crew named Concrete Crew A, trade concrete, target size 6.'"
      listEndpoint="/crews"
      createEndpoint="/crews"
      patchEndpoint={(r) => `/crud/crews/${r.id}`}
      deleteEndpoint={(r) => `/crud/crews/${r.id}`}
      testidPrefix="crews"
      columns={[
        { key: "name", label: "Name" },
        { key: "trade", label: "Trade" },
        { key: "target_size", label: "Target size" },
        { key: "current_size", label: "Current" },
        { key: "external_company", label: "External" },
        { key: "status", label: "Status", render: (r) => <span className="pill pill-green capitalize">{r.status}</span> },
      ]}
      formFields={[
        { key: "crew_code", label: "Crew code", required: true, default: "" },
        { key: "name", label: "Crew name", required: true },
        { key: "trade", label: "Trade", type: "select", options: TRADE_OPTIONS, required: true },
        { key: "target_size", label: "Target size (workers)", type: "number", default: 4 },
        { key: "hourly_burdened_cost", label: "Hourly burdened cost (USD)", type: "number" },
        { key: "notes", label: "Notes", type: "textarea" },
      ]}
    />
  );
}

export function PeoplePage() {
  return (
    <CrudList
      title="People"
      subtitle="Individuals tracked for attendance, prevailing-wage rates, and certifications."
      help="People are individuals on the project (workers, foremen, supers, PMs, QC, safety). Each can be linked to a crew, has a trade, and an hourly rate that drives cost forecasting. Quick add via copilot: 'Add person John Doe, foreman, trade electrical, hourly rate 68 dollars.'"
      listEndpoint="/people"
      createEndpoint="/people"
      patchEndpoint={(r) => `/crud/people/${r.id}`}
      deleteEndpoint={(r) => `/crud/people/${r.id}`}
      testidPrefix="people"
      columns={[
        { key: "first_name", label: "First", render: (r) => `${r.first_name} ${r.last_name || ""}` },
        { key: "role", label: "Role" },
        { key: "trade", label: "Trade" },
        { key: "email", label: "Email" },
        { key: "phone", label: "Phone" },
        { key: "hourly_rate", label: "$/hr", render: (r) => r.hourly_rate ? `$${r.hourly_rate}` : "—" },
        { key: "employment_type", label: "Type" },
      ]}
      formFields={[
        { key: "first_name", label: "First name", required: true },
        { key: "last_name", label: "Last name", required: true },
        { key: "role", label: "Role", type: "select", options: PEOPLE_ROLE_OPTIONS },
        { key: "trade", label: "Trade", type: "select", options: TRADE_OPTIONS },
        { key: "email", label: "Email" },
        { key: "phone", label: "Phone" },
        { key: "hourly_rate", label: "Hourly rate (USD)", type: "number" },
        { key: "employment_type", label: "Employment type", type: "select", options: [{ value: "ft", label: "Full-time" }, { value: "pt", label: "Part-time" }, { value: "contractor", label: "Contractor" }], default: "ft" },
        { key: "union_local", label: "Union local" },
      ]}
    />
  );
}

export function EquipmentPage() {
  return (
    <CrudList
      title="Equipment"
      subtitle="Owned and rented equipment with hourly rates."
      help="Equipment is heavy machinery (excavator, crane, lift) or specialised tools (welder, saw). Each piece has an asset tag, type, and hourly rate. Self-perform vs rental flag drives cost forecasting and the marketplace 'equipment_rental' postings."
      listEndpoint="/equipment"
      createEndpoint="/equipment"
      patchEndpoint={(r) => `/crud/equipment/${r.id}`}
      deleteEndpoint={(r) => `/crud/equipment/${r.id}`}
      testidPrefix="equipment"
      columns={[
        { key: "asset_tag", label: "Asset tag" },
        { key: "type", label: "Type" },
        { key: "manufacturer", label: "Mfr" },
        { key: "model", label: "Model" },
        { key: "year", label: "Year" },
        { key: "ownership", label: "Owner" },
        { key: "hourly_rate", label: "$/hr", render: (r) => r.hourly_rate ? `$${r.hourly_rate}` : "—" },
        { key: "status", label: "Status" },
      ]}
      formFields={[
        { key: "asset_tag", label: "Asset tag", required: true },
        { key: "type", label: "Type", type: "select", options: EQUIPMENT_TYPES, required: true },
        { key: "manufacturer", label: "Manufacturer" },
        { key: "model", label: "Model" },
        { key: "year", label: "Year", type: "number" },
        { key: "ownership", label: "Ownership", type: "select", options: [{ value: "owned", label: "Owned" }, { value: "rental", label: "Rental" }, { value: "leased", label: "Leased" }], default: "owned" },
        { key: "hourly_rate", label: "Hourly rate (USD)", type: "number" },
        { key: "daily_rate", label: "Daily rate (USD)", type: "number" },
      ]}
    />
  );
}

export function CertificationsPage() {
  return (
    <CrudList
      title="Certifications"
      subtitle="Trade certifications, OSHA cards, MEP licences. Linked to people."
      help="Certifications track regulatory requirements per worker (e.g. OSHA 30, journeyman electrician licence, asbestos abatement). The platform warns when a person without a required cert is assigned to a task that needs it."
      listEndpoint="/certifications"
      createEndpoint="/certifications"
      patchEndpoint={(r) => `/crud/certifications/${r.id}`}
      deleteEndpoint={(r) => `/crud/certifications/${r.id}`}
      testidPrefix="certs"
      columns={[
        { key: "name", label: "Certification" },
        { key: "issuing_body", label: "Issuer" },
        { key: "validity_period_months", label: "Valid (mo)" },
        { key: "is_required_for_trades", label: "Required for", render: (r) => (r.is_required_for_trades || []).join(", ") || "—" },
      ]}
      formFields={[
        { key: "name", label: "Certification name", required: true },
        { key: "issuing_body", label: "Issuing body" },
        { key: "validity_period_months", label: "Validity period (months)", type: "number" },
      ]}
    />
  );
}

// Legacy compat — App.js routes pass kind="crews|equipment|people|certifications"
const PAGE_BY_KIND = {
  crews: CrewsPage,
  equipment: EquipmentPage,
  people: PeoplePage,
  certifications: CertificationsPage,
};
export default function ResourceList({ kind }) {
  const Component = PAGE_BY_KIND[kind] || CrewsPage;
  return <Component />;
}

// Availability stub — placeholder until calendar UI is built. Imported by App.js.
export function Availability() {
  return (
    <div data-testid="availability-page" className="space-y-3">
      <h1 className="text-2xl font-semibold tracking-tight">Resource availability</h1>
      <p className="text-sm text-slate-500">Calendar view of crews and equipment availability — coming next iteration. For now use Crews / People pages to manage records and the AI copilot to query availability conflicts.</p>
    </div>
  );
}
