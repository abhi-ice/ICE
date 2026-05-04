import { useState, useEffect } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import { FileUp, Download } from "lucide-react";

export default function Imports() {
  const { activeId } = useProjects();
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState([]);
  const [last, setLast] = useState(null);

  const load = () => api.get("/imports").then(({ data }) => setHistory(data));
  useEffect(load, []);

  const upload = async () => {
    if (!file || !activeId) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("project_id", activeId);
      fd.append("file", file);
      const { data } = await api.post("/imports/schedule", fd);
      setLast(data);
      toast.success(`Imported ${data.created} new, ${data.updated} updated`);
      load();
    } catch (e) {
      toast.error(JSON.stringify(e.response?.data?.detail || e.message));
    } finally {
      setBusy(false);
    }
  };

  const downloadTemplate = () => {
    const csv = "id,name,start,finish,duration,predecessors,trade\n" +
                "T100,Mobilization,2026-03-01,2026-03-05,5,,\n" +
                "T101,Demo,2026-03-06,2026-03-15,8,T100,Demolition\n" +
                "T102,Framing,2026-03-16,2026-03-30,12,T101,Framing/Drywall\n";
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "schedule_template.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div data-testid="imports-page" className="space-y-4">
      <h1 className="text-2xl font-semibold">Imports</h1>
      <p className="text-sm text-muted-foreground">
        Upload schedule files (.xlsx or .csv). MPP / XER files require the Railway parser
        (see substitution note in <code>services/schedule_parser.py</code>).
      </p>

      <div className="bg-card border border-border rounded-lg p-4 space-y-3">
        <div className="flex items-center gap-2">
          <FileUp className="h-4 w-4" />
          <div className="font-medium text-sm">Schedule file</div>
          <button onClick={downloadTemplate} className="ml-auto text-xs text-primary hover:underline flex items-center gap-1">
            <Download className="h-3 w-3" /> Download template
          </button>
        </div>
        <input
          data-testid="imports-file"
          type="file"
          accept=".xlsx,.csv,.mpp,.xer,.xml"
          onChange={(e) => setFile(e.target.files[0])}
          className="block text-sm"
        />
        <button data-testid="imports-upload" onClick={upload} disabled={!file || busy} className="h-9 px-4 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50">
          {busy ? "Uploading…" : "Import schedule"}
        </button>
      </div>

      {last && (
        <div data-testid="import-last" className="bg-card border border-border rounded-lg p-4 text-sm">
          <div className="font-medium mb-2">Last import</div>
          <div>Rows parsed: <span className="font-mono">{last.row_count}</span></div>
          <div>Created: <span className="font-mono">{last.created}</span></div>
          <div>Updated: <span className="font-mono">{last.updated}</span></div>
          {last.failed?.length ? <div className="text-red-700 text-xs mt-2">Failed: {JSON.stringify(last.failed)}</div> : null}
          {last.warnings?.length ? <div className="text-amber-700 text-xs mt-2">Warnings: {last.warnings.join(", ")}</div> : null}
        </div>
      )}

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border font-medium text-sm">History</div>
        <table className="w-full dense-table">
          <thead><tr><th>When</th><th>Kind</th><th>File</th><th>Rows</th><th>Created</th><th>Updated</th></tr></thead>
          <tbody>
            {history.map((h) => (
              <tr key={h.id} data-testid={`import-row-${h.id}`}>
                <td className="font-mono text-xs">{new Date(h.created_at).toLocaleString()}</td>
                <td>{h.kind}</td>
                <td className="truncate max-w-[280px]">{h.filename}</td>
                <td className="tabular-nums">{h.row_count}</td>
                <td className="tabular-nums">{h.created}</td>
                <td className="tabular-nums">{h.updated}</td>
              </tr>
            ))}
            {!history.length && <tr><td colSpan="6" className="text-center py-6 text-sm text-muted-foreground">No imports yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
