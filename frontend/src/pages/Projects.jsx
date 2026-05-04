import { useEffect, useState } from "react";
import api from "../lib/api";
import { Plus } from "lucide-react";
import { Button } from "../components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";

export default function Projects() {
  const [projects, setProjects] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", contract_number: "", client_name: "", contract_value_usd: "" });

  const load = () => api.get("/projects").then(({ data }) => setProjects(data));
  useEffect(() => { load(); }, []);

  const submit = async () => {
    try {
      const payload = { ...form, contract_value_usd: form.contract_value_usd ? Number(form.contract_value_usd) : null };
      Object.keys(payload).forEach((k) => { if (payload[k] === "") delete payload[k]; });
      await api.post("/projects", payload);
      toast.success("Project created");
      setOpen(false);
      setForm({ name: "", contract_number: "", client_name: "", contract_value_usd: "" });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail?.[0]?.msg || "Failed to create");
    }
  };

  return (
    <div data-testid="projects-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Projects</h1>
          <p className="text-sm text-muted-foreground">All federal contracts in this tenant.</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button data-testid="new-project-button"><Plus className="h-4 w-4 mr-1" /> New project</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>New project</DialogTitle></DialogHeader>
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
              <Button data-testid="np-submit" onClick={submit} disabled={!form.name}>Create</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead>
            <tr><th>Name</th><th>Contract #</th><th>Client</th><th>Status</th><th>Value</th><th>Plan finish</th></tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id} data-testid={`project-row-${p.id}`}>
                <td className="font-medium">{p.name}</td>
                <td className="font-mono text-xs">{p.contract_number || "—"}</td>
                <td>{p.client_name || "—"}</td>
                <td><span className={`pill ${p.status === "active" ? "pill-green" : "pill-slate"}`}>{p.status}</span></td>
                <td className="tabular-nums">{p.contract_value_usd ? `$${(p.contract_value_usd / 1_000_000).toFixed(2)}M` : "—"}</td>
                <td className="font-mono text-xs">{p.planned_completion_date || "—"}</td>
              </tr>
            ))}
            {!projects.length && <tr><td colSpan="6" className="text-center text-sm text-muted-foreground py-6">No projects yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
