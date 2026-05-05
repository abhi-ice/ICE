// Voice PIN admin — issue numeric PINs to foremen for capture-only access.
// Master prompt §13 (voice user persona).
import { useState } from "react";
import { Mic, KeyRound } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";

export default function VoicePinAdmin() {
  const { activeId, projects } = useProjects();
  const [name, setName] = useState("");
  const [hours, setHours] = useState(24);
  const [issued, setIssued] = useState([]);
  const project = projects.find((p) => p.id === activeId);

  const issue = async () => {
    try {
      const { data } = await api.post("/auth/voice-pin/issue", {
        name, project_id: activeId, ttl_hours: hours,
      });
      setIssued((s) => [{ ...data, issued_at: new Date().toISOString() }, ...s]);
      setName("");
      toast.success("PIN issued");
    } catch (e) { toast.error(e.response?.data?.detail || "Failed"); }
  };

  return (
    <div data-testid="voice-pin-admin" className="space-y-4 max-w-3xl">
      <div className="flex items-center gap-2">
        <KeyRound className="h-5 w-5 text-blue-600" />
        <h1 className="text-2xl font-semibold tracking-tight">Voice PINs</h1>
      </div>
      <p className="text-sm text-slate-500">{project?.name} · Issue 6-digit PINs to foremen for voice-only capture (no email needed).</p>

      <div className="surface p-4 grid grid-cols-3 gap-3 items-end">
        <div className="col-span-2">
          <label className="field-label">Foreman name</label>
          <input data-testid="vpin-name" value={name} onChange={(e) => setName(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="John Doe" />
        </div>
        <div>
          <label className="field-label">Expires (hours)</label>
          <input type="number" value={hours} onChange={(e) => setHours(Number(e.target.value))} className="w-full h-9 px-3 rounded border border-slate-200" />
        </div>
        <Button data-testid="vpin-issue" onClick={issue} disabled={!name} className="col-span-3 bg-blue-600 hover:bg-blue-700 gap-1">
          <Mic className="h-4 w-4" /> Issue PIN
        </Button>
      </div>

      {issued.length > 0 && (
        <div className="surface overflow-hidden">
          <table className="dense-table">
            <thead><tr><th>Name</th><th>PIN</th><th>Expires</th></tr></thead>
            <tbody>
              {issued.map((it, i) => (
                <tr key={i} data-testid={`vpin-row-${i}`}>
                  <td>{it.name}</td>
                  <td className="font-mono text-xl tracking-widest font-semibold text-blue-600">{it.pin}</td>
                  <td className="font-mono text-xs">{new Date(it.expires_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-4 py-2 text-[11px] text-slate-500 border-t border-slate-200">
            Direct foremen to <a href="/voice-login" className="text-blue-600 hover:underline">/voice-login</a> and have them key in their PIN. Each PIN is single-use.
          </div>
        </div>
      )}
    </div>
  );
}
