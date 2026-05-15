// CPM Part J-6 (2026-05-15) -- reusable per-task schedule-impact input.
//
// Renders one integer input per task in `linkedTaskIds`, pre-populates
// from the current `value` array, and emits a CLEANED array via
// onChange where:
//
//     Empty input         -> entry NOT emitted (no per_task_impacts row)
//     0 input             -> entry NOT emitted (treated identical to empty)
//     positive integer    -> entry { task_id, projected_impact_days } emitted
//
// AUTO-SYNC with linkedTaskIds:
//   - Tasks removed from linkedTaskIds -> their entries are pruned
//     from the emitted array automatically (the rendered inputs match
//     the current linkedTaskIds; stale value entries are filtered out
//     before any onChange emission).
//   - Tasks added to linkedTaskIds -> new empty inputs appear; user
//     hasn't entered impact yet, so no entry is emitted.
//
// EDIT PRE-POPULATION:
//   When `value` arrives with existing entries (edit mode), each
//   matching input is filled with its projected_impact_days value.
//   A small "clear" link next to each filled input lets the super
//   explicitly remove a previously-captured impact estimate.
//
// CONTRACT:
//   Parent owns `value`. Each keystroke calls onChange with the new
//   cleaned array. Component never tracks separate local state for
//   the input values -- the parent's value array is the source of
//   truth. This means equality / reference behavior is determined
//   by what the parent passes; consumers that need stable references
//   for change-detection (e.g., locked-edit modal diff) should use
//   array deep-comparison rather than `===`.
//
// SERVER-STAMPED FIELDS (pass-through on edit):
//   When value entries carry server-stamped fields (e.g.,
//   float_at_logging_days, prior_open_impacts_at_logging_days,
//   remaining_float_at_logging_days, consumes_float_only,
//   pushes_critical_path -- Part I/J stamps), this component
//   preserves them on each entry. The parent re-sends the full
//   entry on PATCH; backend's _resolve_rfi_task_impacts re-stamps
//   them post-recompute (Part J-2 wiring).
//
// PROPS:
//   linkedTaskIds  array of task ids  (string[])
//   value          array of impact entries (current per_task_impacts)
//   onChange       function(newArray) -- emits cleaned array
//   projectId      string  -- for resolving task names via API
//   testidPrefix   string  -- e.g. "rfi-impacts"
//   helperText     optional string override for the gray helper line
//
import { useEffect, useState, useCallback, useMemo } from "react";
import { Info } from "lucide-react";
import api from "../../lib/api";


const DEFAULT_HELPER_TEXT =
    "Enter impact days when known. Leave blank if not yet known — " +
    "can be edited later.";


export default function PerTaskImpactsInput({
    linkedTaskIds,
    value,
    onChange,
    projectId,
    testidPrefix = "per-task-impacts",
    helperText = DEFAULT_HELPER_TEXT,
}) {
    // Local cache of task display-info, keyed by task.id. Populated
    // once per projectId via /projects/{id}/tasks. Pure UI -- never
    // posted back. Robust to fetch failure (falls back to truncated
    // task_id as the label).
    const [tasksById, setTasksById] = useState({});
    useEffect(() => {
        if (!projectId) return undefined;
        let cancelled = false;
        api.get(`/projects/${projectId}/tasks`)
            .then(({ data }) => {
                if (cancelled) return;
                const map = {};
                for (const t of (data || [])) {
                    map[t.id] = t;
                }
                setTasksById(map);
            })
            .catch(() => {
                // Network / auth hiccup -- leave map empty. Inputs
                // still render; labels degrade to truncated id.
            });
        return () => { cancelled = true; };
    }, [projectId]);

    // Build a lookup of current entries by task_id so we can render
    // pre-populated inputs in O(1). Server-stamped fields ride
    // along on the entry (we preserve them on edit).
    const valueByTaskId = useMemo(() => {
        const map = {};
        for (const v of (value || [])) {
            if (v && v.task_id) {
                map[v.task_id] = v;
            }
        }
        return map;
    }, [value]);

    // CPM Part J-6 -- emit a cleaned array on every change.
    //
    // Rules:
    //   - Iterate linkedTaskIds in order so the emitted array order
    //     is stable + predictable.
    //   - For each task, if user typed a positive integer, emit an
    //     entry; otherwise omit (covers empty AND 0).
    //   - Preserve server-stamped fields on entries already present
    //     in value (so a re-send doesn't strip them).
    //   - Tasks in value but NOT in linkedTaskIds are dropped (auto-
    //     sync when super removes a task from linked_task_ids).
    const emitChange = useCallback(
        (overrides) => {
            // overrides: { [task_id]: stringValueFromInput | null }
            // null means "user cleared this entry"; the entry should
            // be omitted. A blank/empty string in overrides is also
            // treated as omit.
            const out = [];
            for (const tid of (linkedTaskIds || [])) {
                let rawVal;
                if (Object.prototype.hasOwnProperty.call(overrides, tid)) {
                    rawVal = overrides[tid];
                } else {
                    // Use the current value if present.
                    const existing = valueByTaskId[tid];
                    rawVal = existing
                        ? String(existing.projected_impact_days ?? "")
                        : "";
                }
                if (rawVal == null) continue;   // explicit clear
                const trimmed = String(rawVal).trim();
                if (trimmed === "") continue;   // empty -> omit
                const n = Number(trimmed);
                if (!Number.isFinite(n) || n <= 0) {
                    // Non-numeric OR zero OR negative -> omit. Backend
                    // validator (Part J-1) rejects negative loudly; we
                    // pre-filter zero per spec ("Tasks with `0`
                    // impact_days -> entry NOT included").
                    continue;
                }
                const days = Math.round(n);    // RFI integer convention
                // Preserve server-stamped fields if the existing entry
                // had them. The parent re-sends the full entry on
                // PATCH; the backend re-stamps on recompute.
                const existing = valueByTaskId[tid] || {};
                out.push({
                    ...existing,
                    task_id: tid,
                    projected_impact_days: days,
                });
            }
            onChange(out);
        },
        [linkedTaskIds, valueByTaskId, onChange],
    );

    // CPM Part J-6 -- auto-sync on linkedTaskIds change.
    //
    // When the parent's linkedTaskIds changes (super added or removed
    // a task), stale entries for removed tasks would persist in value.
    // The parent SHOULD prune them, but as a defensive measure we
    // also prune on render here -- IF the current value contains any
    // entry whose task_id is NOT in linkedTaskIds, we emit a cleaned
    // array.
    //
    // Guard: only fire when there's a divergence (avoids infinite
    // re-renders). React's useEffect already short-circuits on
    // identical deps, but the inner emitChange may produce a new
    // array reference; we only call it when content actually changed.
    useEffect(() => {
        const presentIds = new Set(linkedTaskIds || []);
        const hasStale = (value || []).some(
            (v) => v && v.task_id && !presentIds.has(v.task_id),
        );
        if (hasStale) {
            emitChange({});
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [linkedTaskIds]);

    const handleInput = (tid, raw) => {
        emitChange({ [tid]: raw });
    };

    const handleClear = (tid) => {
        emitChange({ [tid]: null });
    };

    if (!linkedTaskIds || linkedTaskIds.length === 0) {
        // No linked tasks yet -- nothing to render. The parent's
        // TaskPicker will surface inputs as the super adds tasks.
        return null;
    }

    return (
        <div
            data-testid={`${testidPrefix}-section`}
            className="border border-slate-200 rounded p-3 space-y-2 bg-slate-50/30"
        >
            <div className="flex items-start gap-1.5">
                <div className="flex-1">
                    <div className="text-xs font-semibold text-slate-700">
                        Schedule impact (optional)
                    </div>
                    <div className="text-[11px] text-slate-500 mt-0.5">
                        {helperText}
                    </div>
                </div>
                <span
                    title={
                        "Impact days reduce remaining float on each task. "
                        + "Visible on Tasks page Slip column when this "
                        + "RFI is open."
                    }
                    className="text-slate-400 cursor-help"
                >
                    <Info className="h-3.5 w-3.5" />
                </span>
            </div>

            <div className="space-y-1.5">
                {(linkedTaskIds || []).map((tid) => {
                    const t = tasksById[tid] || {};
                    const label = t.activity_code
                        ? `${t.activity_code}${t.description ? ` ${t.description}` : ""}`
                        : (tid ? tid.slice(0, 12) : "?");
                    const current = valueByTaskId[tid];
                    const currentDays = current
                        ? String(current.projected_impact_days ?? "")
                        : "";
                    return (
                        <div
                            key={tid}
                            data-testid={`${testidPrefix}-row-${tid}`}
                            className="flex items-center gap-2 text-xs"
                        >
                            <span
                                className="flex-1 text-slate-700 truncate"
                                title={label}
                            >
                                {label}
                            </span>
                            <input
                                data-testid={`${testidPrefix}-input-${tid}`}
                                type="number"
                                min={0}
                                step={1}
                                value={currentDays}
                                placeholder=""
                                onChange={(e) => handleInput(tid, e.target.value)}
                                className="w-16 h-7 px-2 rounded border border-slate-200 text-right text-xs"
                            />
                            <span className="text-[10px] text-slate-500 w-8">
                                day{currentDays === "1" ? "" : "s"}
                            </span>
                            {currentDays && currentDays !== "0" ? (
                                <button
                                    type="button"
                                    data-testid={`${testidPrefix}-clear-${tid}`}
                                    onClick={() => handleClear(tid)}
                                    title="Clear impact estimate for this task"
                                    className="text-[10px] text-slate-500 hover:text-rose-600 underline"
                                >
                                    clear
                                </button>
                            ) : (
                                <span className="w-8" /> /* spacer */
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
