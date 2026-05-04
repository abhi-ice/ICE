import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import { Download, FilePlus2 } from "lucide-react";

export default function DailyReports() {
  const { activeId } = useProjects();
  const [reports, setReports] = useState([]);
  const [openId, setOpenId] = useState(null);

  const load = () => activeId && api.get(`/projects/${activeId}/daily-reports`).then(({ data }) => setReports(data));
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  const create = async () => {
    const today = new Date().toISOString().slice(0, 10);
    try {
      const { data } = await api.post("/daily-reports", { project_id: activeId, report_date: today });
      toast.success(`Daily report opened for ${data.report_date}`);
      setOpenId(data.id);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  return (
    <div data-testid="daily-reports-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Daily reports</h1>
          <p className="text-sm text-muted-foreground">Pre-populated from the 2-week lookahead at the time of opening.</p>
        </div>
        <button data-testid="open-today-report" onClick={create} className="h-9 px-3 rounded bg-primary text-primary-foreground text-sm flex items-center gap-2">
          <FilePlus2 className="h-4 w-4" /> Open today's report
        </button>
      </div>

      {openId ? (
        <DailyReportGrid id={openId} onBack={() => setOpenId(null)} />
      ) : (
        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <table className="w-full dense-table">
            <thead><tr><th>Date</th><th>Status</th><th>Submitted at</th><th></th></tr></thead>
            <tbody>
              {reports.map((r) => (
                <tr key={r.id} data-testid={`dr-row-${r.id}`}>
                  <td className="font-mono">{r.report_date}</td>
                  <td><span className={`pill ${r.status === "submitted" ? "pill-green" : "pill-slate"}`}>{r.status}</span></td>
                  <td className="text-xs">{r.submitted_at ? new Date(r.submitted_at).toLocaleString() : "—"}</td>
                  <td>
                    <button data-testid={`open-dr-${r.id}`} onClick={() => setOpenId(r.id)} className="text-sm text-primary hover:underline">Open grid</button>
                  </td>
                </tr>
              ))}
              {!reports.length && <tr><td colSpan="4" className="text-center py-6 text-sm text-muted-foreground">No reports yet.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function DailyReportGrid({ id, onBack }) {
  const [rows, setRows] = useState([]);
  const [report, setReport] = useState(null);

  const load = async () => {
    const [r, g] = await Promise.all([
      api.get(`/daily-reports/${id}`),
      api.get(`/daily-reports/${id}/grid`),
    ]);
    setReport(r.data);
    setRows(g.data);
  };
  useEffect(() => { load(); }, [id]);

  const updateRow = async (rowId, updates) => {
    await api.patch(`/daily-report-activities/${rowId}`, updates);
    load();
  };

  const submit = async () => {
    await api.post(`/daily-reports/${id}/submit`, {});
    toast.success("Submitted");
    load();
  };

  const downloadPdf = async () => {
    const res = await api.get(`/daily-reports/${id}/pdf`, { responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = `daily_report_${report?.report_date}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!report) return <div>Loading…</div>;

  const totalHours = rows.reduce((s, r) => s + (r.hours_worked || 0), 0);
  const totalDelay = rows.reduce((s, r) => s + (r.delay_minutes || 0), 0);
  const planned = rows.length;
  const completed = rows.filter((r) => (r.pct_complete_end_of_day || 0) >= 100).length;
  const ppc = planned ? Math.round((completed / planned) * 100) : 0;

  return (
    <div className="space-y-4 dr-grid" data-testid="dr-grid-view">
      <div className="bg-card border border-border rounded-lg p-4 flex flex-wrap gap-4 items-start">
        <button onClick={onBack} className="text-sm text-primary">← Back</button>
        <div>
          <div className="text-xs text-muted-foreground">Date</div>
          <div className="font-mono text-base">{report.report_date}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Status</div>
          <div><span className={`pill ${report.status === "submitted" ? "pill-green" : "pill-slate"}`}>{report.status}</span></div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">PPC</div>
          <div className="text-xl font-semibold tabular-nums">{ppc}%</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Total hours</div>
          <div className="text-xl font-semibold tabular-nums">{totalHours}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Delay (min)</div>
          <div className="text-xl font-semibold tabular-nums">{totalDelay}</div>
        </div>
        <div className="ml-auto flex gap-2">
          <button data-testid="dr-download-pdf" onClick={downloadPdf} className="h-9 px-3 rounded border border-border text-sm flex items-center gap-1">
            <Download className="h-4 w-4" /> Export
          </button>
          {report.status !== "submitted" && (
            <button data-testid="dr-submit" onClick={submit} className="h-9 px-3 rounded bg-primary text-primary-foreground text-sm">
              Submit
            </button>
          )}
        </div>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-x-auto">
        <table className="w-full dense-table">
          <thead>
            <tr><th>Code</th><th>Description</th><th>Loc</th><th>Trade</th><th>Crew</th><th>%</th><th>Hours</th><th>Delay min</th><th>Notes</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} data-testid={`dr-row-${r.activity_code}`}>
                <td className="font-mono text-xs">{r.activity_code}</td>
                <td className="truncate max-w-[260px]">{r.activity_description}</td>
                <td className="text-xs">{r.location_name || "—"}</td>
                <td className="text-xs">{r.trade_code || "—"}</td>
                <td className="tabular-nums">
                  <input data-testid={`dr-crew-${r.activity_code}`} type="number" defaultValue={r.crew_count} onBlur={(e) => updateRow(r.id, { crew_count: Number(e.target.value) })} className="w-16" disabled={report.status === "submitted"} />
                </td>
                <td className="tabular-nums">
                  <input data-testid={`dr-pct-${r.activity_code}`} type="number" min="0" max="100" defaultValue={r.pct_complete_end_of_day} onBlur={(e) => updateRow(r.id, { pct_complete_end_of_day: Number(e.target.value) })} className="w-16" disabled={report.status === "submitted"} />
                  <span className="text-xs text-muted-foreground ml-1">/ {r.pct_complete_start_of_day}</span>
                </td>
                <td>
                  <input type="number" defaultValue={r.hours_worked} onBlur={(e) => updateRow(r.id, { hours_worked: Number(e.target.value) })} className="w-16" disabled={report.status === "submitted"} />
                </td>
                <td>
                  <input type="number" defaultValue={r.delay_minutes} onBlur={(e) => updateRow(r.id, { delay_minutes: Number(e.target.value) })} className="w-16" disabled={report.status === "submitted"} />
                </td>
                <td>
                  <input type="text" defaultValue={r.notes || ""} onBlur={(e) => updateRow(r.id, { notes: e.target.value })} placeholder="Notes…" className="w-44" disabled={report.status === "submitted"} />
                </td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan="9" className="text-center py-6 text-sm text-muted-foreground">No activities planned for today. The grid auto-populates from the 2WLA when activities have planned work today.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
