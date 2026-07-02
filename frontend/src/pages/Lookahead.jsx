// Lookahead — date-bounded query view. Master prompt §3.
// View: list (table) OR Gantt-style timeline.
// Drag a Gantt bar horizontally to reschedule the activity (PATCH /tasks/:id).
import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarRange, List, BarChart3 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import ExportDropdown from "../components/ExportDropdown";
// CPM Part F (2026-05-15) -- shared display helpers for Impacted
// Start / Impacted Finish / Slip columns. The local slipDays() helper
// that used to live in this file was promoted to lib/dateUtil so
// Tasks.jsx + Lookahead.jsx + the Gantt projected-shift overlay
// all share one calendar-day computation. Behavior preserved for
// the Gantt path (slip > 0 only -- acceleration is not a "slip").
import { formatDateDisplay, slipDays } from "../lib/dateUtil";

const DAY_PX = 32; // pixels per day in Gantt
const ROW_H = 32;

export default function Lookahead() {
  const { activeId, projects } = useProjects();
  const [days, setDays] = useState(14);
  const [rows, setRows] = useState([]);
  const [view, setView] = useState("list");
  // CPM Part F (2026-05-15) -- sort + filter affordances mirror the
  // Tasks page so a PM moving between the two surfaces gets the same
  // controls. Defaults preserve pre-Part-F behavior (no sort, show all).
  const [sortBySlipDesc, setSortBySlipDesc] = useState(false);
  const [showOnlyImpacted, setShowOnlyImpacted] = useState(false);

  const refresh = async () => {
    if (!activeId) return;
    const { data } = await api.get(`/projects/${activeId}/lookahead?days=${days}`);
    setRows(data);
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId, days]);

  // CPM Part F -- enrich each row with the slip days, then optionally
  // filter and sort. Same memoization pattern as Tasks.jsx.
  const displayRows = useMemo(() => {
    const enriched = rows.map((t) => ({
      ...t,
      _slip: slipDays(t.planned_finish, t.projected_finish_date),
    }));
    const filtered = showOnlyImpacted
      ? enriched.filter((t) => t._slip > 0)
      : enriched;
    if (sortBySlipDesc) {
      return [...filtered].sort((a, b) => b._slip - a._slip);
    }
    return filtered;
  }, [rows, sortBySlipDesc, showOnlyImpacted]);

  // MS Project file pipeline (2026-07-02) -- Mode-2 projects recalculate
  // out-of-band in Microsoft Project. While the worker holds a job, show a
  // thin heads-up above the table so PMs know these dates may still move.
  // One sync-state fetch on load; a 15s poll runs only while a job is active.
  const isMode2 = projects.find((p) => p.id === activeId)?.schedule_mode === "mode2";
  const [mspRecalculating, setMspRecalculating] = useState(false);
  useEffect(() => {
    if (!activeId || !isMode2) { setMspRecalculating(false); return undefined; }
    let cancelled = false;
    let timer = null;
    const check = async () => {
      try {
        const { data } = await api.get(`/projects/${activeId}/ms-project/sync-state`);
        if (cancelled) return;
        const active = !!data?.active_job;
        setMspRecalculating(active);
        if (active) timer = setTimeout(check, 15000);
      } catch { /* transient — leave the current line as-is */ }
    };
    check();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [activeId, isMode2]);

  return (
    <div data-testid="lookahead-page" className="space-y-4">
      <div className="flex items-center justify-between max-md:flex-col max-md:items-start max-md:gap-2">
        <div>
          <div className="flex items-center gap-2">
            <CalendarRange className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">{days}-day lookahead</h1>
          </div>
          <p className="text-sm text-slate-500">Date-bounded query · {rows.length} activities</p>
        </div>
        <div className="flex max-md:flex-wrap items-center gap-2">
          {[7, 14, 21, 28].map((d) => (
            <button key={d} data-testid={`lookahead-window-${d}`} onClick={() => setDays(d)} className={`h-8 max-md:min-h-[44px] px-3 rounded border text-sm ${days === d ? "bg-blue-600 text-white border-blue-600" : "border-slate-200"}`}>
              {d}d
            </button>
          ))}
          <div className="ml-2 flex items-center gap-1 border border-slate-200 rounded p-0.5">
            <button data-testid="lookahead-list" onClick={() => setView("list")} className={`h-7 max-md:min-h-[44px] px-3 rounded text-xs flex items-center gap-1 ${view === "list" ? "bg-slate-100" : ""}`}>
              <List className="h-3 w-3" /> List
            </button>
            <button data-testid="lookahead-gantt" onClick={() => setView("gantt")} className={`h-7 max-md:min-h-[44px] px-3 rounded text-xs flex items-center gap-1 ${view === "gantt" ? "bg-slate-100" : ""}`}>
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

      {mspRecalculating && (
        <p data-testid="lookahead-msp-recalculating" className="text-xs text-slate-500">
          Microsoft Project is recalculating — dates may update shortly.
        </p>
      )}

      {view === "list" && (
        <>
          {/* CPM Part F (2026-05-15) -- impact-column controls (sort
              by slip desc, filter to slip > 0). Mirrors Tasks.jsx so
              the muscle memory carries between surfaces. Only shown
              when there are rows to operate on. */}
          {rows.length > 0 && (
            <div className="flex max-md:flex-wrap items-center gap-2 text-xs text-slate-600">
              <button
                data-testid="lookahead-sort-by-slip"
                onClick={() => setSortBySlipDesc((v) => !v)}
                className={`h-7 max-md:min-h-[44px] px-2 rounded border text-xs ${sortBySlipDesc ? "bg-rose-50 border-rose-300 text-rose-700" : "bg-white border-slate-200"}`}
                title="Sort by Slip descending so the worst-hit tasks land at the top."
              >
                {sortBySlipDesc ? "✓ Sorted by slip ↓" : "Sort by slip ↓"}
              </button>
              {/* max-md:w-full + 44px row -- checkbox gets its own full-width
                  tappable row on phones instead of wrapping under the Sort
                  button. Desktop keeps it inline. */}
              <label className="inline-flex items-center gap-1 cursor-pointer max-md:w-full max-md:min-h-[44px]">
                <input
                  data-testid="lookahead-show-only-impacted"
                  type="checkbox"
                  checked={showOnlyImpacted}
                  onChange={(e) => setShowOnlyImpacted(e.target.checked)}
                />
                Show only impacted (slip &gt; 0)
              </label>
              <span className="text-slate-400">
                ·{" "}
                {rows.filter((t) => slipDays(t.planned_finish, t.projected_finish_date) > 0).length}{" "}
                impacted of {rows.length}
              </span>
            </div>
          )}
        <div className="surface overflow-hidden">
          <table className="dense-table">
            <thead><tr>
              <th>Code</th><th>Description</th><th>Trade</th>
              <th>Plan start</th><th>Plan finish</th>
              {/* CPM Part F (2026-05-15) -- the single "Projected"
                  column (which surfaced projected_finish_date with
                  slip in the cell text) is replaced by three explicit
                  columns matching the Tasks page contract: Impacted
                  Start, Impacted Finish, Slip (d). Slip uses the
                  shared slipDays() helper; Impacted dates use
                  formatDateDisplay() so Part G can flip the global
                  format in one place. */}
              <th title="Projected start date when an open delay or RFI pushes the task forward via CPM walk-forward.">Impacted Start</th>
              <th title="Projected finish date — planned_finish + accumulated open-delay shift through CPM predecessors.">Impacted Finish</th>
              <th title="Working days of slip: working_days_between(planned_finish, projected_finish_date). Em-dash when zero. Light-red row tint when > 0. (CPM Bug 3.B: working days, was calendar.)">Slip (wd)</th>
              {/* CPM Part L-2 (2026-05-16) -- live remaining float
                  on Lookahead page mirrors the Tasks page column.
                  PMs scanning the 14-day window see at a glance
                  which tasks are at the cliff (rem float = 0). */}
              <th title="Remaining float (working days) = structural float minus open blocker impact days. Updates live as delays/RFIs/submittals open and close.">Rem Float</th>
              <th>%</th>
              <th title="Bucket C1 Phase 2 computed_status">Status</th>
            </tr></thead>
            <tbody>
              {displayRows.map((t) => {
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
                const slip = t._slip;
                const projShifted = slip > 0;
                const blocked = cs === "blocked" || t.has_open_blocker;
                const atRisk = !blocked && cs !== "behind" && cs !== "complete" && projShifted;
                // CPM Part F -- row tint precedence: blocked-red >
                // projected-slip-rose > behind-amber > clean. Rose for
                // Part F slip > 0 matches the Tasks page tint so the
                // two surfaces look consistent.
                // pt.31: blocked rows now use a stronger red tint
                // (bg-red-100) so they read as MORE critical than at-risk
                // (which stays rose-50). Was bg-red-50/50 which looked
                // almost identical to at-risk and confused the eye.
                const tint = blocked ? "bg-red-100"
                  : projShifted ? "bg-rose-50"
                  : cs === "behind" ? "bg-amber-50/50"
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
                    {/* CPM Part G (2026-05-15) -- existing Plan start /
                        Plan finish columns routed through
                        formatDateDisplay for MM/DD/YYYY rendering
                        from the same chokepoint as the Impacted dates. */}
                    <td className="font-mono text-xs">{formatDateDisplay(t.planned_start)}</td>
                    <td className="font-mono text-xs">{formatDateDisplay(t.planned_finish)}</td>
                    {/* CPM Part F -- Impacted Start. formatDateDisplay
                        renders "—" on null so fresh tasks (no
                        projection yet) show the em-dash. Part G now
                        outputs MM/DD/YYYY via this same helper. */}
                    <td
                      data-testid={`lookahead-impacted-start-${t.activity_code}`}
                      className="font-mono text-xs"
                    >
                      {formatDateDisplay(t.projected_start_date)}
                    </td>
                    <td
                      data-testid={`lookahead-impacted-finish-${t.activity_code}`}
                      className="font-mono text-xs"
                    >
                      {formatDateDisplay(t.projected_finish_date)}
                    </td>
                    <td
                      data-testid={`lookahead-slip-${t.activity_code}`}
                      className="tabular-nums text-xs"
                      title={slip > 0
                        ? `Projected slip +${slip} working days vs planned_finish`
                        : "No projected slip"}
                    >
                      {slip > 0
                        ? <span className="text-rose-700 font-semibold">+{slip}</span>
                        : <span className="text-slate-300">—</span>}
                    </td>
                    {/* CPM Bug 3.E (2026-05-16) -- remaining float cell,
                        SIGNED. Negative => task past its slack. RED
                        text when <= 0 (at/over the cliff), normal
                        when > 0, em-dash when null. Matches Tasks
                        page styling. */}
                    <td
                      data-testid={`lookahead-remaining-float-${t.activity_code}`}
                      className="tabular-nums text-xs"
                      title={(() => {
                        const r = t.remaining_float_days;
                        if (r == null) return "Remaining float not yet computed";
                        if (r > 0) return `${r} working days of float remaining`;
                        if (r === 0) return "Zero float remaining — at the edge";
                        return `All float consumed — ${Math.abs(r)} working days past slack, driving the project finish`;
                      })()}
                    >
                      {(() => {
                        const r = t.remaining_float_days;
                        if (r == null) return <span className="text-slate-300">—</span>;
                        if (r <= 0) return <span className="text-rose-700 font-semibold">{r}</span>;
                        return <span className="text-slate-700">{r}</span>;
                      })()}
                    </td>
                    <td>{t.pct_complete}%</td>
                    <td>
                      <span data-testid={`lookahead-status-${t.activity_code}`}
                        className={`pill ${csClass} inline-flex items-center gap-1`}
                        title={atRisk ? `Projected slip +${slip} working days` : undefined}>
                        {pillLabel}
                        {t.has_open_blocker && t.open_blocker_count
                          ? <span className="text-xs text-red-700/80">({t.open_blocker_count})</span>
                          : null}
                      </span>
                    </td>
                  </tr>
                );
              })}
              {!displayRows.length && rows.length > 0 && (
                <tr><td colSpan="11" className="text-center py-8 text-sm text-slate-500">
                  No activities match the current filter. Toggle &ldquo;Show only impacted&rdquo; off to see all.
                </td></tr>
              )}
              {!rows.length && <tr><td colSpan="11" className="text-center py-8 text-sm text-slate-500">No activities in window.</td></tr>}
            </tbody>
          </table>
        </div>
        </>
      )}

      {view === "gantt" && (
        <GanttView rows={rows} days={days} onRefresh={refresh} />
      )}
    </div>
  );
}

// CPM Part F (2026-05-15) -- local slipDays() was promoted to
// lib/dateUtil so Tasks.jsx + Lookahead.jsx + the GanttView's
// projected-shift overlay all share one computation. The shared
// helper returns 0 (not "?") on missing dates so Gantt overlay's
// `if (projShiftDays > 0)` guard keeps the same intent without
// special-casing "?".

function GanttView({ rows, days, onRefresh }) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const totalDays = days + 7;
  const start = new Date(today); start.setDate(start.getDate() - 3);
  // Phone: a 240px activity-label column eats most of the timeline -> narrow
  // it so more days are visible. ONE constant keeps the header label, row
  // labels, the canvas min-width, and the bar left-offset in lockstep (so
  // bars stay aligned). Desktop stays 240 (matchMedia false) -> the Gantt is
  // byte-identical >=768px.
  const LABEL_W = (typeof window !== "undefined"
    && window.matchMedia("(max-width: 767.98px)").matches) ? 124 : 240;

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
        <div style={{ minWidth: LABEL_W + totalDays * DAY_PX }}>
          {/* Day header */}
          <div className="flex border-b border-slate-200 sticky top-0 bg-slate-50">
            <div style={{ width: LABEL_W, minWidth: LABEL_W }} className="h-8 px-3 flex items-center text-xs font-semibold text-slate-500 border-r border-slate-200">Activity</div>
            {dayHeaders.map((d, i) => (
              <div key={i} style={{ width: DAY_PX, minWidth: DAY_PX }} className={`h-8 grid place-items-center text-xs font-mono ${d.getTime() === today.getTime() ? "bg-blue-50 text-blue-700 font-semibold" : "text-slate-500"}`}>
                {d.getDate()}
              </div>
            ))}
          </div>
          {rows.map((t) => {
            const sIdx = dayIndex(t.planned_start);
            const fIdx = dayIndex(t.planned_finish);
            const left = LABEL_W + sIdx * DAY_PX;
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
                <div style={{ width: LABEL_W, minWidth: LABEL_W }} className="px-3 flex items-center gap-2 border-r border-slate-200 text-xs">
                  <span className="font-mono text-slate-500">{t.activity_code}</span>
                  <span className="truncate">{t.description}</span>
                </div>
                <div
                  id={`gbar-${t.id}`}
                  data-testid={`gantt-bar-${t.activity_code}`}
                  onMouseDown={(e) => onMouseDown(e, t)}
                  style={{ position: "absolute", left, top: 4, width, height: ROW_H - 8 }}
                  className={`rounded text-white text-xs flex items-center px-2 cursor-grab select-none transition-transform ${barClass}`}
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
                    className="rounded-r border border-amber-400 bg-amber-100/70 text-amber-700 text-xs flex items-center justify-end px-1 select-none"
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
      <div className="px-3 py-2 text-xs text-slate-500 border-t border-slate-200">
        {/* drag-to-reschedule is mouse-only; hide that instruction on touch
            where it doesn't apply, keep the critical-path legend. */}
        <span className="max-md:hidden">Click + drag a Gantt bar horizontally to reschedule. </span>Red = critical path.
      </div>
    </div>
  );
}
