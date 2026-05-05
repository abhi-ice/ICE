// Knowledge base — search past RFIs/work-items/lessons; ingest historical
// records to bootstrap the corpus. Master prompt §6 (RAG).
import { useEffect, useState } from "react";
import { BookOpen, Sparkles, Search, Plus } from "lucide-react";
import api from "../lib/api";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";

const SOURCE_PILL = {
  rfi: "pill-blue", work_item: "pill-amber", lesson: "pill-green",
  submittal: "pill-purple", manual: "pill-slate",
};

export default function KnowledgeBase() {
  const [list, setList] = useState([]);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ title: "", body: "", tags: "", source_kind: "lesson" });

  const refresh = async () => {
    const path = q ? `/kb/entries?q=${encodeURIComponent(q)}` : "/kb/entries?limit=80";
    const { data } = await api.get(path);
    setList(data);
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const ingest = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/kb/ingest-historical");
      toast.success(`Ingested ${data.ingested} historical records into the KB`);
      refresh();
    } catch { toast.error("Ingest failed"); }
    finally { setBusy(false); }
  };

  const create = async () => {
    try {
      await api.post("/kb/entries", {
        ...form,
        tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      toast.success("KB entry saved");
      setOpen(false);
      setForm({ title: "", body: "", tags: "", source_kind: "lesson" });
      refresh();
    } catch (e) { toast.error("Save failed"); }
  };

  return (
    <div data-testid="kb-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BookOpen className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">Knowledge base</h1>
          </div>
          <p className="text-sm text-slate-500">Past resolutions, lessons learned, and answered RFIs. Cited by AI agents in every recommendation.</p>
        </div>
        <div className="flex items-center gap-2">
          <Button data-testid="kb-ingest" onClick={ingest} disabled={busy} variant="outline" className="gap-1">
            <Sparkles className="h-4 w-4" /> {busy ? "Ingesting…" : "Ingest historical"}
          </Button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button data-testid="kb-new" className="bg-blue-600 hover:bg-blue-700 gap-1"><Plus className="h-4 w-4" /> New entry</Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader><DialogTitle>New KB entry</DialogTitle></DialogHeader>
              <div className="space-y-3">
                <input data-testid="kb-form-title" placeholder="Title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                <textarea data-testid="kb-form-body" placeholder="Problem + resolution" value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} className="w-full h-32 p-2 rounded border border-slate-200 text-sm" />
                <input data-testid="kb-form-tags" placeholder="tags, comma, separated" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                <select value={form.source_kind} onChange={(e) => setForm({ ...form, source_kind: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                  <option value="lesson">lesson</option>
                  <option value="manual">manual entry</option>
                </select>
                <Button data-testid="kb-form-save" onClick={create} disabled={!form.title || !form.body} className="bg-blue-600 hover:bg-blue-700">Save</Button>
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
        <input
          data-testid="kb-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && refresh()}
          placeholder="Search past resolutions (e.g. 'concrete cure crack', 'IPS substitution', 'roof penetration RFI')…"
          className="w-full h-10 pl-9 pr-3 rounded border border-slate-200 text-sm"
        />
      </div>

      <div className="space-y-2">
        {list.map((e) => (
          <div key={e.id} data-testid={`kb-row-${e.id}`} className="surface p-3">
            <div className="flex items-center gap-2 mb-1">
              <span className={`pill ${SOURCE_PILL[e.source_kind] || "pill-slate"} text-[10px] capitalize`}>{e.source_kind?.replace(/_/g, " ")}</span>
              {e._score && <span className="pill pill-slate text-[10px]">match {(e._score * 100).toFixed(0)}%</span>}
              {(e.tags || []).map((t) => <span key={t} className="text-[10px] text-slate-500">#{t}</span>)}
              <span className="text-[10px] text-slate-400 ml-auto">{new Date(e.created_at).toLocaleDateString()}</span>
            </div>
            <div className="font-medium text-sm">{e.title}</div>
            <div className="text-xs text-slate-600 mt-1 whitespace-pre-wrap line-clamp-4">{e.body}</div>
          </div>
        ))}
        {!list.length && (
          <div className="text-center py-12 text-sm text-slate-500">
            No KB entries yet. Click <b>Ingest historical</b> to import answered RFIs and resolved field issues.
          </div>
        )}
      </div>
    </div>
  );
}
