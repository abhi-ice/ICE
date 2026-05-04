import { useEffect, useState } from "react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";

const RESOURCES = {
  crews: { title: "Crews", testid: "crews-page", path: "crews", cols: [
    { key: "crew_code", label: "Code", className: "font-mono text-xs" },
    { key: "name", label: "Name" },
    { key: "trade", label: "Trade" },
    { key: "target_size", label: "Size" },
    { key: "hourly_burdened_cost", label: "$/hr", render: (v) => v ? `$${v}` : "—" },
    { key: "status", label: "Status" },
  ]},
  equipment: { title: "Equipment", testid: "equipment-page", path: "equipment", cols: [
    { key: "asset_tag", label: "Tag", className: "font-mono text-xs" },
    { key: "type", label: "Type" },
    { key: "manufacturer", label: "Mfr" },
    { key: "model", label: "Model" },
    { key: "year", label: "Year" },
    { key: "daily_rate", label: "Daily $", render: (v) => v ? `$${v}` : "—" },
    { key: "status", label: "Status" },
  ]},
  people: { title: "People", testid: "people-page", path: "people", cols: [
    { key: "first_name", label: "First" },
    { key: "last_name", label: "Last" },
    { key: "role", label: "Role" },
    { key: "trade", label: "Trade" },
    { key: "union_local", label: "Union" },
    { key: "hourly_rate", label: "$/hr", render: (v) => v ? `$${v}` : "—" },
    { key: "i9_on_file", label: "I-9", render: (v) => v ? "✓" : "—" },
    { key: "cleared_for_federal", label: "Fed", render: (v) => v ? "✓" : "—" },
  ]},
  certifications: { title: "Certifications", testid: "certifications-page", path: "certifications", cols: [
    { key: "person_id", label: "Person", className: "font-mono text-xs truncate max-w-[140px]" },
    { key: "certification_type", label: "Type" },
    { key: "issued_date", label: "Issued", className: "font-mono text-xs" },
    { key: "expires_date", label: "Expires", className: "font-mono text-xs" },
    { key: "issuing_authority", label: "Issuer" },
    { key: "verified", label: "Verified", render: (v) => v ? "✓" : "—" },
  ]},
};

export default function ResourceList({ kind }) {
  const cfg = RESOURCES[kind];
  const [rows, setRows] = useState([]);
  const load = () => api.get(`/${cfg.path}`).then(({ data }) => setRows(data));
  useEffect(() => { load(); }, [kind]);

  return (
    <div data-testid={cfg.testid} className="space-y-4">
      <h1 className="text-2xl font-semibold">{cfg.title}</h1>
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead><tr>{cfg.cols.map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.id || i} data-testid={`${cfg.testid}-row-${i}`}>
                {cfg.cols.map((c) => (
                  <td key={c.key} className={c.className}>
                    {c.render ? c.render(r[c.key]) : (r[c.key] ?? "—")}
                  </td>
                ))}
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={cfg.cols.length} className="text-center py-6 text-sm text-muted-foreground">No records.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function Availability() {
  const [data, setData] = useState(null);
  useEffect(() => {
    const start = new Date().toISOString().slice(0, 10);
    const end = new Date(Date.now() + 14 * 86400000).toISOString().slice(0, 10);
    api.get(`/availability?start=${start}&end=${end}`).then(({ data }) => setData(data));
  }, []);

  if (!data) return <div>Loading…</div>;
  return (
    <div data-testid="availability-page" className="space-y-4">
      <h1 className="text-2xl font-semibold">Cross-project availability</h1>
      <p className="text-sm text-muted-foreground">Window: {data.window.start} → {data.window.end}</p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-border font-medium text-sm">Crews ({data.crews.length})</div>
          <table className="w-full dense-table">
            <thead><tr><th>Name</th><th>Trade</th><th>Status</th></tr></thead>
            <tbody>{data.crews.map((c) => (
              <tr key={c.id}><td>{c.name}</td><td>{c.trade}</td><td><span className="pill pill-green">{c.status}</span></td></tr>
            ))}</tbody>
          </table>
        </div>
        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-border font-medium text-sm">Equipment ({data.equipment.length})</div>
          <table className="w-full dense-table">
            <thead><tr><th>Tag</th><th>Type</th><th>Status</th></tr></thead>
            <tbody>{data.equipment.map((e) => (
              <tr key={e.id}><td className="font-mono text-xs">{e.asset_tag}</td><td>{e.type}</td><td><span className="pill pill-green">{e.status}</span></td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
