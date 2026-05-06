// Sidebar Delay detail page.
//
// Shows the full delay record, the linked tasks with their CURRENT state
// (so you can see whether the projected impact has played out), and the
// AI delay report (Phase B) with controls to (re)generate it and export
// it to PDF. The AI report is intentionally NOT shown on the daily report
// page — only here.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronLeft, Sparkles, FileDown, Pencil, RefreshCw } from "lucide-react";
import api from "../lib/api";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";

export default function DelayDetail() {
  const { id } = useParams();
  const [delay, setDelay] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [editing, setEditing] = useState(false);
  const [generating, setGenerating] = useState(false);

  const refresh = async () => {
    try {
      const { data } = await api.get(`/delays/${id}`);
      setDelay(data);
      const ids = data.linked_task_ids || [];
      if (ids.length) {
        // Load each task individually — small N, no bulk endpoint exists
        const results = await Promise.all(ids.map((tid) =>
          api.get(`/tasks/${tid}`).then((r) => r.data).catch(() => null)
        ));
        setTasks(results.filter(Boolean));
      } else {
        setTasks([]);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [id]);

  if (!delay) return <div className="p-4 text-sm text-slate-500">Loading delay…</div>;

  const generateAIReport = async () => {
    setGenerating(true);
    try {
      const { data } = await api.post(`/delays/${id}/generate-ai-report`);
      setDelay(data);
      toast.success("AI report generated");
    } catch (e) {
      toast.error(e.response?.data?.detail || "AI report failed");
    } finally { setGenerating(false); }
  };

  const exportAIReportPDF = async () => {
    try {
      const res = await api.get(`/delays/${id}/ai-report/pdf`, { responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = `delay_${id.slice(0, 8)}_ai_report.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.response?.data?.detail || "PDF export failed");
    }
  };

  const sevColor = delay.severity === "critical" || delay.severity === "major" ? "pill-red" : "pill-slate";
  const statColor = delay.status === "resolved" ? "pill-green" : delay.status === "in_progress" ? "pill-amber" : "pill-slate";

  return (
    <div data-testid="delay-detail-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <Link to="/delays" className="text-sm text-blue-600 hover:underline flex items-center gap-1">
          <ChevronLeft className="h-4 w-4" /> Back to delays
        </Link>
      </div>

      <div className="surface p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="flex-1 min-w-[280px]">
            <div className="text-xs text-slate-500 uppercase tracking-wide">Delay logged {delay.date}</div>
            <div className="text-base mt-1 whitespace-pre-wrap">{delay.description}</div>
          </div>
          <div className="flex flex-col gap-1">
            <span className={`pill ${sevColor}`}>{delay.severity}</span>
            <span className={`pill ${statColor}`}>{(delay.status || "open").replace(/_/g, " ")}</span>
            {delay.reason_category && <span className="text-xs text-slate-500">{delay.reason_category}</span>}
          </div>
          <Button data-testid="delay-detail-edit" onClick={() => setEditing(true)} variant="outline" className="gap-1">
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
        </div>

        <div className="mt-3 pt-3 border-t border-slate-100 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <Metric label="Logged date" value={delay.logged_date || delay.date} />
          <Metric label="Resolved date" value={delay.resolved_date || "—"} />
          <Metric label="Days open" value={delay.duration_days ?? "—"} />
          <Metric label="Linked tasks" value={(delay.linked_task_ids || []).length} />
        </div>
        {delay.resolved_note && (
          <div className="mt-2 text-xs text-slate-600">
            <span className="text-slate-400 uppercase tracking-wide mr-1">Resolution note:</span>
            {delay.resolved_note}
          </div>
        )}
      </div>

      <div className="surface p-4">
        <div className="font-medium text-sm mb-3">Linked tasks · current state</div>
        {!tasks.length && <div className="text-xs text-slate-400">No tasks linked to this delay.</div>}
        <div className="space-y-2">
          {tasks.map((t) => {
            const impact = (delay.per_task_impacts || []).find((i) => i.task_id === t.id) || {};
            const flagColor = impact.pushes_critical_path ? "pill-red"
              : impact.consumes_float_only ? "pill-green" : "pill-amber";
            return (
              <div key={t.id} className="border border-slate-200 rounded p-2 text-xs grid grid-cols-1 md:grid-cols-4 gap-2">
                <div className="md:col-span-2">
                  <span className="font-mono text-blue-700">{t.activity_code}</span>
                  <span className="ml-2">{t.description}</span>
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-[10px]">Now</div>
                  <div>pct {t.pct_complete ?? 0}% · float {t.total_float_days ?? "—"}d{t.is_critical_path ? " · CP" : ""}</div>
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-[10px]">At logging</div>
                  <div>impact {impact.projected_impact_days ?? 0}d · float {impact.float_at_logging_days ?? "—"}d</div>
                  <span className={`pill ${flagColor} mt-1 inline-block`}>
                    {impact.pushes_critical_path ? "pushes CP" : impact.consumes_float_only ? "within float" : "exceeds non-CP float"}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="surface p-4">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="h-4 w-4 text-blue-600" />
          <div className="font-medium text-sm">AI delay report</div>
          {delay.ai_report_generated_at && (
            <span className="text-[10px] text-slate-400 ml-2">last generated {new Date(delay.ai_report_generated_at).toLocaleString()}</span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <Button data-testid="delay-ai-generate" onClick={generateAIReport} disabled={generating} className="gap-1 bg-blue-600 hover:bg-blue-700">
              <RefreshCw className={`h-3.5 w-3.5 ${generating ? "animate-spin" : ""}`} />
              {delay.ai_report ? "Regenerate" : "Generate AI delay report"}
            </Button>
            {delay.ai_report && (
              <Button data-testid="delay-ai-pdf" onClick={exportAIReportPDF} variant="outline" className="gap-1">
                <FileDown className="h-3.5 w-3.5" /> Export AI report as PDF
              </Button>
            )}
          </div>
        </div>
        {delay.ai_report ? (
          <div data-testid="delay-ai-report" className="text-sm whitespace-pre-wrap leading-relaxed text-slate-800 bg-slate-50 rounded p-3 border border-slate-200">
            {delay.ai_report}
          </div>
        ) : (
          <div className="text-xs text-slate-400">No AI report yet. Click Generate to produce a structured narrative covering linked tasks, float impact, and recommendations.</div>
        )}
      </div>

      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Edit delay</DialogTitle></DialogHeader>
          <DelayForm
            projectId={delay.project_id}
            initial={delay}
            onSaved={() => { setEditing(false); refresh(); }}
            onCancel={() => setEditing(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div className="bg-slate-50 rounded px-2 py-1.5 border border-slate-100">
      <div className="text-[10px] text-slate-500 uppercase tracking-wide">{label}</div>
      <div className="text-sm tabular-nums">{value}</div>
    </div>
  );
}
