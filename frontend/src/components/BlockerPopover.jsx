// Shared blocker badge with a click-popover listing the open blockers
// (delays / RFIs / submittals / field-issues) on a linked task. Used
// by the Tasks page AND the Daily Report Activity Grid so both views
// behave identically -- click the red "△ N" badge, see the list, each
// row links to the underlying delay / rfi / submittal / work-item.
//
// 2026-05-19 -- extracted from Tasks.jsx BlockerCell so the DR grid
// can re-use the exact same UX. Same data shape: each row in
// `summary` is { entity_type, entity_id, severity, label }.
import React, { useState, useRef, useEffect } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle, AlertCircle, MessageSquare, FileText,
} from "lucide-react";

export function BlockerPopover({ count, summary, testid }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const n = Number(count || 0);

  // Close on outside click (so clicking another row's badge closes
  // this one). Cheap document-level handler; no global listeners
  // when the popover is closed.
  useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  if (n <= 0) {
    return <span data-testid={testid} className="text-slate-300 text-xs">—</span>;
  }

  const items = (summary || []).filter(
    (s) => s.entity_type !== "qc_issue" && s.entity_type !== "safety_event",
  );
  const counts = items.reduce((acc, s) => {
    acc[s.entity_type] = (acc[s.entity_type] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        data-testid={testid}
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border border-rose-200 bg-rose-100 text-rose-700 hover:bg-rose-200 font-semibold tabular-nums"
        title={`${n} open blocker${n === 1 ? "" : "s"} -- click for details`}
      >
        <AlertTriangle className="h-3 w-3" /> {n}
        {(counts.delay || counts.rfi || counts.submittal || counts.work_item) ? (
          <span className="text-xs text-rose-500/70 font-normal">
            {[
              counts.delay && `${counts.delay}d`,
              counts.rfi && `${counts.rfi}r`,
              counts.submittal && `${counts.submittal}s`,
              counts.work_item && `${counts.work_item}w`,
            ].filter(Boolean).join(" ")}
          </span>
        ) : null}
      </button>

      {open && (
        <div
          data-testid={`${testid}-popover`}
          className="absolute z-30 right-0 mt-1 w-72 bg-white border border-slate-200 rounded shadow-lg p-2 text-xs"
        >
          <div className="font-medium mb-1 text-slate-700">
            Open blockers ({n})
          </div>
          {items.length === 0 ? (
            <div className="text-xs text-slate-500 italic px-1 py-1">
              No detail summary available for these blockers yet.
            </div>
          ) : (
            <div className="space-y-1 max-h-56 overflow-y-auto">
              {items.map((s, i) => {
                const Icon = s.entity_type === "delay" ? AlertTriangle
                  : s.entity_type === "rfi" ? MessageSquare
                  : s.entity_type === "submittal" ? FileText
                  : s.entity_type === "work_item" ? AlertCircle
                  : AlertCircle;
                const sevColor = (
                  s.severity === "critical"
                  || s.severity === "major"
                  || s.severity === "high"
                ) ? "text-red-600" : "text-slate-500";
                const link = s.entity_type === "delay" ? `/delays/${s.entity_id}`
                  : s.entity_type === "rfi" ? "/rfis"
                  : s.entity_type === "submittal" ? "/submittals"
                  : s.entity_type === "work_item" ? "/work-items"
                  : null;
                const inner = (
                  <div className="flex items-start gap-1.5 py-1 border-b border-slate-100 last:border-0">
                    <Icon className={`h-3 w-3 mt-0.5 ${sevColor} shrink-0`} />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs uppercase tracking-wide text-slate-500">
                        {(s.entity_type || "").replace(/_/g, " ")}
                        {" · "}
                        {s.severity || "—"}
                      </div>
                      <div className="truncate text-slate-800">
                        {s.label || s.entity_id?.slice(0, 8)}
                      </div>
                    </div>
                  </div>
                );
                return link ? (
                  <Link key={i} to={link} onClick={() => setOpen(false)}
                    className="block hover:bg-slate-50 -mx-1 px-1 rounded">
                    {inner}
                  </Link>
                ) : (
                  <div key={i}>{inner}</div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
