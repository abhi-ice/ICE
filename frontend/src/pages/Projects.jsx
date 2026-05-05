import { useEffect, useState } from "react";
import api from "../lib/api";
import { Plus, Pencil } from "lucide-react";
import { Button } from "../components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";

const EMPTY_FORM = {
  name: "",
  contract_number: "",
  client_name: "",
  contract_value_usd: "",
  address_line1: "",
  city: "",
  state: "",
  postal_code: "",
};

export default function Projects() {
  const [projects, setProjects] = useState([]);
  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);

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
      contract_value_usd: p.contract_value_usd != null ? String(p.contract_value_usd) : "",
      address_line1: p.address_line1 || "",
      city: p.city || "",
      state: p.state || "",
      postal_code: p.postal_code || "",
    });
    setOpen(true);
  };

  const submit = async () => {
    try {
      const payload = { ...form, contract_value_usd: form.contract_value_usd ? Number(form.contract_value_usd) : null };
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
    }
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
              <div>
                <label className="field-label">Contract number</label>
                <input data-testid="np-contract" value={form.contract_number} onChange={(e) => setForm({ ...form, contract_number: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <label className="field-label">Client</label>
                <input data-testid="np-client" value={form.client_name} onChange={(e) => setForm({ ...form, client_name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
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
              <Button data-testid="np-submit" onClick={submit} disabled={!form.name}>{editId ? "Save" : "Create"}</Button>
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
                  <td className="font-mono text-xs">{p.planned_completion_date || "—"}</td>
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
