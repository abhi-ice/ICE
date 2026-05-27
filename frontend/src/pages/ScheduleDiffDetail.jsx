// Bucket C3c Phase 1 — Schedule update diff detail page. Tabs across the
// payload (added/removed/modified/predecessors/CP), AI narrative button,
// and PDF export.
import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { ChevronLeft, Sparkles, FileDown, GitPullRequest } from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";

const TABS = [
  { key: "summary", label: "Summary" },
  { key: "added", label: "Added" },
  { key: "removed", label: "Removed" },
  { key: "modified", label: "Modified" },
  { key: "predecessors", label: "Predecessors" },
  { key: "cp", label: "Critical path" },
];

function ChangeCell({ change }) {
  if (!change) return <span className="text-slate-300">—</span>;
  return (
    <span className="text-xs">
      <span className="text-slate-400 line-through">{String(change.prior ?? "—")}</span>
      <span className="mx-1 text-slate-400">→</span>
      <span className="text-slate-800 font-medium">{String(change.new ?? "—")}</span>
    </span>
  );
}

export default function ScheduleDiffDetail() {
  const { id } = useParams();
  const [doc, setDoc] = useState(null);
  const [tab, setTab] = useState("summary");
  const [loading, setLoading] = useState(true);
  const [generatingNarrative, setGeneratingNarrative] = useState(false);
  const [pdfBusy, setPdfBusy] = useState(false);

  useEffect(() => {
    setLoading(true);
    api.get(`/schedule-diffs/${id}`)
      .then((r) => setDoc(r.data))
      .catch((e) => toast.error(e.response?.data?.detail || "Failed to load diff"))
      .finally(() => setLoading(false));
  }, [id]);

  const generateNarrative = async () => {
    setGeneratingNarrative(true);
    try {
      const r = await api.post(`/schedule-diffs/${id}/ai-narrative`);
      setDoc((d) => d ? { ...d, ...r.data } : d);
      toast.success("Narrative generated");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Narrative generation failed");
    } finally {
      setGeneratingNarrative(false);
    }
  };

  const downloadPdf = async () => {
    setPdfBusy(true);
    try {
      const r = await api.get(`/schedule-diffs/${id}/pdf`, { responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url; a.download = `schedule_diff_${id}.pdf`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.response?.data?.detail || "PDF export failed");
    } finally {
      setPdfBusy(false);
    }
  };

  if (loading) return <div className="text-sm text-slate-500">Loading…</div>;
  if (!doc) return <div className="text-sm text-slate-500">Diff not found.</div>;

  return (
    /* pt.36: dropped p-6 -- AppLayout main already pads p-6. */
    <div data-testid="schedule-diff-detail">
      <Link to="/schedule-updates" className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 mb-3">
        <ChevronLeft className="h-3 w-3" /> Back to updates
      </Link>
      <header className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-2">
            <GitPullRequest className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800">Schedule update</h1>
          </div>
          <div className="text-xs text-slate-500 mt-1">
            Computed {new Date(doc.computed_at).toLocaleString()}
            <span className="mx-2 text-slate-300">·</span>
            <span className="font-mono">{(doc.earlier_schedule_version_id || "").slice(0, 8)} → {(doc.later_schedule_version_id || "").slice(0, 8)}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            data-testid="diff-generate-narrative"
            onClick={generateNarrative}
            disabled={generatingNarrative}
            className="h-8 px-3 rounded bg-amber-500 text-white text-sm hover:bg-amber-600 disabled:opacity-50 inline-flex items-center gap-1"
          >
            <Sparkles className="h-3.5 w-3.5" />
            {generatingNarrative ? "Generating…" : (doc.ai_narrative ? "Regenerate AI narrative" : "Generate AI narrative")}
          </button>
          <button
            data-testid="diff-export-pdf"
            onClick={downloadPdf}
            disabled={pdfBusy}
            className="h-8 px-3 rounded bg-slate-700 text-white text-sm hover:bg-slate-800 disabled:opacity-50 inline-flex items-center gap-1"
          >
            <FileDown className="h-3.5 w-3.5" /> {pdfBusy ? "Exporting…" : "Export PDF"}
          </button>
        </div>
      </header>

      {doc.ai_narrative && (
        <div className="bg-amber-50 border-l-4 border-amber-400 p-3 rounded mb-4 text-sm text-slate-700 whitespace-pre-wrap">
          {doc.ai_narrative}
        </div>
      )}

      <div className="border-b border-slate-200 mb-3 flex gap-1">
        {TABS.map((t) => (
          <button
            key={t.key}
            data-testid={`tab-${t.key}`}
            onClick={() => setTab(t.key)}
            className={`px-3 py-1.5 text-sm border-b-2 -mb-px ${
              tab === t.key ? "border-blue-500 text-slate-800 font-medium" : "border-transparent text-slate-500 hover:text-slate-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "summary" && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat n={doc.activities_added_count} label="Activities added" />
          <Stat n={doc.activities_removed_count} label="Activities removed" />
          <Stat n={doc.activities_modified_count} label="Activities modified" />
          <Stat n={doc.cp_changes_count} label="Critical path changes" />
          <Stat n={doc.predecessors_added_count} label="Predecessors added" />
          <Stat n={doc.predecessors_removed_count} label="Predecessors removed" />
          <Stat n={`${(doc.total_change_percentage ?? 0).toFixed?.(1) ?? "0.0"}%`} label="Change rate" />
          <Stat n={doc.total_float_distribution_shift?.delta ?? 0} label="Critical activities Δ" />
        </div>
      )}

      {tab === "added" && <SimpleTable rows={doc.activities_added}
        cols={[["activity_code", "Code"], ["description", "Description"], ["duration_days", "Days"],
               ["start_date", "Start"], ["end_date", "End"]]} />}

      {tab === "removed" && <SimpleTable rows={doc.activities_removed}
        cols={[["activity_code", "Code"], ["description", "Description"], ["duration_days", "Days"],
               ["start_date", "Start"], ["end_date", "End"]]} />}

      {tab === "modified" && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="bg-slate-100 text-slate-600">
                <th className="text-left p-2 border border-slate-200">Code</th>
                <th className="text-left p-2 border border-slate-200">Description</th>
                <th className="text-left p-2 border border-slate-200">Duration</th>
                <th className="text-left p-2 border border-slate-200">Start</th>
                <th className="text-left p-2 border border-slate-200">End</th>
                <th className="text-left p-2 border border-slate-200">Status</th>
                <th className="text-left p-2 border border-slate-200">% complete</th>
              </tr>
            </thead>
            <tbody>
              {(doc.activities_modified || []).map((m, i) => (
                <tr key={i} data-testid={`modified-row-${m.activity_code}`}>
                  <td className="p-2 border border-slate-200 font-mono">{m.activity_code}</td>
                  <td className="p-2 border border-slate-200"><ChangeCell change={m.description_change} /></td>
                  <td className="p-2 border border-slate-200"><ChangeCell change={m.duration_change} /></td>
                  <td className="p-2 border border-slate-200"><ChangeCell change={m.start_change} /></td>
                  <td className="p-2 border border-slate-200"><ChangeCell change={m.end_change} /></td>
                  <td className="p-2 border border-slate-200"><ChangeCell change={m.status_change} /></td>
                  <td className="p-2 border border-slate-200"><ChangeCell change={m.pct_change} /></td>
                </tr>
              ))}
              {!(doc.activities_modified || []).length && (
                <tr><td colSpan={7} className="p-4 text-center text-slate-400">No modifications.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === "predecessors" && <SimpleTable rows={doc.predecessor_changes}
        cols={[["successor_id", "Successor"], ["predecessor_id", "Predecessor"],
               ["change_type", "Change"], ["new_relationship", "Relationship"], ["new_lag_days", "Lag (days)"]]} />}

      {tab === "cp" && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <h3 className="text-sm font-medium text-slate-700 mb-2">Newly critical</h3>
            {(doc.newly_critical_activities || []).length ? (
              <ul className="text-xs font-mono space-y-1">
                {doc.newly_critical_activities.map((c) => <li key={c} className="text-emerald-700">+ {c}</li>)}
              </ul>
            ) : <div className="text-xs text-slate-400">None.</div>}
          </div>
          <div>
            <h3 className="text-sm font-medium text-slate-700 mb-2">No longer critical</h3>
            {(doc.removed_critical_activities || []).length ? (
              <ul className="text-xs font-mono space-y-1">
                {doc.removed_critical_activities.map((c) => <li key={c} className="text-rose-700">− {c}</li>)}
              </ul>
            ) : <div className="text-xs text-slate-400">None.</div>}
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ n, label }) {
  return (
    <div className="bg-white border border-slate-200 rounded p-3">
      <div className="text-2xl font-semibold text-slate-800">{n}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}

function SimpleTable({ rows, cols }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="bg-slate-100 text-slate-600">
            {cols.map(([k, l]) => <th key={k} className="text-left p-2 border border-slate-200">{l}</th>)}
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r, i) => (
            <tr key={i}>
              {cols.map(([k]) => {
                const v = r[k];
                let display = v;
                if (Array.isArray(v)) display = v.join(", ");
                else if (v === null || v === undefined) display = "—";
                return <td key={k} className="p-2 border border-slate-200">{display}</td>;
              })}
            </tr>
          ))}
          {!(rows || []).length && (
            <tr><td colSpan={cols.length} className="p-4 text-center text-slate-400">None.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
