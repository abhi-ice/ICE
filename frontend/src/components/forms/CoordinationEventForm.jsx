// Bucket C3b Phase 1 — shared form for coordination events. Used by:
//   - The standalone "+ New" dialog on the Coordination sidebar page
//   - The Edit dialog on the CoordinationEventDetail page
//   - The Quick-log dialog on the daily report (with abbreviated mode)
//
// Props
//   projectId       string — required (used in payload)
//   initial         CoordinationEvent | null — edit when present, create otherwise
//   linkedDailyReportId  string | null — when launched from a DR
//   abbreviated     bool — Quick-log mode skips the "linkages" expander
//   onSaved         function(event) — called on success
//   onCancel        function — close handler
import { useEffect, useState } from "react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
// Bucket D Corrective (2026-05-08) -- master-schedule task picker
// replaces the per-DR DrActivityPicker for coordination linkage.
import TaskPicker from "./TaskPicker";
// Bucket G Phase 3 (2026-05-11) -- Jigsaw pre-fill wrapper.
import JigsawField from "./JigsawField";
import FieldLabel from "./FieldLabel";
import { getUser } from "../../lib/auth";
import { localISODate } from "../../lib/dateUtil";

const CHANNELS = ["email", "phone", "in_person", "formal_letter",
                   "text_message", "meeting", "system_notification"];
const DIRECTIONS = ["inbound", "outbound", "internal"];
const TOPICS = ["tenant_access", "building_coordination", "a_e_clarification",
                 "owner_direction", "sub_coordination", "regulatory", "safety",
                 "quality", "scope", "payment", "other"];
const OUTCOMES = ["agreement", "disagreement", "pending", "informational",
                   "escalation_required"];
// Bug #32 fix (2026-05-13) -- OPTIONAL trade tag on coordination events.
// Matches the AI Vision Jigsaw producer that already suggests trade_code
// on coordination events (see photo_tray_router.py ai_photo_vision path).
// No shared frontend trades constant exists in this codebase -- other
// surfaces (Tasks.jsx, Lookahead.jsx) treat trade_code as a free string,
// but this form's local pattern is enum dropdowns, so we match that.
// Empty value submits as null (matches other optional fields).
const TRADES = ["general", "sitework", "demolition", "concrete", "masonry",
                 "steel", "carpentry", "drywall", "finishes", "doors_windows",
                 "roofing", "plumbing", "mechanical", "electrical",
                 "fire_protection", "elevators", "abatement", "other"];

function defaultsFromInitial(initial, linkedDailyReportId) {
  // Local wall-clock "now" (YYYY-MM-DDTHH:MM) for the datetime-local default,
  // NOT toISOString() (UTC), which shows next-day/shifted time for late US entry.
  const _now = new Date();
  const localNowMinute = `${localISODate(_now)}T${String(_now.getHours()).padStart(2, "0")}:${String(_now.getMinutes()).padStart(2, "0")}`;
  return {
    occurred_at: initial?.occurred_at?.slice(0, 16) || localNowMinute,
    channel: initial?.channel || "phone",
    direction: initial?.direction || "inbound",
    other_party_name: initial?.other_party_name || "",
    other_party_org: initial?.other_party_org || "",
    other_party_role: initial?.other_party_role || "",
    other_party_email: initial?.other_party_email || "",
    other_party_phone: initial?.other_party_phone || "",
    topic: initial?.topic || "tenant_access",
    topic_other_description: initial?.topic_other_description || "",
    summary: initial?.summary || "",
    detail_text: initial?.detail_text || "",
    outcome: initial?.outcome || "informational",
    outcome_notes: initial?.outcome_notes || "",
    linked_daily_report_id: initial?.linked_daily_report_id || linkedDailyReportId || "",
    // Bucket D Corrective -- master-schedule task linkage. OPTIONAL
    // for coordination events (some are project-wide).
    linked_task_ids: initial?.linked_task_ids || [],
    // Bug #32 fix -- OPTIONAL trade tag. Empty string == not set.
    trade_code: initial?.trade_code || "",
  };
}

// Bucket D Addition 5 (2026-05-12) -- new `isLocked` + `lockedAddReason`
// props. Mirrors DelayForm's enforcement: client-side gate rejects
// submit if isLocked + create mode + reason too short, regardless of
// caller role. Submit button disabled when invalid. Backend bypass for
// admin/PM remains intact as the safety net.
const COORD_REASON_MIN = 10;
const coordIsReasonValid = (v) => (v || "").trim().length >= COORD_REASON_MIN;

// Bug #54 (2026-05-13) -- mirrors backend STATUS_TRANSITION_CONFIGS
// ["coordination"]. Trigger field is `outcome` (not `status`); only
// outcome + outcome_notes count as status-only changes. Any other
// field edit on a locked-DR coordination event requires edit_reason.
const COORD_STATUS_ONLY_FIELDS = new Set(["outcome", "outcome_notes"]);

export default function CoordinationEventForm({ projectId, initial, linkedDailyReportId,
                                                 abbreviated = false,
                                                 onSaved, onCancel,
                                                 isLocked = false, lockedAddReason = null }) {
  const [form, setForm] = useState(() => defaultsFromInitial(initial, linkedDailyReportId));
  const [busy, setBusy] = useState(false);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  // Bug #54 (2026-05-13) -- edit_reason capture for field edits on
  // locked-DR coordination events. Lazy-fetch parent DR status when
  // entering edit mode. Coordination uses linked_daily_report_id
  // (different field name from delays/QC/safety which use
  // daily_report_id directly).
  const [editReason, setEditReason] = useState("");
  const [editLockedDr, setEditLockedDr] = useState(false);
  useEffect(() => {
    if (!initial?.id) return;
    if (isLocked) { setEditLockedDr(true); return; }
    const drId = initial?.linked_daily_report_id || linkedDailyReportId;
    if (!drId) return;
    let cancelled = false;
    api.get(`/daily-reports/${drId}`)
      .then(({ data }) => {
        if (cancelled) return;
        const st = data?.status;
        if (st === "submitted" || st === "approved") setEditLockedDr(true);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [initial?.id, initial?.linked_daily_report_id, linkedDailyReportId, isLocked]);

  // Diff form vs initial. Trigger = outcome; status-only allowed
  // covers only outcome + outcome_notes. Everything else (channel,
  // direction, topic, summary, parties, linked_task_ids, etc.) is a
  // field edit requiring edit_reason.
  const isFieldEdit = (() => {
    if (!initial?.id) return false;
    const NON_STATUS_KEYS = [
      "occurred_at", "channel", "direction", "other_party_name",
      "other_party_org", "other_party_role", "other_party_email",
      "other_party_phone", "topic", "topic_other_description",
      "summary", "detail_text", "trade_code",
    ];
    for (const k of NON_STATUS_KEYS) {
      const f = (form[k] ?? "");
      const i = (initial[k] ?? "");
      // occurred_at slicing: form stores 16-char local datetime,
      // initial may have a longer ISO string — compare prefixes.
      if (k === "occurred_at") {
        if (String(f).slice(0, 16) !== String(i).slice(0, 16)) return true;
        continue;
      }
      if (String(f) !== String(i)) return true;
    }
    // linked_task_ids: list of strings, set-compare
    const ftids = [...(form.linked_task_ids || [])].sort();
    const itids = [...(initial.linked_task_ids || [])].sort();
    if (ftids.length !== itids.length) return true;
    for (let i = 0; i < ftids.length; i++) {
      if (ftids[i] !== itids[i]) return true;
    }
    return false;
  })();
  const needsEditReason = !!initial?.id && editLockedDr && isFieldEdit;
  const editReasonValid = coordIsReasonValid(editReason);

  const submit = async () => {
    if (!form.summary.trim()) { toast.error("Summary is required"); return; }
    if (!form.other_party_name.trim()) { toast.error("Other party name is required"); return; }
    if (form.topic === "other" && !form.topic_other_description.trim()) {
      toast.error("Describe the topic when 'other'");
      return;
    }
    // Bucket D Addition 5 -- client-side add-reason gate on locked-DR
    // creates. Uniform across roles for UX consistency; backend bypass
    // for admin/PM is the safety net.
    if (isLocked && !initial?.id && !coordIsReasonValid(lockedAddReason)) {
      toast.error(
        `Add reason required (min ${COORD_REASON_MIN} chars) when logging a coordination event on a locked daily report.`,
      );
      return;
    }
    // Bug #54 -- field-edit gate for locked-DR coordination events.
    if (needsEditReason && !editReasonValid) {
      toast.error(
        `Edit reason required (min ${COORD_REASON_MIN} chars) for field changes on a locked daily report. Outcome-only changes are exempt.`,
      );
      return;
    }
    setBusy(true);
    try {
      // Followup-fix 9 (2026-05-07): the linkedDailyReportId prop wins
      // over form state on create. Quick-log on a daily report passes
      // the DR id explicitly; nothing in abbreviated mode lets the user
      // change it, so prop > form state matches user intent and avoids
      // any stale-state edge case where the dialog rendered before the
      // prop hydrated and the form initialized to "".
      const effectiveLinkedDr = (!initial?.id && linkedDailyReportId)
        ? linkedDailyReportId
        : (form.linked_daily_report_id || null);
      // Bug #56 (2026-05-14) -- project_id is CREATE-only. The
      // CoordinationEventUpdate Pydantic model intentionally omits
      // project_id ("intentionally not patchable -- would orphan the
      // row across tenants/projects" per the model docstring) and
      // inherits extras=forbid from StrictModel. Sending project_id
      // on PATCH gets a 422 "Extra inputs are not permitted" toast.
      // Build a base payload WITHOUT project_id; the CREATE branch
      // below adds it explicitly.
      const payload = {
        ...form,
        // Datetime-local widget gives "YYYY-MM-DDTHH:MM" — Pydantic accepts.
        occurred_at: form.occurred_at,
        // Strip empties so they read null on the server.
        other_party_org: form.other_party_org || null,
        other_party_role: form.other_party_role || null,
        other_party_email: form.other_party_email || null,
        other_party_phone: form.other_party_phone || null,
        topic_other_description: form.topic === "other" ? form.topic_other_description : null,
        detail_text: form.detail_text || null,
        outcome_notes: form.outcome_notes || null,
        linked_daily_report_id: effectiveLinkedDr,
        // Bucket D Corrective -- master-schedule task linkage.
        linked_task_ids: form.linked_task_ids || [],
        // Bug #32 fix -- OPTIONAL trade tag; empty string -> null.
        trade_code: form.trade_code || null,
      };
      let res;
      if (initial?.id) {
        // Bug #54 -- include user-supplied edit_reason when present.
        const patchBody = (editReason && editReason.trim())
          ? { ...payload, edit_reason: editReason.trim() }
          : payload;
        res = await api.patch(`/coordination-events/${initial.id}`, patchBody);
      } else {
        // Bug #56 -- inject project_id only on CREATE (the Create
        // model requires it).
        payload.project_id = projectId;
        // Bucket D Addition 5 -- ALWAYS include add_reason on locked-DR
        // creates (client-side gate above guarantees it's valid).
        if (isLocked) {
          payload.add_reason = (lockedAddReason || "").trim();
        }
        res = await api.post(`/coordination-events`, payload);
      }
      onSaved?.(res.data);
    } catch (e) {
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
  const entityId = initial?.id || null;

  return (
    <div data-testid="coordination-event-form" className="space-y-3 text-sm">
      {/* max-md:grid-cols-2 -- sm:=640px never fires below the 767.98px mobile
          breakpoint, so these fields used to each take a full row on phones.
          Pairing them 2-up halves the height (Baymard related-field exception);
          the wide datetime field spans the full row. Desktop unchanged. */}
      <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-3 gap-3">
        <div className="max-md:col-span-2">
          <FieldLabel required>Occurred at</FieldLabel>
          <input data-testid="coord-occurred-at" type="datetime-local"
            value={form.occurred_at}
            onChange={(e) => set("occurred_at", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
        <div>
          <label className="text-xs text-slate-500">Channel</label>
          <JigsawField entityType="coordination_event" entityId={entityId}
            fieldPath="channel" userRole={userRole} fieldLabel="channel"
            onConfirm={(v) => set("channel", String(v ?? form.channel))}>
            <select data-testid="coord-channel" value={form.channel}
              onChange={(e) => set("channel", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
              {CHANNELS.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
            </select>
          </JigsawField>
        </div>
        <div>
          <label className="text-xs text-slate-500">Direction</label>
          <select data-testid="coord-direction" value={form.direction}
            onChange={(e) => set("direction", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
            {DIRECTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
      </div>

      <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
        <div>
          <FieldLabel required>Other party name</FieldLabel>
          <input data-testid="coord-other-name" value={form.other_party_name}
            onChange={(e) => set("other_party_name", e.target.value)}
            placeholder="Name of person you spoke with"
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
        <div>
          <label className="text-xs text-slate-500">Other party org</label>
          <input data-testid="coord-other-org" value={form.other_party_org}
            onChange={(e) => set("other_party_org", e.target.value)}
            placeholder="e.g. FCC Tenant Office, GSA, A/E firm"
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
      </div>

      {!abbreviated && (
        <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-3 gap-3">
          <div>
            <label className="text-xs text-slate-500">Other party role</label>
            <input value={form.other_party_role}
              onChange={(e) => set("other_party_role", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500">Email</label>
            <input value={form.other_party_email}
              onChange={(e) => set("other_party_email", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500">Phone</label>
            <input value={form.other_party_phone}
              onChange={(e) => set("other_party_phone", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-3 gap-3">
        <div>
          <label className="text-xs text-slate-500">Topic</label>
          <JigsawField entityType="coordination_event" entityId={entityId}
            fieldPath="topic" userRole={userRole} fieldLabel="topic"
            onConfirm={(v) => set("topic", String(v ?? form.topic))}>
            <select data-testid="coord-topic" value={form.topic}
              onChange={(e) => set("topic", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
              {TOPICS.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
            </select>
          </JigsawField>
        </div>
        <div>
          <label className="text-xs text-slate-500">Outcome</label>
          <select data-testid="coord-outcome" value={form.outcome}
            onChange={(e) => set("outcome", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
            {OUTCOMES.map((o) => <option key={o} value={o}>{o.replace(/_/g, " ")}</option>)}
          </select>
        </div>
        {/* Bug #32 fix (2026-05-13) -- OPTIONAL trade tag. Wrapped in
            JigsawField so the AI Vision producer (which already emits
            trade_code suggestions on coordination_event) can pre-fill
            this from a photo. Empty string == not set. */}
        <div>
          <FieldLabel optional>Trade</FieldLabel>
          <JigsawField entityType="coordination_event" entityId={entityId}
            fieldPath="trade_code" userRole={userRole} fieldLabel="trade"
            onConfirm={(v) => set("trade_code", String(v ?? form.trade_code ?? ""))}>
            <select data-testid="coord-trade" value={form.trade_code}
              onChange={(e) => set("trade_code", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
              <option value="">None</option>
              {TRADES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
            </select>
          </JigsawField>
        </div>
      </div>

      {form.topic === "other" && (
        <div>
          <FieldLabel required>Describe the topic</FieldLabel>
          <input data-testid="coord-topic-other"
            value={form.topic_other_description}
            onChange={(e) => set("topic_other_description", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
      )}

      <div>
        <FieldLabel required>Summary</FieldLabel>
        <JigsawField entityType="coordination_event" entityId={entityId}
          fieldPath="summary" userRole={userRole} fieldLabel="summary"
          onConfirm={(v) => set("summary", String(v ?? form.summary))}>
          <input data-testid="coord-summary" value={form.summary}
            onChange={(e) => set("summary", e.target.value)}
            placeholder="One-line: 'No second-shift work in FCC suite this Friday — email coming.'"
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </JigsawField>
      </div>

      {!abbreviated && (
        <div>
          <FieldLabel optional>Detail</FieldLabel>
          <textarea data-testid="coord-detail" rows={4}
            value={form.detail_text}
            onChange={(e) => set("detail_text", e.target.value)}
            placeholder="Longer narrative if needed; what was discussed, who agreed/disagreed, next steps."
            className="mt-1 w-full p-2 border border-slate-200 rounded text-sm" />
        </div>
      )}

      {!abbreviated && (
        <div>
          <FieldLabel optional>Outcome notes</FieldLabel>
          <textarea rows={2}
            value={form.outcome_notes}
            onChange={(e) => set("outcome_notes", e.target.value)}
            placeholder="What was decided / what's still pending."
            className="mt-1 w-full p-2 border border-slate-200 rounded text-sm" />
        </div>
      )}

      {/* Bucket D Corrective -- master-schedule task picker. OPTIONAL
          for coordination events. No DR-scope dependency: the picker
          loads the project's master schedule directly so it works
          even on the standalone /coordination page where there's no
          linkedDailyReportId. */}
      <TaskPicker projectId={projectId}
        value={form.linked_task_ids}
        onChange={(v) => set("linked_task_ids", v)}
        required={false} label="Linked tasks (optional)"
        testidPrefix="coord-activity" />

      {/* Bug #54 (2026-05-13) -- edit_reason textarea for field edits
          on locked-DR coordination events. Renders only in edit mode
          + locked parent DR. Outcome-only changes (Bug #49 backend
          exemption) don't strictly need a reason but the field is
          still available for user-supplied intent. */}
      {initial?.id && editLockedDr && (
        <div className="pt-2 border-t border-slate-100">
          <label className="text-xs text-slate-500">
            Edit reason{isFieldEdit ? " *" : " (optional for outcome-only)"}
          </label>
          <textarea
            data-testid="coord-edit-reason"
            rows={2}
            value={editReason}
            onChange={(e) => setEditReason(e.target.value)}
            placeholder="Why this edit is needed (min 10 chars)"
            className="mt-1 w-full p-2 rounded border border-slate-200 text-sm" />
          <div className="text-xs text-slate-500 text-right">
            {(editReason || "").trim().length} / {COORD_REASON_MIN} min
            {isFieldEdit ? " — required for field changes" : " — optional for outcome-only"}
          </div>
        </div>
      )}

      <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
        {onCancel && (
          <button onClick={onCancel} disabled={busy}
            className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
            Cancel
          </button>
        )}
        {/* Bucket D Addition 5 -- submit disabled until add-reason gate
            passes on locked-DR creates. Bug #54 -- AND disabled when
            editing a field-edit on a locked DR without an edit_reason
            >= 10 chars. Outcome-only patches stay enabled. */}
        <button data-testid="coord-save" onClick={submit}
          disabled={busy
            || (isLocked && !initial?.id && !coordIsReasonValid(lockedAddReason))
            || (needsEditReason && !editReasonValid)}
          title={
            isLocked && !initial?.id && !coordIsReasonValid(lockedAddReason)
              ? `Add reason required (min ${COORD_REASON_MIN} chars) on a locked DR.`
              : (needsEditReason && !editReasonValid
                ? `Edit reason required (min ${COORD_REASON_MIN} chars) for field changes on a locked DR.`
                : undefined)
          }
          className="h-9 px-4 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed">
          {busy
            ? "Saving…"
            : (initial?.id
                ? "Save changes"
                : (isLocked ? "Log event (post-submit)" : "Log event"))}
        </button>
      </div>
    </div>
  );
}
