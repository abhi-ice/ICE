import { useState, useEffect } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import { FileUp, Download, AlertTriangle } from "lucide-react";

const ACCEPTED_EXTS = ["xlsx", "csv", "mpp", "xer", "xml"];
// Proxy/ingress in front of FastAPI typically caps at 50–100MB; cap at 75MB
// here so users see a clean error before hitting the network limit.
const MAX_BYTES = 75 * 1024 * 1024;

export default function Imports() {
  const { projects, activeId, setActiveId, loading } = useProjects();
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [history, setHistory] = useState([]);
  const [last, setLast] = useState(null);

  const load = () => api.get("/imports").then(({ data }) => setHistory(data)).catch(() => {});
  useEffect(() => { load(); }, []);

  const activeProject = projects.find((p) => p.id === activeId);

  const onPick = (e) => {
    const f = e.target.files?.[0];
    if (!f) { setFile(null); return; }
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    if (!ACCEPTED_EXTS.includes(ext)) {
      toast.error(`Unsupported file type .${ext}. Accepted: ${ACCEPTED_EXTS.join(", ")}`);
      e.target.value = ""; return;
    }
    if (f.size > MAX_BYTES) {
      toast.error(`File is ${(f.size / 1024 / 1024).toFixed(1)}MB — over the 75MB upload limit. Compress or split it.`);
      e.target.value = ""; return;
    }
    setFile(f);
  };

  const upload = async () => {
    if (!file) return toast.error("Pick a file first.");
    if (!activeId) return toast.error("Select a project at the top before importing a schedule.");
    setBusy(true); setProgress(0);
    try {
      const fd = new FormData();
      fd.append("project_id", activeId);
      fd.append("file", file);
      const { data } = await api.post("/imports/schedule", fd, {
        timeout: 180_000,
        onUploadProgress: (e) => {
          if (e.total) setProgress(Math.min(99, Math.round((e.loaded / e.total) * 100)));
        },
      });
      setLast(data);
      setProgress(100);
      toast.success(
        `Proposal created — ${data.row_count} row(s) parsed. Pending PM/Admin review.`,
        { duration: 4500 }
      );
      // Land the user on the proposal review page so they (or their PM) can act on it.
      setTimeout(() => { window.location.assign(`/schedule-changes?id=${data.id}`); }, 600);
    } catch (e) {
      toast.error(formatImportErr(e));
    } finally {
      setBusy(false);
      setTimeout(() => setProgress(0), 1500);
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

  const noProjects = !loading && projects.length === 0;

  return (
    <div data-testid="imports-page" className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Imports</h1>
        <p className="text-sm text-muted-foreground">
          Upload a schedule file (.xlsx, .csv, .mpp, .xer, or .xml). Imports are scoped to the active project.
        </p>
      </div>

      {/* Project context banner */}
      {noProjects ? (
        <Banner color="amber" testid="imports-no-projects">
          You don&apos;t have any projects yet. Create one on the <a href="/projects" className="underline">Projects</a> page first.
        </Banner>
      ) : !activeId ? (
        <Banner color="amber" testid="imports-pick-project">
          <span>No project selected.</span>
          <select data-testid="imports-pick-project-select"
            value=""
            onChange={(e) => setActiveId(e.target.value)}
            className="ml-3 h-8 px-2 rounded border border-amber-300 bg-white text-sm">
            <option value="" disabled>Choose project…</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Banner>
      ) : (
        <Banner color="slate" testid="imports-active-project">
          Importing into <strong>{activeProject?.name}</strong>. To switch, use the project picker in the top bar.
        </Banner>
      )}

      <div className="bg-card border border-border rounded-lg p-4 space-y-3">
        <div className="flex items-center gap-2">
          <FileUp className="h-4 w-4" />
          <div className="font-medium text-sm">Schedule file</div>
          <button onClick={downloadTemplate} className="ml-auto text-xs text-primary hover:underline flex items-center gap-1">
            <Download className="h-3 w-3" /> Download CSV template
          </button>
        </div>

        <input
          data-testid="imports-file"
          type="file"
          accept=".xlsx,.csv,.mpp,.xer,.xml"
          onChange={onPick}
          disabled={busy}
          className="block text-sm"
        />

        {file && (
          <div data-testid="imports-file-meta" className="text-xs text-slate-500">
            Selected: <span className="font-mono">{file.name}</span> · {(file.size / 1024 / 1024).toFixed(2)}MB
          </div>
        )}

        {busy && (
          <div data-testid="imports-progress" className="text-xs">
            <div className="h-1.5 bg-slate-200 rounded overflow-hidden">
              <div className="h-full bg-blue-600 transition-all" style={{ width: `${progress}%` }} />
            </div>
            <div className="text-slate-500 mt-1">
              {progress < 100 ? `Uploading… ${progress}%` : "Parsing on server (large MPP/XER files take 10–60s)…"}
            </div>
          </div>
        )}

        <button data-testid="imports-upload" onClick={upload} disabled={!file || busy || !activeId}
          className="h-9 px-4 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50 hover:opacity-90">
          {busy ? "Uploading…" : "Import schedule"}
        </button>
      </div>

      {last && (
        <div data-testid="import-last" className="bg-card border border-border rounded-lg p-4 text-sm">
          <div className="font-medium mb-2">Proposal created</div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-2">
            <Stat label="Rows parsed" value={last.row_count} />
            <Stat label="Added" value={last.diff?.summary?.added_count || 0} tone="emerald" />
            <Stat label="Dropped" value={last.diff?.summary?.dropped_count || 0} tone="amber" />
            <Stat label="Date shifts >3d" value={last.diff?.summary?.shifted_count || 0} />
          </div>
          <div className="text-xs text-slate-600">
            Status: <strong>pending review</strong>. AI risk: <strong>{last.ai_review?.risk_level || "—"}</strong>.
            Redirecting to the review page…
          </div>
          {last.parse_warnings?.length ? <div className="text-amber-700 text-xs mt-2">Warnings: {last.parse_warnings.join(", ")}</div> : null}
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

function Banner({ color = "slate", testid, children }) {
  const klass = color === "amber"
    ? "bg-amber-50 border-amber-200 text-amber-800"
    : "bg-slate-50 border-slate-200 text-slate-700";
  return (
    <div data-testid={testid} className={`border rounded-lg p-3 text-sm flex items-center gap-2 ${klass}`}>
      {color === "amber" && <AlertTriangle className="h-4 w-4 shrink-0" />}
      <div>{children}</div>
    </div>
  );
}

function Stat({ label, value, tone = "slate" }) {
  const klass = tone === "amber" ? "text-amber-700" : "text-slate-700";
  return (
    <div className="bg-slate-50 border border-slate-200 rounded p-2">
      <div className="text-xs text-slate-500 tracking-wide">{label}</div>
      <div className={`font-semibold tabular-nums ${klass}`}>{value ?? "—"}</div>
    </div>
  );
}

// Surface backend / network errors clearly. Never show URLs or raw HTML.
function formatImportErr(e) {
  const r = e.response;
  // Network / CORS: axios sets e.message (often "Network Error") and e.code.
  if (!r) {
    if (e.code === "ECONNABORTED") return "Upload timed out. Big MPP files can take a while — try again or split the file.";
    return `Could not reach the server (${e.code || "network error"}). Check your connection and retry.`;
  }
  // Pydantic 422 returns { detail: [{loc, msg, type}, ...] }
  const detail = r.data?.detail;
  if (Array.isArray(detail)) {
    const parts = detail.slice(0, 3).map((d) => `${(d.loc || []).slice(-1)[0] || "?"}: ${d.msg || d}`);
    return `Import failed (${r.status}): ${parts.join("; ")}`;
  }
  if (typeof detail === "string") return `Import failed (${r.status}): ${detail}`;
  if (detail && typeof detail === "object") {
    if (detail.errors) return `Import failed (${r.status}): ${JSON.stringify(detail.errors).slice(0, 240)}`;
    return `Import failed (${r.status}): ${JSON.stringify(detail).slice(0, 240)}`;
  }
  // Proxy returned HTML (e.g., 502/504). Show a clean message instead of dumping HTML.
  if (typeof r.data === "string" && r.data.startsWith("<")) {
    return `Server returned an error page (${r.status}). The file may be too large or the schedule parser timed out.`;
  }
  return `Import failed (${r.status})`;
}
