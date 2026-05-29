import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Plus, FileUp, BookOpen } from "lucide-react";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import FieldLabel from "../components/forms/FieldLabel";

export default function Specs() {
  const { activeId, projects } = useProjects();
  const [list, setList] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", revision_label: "" });
  const [uploadFor, setUploadFor] = useState(null);
  const [pdfFile, setPdfFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [openSpec, setOpenSpec] = useState(null);
  const [sections, setSections] = useState([]);
  const project = projects.find((p) => p.id === activeId);

  const load = () => activeId && api.get(`/projects/${activeId}/specs`).then(({ data }) => setList(data));
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  const create = async () => {
    try {
      await api.post("/specs", { project_id: activeId, name: form.name, revision_label: form.revision_label || null });
      toast.success("Spec created");
      setOpen(false);
      setForm({ name: "", revision_label: "" });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const upload = async () => {
    if (!pdfFile || !uploadFor) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", pdfFile);
      const { data } = await api.post(`/specs/${uploadFor.id}/upload`, fd);
      toast.success("PDF queued for ingestion (sections + division detection runs in background)");
      setUploadFor(null);
      setPdfFile(null);
      // poll for completion every 1.5s up to 15s
      let tries = 0;
      const poll = setInterval(async () => {
        tries += 1;
        const { data: s } = await api.get(`/projects/${activeId}/specs`);
        const found = s.find((x) => x.id === uploadFor.id);
        if (found && found.processing_status !== "processing" && found.processing_status !== "pending") {
          toast.success(`Ingest complete — ${found.total_sections} sections detected`);
          clearInterval(poll);
          load();
        } else if (tries > 10) clearInterval(poll);
      }, 1500);
      load();
    } catch (e) {
      toast.error(JSON.stringify(e.response?.data?.detail || e.message));
    } finally {
      setBusy(false);
    }
  };

  const openSections = async (s) => {
    setOpenSpec(s);
    const { data } = await api.get(`/specs/${s.id}/sections`);
    setSections(data);
  };

  return (
    <div data-testid="specs-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Specifications</h1>
          <p className="text-sm text-slate-500">{project?.name}</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            {/* pt.40: primary sbtn, no Plus icon. */}
            <button data-testid="new-spec" className="sbtn sbtn-sm sbtn-primary">New spec</button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>New spec</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div>
                <FieldLabel required className="field-label">Name</FieldLabel>
                <input data-testid="spec-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
              <div>
                <FieldLabel optional className="field-label">Revision label</FieldLabel>
                <input value={form.revision_label} onChange={(e) => setForm({ ...form, revision_label: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="100% CD / Addendum 2 / ..." />
              </div>
              <Button data-testid="spec-create" onClick={create} disabled={!form.name} className="bg-blue-600 hover:bg-blue-700">Create</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead><tr><th>Name</th><th>Revision</th><th>Pages</th><th>Sections</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id} data-testid={`spec-row-${s.id}`}>
                <td>{s.name}</td>
                <td>{s.revision_label || "—"}</td>
                <td className="tabular-nums">{s.pdf_page_count || "—"}</td>
                <td className="tabular-nums">{s.total_sections}</td>
                <td>
                  <span className={`pill ${s.processing_status === "complete" ? "pill-green" : s.processing_status === "processing" ? "pill-amber" : s.processing_status === "failed" ? "pill-red" : "pill-slate"}`}>
                    {s.processing_status}
                  </span>
                </td>
                <td className="text-right space-x-2">
                  <button data-testid={`upload-spec-${s.id}`} onClick={() => setUploadFor(s)} className="text-xs text-blue-600 hover:underline inline-flex items-center gap-1"><FileUp className="h-3 w-3" /> Upload PDF</button>
                  {s.total_sections > 0 && (
                    <button data-testid={`open-spec-${s.id}`} onClick={() => openSections(s)} className="text-xs text-blue-600 hover:underline inline-flex items-center gap-1"><BookOpen className="h-3 w-3" /> Open sections</button>
                  )}
                </td>
              </tr>
            ))}
            {!list.length && <tr><td colSpan="6" className="text-center py-6 text-sm text-slate-500">No specs yet. Create one and upload a PDF for MasterFormat division/section detection.</td></tr>}
          </tbody>
        </table>
      </div>

      <Dialog open={!!uploadFor} onOpenChange={(o) => !o && setUploadFor(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Upload PDF — {uploadFor?.name}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <input data-testid="spec-file" type="file" accept=".pdf" onChange={(e) => setPdfFile(e.target.files[0])} />
            <div className="text-xs text-slate-500">Master prompt §14: pdfplumber + MasterFormat (DIVISION / SECTION / PART) regex extraction. Embedding deferred to next iteration (would use Voyage SDK).</div>
            <Button data-testid="spec-upload" onClick={upload} disabled={!pdfFile || busy} className="bg-blue-600 hover:bg-blue-700">
              {busy ? "Uploading…" : "Upload & ingest"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!openSpec} onOpenChange={(o) => !o && setOpenSpec(null)}>
        <DialogContent className="max-w-3xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Sections — {openSpec?.name}</DialogTitle>
          </DialogHeader>
          <div className="overflow-y-auto scrollbar-soft flex-1 space-y-3 pr-1">
            {sections.map((sec) => (
              <div key={sec.id} className="border border-slate-200 rounded p-3" data-testid={`spec-section-${sec.section_code}`}>
                <div className="flex items-center gap-2">
                  <span className="spec-citation">{sec.section_code}</span>
                  <span className="font-medium">{sec.section_title}</span>
                  <span className="ml-auto text-xs text-slate-400">p.{sec.page_start}{sec.page_end !== sec.page_start ? `-${sec.page_end}` : ""}</span>
                </div>
                {sec.text_content && (
                  <pre className="text-xs text-slate-600 mt-2 whitespace-pre-wrap max-h-40 overflow-y-auto">{sec.text_content.slice(0, 1500)}{sec.text_content.length > 1500 ? "…" : ""}</pre>
                )}
              </div>
            ))}
            {!sections.length && <div className="text-sm text-slate-500">No sections detected.</div>}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
