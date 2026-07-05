// Shared delay form — used by the daily report inline panel AND the sidebar
// Delays page. Multi-task picker with per-task projected impact (decimal days),
// inline float-vs-impact indicator, status, and conditional resolution fields.
//
// Props:
//   projectId    string — required, used to load project tasks for the picker
//   reportDate   string YYYY-MM-DD — used as default `date` when creating
//   dailyReportId string|null — set when invoked inside a daily report panel
//   initial      Delay | null — when present, edit mode (we PATCH); else create
//   onSaved      function(delay) — called after a successful POST/PATCH
//   onCancel     function — called when the user dismisses the form
import { useEffect, useMemo, useState } from "react";
import { Plus, X, AlertTriangle, ShieldCheck } from "lucide-react";
import api from "../../lib/api";
import { localTodayISO } from "../../lib/dateUtil";
import { toast } from "../ui/sonner";
import { isMasterTaskComplete } from "../../lib/taskCompletion";
// Bucket G Phase 3 (2026-05-11) -- Jigsaw pre-fill wrapper. Renders
// the AI-suggested chrome on edit-mode fields. On create-mode (no
// initial.id) it passes through cleanly (no entity to bind against
// yet). User role drives Accept/Reject role-gating via the
// authorized_confirmer_roles list on each suggestion.
import JigsawField from "./JigsawField";
import FieldLabel from "./FieldLabel";
import { getUser } from "../../lib/auth";
// Bucket D Corrective (2026-05-08) -- DrActivityPicker import removed.
// The duplicate "Affected activities (optional)" picker that briefly
// lived on this form (alongside the canonical "Linked tasks" picker
// with float impact) is gone -- delay's master-schedule linkage was
// always the source of truth.

const SEVERITY_OPTS = ["minor", "moderate", "major", "critical"];
const STATUS_OPTS = ["open", "in_progress", "resolved"];

// pt.49 (2026-05-28): merged the old "Category" (reason_category) and
// "Cause class" dropdowns into ONE "Cause" field. They duplicated each
// other (design_change / concealed_condition / site_condition lived in
// both), and the who-flavored Category values (owner_caused / sub_caused)
// just restated Owner attribution. This union keeps every distinct cause
// from both lists, drops the who-values, and is stored in cause_class
// (the claims-engine field) + mirrored into reason_category so the voice /
// recovery / export readers still resolve. cause_class is a free string
// server-side, so the added values (weather, manpower, ...) need no
// backend change.
const CAUSE_OPTS = [
  "weather", "differing_site_condition", "concealed_condition",
  "design_clarification", "design_change", "rfi_pending", "submittal_pending",
  "procurement", "manpower", "utility", "regulatory",
  "tenant_coordination", "building_coordination", "other",
];

// Owner attribution -- WHO is responsible. pt.49: optional for field
// supers; the office / PM (or AI) sets it for the formal claim rollup.
// Same vocabulary as gating_entities so TIA Section 4 (Owner Attribution
// Summary) rolls up consistently across delays and gates.
const OWNER_ATTRIBUTION_OPTS = [
  { value: "none", label: "(no specific attribution)" },
  { value: "gc", label: "General Contractor (GC)" },
  { value: "a_e", label: "Architect / Engineer (A/E)" },
  { value: "owner", label: "Owner / Government" },
  { value: "sub", label: "Subcontractor" },
  { value: "external_authority", label: "External Authority (utility, regulator)" },
  { value: "tenant", label: "Tenant" },
];

// Bucket D Addition 5 (2026-05-12) -- new `isLocked` + `lockedAddReason`
// props. When the parent DR is locked AND this form is in create mode
// (no `initial.id`), the panel passes:
//   - isLocked: true so the form knows to apply the add-reason gate
//   - lockedAddReason: the reason text from the panel's reason block
//
// On submit, the form validates client-side that the reason meets the
// min-char threshold (10) BEFORE hitting the backend. Submit button is
// also disabled when isLocked && reason invalid, so the user gets
// immediate UX feedback that the field is required. Backend bypass for
// admin/PM remains intact -- the client-side gate is intentionally
// uniform across roles so audit captures intent for everyone.
//
// Test 2D follow-up (2026-05-12): prior version checked
// `if (lockedAddReason)` truthy which omitted the field when empty --
// admin/PM bypass then let the create through with no audit reason.
// Now we always include the field when isLocked + abort client-side
// when invalid, regardless of role.
const REASON_MIN = 10;
const isReasonValid = (v) => (v || "").trim().length >= REASON_MIN;

// Bug #54 (2026-05-13) -- the backend's STATUS_TRANSITION_CONFIGS["delay"]
// allowed set: a PATCH whose changed fields fall within this set is the
// Bug #49 status-only-transition exemption path (no edit_reason needed).
// Any other field changes require edit_reason on a locked DR. Mirrored
// here so the frontend gates Save when reason is missing on field edits.
// Must stay in sync with services/edit_permissions.py STATUS_TRANSITION_CONFIGS.
const DELAY_STATUS_ONLY_FIELDS = new Set([
  "status", "resolved_note", "resolved_date", "resolved_by_user_id",
]);

export default function DelayForm({ projectId, reportDate, dailyReportId, initial,
                                     onSaved, onCancel,
                                     isLocked = false, lockedAddReason = null,
                                     prelinkedTaskId = null }) {
  const [tasks, setTasks] = useState([]);  // [{id, activity_code, description, total_float_days, is_critical_path}]
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [picker, setPicker] = useState("");
  // Feature F5 (2026-05-18) -- completed tasks are hidden from the
  // linked-task picker by default; this toggle reveals them.
  const [showCompletedTasks, setShowCompletedTasks] = useState(false);
  const [form, setForm] = useState(() => ({
    description: initial?.description || "",
    // 2026-05-18 -- defaults for CREATE mode are now empty ("") so the
    // user has to consciously pick a value. The placeholder option
    // "— select —" guards the form via the submit() validation below;
    // backend still applies its own defaults if a field is omitted,
    // but our client always sends the selected value (never empty)
    // because the validation gate runs first.
    severity: initial?.severity || "",
    status: initial?.status || "",
    resolved_date: initial?.resolved_date || "",
    resolved_note: initial?.resolved_note || "",
    owner_attribution: initial?.owner_attribution || "",
    // pt.49: single merged Cause. Read from cause_class (canonical) with a
    // fallback to a legacy reason_category value so older delays still show.
    cause: initial?.cause_class || initial?.reason_category || "",
    // Explicit impact start (2026-07-03) -- when the estimated impact
    // begins. Create mode defaults like the `date` field on submit
    // (reportDate || localTodayISO); edit mode prefills the stored value,
    // falling back to the delay's own log date (the effective anchor for
    // legacy rows).
    impact_start_date: initial?.id
      ? (initial?.impact_start_date || initial?.date || "")
      : (reportDate || localTodayISO()),
    impacts: (initial?.per_task_impacts || []).map((i) => ({ ...i })),
  }));
  const [busy, setBusy] = useState(false);

  // 2026-05-26 — when opened via the Activity Grid Issues dropdown,
  // pre-link to the activity that the super flagged. We seed it
  // through `impacts` so the rest of the form pipeline treats it like
  // any other linked task.
  useEffect(() => {
    if (!prelinkedTaskId) return;
    if (initial?.id) return;   // edit mode -- don't override existing
    setForm((f) => {
      if (f.impacts.some((i) => i.task_id === prelinkedTaskId)) return f;
      return {
        ...f,
        impacts: [...f.impacts, { task_id: prelinkedTaskId, projected_impact_days: 0 }],
      };
    });
  }, [prelinkedTaskId, initial?.id]);

  // Bug #54 (2026-05-13) -- edit_reason capture for field edits on
  // locked-DR delays. When the user opens this form from /delays or
  // /delays/:id (standalone surfaces, not inside a DR panel), the
  // caller doesn't know whether the parent DR is locked. We lazy-fetch
  // the parent DR's status when entering edit mode so the textarea +
  // gate can render conditionally. If the caller already passes
  // isLocked=true (DR panel context), we skip the fetch.
  const [editReason, setEditReason] = useState("");
  const [editLockedDr, setEditLockedDr] = useState(false);
  useEffect(() => {
    if (!initial?.id) return;            // create mode: skip
    if (isLocked) {                       // DR-panel caller already knows
      setEditLockedDr(true);
      return;
    }
    const drId = initial?.daily_report_id;
    if (!drId) return;                    // orphan event: nothing to fetch
    let cancelled = false;
    api.get(`/daily-reports/${drId}`)
      .then(({ data }) => {
        if (cancelled) return;
        const st = data?.status;
        if (st === "submitted" || st === "approved") setEditLockedDr(true);
      })
      .catch(() => {});  // defensive: treat as unlocked on fetch failure
    return () => { cancelled = true; };
  }, [initial?.id, initial?.daily_report_id, isLocked]);

  useEffect(() => {
    if (!projectId) return;
    api.get(`/projects/${projectId}/tasks`)
      .then(({ data }) => setTasks(Array.isArray(data) ? data : []))
      .catch(() => setTasks([]))
      .finally(() => setTasksLoaded(true));
  }, [projectId]);

  // Bug #54 -- compute whether the user has touched any non-status-only
  // field vs `initial`. Field defaults applied symmetrically on both
  // sides (e.g. owner_attribution defaults to "none" on read; we match
  // here so a no-touch save isn't falsely flagged as a field edit).
  // Mirrors the backend Bug #49 hotfix 2 _projection_equal logic for
  // per_task_impacts (the form's impacts state is a thin projection
  // of the DB-stored 5-key items; compare by task_id + value only).
  const isFieldEdit = (() => {
    if (!initial?.id) return false;
    if ((form.description || "") !== (initial.description || "")) return true;
    if ((form.severity || "") !== (initial.severity || "")) return true;
    if ((form.owner_attribution || "") !== (initial.owner_attribution || "")) return true;
    if ((form.cause || "") !== (initial.cause_class || initial.reason_category || "")) return true;
    // Explicit impact start (2026-07-03) -- a changed impact start moves
    // the schedule-impact anchor, so it counts as a field edit. Untouched
    // (still the stored value, or the legacy log-date prefill) doesn't.
    if (form.impact_start_date
        && form.impact_start_date !== (initial.impact_start_date || initial.date || "")) return true;
    const initImpacts = initial.per_task_impacts || [];
    if (form.impacts.length !== initImpacts.length) return true;
    const initBy = Object.fromEntries(
      initImpacts.map((i) => [i.task_id, Number(i.projected_impact_days || 0)]),
    );
    for (const imp of form.impacts) {
      const iv = initBy[imp.task_id];
      if (iv === undefined) return true;
      if (iv !== Number(imp.projected_impact_days || 0)) return true;
    }
    return false;
  })();
  const needsEditReason = !!initial?.id && editLockedDr && isFieldEdit;
  const editReasonValid = isReasonValid(editReason);

  const taskById = useMemo(() => Object.fromEntries(tasks.map((t) => [t.id, t])), [tasks]);
  const linkedIds = form.impacts.map((i) => i.task_id);
  // Feature F5 -- a completed task already linked keeps its impact row
  // (we never drop existing links); completed tasks are only hidden
  // from the dropdown of NEW options unless the toggle is on.
  const unlinkedTasks = tasks.filter((t) => !linkedIds.includes(t.id));
  const hiddenCompletedCount = unlinkedTasks.filter(isMasterTaskComplete).length;
  const availableTasks = showCompletedTasks
    ? unlinkedTasks
    : unlinkedTasks.filter((t) => !isMasterTaskComplete(t));

  const addLinkedTask = () => {
    if (!picker) return;
    setForm((f) => ({
      ...f,
      impacts: [...f.impacts, { task_id: picker, projected_impact_days: 0 }],
    }));
    setPicker("");
  };
  const removeLinkedTask = (tid) => {
    setForm((f) => ({ ...f, impacts: f.impacts.filter((i) => i.task_id !== tid) }));
  };
  const setImpact = (tid, days) => {
    const v = days === "" ? 0 : Number(days);
    setForm((f) => ({
      ...f,
      impacts: f.impacts.map((i) => i.task_id === tid ? { ...i, projected_impact_days: v } : i),
    }));
  };

  // Live preview of consumes_float_only / pushes_critical_path so the user
  // can see the indicator before submit. Backend recomputes authoritative
  // values on save (see _resolve_task_impacts in field_events_router).
  const previewFlag = (impact) => {
    const t = taskById[impact.task_id];
    if (!t) return { color: "slate", label: "task not loaded" };
    const f = t.total_float_days;
    const proj = Number(impact.projected_impact_days || 0);
    if (f === null || f === undefined) return { color: "slate", label: "no float data" };
    if (proj <= f) return { color: "emerald", label: `within float (${f}d)` };
    if (t.is_critical_path) return { color: "red", label: `pushes CP (float ${f}d)` };
    return { color: "amber", label: `exceeds float ${f}d (non-CP)` };
  };

  const submit = async () => {
    if (!form.description.trim()) { toast.error("What happened? is required"); return; }
    // 2026-05-18 -- explicit dropdown selection required. No more
    // pre-selected defaults; supers must pick each field consciously
    // so we don't end up with false "moderate / weather" data on
    // unattended logs.
    if (!form.severity) { toast.error("Severity is required"); return; }
    if (!form.cause) { toast.error("Cause is required"); return; }
    // pt.49: Owner attribution is now optional for field supers — the
    // office / PM (or AI) sets it later for the formal claim rollup, so
    // it's intentionally NOT gated here.
    if (!form.status) { toast.error("Status is required"); return; }
    if (!form.impacts.length) { toast.error("Link at least one task"); return; }
    // Bucket D Addition 5 -- client-side add-reason gate. Uniform
    // across roles; backend bypass for admin/PM still operates as the
    // safety net but the UI insists every add captures intent.
    if (isLocked && !initial?.id && !isReasonValid(lockedAddReason)) {
      toast.error(
        `Add reason required (min ${REASON_MIN} chars) when logging a delay on a locked daily report.`,
      );
      return;
    }
    // Bug #54 -- edit_reason gate on field edits to locked-DR delays.
    // Status-only transitions (e.g. open -> resolved + note) flow
    // through Bug #49's exemption and don't need a reason.
    if (needsEditReason && !editReasonValid) {
      toast.error(
        `Edit reason required (min ${REASON_MIN} chars) for field changes on a locked daily report. Status-only changes (resolve/close) are exempt.`,
      );
      return;
    }
    setBusy(true);
    try {
      const payload = {
        description: form.description,
        severity: form.severity,
        status: form.status,
        // pt.49: the merged Cause is stored in cause_class (the claims-engine
        // field used by TIA + gating entities) AND mirrored into
        // reason_category, so the voice / recovery / export readers that key
        // off reason_category keep resolving. Owner attribution is optional
        // now; default to "none" when the super leaves it blank, which TIA
        // already renders as "no attribution".
        cause_class: form.cause,
        reason_category: form.cause,
        owner_attribution: form.owner_attribution || "none",
        linked_task_ids: form.impacts.map((i) => i.task_id),
        per_task_impacts: form.impacts.map((i) => ({
          task_id: i.task_id,
          projected_impact_days: Number(i.projected_impact_days || 0),
        })),
      };
      if (form.status === "resolved") {
        payload.resolved_date = form.resolved_date || localTodayISO();
        payload.resolved_note = form.resolved_note || null;
      }
      // Explicit impact start (2026-07-03). Create: always sent (defaults
      // to the same value the `date` field gets below). Edit: sent ONLY
      // when the user actually changed it, so a status-only resolve on a
      // legacy row stays a status-only patch (Bug #49 exemption on locked
      // daily reports).
      if (!initial?.id) {
        payload.impact_start_date = form.impact_start_date || reportDate || localTodayISO();
      } else if (form.impact_start_date
                 && form.impact_start_date !== (initial.impact_start_date || initial.date || "")) {
        payload.impact_start_date = form.impact_start_date;
      }
      let res;
      if (initial?.id) {
        // Bug #54 -- attach edit_reason when the user supplied one.
        // The backend ignores it on draft DRs (no gate to satisfy);
        // on locked DRs it satisfies the field-edit gate, or wins
        // over the synthesized note-reason on a status-only patch.
        const patchBody = (editReason && editReason.trim())
          ? { ...payload, edit_reason: editReason.trim() }
          : payload;
        res = await api.patch(`/delays/${initial.id}`, patchBody);
      } else {
        const createBody = {
          ...payload,
          project_id: projectId,
          daily_report_id: dailyReportId || undefined,
          date: reportDate || localTodayISO(),
        };
        // Bucket D Addition 5 -- ALWAYS include add_reason on locked-DR
        // creates (not just when truthy). Empty string would still
        // trigger backend 422 for non-admin/PM; client-side gate above
        // already rejected empty, so this branch always carries the
        // valid reason text.
        if (isLocked) {
          createBody.add_reason = (lockedAddReason || "").trim();
        }
        res = await api.post(`/delays`, createBody);
      }
      onSaved?.(res.data);
    } catch (e) {
      // Bucket D Addition 5 -- 24h self-edit window closed handling.
      const status = e.response?.status;
      const detail = e.response?.data?.detail;
      if (
        status === 403 && detail && typeof detail === "object"
        && detail.error === "edit_request_required"
      ) {
        toast.error(
          "Self-edit window (24h) has closed. File an edit request from "
          + "the edit pencil on any existing row -- approval opens a "
          + "1-hour window for adds too.",
        );
      } else {
        toast.error(detail || e.message || "Save failed");
      }
    } finally { setBusy(false); }
  };

  const userRole = (getUser()?.role || "").toLowerCase();
  // Per Sid: Jigsaw bindings are post-create only. Create-mode forms
  // pass entityId=null which makes JigsawField a no-op identity wrap.
  const entityId = initial?.id || null;

  return (
    <div data-testid="delay-form" className="space-y-3">
      <div>
        <FieldLabel required>What happened?</FieldLabel>
        <JigsawField entityType="delay" entityId={entityId}
          fieldPath="description" userRole={userRole}
          fieldLabel="description"
          onConfirm={(v) => setForm({ ...form, description: String(v ?? "") })}>
          <textarea
            data-testid="delay-form-description"
            rows={4}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Describe what caused the delay, who was affected, and any photos/observations on site"
            className="mt-1 w-full p-2 rounded border border-slate-200 text-sm"
          />
        </JigsawField>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <FieldLabel required>Severity</FieldLabel>
          <JigsawField entityType="delay" entityId={entityId}
            fieldPath="severity" userRole={userRole}
            fieldLabel="severity"
            onConfirm={(v) => setForm({ ...form, severity: String(v ?? form.severity) })}>
            <select data-testid="delay-form-severity" value={form.severity}
              onChange={(e) => setForm({ ...form, severity: e.target.value })}
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm">
              <option value="">Select…</option>
              {SEVERITY_OPTS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </JigsawField>
        </div>
        <div>
          {/* pt.49: merged Cause (was Category + Cause class). Bound to
              cause_class via Jigsaw; mirrored to reason_category on save. */}
          <FieldLabel required title="What type of delay this was (merged the old Category + Cause class)">Cause</FieldLabel>
          <JigsawField entityType="delay" entityId={entityId}
            fieldPath="cause_class" userRole={userRole}
            fieldLabel="cause"
            onConfirm={(v) => setForm({ ...form, cause: String(v ?? form.cause) })}>
            <select data-testid="delay-form-cause" value={form.cause}
              onChange={(e) => setForm({ ...form, cause: e.target.value })}
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm">
              <option value="">Select…</option>
              {CAUSE_OPTS.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
            </select>
          </JigsawField>
        </div>
      </div>

      {/* pt.49: Owner attribution is optional now (formal claim field) and
          stands alone — the old "Cause class" half merged into the single
          "Cause" dropdown above. Left blank -> saved as "none", which TIA
          Section 4 renders as "no attribution". */}
      {/* max-md:grid-cols-1 -- this grid wraps a single standalone field, so
          on phones collapse to one column (full-width) instead of a half-width
          control with a blank right cell. Desktop keeps the 2-col grid as-is. */}
      <div className="grid grid-cols-2 max-md:grid-cols-1 gap-3">
        <div>
          <FieldLabel optional title="Who is responsible for the delay event — usually set by the office/PM (or AI) for the formal claim rollup">
            Owner attribution
          </FieldLabel>
          <JigsawField entityType="delay" entityId={entityId}
            fieldPath="owner_attribution" userRole={userRole}
            fieldLabel="owner_attribution"
            onConfirm={(v) => setForm({ ...form, owner_attribution: String(v ?? form.owner_attribution) })}>
            <select data-testid="delay-form-owner-attribution" value={form.owner_attribution}
              onChange={(e) => setForm({ ...form, owner_attribution: e.target.value })}
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm bg-white">
              <option value="">Select…</option>
              {OWNER_ATTRIBUTION_OPTS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </JigsawField>
        </div>
      </div>

      <div>
        <FieldLabel required>Linked tasks</FieldLabel>
        <div className="mt-1 space-y-2">
          {form.impacts.map((imp) => {
            const t = taskById[imp.task_id];
            const flag = previewFlag(imp);
            const colorClass = {
              emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
              amber: "bg-amber-50 text-amber-700 border-amber-200",
              red: "bg-red-50 text-red-700 border-red-200",
              slate: "bg-slate-50 text-slate-600 border-slate-200",
            }[flag.color];
            return (
              <div key={imp.task_id} data-testid={`delay-form-impact-${imp.task_id}`} className="flex flex-wrap items-center gap-2 p-2 rounded border border-slate-200 bg-white">
                <button type="button" onClick={() => removeLinkedTask(imp.task_id)}
                  className="h-6 w-6 grid place-items-center rounded hover:bg-red-50 text-red-500"
                  aria-label="remove linked task">
                  <X className="h-3.5 w-3.5" />
                </button>
                <div className="flex-1 min-w-[180px] text-sm">
                  <span className="text-slate-700">{t?.description || "(task not in current project list)"}</span>
                </div>
                <label className="flex items-center gap-1 text-xs">
                  <span className="text-slate-500">Impact (days)</span>
                  <input
                    data-testid={`delay-form-impact-days-${imp.task_id}`}
                    type="number" step="0.5" min="0"
                    value={imp.projected_impact_days ?? 0}
                    onChange={(e) => setImpact(imp.task_id, e.target.value)}
                    className="h-7 w-20 px-1 border border-slate-200 rounded text-sm tabular-nums"
                  />
                </label>
                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs ${colorClass}`}>
                  {flag.color === "red" ? <AlertTriangle className="h-3 w-3" /> : <ShieldCheck className="h-3 w-3" />}
                  {flag.label}
                </span>
              </div>
            );
          })}
          {!form.impacts.length && (
            <div className="text-xs text-slate-400 italic">No tasks linked yet — pick one below.</div>
          )}
          {/* Feature F5 -- "Show completed tasks" toggle, default OFF. */}
          <label className="flex items-center gap-1.5 pt-1 text-xs text-slate-500 cursor-pointer">
            <input type="checkbox" data-testid="delay-form-show-completed"
              checked={showCompletedTasks}
              onChange={(e) => setShowCompletedTasks(e.target.checked)} />
            Show completed tasks
            {hiddenCompletedCount > 0 && !showCompletedTasks && (
              <span className="text-slate-400">({hiddenCompletedCount} hidden)</span>
            )}
          </label>
          <div className="flex items-center gap-2 min-w-0">
            <select data-testid="delay-form-add-task-select" value={picker}
              onChange={(e) => setPicker(e.target.value)}
              className="flex-1 min-w-0 h-8 px-2 rounded border border-slate-200 text-sm"
              disabled={!tasksLoaded}>
              <option value="">{tasksLoaded ? `Select a task (${availableTasks.length} available)…` : "Loading project tasks…"}</option>
              {availableTasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.description}{t.is_critical_path ? " · CP" : ""}
                </option>
              ))}
            </select>
            {/* pt.42: was a round Plus-icon button; switched to a
                text-label primary sbtn ("Add"). */}
            <button data-testid="delay-form-add-task" type="button" onClick={addLinkedTask} disabled={!picker}
              title="Add selected task as linked"
              className="sbtn sbtn-sm sbtn-primary shrink-0"
              aria-label="Add task">
              Add
            </button>
          </div>
        </div>
      </div>

      {/* Bucket D Corrective (2026-05-08) -- the duplicate "Affected
          activities (optional)" picker that briefly lived here was
          dropped. The "Linked tasks" picker above (with per-task float
          impact + pushes-CP indicator) was always the canonical
          master-schedule linkage; the dr-activity picker was a wrong-
          build duplicate that confused the form. */}

      {/* Explicit impact start (2026-07-03) -- when the estimated impact
          begins. Elapsed-day growth and the closing actual anchor here;
          left untouched it matches the delay's own log date. */}
      <div>
        <FieldLabel optional title="When the estimated impact begins — elapsed-day growth and the closing actual anchor on this date">
          Impact starts on
        </FieldLabel>
        <input data-testid="delay-form-impact-start" type="date"
          value={form.impact_start_date || ""}
          onChange={(e) => setForm({ ...form, impact_start_date: e.target.value })}
          className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm" />
        <p className="mt-1 text-xs text-slate-500">
          Pick today if the delay already affected today&apos;s work; pick
          tomorrow if the impact begins then.
        </p>
      </div>

      <div>
        <FieldLabel required>Status</FieldLabel>
        <select data-testid="delay-form-status" value={form.status}
          onChange={(e) => setForm({ ...form, status: e.target.value })}
          className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm">
          <option value="">Select…</option>
          {STATUS_OPTS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
        </select>
      </div>

      {form.status === "resolved" && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <FieldLabel optional>Resolved date</FieldLabel>
            <input data-testid="delay-form-resolved-date" type="date"
              value={form.resolved_date || ""}
              onChange={(e) => setForm({ ...form, resolved_date: e.target.value })}
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm" />
          </div>
          <div>
            <FieldLabel optional>Resolution note</FieldLabel>
            <input data-testid="delay-form-resolved-note" type="text"
              value={form.resolved_note || ""}
              onChange={(e) => setForm({ ...form, resolved_note: e.target.value })}
              placeholder="What was done to resolve"
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm" />
          </div>
        </div>
      )}

      {/* Bug #54 (2026-05-13) -- edit_reason textarea for field edits
          on locked-DR delays. Renders ONLY when:
            - we're editing an existing delay (not creating), AND
            - the parent DR is locked (lazy-fetched on mount unless
              the caller passed isLocked=true).
          Status-only transitions (resolve/close + note) flow through
          Bug #49's backend exemption and don't need a reason; for
          those the textarea still renders but the user can leave it
          empty. The Save gate below only insists on reason text when
          isFieldEdit is true. */}
      {initial?.id && editLockedDr && (
        <div className="pt-2 border-t border-slate-100">
          <label className="text-xs text-slate-500">
            Edit reason{isFieldEdit ? " *" : " (optional for status-only)"}
          </label>
          <textarea
            data-testid="delay-form-edit-reason"
            rows={2}
            value={editReason}
            onChange={(e) => setEditReason(e.target.value)}
            placeholder="Why this edit is needed (min 10 chars)"
            className="mt-1 w-full p-2 rounded border border-slate-200 text-sm" />
          <div className="text-xs text-slate-500 text-right">
            {(editReason || "").trim().length} / {REASON_MIN} min
            {isFieldEdit ? " — required for field changes" : " — optional for status-only"}
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 pt-2 border-t border-slate-100">
        {/* Bucket D Addition 5 -- submit disabled until add-reason gate
            passes on locked DRs (create mode only). Bug #54 (2026-05-13)
            -- AND submit disabled when editing a field-edit on a locked
            DR without an edit_reason >= 10 chars. Status-only patches
            stay enabled (Bug #49 backend exemption handles them). */}
        <button data-testid="delay-form-save" onClick={submit}
          disabled={busy
            || (isLocked && !initial?.id && !isReasonValid(lockedAddReason))
            || (needsEditReason && !editReasonValid)}
          title={
            isLocked && !initial?.id && !isReasonValid(lockedAddReason)
              ? `Add reason required (min ${REASON_MIN} chars) on a locked DR.`
              : (needsEditReason && !editReasonValid
                ? `Edit reason required (min ${REASON_MIN} chars) for field changes on a locked DR.`
                : undefined)
          }
          className="h-9 px-4 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed">
          {initial?.id
            ? "Save changes"
            : (isLocked ? "Add delay (post-submit)" : "Add delay")}
        </button>
        {onCancel && (
          <button data-testid="delay-form-cancel" type="button" onClick={onCancel} className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
