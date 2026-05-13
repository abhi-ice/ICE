// Lookahead — date-bounded query view. Master prompt §3.
// View: list (table) OR Gantt-style timeline.
// Drag a Gantt bar horizontally to reschedule the activity (PATCH /tasks/:id).
import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarRange, List, BarChart3 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import ExportDropdown from "../components/ExportDropdown";

const DAY_PX = 32; // pixels per day in Gantt
const ROW_H = 32;

export default function Lookahead() {
  const { activeId } = useProjects();
  const [days, setDays] = useState(14);
  const [rows, setRows] = useState([]);
  const [view, setView] = useState("list");

  const refresh = async () => {
    if (!activeId) return;
    const { data } = await api.get(`/projects/${activeId}/lookahead?days=${days}`);
    setRows(data);
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId, days]);

  return (
    <div data-testid="lookahead-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CalendarRange className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">{days}-day lookahead</h1>
          </div>
          <p className="text-sm text-slate-500">Date-bounded query · {rows.length} activities</p>
        </div>
        <div className="flex items-center gap-2">
          {[7, 14, 21, 28].map((d) => (
            <button key={d} data-testid={`lookahead-window-${d}`} onClick={() => setDays(d)} className={`h-8 px-3 rounded border text-sm ${days === d ? "bg-blue-600 text-white border-blue-600" : "border-slate-200"}`}>
              {d}d
            </button>
          ))}
          <div className="ml-2 flex items-center gap-1 border border-slate-200 rounded p-0.5">
            <button data-testid="lookahead-list" onClick={() => setView("list")} className={`h-7 px-3 rounded text-xs flex items-center gap-1 ${view === "list" ? "bg-slate-100" : ""}`}>
              <List className="h-3 w-3" /> List
            </button>
            <button data-testid="lookahead-gantt" onClick={() => setView("gantt")} className={`h-7 px-3 rounded text-xs flex items-center gap-1 ${view === "gantt" ? "bg-slate-100" : ""}`}>
              <BarChart3 className="h-3 w-3" /> Gantt
            </button>
          </div>
          {/* Bug #43 (2026-05-12) -- Look-ahead export. Honors the
              currently-selected `days` window. */}
          <ExportDropdown
            label="Export"
            testidPrefix="lookahead-export"
            buildUrl={(fmt) => `/projects/${activeId}/lookahead/export?format=${fmt}&days=${days}`}
            filenameHint={(fmt) => `lookahead_${(activeId || "").slice(0, 8)}.${fmt}`}
            disabled={!activeId}
          />
        </div>
      </div>

      {view === "list" && (
        <div className="surface overflow-hidden">
          <table className="dense-table">
            <thead><tr>
              <th>Code</th><th>Description</th><th>Trade</th>
              <th>Plan start</th><th>Plan finish</th>
              <th title="Bucket C1 Phase 4 — projected_finish_date when open delays push the task past its planned_finish.">Projected</th>
              <th>%</th>
              <th title="Bucket C1 Phase 2 computed_status">Status</th>
            </tr></thead>
            <tbody>
              {rows.map((t) => {
                // Bucket C1 Phase 4 — at-risk highlighting. Red when blocker
                // present, amber when behind or projected_finish > planned.
                //
                // Followup-fix 3 (2026-05-07): the row tint already
                // amber-tinted on projShifted, but the status PILL still
                // displayed "on track" with slate color when
                // computed_status was on_track and only the projection
                // had moved. PMs scanning the column missed at-risk
                // tasks. Now the pill text/color reflects projShifted
                // even when the underlying computed_status is on_track.
                const cs = t.computed_status || "on_track";
                const projShifted = t.projected_finish_date && t.planned_finish &&
                  t.projected_finish_date > t.planned_finish;
                const blocked = cs === "blocked" || t.has_open_blocker;
                const atRisk = !blocked && cs !== "behind" && cs !== "complete" && projShifted;
                const tint = blocked ? "bg-red-50/50"
                  : (cs === "behind" || projShifted) ? "bg-amber-50/50"
                  : "";
                const csClass = cs === "complete" ? "pill-green"
                  : blocked ? "pill-red"
                  : cs === "behind" || atRisk ? "pill-amber"
                  : "pill-slate";
                const pillLabel = blocked ? "blocked"
                  : cs === "complete" ? "complete"
                  : cs === "behind" ? "behind"
                  : atRisk ? "at risk"
                  : cs.replace(/_/g, " ");
                return (
                  <tr key={t.id} data-testid={`lookahead-row-${t.activity_code}`} className={tint}>
                    <td className="font-mono text-xs">{t.activity_code}</td>
                    <td>{t.description}</td>
                    <td>{t.trade_code || "—"}</td>
                    <td className="font-mono text-xs">{t.planned_start}</td>
                    <td className="font-mono text-xs">{t.planned_finish}</td>
                    <td className="font-mono text-xs">
                      {t.projected_finish_date
                        ? <span className={projShifted ? "text-amber-700 font-semibold" : "text-slate-500"}
                            title={projShifted ? `Projected ${t.projected_finish_date} (${slipDays(t.planned_finish, t.projected_finish_date)}d slip)` : "On schedule"}>
                            {t.projected_finish_date}
                          </span>
                        : <span className="text-slate-300">—</span>}
                    </td>
                    <td>{t.pct_complete}%</td>
                    <td>
                      <span data-testid={`lookahead-status-${t.activity_code}`}
                        className={`pill ${csClass} inline-flex items-center gap-1`}
                        title={atRisk ? `Projected slip +${slipDays(t.planned_finish, t.projected_finish_date)}d` : undefined}>
                        {pillLabel}
                        {t.has_open_blocker && t.open_blocker_count
                          ? <span className="text-[10px] text-red-700/80">({t.open_blocker_count})</span>
                          : null}
                      </span>
                    </td>
                  </tr>
                );
              })}
              {!rows.length && <tr><td colSpan="8" className="text-center py-8 text-sm text-slate-500">No activities in window.</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {view === "gantt" && (
        <GanttView rows={rows} days={days} onRefresh={refresh} />
      )}
    </div>
  );
}

function slipDays(planned, projected) {
  if (!planned || !projected) return "?";
  try {
    const p = new Date(planned + "T00:00:00");
    const pr = new Date(projected + "T00:00:00");
    return Math.round((pr - p) / 86400000);
  } catch { return "?"; }
}

function GanttView({ rows, days, onRefresh }) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const totalDays = days + 7;
  const start = new Date(today); start.setDate(start.getDate() - 3);

  const dayHeaders = useMemo(() => {
    const arr = [];
    for (let i = 0; i < totalDays; i++) {
      const d = new Date(start); d.setDate(d.getDate() + i);
      arr.push(d);
    }
    return arr;
  }, [start, totalDays]);

  const dragRef = useRef(null);

  const dayIndex = (iso) => {
    if (!iso) return 0;
    const d = new Date(iso); d.setHours(0, 0, 0, 0);
    return Math.round((d - start) / 86400000);
  };

  const onMouseDown = (e, t) => {
    dragRef.current = {
      taskId: t.id,
      activityCode: t.activity_code,
      startDay: dayIndex(t.planned_start),
      finishDay: dayIndex(t.planned_finish),
      startX: e.clientX,
      origStart: t.planned_start,
      origFinish: t.planned_finish,
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  };

  const onMouseMove = (e) => {
    const drag = dragRef.current;
    if (!drag) return;
    const delta = Math.round((e.clientX - drag.startX) / DAY_PX);
    drag.deltaDays = delta;
    // Visually shift by adjusting style
    const el = document.getElementById(`gbar-${drag.taskId}`);
    if (el) el.style.transform = `translateX(${delta * DAY_PX}px)`;
  };

  const onMouseUp = async () => {
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseup", onMouseUp);
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag || !drag.deltaDays) return;
    const shift = (iso) => {
      const d = new Date(iso); d.setDate(d.getDate() + drag.deltaDays);
      return d.toISOString().slice(0, 10);
    };
    try {
      await api.patch(`/tasks/${drag.taskId}`, {
        planned_start: shift(drag.origStart),
        planned_finish: shift(drag.origFinish),
      });
      toast.success(`Rescheduled ${drag.activityCode} by ${drag.deltaDays}d`);
      onRefresh();
    } catch (e) {
      toast.error("Reschedule failed: " + (e.response?.data?.detail || e.message));
      const el = document.getElementById(`gbar-${drag.taskId}`);
      if (el) el.style.transform = "";
    }
  };

  return (
    <div data-testid="lookahead-gantt-view" className="surface overflow-hidden">
      <div className="overflow-x-auto scrollbar-soft">
        <div style={{ minWidth: 240 + totalDays * DAY_PX }}>
          {/* Day header */}
          <div className="flex border-b border-slate-200 sticky top-0 bg-slate-50">
            <div style={{ width: 240, minWidth: 240 }} className="h-8 px-3 flex items-center text-xs font-semibold text-slate-500 border-r border-slate-200">Activity</div>
            {dayHeaders.map((d, i) => (
              <div key={i} style={{ width: DAY_PX, minWidth: DAY_PX }} className={`h-8 grid place-items-center text-[10px] font-mono ${d.getTime() === today.getTime() ? "bg-blue-50 text-blue-700 font-semibold" : "text-slate-500"}`}>
                {d.getDate()}
              </div>
            ))}
          </div>
          {rows.map((t) => {
            const sIdx = dayIndex(t.planned_start);
            const fIdx = dayIndex(t.planned_finish);
            const left = 240 + sIdx * DAY_PX;
            const width = Math.max(DAY_PX, (fIdx - sIdx + 1) * DAY_PX);
            const isCP = t.is_critical_path;
            // Bucket C1 Phase 4 — bar color reflects computed_status when
            // it's worse than CP; amber/red overrides regardless of CP.
            const cs = t.computed_status || "on_track";
            let barClass = isCP ? "bg-red-600"
              : t.status === "in_progress" ? "bg-blue-600"
              : "bg-slate-400";
            if (cs === "blocked") barClass = "bg-red-700";
            else if (cs === "behind") barClass = "bg-amber-500";
            // Projected_finish overlay — extension after the planned bar
            // showing how far the open delays push the finish.
            const projShiftDays = t.projected_finish_date && t.planned_finish && t.projected_finish_date > t.planned_finish
              ? slipDays(t.planned_finish, t.projected_finish_date) : 0;
            const projWidth = projShiftDays > 0 ? projShiftDays * DAY_PX : 0;
            return (
              <div key={t.id} className="flex relative border-b border-slate-100 hover:bg-slate-50" style={{ height: ROW_H }} data-testid={`gantt-row-${t.activity_code}`}>
                <div style={{ width: 240, minWidth: 240 }} className="px-3 flex items-center gap-2 border-r border-slate-200 text-xs">
                  <span className="font-mono text-slate-500">{t.activity_code}</span>
                  <span className="truncate">{t.description}</span>
                </div>
                <div
                  id={`gbar-${t.id}`}
                  data-testid={`gantt-bar-${t.activity_code}`}
                  onMouseDown={(e) => onMouseDown(e, t)}
                  style={{ position: "absolute", left, top: 4, width, height: ROW_H - 8 }}
                  className={`rounded text-white text-[10px] flex items-center px-2 cursor-grab select-none transition-transform ${barClass}`}
                  title={cs === "blocked" ? "Blocked — open delay/RFI/work-item linked"
                    : cs === "behind" ? "Behind planned %"
                    : isCP ? "Critical path" : ""}
                >
                  <span className="truncate">{t.activity_code} · {t.pct_complete}%</span>
                </div>
                {projWidth > 0 && (
                  <div
                    title={`Projected slip +${projShiftDays}d → ${t.projected_finish_date}`}
                    style={{ position: "absolute", left: left + width, top: 4, width: projWidth, height: ROW_H - 8 }}
                    className="rounded-r border border-amber-400 bg-amber-100/70 text-amber-700 text-[10px] flex items-center justify-end px-1 select-none"
                  >
                    <span>+{projShiftDays}d</span>
                  </div>
                )}
              </div>
            );
          })}
          {!rows.length && <div className="text-sm text-slate-500 text-center py-8">No activities in window.</div>}
        </div>
      </div>
      <div className="px-3 py-2 text-[10px] text-slate-500 border-t border-slate-200">
        Click + drag a Gantt bar horizontally to reschedule. Red = critical path.
      </div>
    </div>
  );
}
