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
import { useState } from "react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
// Bucket D Corrective (2026-05-08) -- master-schedule task picker
// replaces the per-DR DrActivityPicker for coordination linkage.
import TaskPicker from "./TaskPicker";
// Bucket G Phase 3 (2026-05-11) -- Jigsaw pre-fill wrapper.
import JigsawField from "./JigsawField";
import { getUser } from "../../lib/auth";

const CHANNELS = ["email", "phone", "in_person", "formal_letter",
                   "text_message", "meeting", "system_notification"];
const DIRECTIONS = ["inbound", "outbound", "internal"];
const TOPICS = ["tenant_access", "building_coordination", "a_e_clarification",
                 "owner_direction", "sub_coordination", "regulatory", "safety",
                 "quality", "scope", "payment", "other"];
const OUTCOMES = ["agreement", "disagreement", "pending", "informational",
                   "escalation_required"];

function defaultsFromInitial(initial, linkedDailyReportId) {
  return {
    occurred_at: initial?.occurred_at?.slice(0, 16) || new Date().toISOString().slice(0, 16),
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
  };
}

// Bucket D Addition 5 (2026-05-12) -- new `isLocked` + `lockedAddReason`
// props. Mirrors DelayForm's enforcement: client-side gate rejects
// submit if isLocked + create mode + reason too short, regardless of
// caller role. Submit button disabled when invalid. Backend bypass for
// admin/PM remains intact as the safety net.
const COORD_REASON_MIN = 10;
const coordIsReasonValid = (v) => (v || "").trim().length >= COORD_REASON_MIN;
export default function CoordinationEventForm({ projectId, initial, linkedDailyReportId,
                                                 abbreviated = false,
                                                 onSaved, onCancel,
                                                 isLocked = false, lockedAddReason = null }) {
  const [form, setForm] = useState(() => defaultsFromInitial(initial, linkedDailyReportId));
  const [busy, setBusy] = useState(false);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

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
      const payload = {
        ...form,
        project_id: projectId,
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
      };
      let res;
      if (initial?.id) {
        res = await api.patch(`/coordination-events/${initial.id}`, payload);
      } else {
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
      <div className="grid grid-cols-3 gap-3">
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Occurred at *</label>
          <input data-testid="coord-occurred-at" type="datetime-local"
            value={form.occurred_at}
            onChange={(e) => set("occurred_at", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Channel</label>
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
          <label className="text-xs text-slate-500 uppercase tracking-wide">Direction</label>
          <select data-testid="coord-direction" value={form.direction}
            onChange={(e) => set("direction", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
            {DIRECTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Other party name *</label>
          <input data-testid="coord-other-name" value={form.other_party_name}
            onChange={(e) => set("other_party_name", e.target.value)}
            placeholder="Name of person you spoke with"
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Other party org</label>
          <input data-testid="coord-other-org" value={form.other_party_org}
            onChange={(e) => set("other_party_org", e.target.value)}
            placeholder="e.g. FCC Tenant Office, GSA, A/E firm"
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
      </div>

      {!abbreviated && (
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="text-xs text-slate-500 uppercase tracking-wide">Other party role</label>
            <input value={form.other_party_role}
              onChange={(e) => set("other_party_role", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500 uppercase tracking-wide">Email</label>
            <input value={form.other_party_email}
              onChange={(e) => set("other_party_email", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
          </div>
          <div>
            <label className="text-xs text-slate-500 uppercase tracking-wide">Phone</label>
            <input value={form.other_party_phone}
              onChange={(e) => set("other_party_phone", e.target.value)}
              className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Topic</label>
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
          <label className="text-xs text-slate-500 uppercase tracking-wide">Outcome</label>
          <select data-testid="coord-outcome" value={form.outcome}
            onChange={(e) => set("outcome", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm">
            {OUTCOMES.map((o) => <option key={o} value={o}>{o.replace(/_/g, " ")}</option>)}
          </select>
        </div>
      </div>

      {form.topic === "other" && (
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Describe the topic *</label>
          <input data-testid="coord-topic-other"
            value={form.topic_other_description}
            onChange={(e) => set("topic_other_description", e.target.value)}
            className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm" />
        </div>
      )}

      <div>
        <label className="text-xs text-slate-500 uppercase tracking-wide">Summary *</label>
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
          <label className="text-xs text-slate-500 uppercase tracking-wide">Detail (optional)</label>
          <textarea data-testid="coord-detail" rows={4}
            value={form.detail_text}
            onChange={(e) => set("detail_text", e.target.value)}
            placeholder="Longer narrative if needed; what was discussed, who agreed/disagreed, next steps."
            className="mt-1 w-full p-2 border border-slate-200 rounded text-sm" />
        </div>
      )}

      {!abbreviated && (
        <div>
          <label className="text-xs text-slate-500 uppercase tracking-wide">Outcome notes (optional)</label>
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

      <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
        {onCancel && (
          <button onClick={onCancel} disabled={busy}
            className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
            Cancel
          </button>
        )}
        {/* Bucket D Addition 5 -- submit disabled until add-reason gate
            passes on locked-DR creates. */}
        <button data-testid="coord-save" onClick={submit}
          disabled={busy || (isLocked && !initial?.id && !coordIsReasonValid(lockedAddReason))}
          title={isLocked && !initial?.id && !coordIsReasonValid(lockedAddReason)
            ? `Add reason required (min ${COORD_REASON_MIN} chars) on a locked DR.`
            : undefined}
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
