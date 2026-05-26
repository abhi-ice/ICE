// Bucket C3c Phase 4 — TIA source document generator. One screen, three
// outputs: structured JSON, Word doc, evidence-index PDF. Date range +
// optional filters; a generation can take 30-60s on big projects.
//
// Followup-fix 2 (2026-05-07): backend now returns 202 + status=pending
// for large jobs (>200 evidence items). Frontend polls every 5s until
// status flips to complete/failed, then renders the artifacts.
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { FileText, Download, History, Sparkles, ShieldAlert, Loader2 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

function isoToday() { return new Date().toISOString().slice(0, 10); }

function isoFromNTPOrYearAgo(proj) {
  if (proj?.ntp_date) return String(proj.ntp_date).slice(0, 10);
  const d = new Date();
  d.setFullYear(d.getFullYear() - 1);
  return d.toISOString().slice(0, 10);
}

const CAUSE_CLASSES = [
  "design_clarification", "design_change", "procurement", "regulatory",
  "weather", "owner_directed", "tenant_access", "building_coordination",
  "labor", "subcontractor", "other",
];

export default function TIAGenerator() {
  const { activeId, projects } = useProjects();
  const proj = (projects || []).find((p) => p.id === activeId);

  const [start, setStart] = useState(isoFromNTPOrYearAgo(proj));
  const [end, setEnd] = useState(isoToday());
  const [causeClass, setCauseClass] = useState("");
  const [zoneId, setZoneId] = useState("");
  const [gatingEntityId, setGatingEntityId] = useState("");

  const [generating, setGenerating] = useState(false);
  const [latest, setLatest] = useState(null);
  // Followup-fix 2 (2026-05-07): when the backend returns 202 with
  // status pending/running, poll every 5s until status flips to
  // complete or failed. The pollRef cleanup runs on unmount or when
  // a new generation kicks off.
  const pollRef = useRef(null);

  // Refresh start when project changes (NTP-driven default).
  useEffect(() => { if (proj) setStart(isoFromNTPOrYearAgo(proj)); }, [proj?.id]);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const startPolling = (generationId) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const r = await api.get(`/tia/generations/${generationId}`);
        const row = r.data;
        setLatest(row);
        if (row?.status === "complete" || row?.status === "failed") {
          clearInterval(pollRef.current); pollRef.current = null;
          setGenerating(false);
          if (row.status === "complete") {
            toast.success(`TIA generated (${row.total_evidence_items || 0} evidence items)`);
          } else {
            toast.error(`TIA generation failed: ${row.failure_reason || row.error || "unknown"}`);
          }
        }
      } catch (e) {
        clearInterval(pollRef.current); pollRef.current = null;
        setGenerating(false);
        toast.error("TIA polling failed: " + (e.response?.data?.detail || e.message));
      }
    }, 5000);
  };

  const generate = async () => {
    if (!activeId) return;
    setGenerating(true);
    setLatest(null);
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    try {
      const r = await api.post(`/projects/${activeId}/tia/generate`, {
        project_id: activeId,
        date_range_start: start,
        date_range_end: end,
        cause_class: causeClass || null,
        zone_id: zoneId || null,
        gating_entity_id: gatingEntityId || null,
      });
      const row = r.data;
      setLatest(row);
      // 202 path -- backend handed off to BackgroundTask. Poll until done.
      if (row?.status === "pending" || row?.status === "running") {
        toast.info(`Generating TIA in background (~${row.estimated_evidence_count || "?"} items)…`);
        startPolling(row.id);
        return;
      }
      // Sync path -- complete inline.
      setGenerating(false);
      toast.success(`TIA generated (${row?.total_evidence_items || 0} evidence items)`);
    } catch (e) {
      setGenerating(false);
      toast.error(e.response?.data?.detail || "TIA generation failed");
    }
  };

  const download = async (kind) => {
    if (!latest?.id) return;
    try {
      const r = await api.get(`/tia/generations/${latest.id}/${kind}`, { responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url;
      const ext = kind === "word" ? "docx" : kind;
      a.download = `tia_${latest.id.slice(0, 8)}.${ext}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Download failed");
    }
  };

  if (!activeId) return <div className="p-6 text-sm text-slate-500">Pick a project to generate a TIA.</div>;

  return (
    <div className="p-6" data-testid="tia-generator">
      <header className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <FileText className="h-5 w-5 text-slate-700" />
          <h1 className="text-xl font-semibold text-slate-800">Time Impact Analysis — Source Document Generator</h1>
        </div>
        <Link to="/tia/history" className="text-xs text-slate-500 hover:text-slate-700 inline-flex items-center gap-1">
          <History className="h-3.5 w-3.5" /> Generation history
        </Link>
      </header>

      <div className="bg-amber-50 border border-amber-200 rounded p-3 mb-4 text-xs text-slate-700 flex gap-2">
        <ShieldAlert className="h-4 w-4 text-amber-600 flex-shrink-0" />
        <div>
          <strong>Counsel privileged work product (default).</strong> Generated TIA documents are flagged privileged and access-restricted.
          An admin can unflag from the generation detail to allow broader distribution.
        </div>
      </div>

      <div className="bg-white border border-slate-200 rounded p-4 mb-4 space-y-3">
        <h2 className="text-sm font-medium text-slate-700">Inputs</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          <div>
            <label className="text-xs text-slate-500">Date range start</label>
            <input data-testid="tia-start" type="date" value={start} onChange={(e) => setStart(e.target.value)}
              className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500">Date range end</label>
            <input data-testid="tia-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)}
              className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500">Cause class (optional)</label>
            <select data-testid="tia-cause" value={causeClass} onChange={(e) => setCauseClass(e.target.value)}
              className="block w-full h-9 px-2 rounded border border-slate-300 text-sm bg-white">
              <option value="">(any)</option>
              {CAUSE_CLASSES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-slate-500">Zone ID (optional)</label>
            <input data-testid="tia-zone" type="text" value={zoneId} onChange={(e) => setZoneId(e.target.value)}
              placeholder="e.g. floor-3-east" className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500">Gating entity ID (optional)</label>
            <input data-testid="tia-gate" type="text" value={gatingEntityId} onChange={(e) => setGatingEntityId(e.target.value)}
              placeholder="paste a UUID to scope" className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
          </div>
        </div>
        <div>
          <button data-testid="tia-generate" onClick={generate} disabled={generating}
            className="h-10 px-4 rounded bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-500 transition disabled:opacity-50 inline-flex items-center gap-2">
            <Sparkles className="h-4 w-4" />
            {generating ? "Generating (this can take a minute)…" : "Generate TIA"}
          </button>
        </div>
      </div>

      {latest && (
        <div className="bg-white border border-slate-200 rounded p-4">
          <h2 className="text-sm font-medium text-slate-700 mb-3 flex items-center gap-2">
            Latest generation
            {(latest.status === "pending" || latest.status === "running" || latest.status === "generating") && (
              <span data-testid="tia-status-spinner" className="inline-flex items-center gap-1 text-xs text-blue-700">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> {latest.status}…
              </span>
            )}
            {latest.status === "failed" && (
              <span data-testid="tia-status-failed" className="text-xs text-rose-700 bg-rose-50 px-2 py-0.5 rounded">
                failed
              </span>
            )}
          </h2>
          {latest.status === "failed" && (latest.failure_reason || latest.error) && (
            <div className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded p-2 mb-3">
              {latest.failure_reason || latest.error}
            </div>
          )}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
            <Stat n={latest.delay_events_count} label="Delays" />
            <Stat n={latest.mitigations_count} label="Mitigations" />
            <Stat n={latest.coordination_events_count} label="Coordination" />
            <Stat n={latest.photos_evidentiary_count} label="Photos" />
            <Stat n={latest.schedule_diffs_count} label="Schedule diffs" />
            <Stat n={latest.total_evidence_items} label="Total evidence items" />
          </div>
          <div className="text-xs text-slate-500 mb-3">
            Generation ID: <span className="font-mono">{latest.id}</span> · Status: {latest.status}
            {latest.estimated_evidence_count != null && latest.status !== "complete" && (
              <span className="ml-2">· est. {latest.estimated_evidence_count} items</span>
            )}
            {latest.counsel_privileged_work_product && (
              <span className="ml-2 px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 text-xs">PRIVILEGED</span>
            )}
          </div>
          <div className="flex items-center gap-2 flex-wrap" style={{ opacity: latest.status === "complete" ? 1 : 0.5 }}>
            <button data-testid="tia-download-json" onClick={() => download("json")}
              disabled={latest.status !== "complete"}
              className="h-9 px-3 rounded bg-slate-700 text-white text-sm hover:bg-slate-800 inline-flex items-center gap-1 disabled:cursor-not-allowed">
              <Download className="h-3.5 w-3.5" /> Structured JSON
            </button>
            <button data-testid="tia-download-word" onClick={() => download("word")}
              disabled={latest.status !== "complete"}
              className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1 disabled:cursor-not-allowed">
              <Download className="h-3.5 w-3.5" /> Word document
            </button>
            <button data-testid="tia-download-pdf" onClick={() => download("pdf")}
              disabled={latest.status !== "complete"}
              className="h-9 px-3 rounded bg-rose-600 text-white text-sm hover:bg-rose-700 inline-flex items-center gap-1 disabled:cursor-not-allowed">
              <Download className="h-3.5 w-3.5" /> Evidence index PDF
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ n, label }) {
  return (
    <div className="bg-slate-50 border border-slate-200 rounded p-2">
      <div className="text-xl font-semibold text-slate-800">{n ?? 0}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}
