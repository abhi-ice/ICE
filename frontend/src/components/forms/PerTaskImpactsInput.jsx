// CPM Part J-6 (2026-05-15) + Part K-6 (2026-05-15) -- reusable per-
// task schedule-impact input. Renders one integer input per task in
// `linkedTaskIds`, pre-populates from the current `value` array, and
// emits a CLEANED array via onChange.
//
// MODE DISPATCH (K-6):
//   mode="rfi"        (default, J-6 behavior unchanged):
//                       - Simple integer inputs
//                       - 0 input -> omitted (treated same as empty)
//                       - No source pills, no reset button
//                       - Used by RFI create + DR-embedded RFI panel
//
//   mode="submittal"  (K-6 hybrid auto-suggest UX):
//                       - 0 input is a VALID persisted value (K-2 may
//                         auto-suggest 0 at the SLA boundary)
//                       - Source pill renders next to each input:
//                           system_suggested -> neutral hint icon +
//                               "Suggested from aging (X days)" tooltip
//                           super_overridden -> warning icon +
//                               "Super-edited (was Y auto-suggested)"
//                       - "Reset to suggestion" button on super_
//                         overridden entries with an
//                         original_suggested_days field (K-2 stamps
//                         this; preserved through K-3 verdict stamp)
//                       - When slaStatus !== "breached", a banner
//                         renders above the inputs:
//                           "Submittal currently within SLA — impacts
//                            not active"
//                         Inputs stay rendered (super may want to
//                         pre-fill) but are visually de-emphasized.
//                       - Edit auto-flips source: changing the value
//                         on a system_suggested entry flips source to
//                         super_overridden and stamps original_
//                         suggested_days from the prior value if
//                         absent. The K-1 validator accepts the new
//                         shape; backend K-3 re-stamps verdict keys.
//
// EMIT RULES (both modes):
//   Empty input               -> entry NOT emitted (no row)
//   0 input (mode=rfi)        -> entry NOT emitted (J-6 unchanged)
//   0 input (mode=submittal)  -> entry kept with value 0 (K-6 new)
//   Positive integer          -> entry emitted
//
// AUTO-SYNC with linkedTaskIds (both modes):
//   - Tasks removed -> entries pruned automatically.
//   - Tasks added -> new empty inputs appear.
//
// SERVER-STAMPED FIELDS:
//   * J/I verdict keys (float_at_logging_days,
//     prior_open_impacts_at_logging_days, etc.) ride through unchanged
//   * K-1 `source` field rides through unchanged (overridden only on
//     edit per the hybrid flow below)
//   * K-2 `original_suggested_days` rides through unchanged
//
// CONTRACT:
//   Parent owns `value`. Each keystroke calls onChange with the new
//   cleaned array. Component is stateless w.r.t. input magnitudes --
//   the parent's value array is the source of truth.
//
// PROPS:
//   linkedTaskIds  array of task ids  (string[])
//   value          array of impact entries (current per_task_impacts)
//   onChange       function(newArray) -- emits cleaned array
//   projectId      string  -- for resolving task names via API
//   testidPrefix   string  -- e.g. "rfi-impacts" / "sub-impacts"
//   helperText     optional string override for the gray helper line
//   mode           "rfi" | "submittal"  (default "rfi" for backward-compat)
//   slaStatus      string  -- submittal sla_status when mode="submittal";
//                              "breached" enables active styling,
//                              anything else shows the within-SLA banner
//
import { useEffect, useState, useCallback, useMemo } from "react";
import { Info, RotateCcw, AlertTriangle } from "lucide-react";
import api from "../../lib/api";


const DEFAULT_HELPER_TEXT =
    "Enter impact days when known. Leave blank if not yet known — " +
    "can be edited later.";

const SUBMITTAL_HELPER_TEXT =
    "Auto-suggested from aging when SLA breaches. Edit to override; " +
    "click reset to restore the suggestion.";


export default function PerTaskImpactsInput({
    linkedTaskIds,
    value,
    onChange,
    projectId,
    testidPrefix = "per-task-impacts",
    helperText,
    mode = "rfi",
    slaStatus,
}) {
    // Default helper text depends on mode if caller didn't override.
    const effectiveHelperText = helperText
        ?? (mode === "submittal" ? SUBMITTAL_HELPER_TEXT : DEFAULT_HELPER_TEXT);
    const isSubmittalMode = mode === "submittal";
    // K-6: in submittal mode, the "active" gate is sla_status === "breached"
    // (matches the backend Path 1 + Path 2 + count_open_blockers filter).
    // Within-SLA submittals get the de-emphasis banner but still render
    // inputs so super can pre-fill if they know an impact is coming.
    const isWithinSla = isSubmittalMode && slaStatus !== "breached";
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

    // CPM Part J-6 + K-6 -- emit a cleaned array on every change.
    //
    // Rules:
    //   - Iterate linkedTaskIds in order so the emitted array order
    //     is stable + predictable.
    //   - For each task, parse the input. Empty -> omit. Mode-
    //     dependent handling of 0 (see below). Positive -> emit.
    //   - Preserve server-stamped fields on entries already present
    //     in value (so a re-send doesn't strip them).
    //   - Tasks in value but NOT in linkedTaskIds are dropped.
    //
    // ZERO HANDLING (K-6 mode dispatch):
    //   mode="rfi":       0 -> omit (J-6 unchanged; no UX path lets
    //                     the user type 0 -- they just leave blank)
    //   mode="submittal": 0 -> keep (K-2 may auto-suggest 0 at the
    //                     SLA boundary; super may also explicitly
    //                     set 0 to say "no impact despite breach")
    //
    // SOURCE FLIP (K-6 submittal mode only):
    //   When the user edits a value whose existing entry had
    //   source="system_suggested", we:
    //     1. Flip source to "super_overridden"
    //     2. Stamp original_suggested_days from the existing
    //        projected_impact_days (or preserve any prior
    //        original_suggested_days from K-2)
    //   When the user edits an already-super_overridden entry,
    //   source stays super_overridden; original_suggested_days
    //   rides through unchanged.
    //   When the user RESETS (handleResetToSuggestion below), source
    //   flips back to system_suggested and projected_impact_days
    //   restores to original_suggested_days.
    const emitChange = useCallback(
        (overrides) => {
            // overrides: { [task_id]: stringValueFromInput | null }
            //   null -> explicit clear (omit entry)
            //   string -> raw input value
            // For K-6 reset operations, callers can also pass:
            //   overrides[tid] = { __reset: true } -- sentinel that
            //   means "restore from original_suggested_days + flip
            //   source back to system_suggested"
            const out = [];
            for (const tid of (linkedTaskIds || [])) {
                let rawVal;
                let isReset = false;
                if (Object.prototype.hasOwnProperty.call(overrides, tid)) {
                    const ov = overrides[tid];
                    if (ov && typeof ov === "object" && ov.__reset) {
                        isReset = true;
                    } else {
                        rawVal = ov;
                    }
                } else {
                    // Use the current value if present.
                    const existing = valueByTaskId[tid];
                    rawVal = existing
                        ? String(existing.projected_impact_days ?? "")
                        : "";
                }
                const existing = valueByTaskId[tid] || {};

                if (isReset) {
                    // K-6 reset: restore from original_suggested_days
                    // (must be present -- the button is only shown
                    // when this field exists on the entry).
                    const orig = existing.original_suggested_days;
                    if (orig == null) continue;  // defensive: nothing to reset to
                    out.push({
                        ...existing,
                        task_id: tid,
                        projected_impact_days: Number(orig),
                        source: "system_suggested",
                        // original_suggested_days stays the same; the
                        // K-2 baseline doesn't change on a reset.
                    });
                    continue;
                }

                if (rawVal == null) continue;   // explicit clear
                const trimmed = String(rawVal).trim();
                if (trimmed === "") continue;   // empty -> omit
                const n = Number(trimmed);
                if (!Number.isFinite(n) || n < 0) {
                    // Non-numeric or negative -> omit. Backend
                    // validators (J-1, K-1) reject negative loudly;
                    // we pre-filter here too.
                    continue;
                }
                if (n === 0 && !isSubmittalMode) {
                    // RFI mode (J-6): 0 == empty.
                    continue;
                }
                const days = Math.round(n);    // integer convention

                // K-6 submittal-mode source flip: if value changed
                // and the entry was system_suggested, flip to
                // super_overridden + stamp original_suggested_days.
                let nextSource = existing.source;
                let nextOriginal = existing.original_suggested_days;
                if (isSubmittalMode) {
                    const priorDays = existing.projected_impact_days;
                    const valueChanged = priorDays !== days;
                    if (valueChanged && existing.source === "system_suggested") {
                        nextSource = "super_overridden";
                        // Preserve the original if K-2 already stamped
                        // it; otherwise fall back to the prior value
                        // (typical when an entry was created pre-K-6).
                        nextOriginal = (existing.original_suggested_days != null)
                            ? existing.original_suggested_days
                            : priorDays;
                    }
                    // If no value change OR source was already
                    // super_overridden, leave source + original alone.
                }

                const merged = {
                    ...existing,
                    task_id: tid,
                    projected_impact_days: days,
                };
                if (isSubmittalMode) {
                    // Ensure source is set (defensive -- K-1 backend
                    // validator requires source on submittal entries).
                    merged.source = nextSource || "super_overridden";
                    if (nextOriginal != null) {
                        merged.original_suggested_days = nextOriginal;
                    }
                }
                out.push(merged);
            }
            onChange(out);
        },
        [linkedTaskIds, valueByTaskId, onChange, isSubmittalMode],
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

    // K-6 submittal-mode reset: restore projected_impact_days from
    // original_suggested_days + flip source back to system_suggested.
    // emitChange handles the actual logic via the __reset sentinel.
    const handleResetToSuggestion = (tid) => {
        emitChange({ [tid]: { __reset: true } });
    };

    if (!linkedTaskIds || linkedTaskIds.length === 0) {
        // No linked tasks yet -- nothing to render. The parent's
        // TaskPicker will surface inputs as the super adds tasks.
        return null;
    }

    // K-6: in submittal mode, when the row is within SLA, the inputs
    // render but with reduced visual weight + a banner explaining
    // why the math isn't active. Path 1 + Path 2 + count_open_blockers
    // gate on sla_status=="breached", so a within-SLA submittal's
    // per_task_impacts entries don't contribute to Slip / cumulative
    // float verdicts even when populated.
    const sectionClasses = isWithinSla
        ? "border border-slate-200 rounded p-3 space-y-2 bg-slate-100/40 opacity-80"
        : "border border-slate-200 rounded p-3 space-y-2 bg-slate-50/30";

    return (
        <div
            data-testid={`${testidPrefix}-section`}
            data-mode={mode}
            data-sla-active={isSubmittalMode ? (isWithinSla ? "false" : "true") : undefined}
            className={sectionClasses}
        >
            <div className="flex items-start gap-1.5">
                <div className="flex-1">
                    <div className="text-xs font-semibold text-slate-700">
                        Schedule impact{isSubmittalMode ? "" : " (optional)"}
                    </div>
                    <div className="text-[11px] text-slate-500 mt-0.5">
                        {effectiveHelperText}
                    </div>
                </div>
                <span
                    title={
                        isSubmittalMode
                            ? "Impact days reduce remaining float on the linked "
                              + "task once the submittal is over SLA. Auto-"
                              + "suggested from aging at the breach moment."
                            : "Impact days reduce remaining float on each task. "
                              + "Visible on Tasks page Slip column when this "
                              + "RFI is open."
                    }
                    className="text-slate-400 cursor-help"
                >
                    <Info className="h-3.5 w-3.5" />
                </span>
            </div>

            {/* K-6 within-SLA banner. Inputs still render below so the
                super can pre-fill a known-coming impact estimate.
                Backend filters keep these entries out of live math
                until sla_status flips to breached. */}
            {isWithinSla && (
                <div
                    data-testid={`${testidPrefix}-within-sla-banner`}
                    className="flex items-center gap-1.5 text-[11px] text-slate-600 bg-slate-200/60 border border-slate-300 rounded px-2 py-1"
                >
                    <Info className="h-3 w-3 text-slate-500" />
                    <span>
                        Submittal currently within SLA — impacts not active
                        until SLA breaches.
                    </span>
                </div>
            )}

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
                    const source = current?.source;
                    const origSuggested = current?.original_suggested_days;
                    const hasNonEmptyValue = currentDays !== "";
                    // K-6: clear-link rule for submittal mode is
                    // different from RFI mode because 0 is a valid
                    // persisted value. Submittal mode: show clear for
                    // any non-empty value. RFI mode: J-6 unchanged
                    // (any non-empty, non-zero value).
                    const showClear = isSubmittalMode
                        ? hasNonEmptyValue
                        : (currentDays && currentDays !== "0");
                    // K-6: source pill + reset button only render in
                    // submittal mode. The pill shows source provenance;
                    // reset only renders for super_overridden entries
                    // with a stamped original_suggested_days.
                    const showSourcePill = isSubmittalMode && source;
                    const showResetButton = isSubmittalMode
                        && source === "super_overridden"
                        && origSuggested != null;
                    return (
                        <div
                            key={tid}
                            data-testid={`${testidPrefix}-row-${tid}`}
                            className="flex items-center gap-2 text-xs flex-wrap"
                        >
                            <span
                                className="flex-1 text-slate-700 truncate min-w-0"
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
                            {showSourcePill && (
                                <SourcePill
                                    source={source}
                                    originalSuggested={origSuggested}
                                    currentDays={currentDays}
                                    testid={`${testidPrefix}-source-${tid}`}
                                />
                            )}
                            {showResetButton && (
                                <button
                                    type="button"
                                    data-testid={`${testidPrefix}-reset-${tid}`}
                                    onClick={() => handleResetToSuggestion(tid)}
                                    title={`Reset to auto-suggested value (${origSuggested} day${origSuggested === 1 ? "" : "s"})`}
                                    className="text-[10px] text-blue-700 hover:text-blue-900 inline-flex items-center gap-0.5 underline"
                                >
                                    <RotateCcw className="h-3 w-3" />
                                    reset
                                </button>
                            )}
                            {showClear ? (
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


// CPM Part K-6 (2026-05-15) -- source pill for submittal-mode entries.
// Two visual states map 1:1 to the K-1 source enum:
//   system_suggested -> neutral hint icon + "Suggested from aging"
//   super_overridden -> amber warning icon + "Super-edited (was Y)"
// Tooltip exposes the underlying numbers so the super can read the
// audit at a glance without opening the audit log.
function SourcePill({ source, originalSuggested, currentDays, testid }) {
    if (source === "system_suggested") {
        const daysWord = currentDays === "1" ? "day" : "days";
        return (
            <span
                data-testid={testid}
                data-source="system_suggested"
                title={`Auto-suggested from aging: ${currentDays} ${daysWord}. Edit to override.`}
                className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] bg-slate-100 text-slate-700 border border-slate-200"
            >
                <Info className="h-2.5 w-2.5" />
                suggested
            </span>
        );
    }
    if (source === "super_overridden") {
        const wasText = originalSuggested != null
            ? ` (was ${originalSuggested} auto-suggested)`
            : "";
        return (
            <span
                data-testid={testid}
                data-source="super_overridden"
                title={`Super-edited${wasText}. Click reset to restore the suggestion.`}
                className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] bg-amber-50 text-amber-900 border border-amber-200"
            >
                <AlertTriangle className="h-2.5 w-2.5" />
                edited
            </span>
        );
    }
    return null;
}
