import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Plus, Eye } from "lucide-react";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import DrawingViewer from "../components/drawings/DrawingViewer";

export default function Drawings() {
  const { activeId, projects } = useProjects();
  const [list, setList] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ sheet_number: "", title: "", discipline: "", drawing_set_name: "Issued for Construction" });
  const [revisionDialogFor, setRevisionDialogFor] = useState(null);
  const [revFile, setRevFile] = useState(null);
  const [revLabel, setRevLabel] = useState("Rev 0");
  const [busy, setBusy] = useState(false);
  const [viewerRev, setViewerRev] = useState(null);
  const project = projects.find((p) => p.id === activeId);

  const load = () => activeId && api.get(`/projects/${activeId}/drawings`).then(({ data }) => setList(data));
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  const create = async () => {
    try {
      await api.post("/drawings", { project_id: activeId, ...form });
      toast.success("Drawing created");
      setOpen(false);
      setForm({ sheet_number: "", title: "", discipline: "", drawing_set_name: "Issued for Construction" });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const uploadRevision = async () => {
    if (!revFile || !revisionDialogFor) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("drawing_id", revisionDialogFor.id);
      fd.append("revision_label", revLabel);
      fd.append("file", revFile);
      const { data } = await api.post("/drawing-revisions/upload", fd);
      toast.success(`Revision uploaded — ${data.page_count} pages`);
      setRevisionDialogFor(null);
      setRevFile(null);
      setRevLabel("Rev 0");
      load();
    } catch (e) {
      toast.error(JSON.stringify(e.response?.data?.detail || e.message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="drawings-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Drawings</h1>
          <p className="text-sm text-slate-500">{project?.name}</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            {/* pt.40: primary sbtn, no Plus icon. */}
            <button data-testid="new-drawing" className="sbtn sbtn-sm sbtn-primary">New drawing</button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>New drawing</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div>
                <label className="field-label">Sheet number *</label>
                <input data-testid="dr-sheet" value={form.sheet_number} onChange={(e) => setForm({ ...form, sheet_number: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="A-101" />
              </div>
              <div>
                <label className="field-label">Title *</label>
                <input data-testid="dr-title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
              <div>
                <label className="field-label">Discipline</label>
                <input value={form.discipline} onChange={(e) => setForm({ ...form, discipline: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="Architectural / Structural / MEP / ..." />
              </div>
              <Button data-testid="dr-create" onClick={create} disabled={!form.sheet_number || !form.title} className="bg-blue-600 hover:bg-blue-700">Create</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="surface overflow-hidden max-md:overflow-x-auto">
        <table className="dense-table">
          <thead><tr><th>Sheet</th><th>Title</th><th>Discipline</th><th>Set</th><th>Rev</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {list.map((d) => (
              <tr key={d.id} data-testid={`drawing-row-${d.sheet_number}`}>
                <td className="font-mono text-xs">{d.sheet_number}</td>
                <td>{d.title}</td>
                <td>{d.discipline || "—"}</td>
                <td className="text-xs">{d.drawing_set_name}</td>
                <td className="tabular-nums">{d.revision_count}</td>
                <td><span className="pill pill-green">{d.status}</span></td>
                <td className="text-right space-x-2">
                  <button data-testid={`upload-rev-${d.sheet_number}`} onClick={() => setRevisionDialogFor(d)} className="text-xs text-blue-600 hover:underline max-md:min-h-[44px] max-md:inline-flex max-md:items-center">Upload PDF</button>
                  {d.current_revision_id && (
                    <button data-testid={`view-rev-${d.sheet_number}`} onClick={() => setViewerRev(d.current_revision_id)} className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline max-md:min-h-[44px] max-md:inline-flex max-md:items-center">
                      <Eye className="h-3 w-3" /> View
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!list.length && <tr><td colSpan="7" className="text-center py-6 text-sm text-slate-500">No drawings. Create one and upload a PDF revision.</td></tr>}
          </tbody>
        </table>
      </div>

      <Dialog open={!!revisionDialogFor} onOpenChange={(o) => !o && setRevisionDialogFor(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Upload revision — {revisionDialogFor?.sheet_number} {revisionDialogFor?.title}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <label className="field-label">Revision label</label>
              <input value={revLabel} onChange={(e) => setRevLabel(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="Rev 1" />
            </div>
            <div>
              <label className="field-label">PDF file *</label>
              <input data-testid="rev-file" type="file" accept=".pdf" onChange={(e) => setRevFile(e.target.files[0])} className="block text-sm" />
            </div>
            <Button data-testid="rev-upload" onClick={uploadRevision} disabled={!revFile || busy} className="bg-blue-600 hover:bg-blue-700">
              {busy ? "Uploading…" : "Upload"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {viewerRev && <DrawingViewer revisionId={viewerRev} onClose={() => setViewerRev(null)} />}
    </div>
  );
}
