// Bucket C3c Phase 4 — TIA generation detail page. Shows the run record,
// counts, download log, and the three download buttons. Phase 5 wires
// privilege flag/unflag controls and access gating.
import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { ChevronLeft, Download, ShieldAlert, ShieldOff, History } from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";

export default function TIAGenerationDetail() {
  const { id } = useParams();
  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    api.get(`/tia/generations/${id}`)
      .then((r) => setDoc(r.data))
      .catch((e) => toast.error(e.response?.data?.detail || "Failed to load"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [id]);

  const download = async (kind) => {
    try {
      const r = await api.get(`/tia/generations/${id}/${kind}`, { responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url;
      const ext = kind === "word" ? "docx" : kind;
      a.download = `tia_${id.slice(0, 8)}.${ext}`;
      a.click();
      URL.revokeObjectURL(url);
      load();  // refresh download_log
    } catch (e) {
      const status = e.response?.status;
      if (status === 403) toast.error("Privileged — admin must unflag before this can be downloaded.");
      else toast.error(e.response?.data?.detail || "Download failed");
    }
  };

  if (loading) return <div className="p-6 text-sm text-slate-500">Loading…</div>;
  if (!doc) return <div className="p-6 text-sm text-slate-500">Generation not found.</div>;

  return (
    <div className="p-6 max-w-5xl mx-auto" data-testid="tia-detail">
      <Link to="/tia/history" className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 mb-3">
        <ChevronLeft className="h-3 w-3" /> Back to history
      </Link>
      <header className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-800">TIA generation detail</h1>
        {doc.counsel_privileged_work_product
          ? <span className="inline-flex items-center gap-1 text-xs text-amber-700 bg-amber-100 px-2 py-1 rounded"><ShieldAlert className="h-3 w-3" /> Privileged work product</span>
          : <span className="inline-flex items-center gap-1 text-xs text-slate-500 bg-slate-100 px-2 py-1 rounded"><ShieldOff className="h-3 w-3" /> Open</span>}
      </header>

      <div className="bg-white border border-slate-200 rounded p-4 mb-4 space-y-2 text-sm">
        <div><span className="text-slate-500 text-xs uppercase">Generated:</span> {new Date(doc.generated_at).toLocaleString()}</div>
        <div><span className="text-slate-500 text-xs uppercase">Date range:</span> {doc.date_range_start} → {doc.date_range_end}</div>
        <div><span className="text-slate-500 text-xs uppercase">Status:</span> {doc.status}</div>
        {doc.error && <div className="text-rose-600 text-xs">{doc.error}</div>}
        {Object.keys(doc.filters_applied || {}).length > 0 && (
          <div><span className="text-slate-500 text-xs uppercase">Filters:</span> <span className="font-mono text-xs">{JSON.stringify(doc.filters_applied)}</span></div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-2 mb-4">
        <Stat n={doc.delay_events_count} label="Delays" />
        <Stat n={doc.mitigations_count} label="Mitigations" />
        <Stat n={doc.coordination_events_count} label="Coordination" />
        <Stat n={doc.photos_evidentiary_count} label="Evidentiary photos" />
        <Stat n={doc.schedule_diffs_count} label="Schedule diffs" />
        <Stat n={doc.total_evidence_items} label="Total evidence items" />
      </div>

      {doc.status === "complete" && (
        <div className="bg-white border border-slate-200 rounded p-4 mb-4">
          <h2 className="text-sm font-medium text-slate-700 mb-3">Downloads</h2>
          <div className="flex items-center gap-2 flex-wrap">
            <button data-testid="tia-detail-download-json" onClick={() => download("json")}
              className="h-9 px-3 rounded bg-slate-700 text-white text-sm hover:bg-slate-800 inline-flex items-center gap-1">
              <Download className="h-3.5 w-3.5" /> Structured JSON
            </button>
            <button data-testid="tia-detail-download-word" onClick={() => download("word")}
              className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1">
              <Download className="h-3.5 w-3.5" /> Word document
            </button>
            <button data-testid="tia-detail-download-pdf" onClick={() => download("pdf")}
              className="h-9 px-3 rounded bg-rose-600 text-white text-sm hover:bg-rose-700 inline-flex items-center gap-1">
              <Download className="h-3.5 w-3.5" /> Evidence index PDF
            </button>
          </div>
        </div>
      )}

      <div className="bg-white border border-slate-200 rounded p-4">
        <h2 className="text-sm font-medium text-slate-700 mb-3 flex items-center gap-1">
          <History className="h-4 w-4" /> Download log ({(doc.download_log || []).length})
        </h2>
        {!(doc.download_log || []).length ? (
          <div className="text-xs text-slate-400">No downloads yet.</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500">
                <th className="text-left p-1">When</th>
                <th className="text-left p-1">Format</th>
                <th className="text-left p-1">By</th>
                <th className="text-left p-1">Privilege at download</th>
              </tr>
            </thead>
            <tbody>
              {(doc.download_log || []).slice().reverse().map((d, i) => (
                <tr key={i} data-testid={`download-log-${i}`}>
                  <td className="p-1">{new Date(d.downloaded_at).toLocaleString()}</td>
                  <td className="p-1 uppercase">{d.format}</td>
                  <td className="p-1 font-mono text-[10px]">{(d.downloaded_by_user_id || "").slice(0, 8)}</td>
                  <td className="p-1">{d.privilege_state_at_download ? "privileged" : "open"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Stat({ n, label }) {
  return (
    <div className="bg-white border border-slate-200 rounded p-3">
      <div className="text-2xl font-semibold text-slate-800">{n ?? 0}</div>
      <div className="text-[11px] text-slate-500 uppercase tracking-wide">{label}</div>
    </div>
  );
}
