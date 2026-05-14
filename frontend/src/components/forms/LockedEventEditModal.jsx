// Addition 4 -- locked-DR edit modal. Reusable across every event
// type (delay / rfi / qc / safety / photo / coordination / delivery).
//
// Two modes:
//   1. "edit" mode (default) -- shows the editable fields + a required
//      edit_reason textarea. Submit PATCHes the event endpoint with
//      {edit_reason, ...field updates}. On 403 needs_request from the
//      server, the modal flips into mode 2.
//   2. "request" mode -- one textarea (request reason). Submit POSTs
//      /edit-requests with the same metadata. On 200, closes with a
//      toast directing the user to wait for admin/PM approval.
//
// The caller passes:
//   eventType: "delay" | "rfi" | "qc" | "safety" | "photo" |
//              "coordination" | "delivery" | "activity"
//   eventId: str
//   dailyReportId: str (the parent DR's id)
//   patchUrl: str (e.g. `/delays/${id}` -- what PATCH endpoint to hit)
//   fields: [{key, label, type: "text"|"textarea"|"select"|"number",
//             options?: [{value,label}]}, ...]
//   currentValues: dict (key -> current value)
//   onSaved: () => void (called after successful patch or request)
import { useState } from "react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { Button } from "../ui/button";
// Bucket G Phase 3 (2026-05-11) -- Jigsaw pre-fill wrapper around each
// FieldEditor. The locked-edit modal is always edit-mode (eventId
// known), so suggestions can bind cleanly. The wrapper covers every
// post-submit-edit surface on DR -- including the RFI / QC / safety /
// delivery / coordination event types that the spec called out for
// Jigsaw wrapping, since they all reach this modal.
import JigsawField from "./JigsawField";
import { getUser } from "../../lib/auth";

// Map this modal's eventType keys to the field_suggestions entity_type
// vocabulary. The keys here mirror what the call sites in
// DailyReports.jsx pass; new event types route through "unknown" and
// JigsawField degrades gracefully (no suggestion will match).
const EVENT_TYPE_TO_ENTITY = {
  delay:        "delay",
  rfi:          "rfi",
  qc:           "qc_issue",
  qc_issue:     "qc_issue",
  safety:       "safety_event",
  safety_event: "safety_event",
  coordination: "coordination_event",
  coordination_event: "coordination_event",
  delivery:     "delivery",
  photo:        "photo_tag",
  activity:     "daily_report_activity",
};

const REASON_MIN = 10;

// Bug #57 (2026-05-14) -- mirrors backend STATUS_TRANSITION_CONFIGS
// (services/edit_permissions.py). Key: eventType prop passed by the
// caller (DailyReports.jsx renders 1 modal per event-row). Value:
// Set of field names that count as "status-only" for that event type.
//
// A patch whose CHANGED fields fall entirely within this set is a
// status-only transition; the backend Bug #49 exemption skips the
// edit_reason requirement and synthesizes a reason from the note
// field (resolved_note / closed_note / response_text / outcome_notes
// / etc.). The modal mirrors this set so the client-side gate matches:
// the user resolves an RFI from inside the DR view with just a status
// change + answer, no reason required.
//
// MUST stay in sync with backend. If an event type's status-only set
// changes server-side, update this map too. Both spellings (e.g.
// "qc" + "qc_issue") are listed because callers pass slightly
// different keys -- DailyReports.jsx passes "qc" while standalone
// flows sometimes use "qc_issue". Easier to handle both than force
// callers to normalize.
//
// photo + activity are intentionally NOT in the map -- they have no
// status-only path, so every edit on those event types requires
// edit_reason (matching the pre-Bug-#57 universal behavior).
const STATUS_ONLY_FIELDS_BY_EVENT_TYPE = {
  delay:               new Set(["status", "resolved_note", "resolved_date", "resolved_by_user_id"]),
  qc:                  new Set(["status", "resolution_notes", "resolved_at"]),
  qc_issue:            new Set(["status", "resolution_notes", "resolved_at"]),
  rfi:                 new Set(["status", "response_text", "date_responded"]),
  coordination:        new Set(["outcome", "outcome_notes"]),
  coordination_event:  new Set(["outcome", "outcome_notes"]),
  delivery:            new Set(["status", "received_by"]),
  safety:              new Set(["status", "closed_note", "closed_date"]),
  safety_event:        new Set(["status", "closed_note", "closed_date"]),
};

export default function LockedEventEditModal({
  open, onOpenChange,
  eventType, eventId, dailyReportId, patchUrl,
  fields = [], currentValues = {}, onSaved,
  // Optional override label for the modal title (e.g. "Edit delay").
  title,
}) {
  const [mode, setMode] = useState("edit"); // "edit" | "request"
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [requestReason, setRequestReason] = useState("");
  const [values, setValues] = useState({ ...currentValues });

  const reset = () => {
    setMode("edit");
    setReason(""); setRequestReason("");
    setValues({ ...currentValues });
    setBusy(false);
  };

  const close = () => { reset(); onOpenChange?.(false); };

  // Bug #57 (2026-05-14) -- compute the diff once. Powers BOTH:
  //   (a) the gate below (reason required iff any changed key is
  //       outside the event-type's status-only allowed set)
  //   (b) the PATCH body in submitEdit (send only what changed --
  //       matches the modal's existing "Diff: only send fields that
  //       changed" intent, now exposed as a shared computation so
  //       the gate can read the same source of truth)
  const allowedStatusOnly = STATUS_ONLY_FIELDS_BY_EVENT_TYPE[eventType] || new Set();
  const changedKeys = (() => {
    const out = [];
    for (const f of fields) {
      const prev = currentValues[f.key];
      const next = values[f.key];
      if (next !== undefined && next !== prev) out.push(f.key);
    }
    return out;
  })();
  // isFieldEdit: true iff ANY changed key is outside the allowed set.
  // Empty changedKeys -> not a field edit (nothing changed at all).
  // No allowed set (event type without a status-only path) -> every
  // change is a field edit and requires reason.
  const isFieldEdit = (() => {
    if (changedKeys.length === 0) return false;
    if (allowedStatusOnly.size === 0) return true;
    for (const k of changedKeys) {
      if (!allowedStatusOnly.has(k)) return true;
    }
    return false;
  })();
  // Bug #57 -- reason is REQUIRED only when at least one changed
  // field is outside the status-only allowed set. Status-only
  // transitions (e.g. RFI close from inside the DR view: status +
  // response_text + date_responded) flow through Bug #49's backend
  // exemption with no client-side gate.
  const reasonRequired = isFieldEdit;
  const reasonValid = (reason || "").trim().length >= REASON_MIN;
  const reasonOk = !reasonRequired || reasonValid;
  const hasAnyChange = changedKeys.length > 0;

  // ---- Edit mode submit ----
  const submitEdit = async () => {
    if (busy) return;
    if (!hasAnyChange) {
      toast.error("No changes to save.");
      return;
    }
    if (reasonRequired && !reasonValid) {
      toast.error(
        `Edit reason must be at least ${REASON_MIN} characters for field changes. ` +
        `Status-only changes (close / resolve) are exempt.`,
      );
      return;
    }
    // Diff: only send fields that changed (avoid blasting unset values
    // back at the server, especially for fields that weren't shown).
    const updates = {};
    for (const k of changedKeys) {
      updates[k] = values[k];
    }
    setBusy(true);
    try {
      // Bug #57: attach edit_reason ONLY if user supplied one. Empty
      // is omitted so status-only patches route through the Bug #49
      // exemption cleanly (the backend synthesizes a reason from the
      // note field instead of getting an empty-string user reason).
      const body = (reason && reason.trim())
        ? { ...updates, edit_reason: reason.trim() }
        : updates;
      await api.patch(patchUrl, body);
      toast.success(
        isFieldEdit
          ? "Edit saved. Audit log captured before/after."
          : "Status updated. Audit log captured the transition.",
      );
      onSaved?.();
      close();
    } catch (e) {
      const status = e.response?.status;
      const detail = e.response?.data?.detail;
      // Server says we need to file a request first.
      if (
        status === 403
        && detail && typeof detail === "object"
        && detail.error === "edit_request_required"
      ) {
        setMode("request");
        toast.info("Self-edit window has closed. File an edit request to ask Admin/PM for a new window.");
        return;
      }
      toast.error(detail?.message || detail || e.message || "Edit failed");
    } finally { setBusy(false); }
  };

  // ---- Request mode submit ----
  const submitRequest = async () => {
    if (busy) return;
    if ((requestReason || "").trim().length < REASON_MIN) {
      toast.error(`Request reason must be at least ${REASON_MIN} characters.`);
      return;
    }
    setBusy(true);
    try {
      await api.post("/edit-requests", {
        daily_report_id: dailyReportId,
        event_type: eventType,
        event_id: eventId,
        reason: requestReason.trim(),
      });
      toast.success("Edit request filed. Admin or PM will review.");
      onSaved?.();
      close();
    } catch (e) {
      const status = e.response?.status;
      const detail = e.response?.data?.detail;
      if (status === 409) {
        toast.error(detail || "You already have a pending request for this DR.");
      } else {
        toast.error(detail?.message || detail || e.message || "Request failed");
      }
    } finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => o ? onOpenChange?.(true) : close()}>
      <DialogContent className="max-w-lg" data-testid="locked-edit-modal">
        <DialogHeader>
          <DialogTitle>
            {mode === "edit"
              ? (title || `Edit ${eventType} (post-submit)`)
              : `Request edit access for this ${eventType}`}
          </DialogTitle>
        </DialogHeader>
        {mode === "edit" ? (
          <div className="space-y-3 text-sm">
            <div className="bg-amber-50 border border-amber-200 rounded p-2 text-xs text-amber-900">
              <div className="font-semibold mb-1">Edit posture — post-submit window</div>
              <p>
                The parent daily report is locked. Your edit will be
                captured in the audit log with before/after snapshots and
                surfaced in the TIA. {reasonRequired
                  ? `Reason is required (min ${REASON_MIN} characters) for field changes.`
                  : `Status-only changes (resolve / close / answer) are exempt from the reason requirement -- the status-specific note field replaces it for audit purposes.`
                }
              </p>
              <p className="mt-1">
                The same 24-hour self-edit window also governs <em>adding new
                events</em> to this locked DR (Bucket D Addition 5,
                2026-05-12). If you need to add a forgotten delay / RFI /
                QC / safety event / delivery / coordination event / photo /
                activity row, use the "+ event" button on the relevant
                panel rather than editing an existing row. After the 24-
                hour window closes, an approved edit_request opens BOTH
                edit AND add for the same 1-hour window.
              </p>
            </div>
            {fields.map((f) => (
              <FieldEditor key={f.key} f={f} value={values[f.key]}
                onChange={(v) => setValues({ ...values, [f.key]: v })}
                entityType={EVENT_TYPE_TO_ENTITY[eventType] || eventType}
                entityId={eventId} />
            ))}
            <div>
              {/* Bug #57 (2026-05-14) -- conditional label / hint based
                  on whether the current diff is a status-only transition
                  (Bug #49 exemption) or a field edit (reason required). */}
              <label className="field-label">
                Edit reason{reasonRequired ? " *" : " (optional for status-only changes)"}
              </label>
              <textarea data-testid="locked-edit-reason"
                value={reason} onChange={(e) => setReason(e.target.value)}
                placeholder={reasonRequired
                  ? `Why this edit is needed (min ${REASON_MIN} characters).`
                  : `Optional: explain the resolve/close. Will be appended to the audit row alongside the note field.`
                }
                className="w-full p-2 rounded border border-slate-200 text-sm h-20" />
              <div className="text-[10px] text-slate-500 text-right">
                {(reason || "").trim().length} / {REASON_MIN} min
                {reasonRequired
                  ? " — required for field changes"
                  : " — optional for status-only"}
              </div>
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={close}
                className="h-9 px-3 rounded border border-slate-200 text-sm">
                Cancel
              </button>
              {/* Bug #57 -- Save disabled when either nothing has changed
                  OR (reason is required AND the user hasn't supplied
                  10+ chars). Title hint surfaces the exact gate. */}
              <Button data-testid="locked-edit-save" onClick={submitEdit}
                disabled={busy || !hasAnyChange || !reasonOk}
                title={
                  !hasAnyChange
                    ? "No changes to save."
                    : (!reasonOk
                      ? `Edit reason must be at least ${REASON_MIN} characters for field changes.`
                      : undefined)
                }
                className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed">
                {busy ? "Saving…" : "Save edit"}
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-3 text-sm">
            <div className="bg-amber-50 border border-amber-200 rounded p-2 text-xs text-amber-900">
              The 24-hour self-edit window has closed for this daily
              report. File an edit request -- an Admin or PM will review
              and (if approved) open a 1-hour edit window for you.
            </div>
            <div>
              <label className="field-label">Why do you need to edit? *</label>
              <textarea data-testid="locked-edit-request-reason"
                value={requestReason} onChange={(e) => setRequestReason(e.target.value)}
                placeholder={`Describe what you need to fix (min ${REASON_MIN} characters).`}
                className="w-full p-2 rounded border border-slate-200 text-sm h-24" />
              <div className="text-[10px] text-slate-500 text-right">
                {(requestReason || "").trim().length} / {REASON_MIN} min
              </div>
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={close}
                className="h-9 px-3 rounded border border-slate-200 text-sm">
                Cancel
              </button>
              <Button data-testid="locked-edit-request-submit"
                onClick={submitRequest} disabled={busy}
                className="bg-amber-600 hover:bg-amber-700 disabled:opacity-50">
                {busy ? "Filing…" : "File edit request"}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function FieldEditor({ f, value, onChange, entityType, entityId }) {
  const id = `locked-edit-${f.key}`;
  const userRole = (getUser()?.role || "").toLowerCase();
  // For numeric fields, coerce the AI-suggested value through Number()
  // so the input renders correctly + onChange semantics stay numeric.
  const wrap = (input) => (
    <JigsawField
      entityType={entityType} entityId={entityId}
      fieldPath={f.key} userRole={userRole}
      fieldLabel={f.label}
      onConfirm={(v) => {
        if (f.type === "number") {
          const n = Number(v);
          onChange(Number.isFinite(n) ? n : (v ?? ""));
        } else {
          onChange(v ?? "");
        }
      }}
    >
      {input}
    </JigsawField>
  );
  if (f.type === "select") {
    return (
      <div>
        <label className="field-label">{f.label}</label>
        {wrap(
          <select data-testid={id} value={value ?? ""}
            onChange={(e) => onChange(e.target.value)}
            className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
            <option value="">— select —</option>
            {(f.options || []).map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        )}
      </div>
    );
  }
  if (f.type === "textarea") {
    return (
      <div>
        <label className="field-label">{f.label}</label>
        {wrap(
          <textarea data-testid={id} value={value ?? ""}
            onChange={(e) => onChange(e.target.value)}
            className="w-full p-2 rounded border border-slate-200 text-sm h-20" />
        )}
      </div>
    );
  }
  return (
    <div>
      <label className="field-label">{f.label}</label>
      {wrap(
        <input data-testid={id} type={f.type || "text"}
          value={value ?? ""}
          onChange={(e) => onChange(f.type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value)}
          className="w-full h-9 px-3 rounded border border-slate-200 text-sm" />
      )}
    </div>
  );
}
