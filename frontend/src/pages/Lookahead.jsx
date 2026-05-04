import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";

export default function Lookahead() {
  const { activeId } = useProjects();
  const [days, setDays] = useState(14);
  const [rows, setRows] = useState([]);

  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/lookahead?days=${days}`).then(({ data }) => setRows(data));
  }, [activeId, days]);

  return (
    <div data-testid="lookahead-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">{days}-day Lookahead</h1>
          <p className="text-sm text-muted-foreground">Date-bounded query over schedule activities (per master prompt §3).</p>
        </div>
        <div className="flex items-center gap-2">
          {[7, 14, 21, 28].map((d) => (
            <button key={d} data-testid={`lookahead-window-${d}`} onClick={() => setDays(d)} className={`h-8 px-3 rounded border text-sm ${days === d ? "bg-primary text-primary-foreground border-primary" : "border-border"}`}>
              {d}d
            </button>
          ))}
        </div>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead>
            <tr><th>Code</th><th>Description</th><th>Trade</th><th>Plan start</th><th>Plan finish</th><th>%</th><th>Status</th></tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id} data-testid={`lookahead-row-${t.activity_code}`}>
                <td className="font-mono text-xs">{t.activity_code}</td>
                <td>{t.description}</td>
                <td>{t.trade_code || "—"}</td>
                <td className="font-mono text-xs">{t.planned_start}</td>
                <td className="font-mono text-xs">{t.planned_finish}</td>
                <td>{t.pct_complete}%</td>
                <td><span className={`pill ${t.status === "in_progress" ? "pill-blue" : "pill-slate"}`}>{t.status}</span></td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan="7" className="text-center py-8 text-sm text-muted-foreground">No activities in window.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
