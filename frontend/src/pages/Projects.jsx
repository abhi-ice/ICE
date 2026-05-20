import { useEffect, useState } from "react";
import api from "../lib/api";
import { Plus, Pencil } from "lucide-react";
import { Button } from "../components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
// CPM Part G (2026-05-15) -- planned_completion_date rendered
// MM/DD/YYYY via the lib/dateUtil chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";

// Followup-fix 6 (2026-05-07): TIA header inputs added to project form
// so TIA Section 1 (Project Header) renders real values instead of nulls.
const EMPTY_FORM = {
  name: "",
  contract_number: "",
  client_name: "",
  // Feature DR Header Auto-Population (2026-05-20) -- station + GC
  // surfaced in every Daily Report header. Both optional.
  station_id: "",
  contractor_name: "",
  contract_value_usd: "",
  address_line1: "",
  city: "",
  state: "",
  postal_code: "",
  agency: "",
  ntp_date: "",
  original_substantial_completion: "",
  current_substantial_completion: "",
  contract_days: "",
  calendar_basis: "calendar_days",
};

export default function Projects() {
  const [projects, setProjects] = useState([]);
  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);

  const load = () => api.get("/projects").then(({ data }) => setProjects(data));
  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setEditId(null);
    setForm(EMPTY_FORM);
    setOpen(true);
  };

  const openEdit = (p) => {
    setEditId(p.id);
    setForm({
      name: p.name || "",
      contract_number: p.contract_number || "",
      client_name: p.client_name || "",
      // Feature DR Header Auto-Population (2026-05-20).
      station_id: p.station_id || "",
      contractor_name: p.contractor_name || "",
      contract_value_usd: p.contract_value_usd != null ? String(p.contract_value_usd) : "",
      address_line1: p.address_line1 || "",
      city: p.city || "",
      state: p.state || "",
      postal_code: p.postal_code || "",
      agency: p.agency || "",
      ntp_date: p.ntp_date || "",
      original_substantial_completion: p.original_substantial_completion || "",
      current_substantial_completion: p.current_substantial_completion || "",
      contract_days: p.contract_days != null ? String(p.contract_days) : "",
      calendar_basis: p.calendar_basis || "calendar_days",
    });
    setOpen(true);
  };

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const payload = { ...form,
        contract_value_usd: form.contract_value_usd ? Number(form.contract_value_usd) : null,
        contract_days: form.contract_days ? Number(form.contract_days) : null,
      };
      Object.keys(payload).forEach((k) => { if (payload[k] === "" || payload[k] === null) delete payload[k]; });
      if (editId) {
        await api.patch(`/projects/${editId}`, payload);
        toast.success("Project updated");
      } else {
        await api.post("/projects", payload);
        toast.success("Project created");
      }
      setOpen(false);
      setEditId(null);
      setForm(EMPTY_FORM);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || "Failed to save");
    } finally { setBusy(false); }
  };

  return (
    <div data-testid="projects-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Projects</h1>
          <p className="text-sm text-muted-foreground">All federal contracts in this tenant.</p>
        </div>
        <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) { setEditId(null); setForm(EMPTY_FORM); } }}>
          <DialogTrigger asChild>
            <Button data-testid="new-project-button" onClick={openCreate}><Plus className="h-4 w-4 mr-1" /> New project</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>{editId ? "Edit project" : "New project"}</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div>
                <label className="field-label">Project name *</label>
                <input data-testid="np-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              {/* Feature DR Header Auto-Population (2026-05-20) -- the
                  four contract / client fields are surfaced (read-only)
                  in every Daily Report header. All optional. */}
              <div className="pt-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Project / Contract Information
              </div>
              <div>
                <label className="field-label">Contract number</label>
                <input data-testid="np-contract" value={form.contract_number} onChange={(e) => setForm({ ...form, contract_number: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <label className="field-label">Client</label>
                <input data-testid="np-client" placeholder="e.g. VA Medical Center, Bronx NY" value={form.client_name} onChange={(e) => setForm({ ...form, client_name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="field-label">Station</label>
                  <input data-testid="np-station" placeholder="e.g. 526-25-100" value={form.station_id} onChange={(e) => setForm({ ...form, station_id: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
                <div>
                  <label className="field-label">Contractor</label>
                  <input data-testid="np-contractor" placeholder="e.g. Integrated Construction Enterprises, Inc." value={form.contractor_name} onChange={(e) => setForm({ ...form, contractor_name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
              </div>
              <div>
                <label className="field-label">Contract value (USD)</label>
                <input data-testid="np-value" type="number" value={form.contract_value_usd} onChange={(e) => setForm({ ...form, contract_value_usd: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <label className="field-label">Address</label>
                <input data-testid="np-address" placeholder="Street address" value={form.address_line1} onChange={(e) => setForm({ ...form, address_line1: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <label className="field-label">City</label>
                <input data-testid="np-city" value={form.city} onChange={(e) => setForm({ ...form, city: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="field-label">State</label>
                  <input data-testid="np-state" placeholder="e.g. NY or New York" value={form.state} onChange={(e) => setForm({ ...form, state: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
                <div>
                  <label className="field-label">Postal code</label>
                  <input data-testid="np-postal" value={form.postal_code} onChange={(e) => setForm({ ...form, postal_code: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">City and state are required for the daily-report weather auto-fill.</p>

              {/* Followup-fix 6 (2026-05-07): TIA header fields. Optional --
                  TIA Section 1 falls back to nulls until populated. */}
              <details className="border-t border-border pt-3 mt-3">
                <summary className="cursor-pointer text-sm font-medium text-slate-700">
                  TIA header (agency, NTP, contract days, calendar basis)
                </summary>
                <div className="mt-3 space-y-3">
                  <div>
                    <label className="field-label">Agency</label>
                    <input data-testid="np-agency" placeholder="e.g. GSA, NYCHA, USMS"
                      value={form.agency} onChange={(e) => setForm({ ...form, agency: e.target.value })}
                      className="w-full h-9 px-3 rounded border border-input bg-background" />
                  </div>
                  <div className="grid grid-cols-3 gap-3">
                    <div>
                      <label className="field-label">NTP date</label>
                      <input data-testid="np-ntp" type="date"
                        value={form.ntp_date} onChange={(e) => setForm({ ...form, ntp_date: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                    <div>
                      <label className="field-label">Contract days</label>
                      <input data-testid="np-contract-days" type="number" min="0"
                        value={form.contract_days} onChange={(e) => setForm({ ...form, contract_days: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                    <div>
                      <label className="field-label">Calendar basis</label>
                      <select data-testid="np-calendar-basis"
                        value={form.calendar_basis} onChange={(e) => setForm({ ...form, calendar_basis: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background">
                        <option value="calendar_days">calendar days</option>
                        <option value="working_days">working days</option>
                      </select>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="field-label">Original substantial completion</label>
                      <input data-testid="np-osc" type="date"
                        value={form.original_substantial_completion}
                        onChange={(e) => setForm({ ...form, original_substantial_completion: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                    <div>
                      <label className="field-label">Current substantial completion</label>
                      <input data-testid="np-csc" type="date"
                        value={form.current_substantial_completion}
                        onChange={(e) => setForm({ ...form, current_substantial_completion: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                  </div>
                </div>
              </details>

              <Button data-testid="np-submit" onClick={submit} disabled={!form.name || busy}>{busy ? "Saving…" : (editId ? "Save" : "Create")}</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead>
            <tr><th>Name</th><th>Contract #</th><th>Client</th><th>Location</th><th>Status</th><th>Value</th><th>Plan finish</th><th></th></tr>
          </thead>
          <tbody>
            {projects.map((p) => {
              const loc = [p.city, p.state].filter(Boolean).join(", ");
              return (
                <tr key={p.id} data-testid={`project-row-${p.id}`}>
                  <td className="font-medium">{p.name}</td>
                  <td className="font-mono text-xs">{p.contract_number || "—"}</td>
                  <td>{p.client_name || "—"}</td>
                  <td className="text-xs">{loc || <span className="text-muted-foreground">—</span>}</td>
                  <td><span className={`pill ${p.status === "active" ? "pill-green" : "pill-slate"}`}>{p.status}</span></td>
                  <td className="tabular-nums">{p.contract_value_usd ? `$${(p.contract_value_usd / 1_000_000).toFixed(2)}M` : "—"}</td>
                  <td className="font-mono text-xs">{formatDateDisplay(p.planned_completion_date)}</td>
                  <td className="text-right pr-3">
                    <button
                      data-testid={`edit-project-${p.id}`}
                      onClick={() => openEdit(p)}
                      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                    >
                      <Pencil className="h-3 w-3" /> Edit
                    </button>
                  </td>
                </tr>
              );
            })}
            {!projects.length && <tr><td colSpan="8" className="text-center text-sm text-muted-foreground py-6">No projects yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
