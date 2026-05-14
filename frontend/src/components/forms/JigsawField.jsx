// Bucket G Phase 3 (2026-05-11) -- Jigsaw Pre-fill UX widget.
//
// Wraps any form input. Three lifecycle states per Sid's V1 Product Flow:
//   - solid green   = human confirmed
//   - light green   = AI suggested, pending confirmation
//   - gray          = no suggestion / missing
// Plus two derived presentations:
//   - rejected      = small grey marker; the suggestion archived
//   - edited        = user changed value from the AI suggestion; the
//                     suggestion stays pending (audit knows what AI said)
//
// Approach (per Sid's call): suggestions bind to existing entity ids.
// For create-new forms (no entityId yet), this component silently
// renders only children -- no fetch, no callout. For edit-existing
// forms, it pulls suggestions for the (entity_type, entity_id,
// field_path) tuple and renders the appropriate state.
//
// Role gating: if user.role NOT in the suggestion's authorized_
// confirmer_roles, Accept/Reject buttons are disabled with a tooltip
// listing who CAN confirm. The Edit affordance is always available
// (anyone can edit the underlying form field; AI lineage just stays
// on the suggestion regardless).
//
// Confidence palette mirrors the Photo AI card from Bucket F so users
// get a consistent visual language for "how sure is the AI" across
// every surface.
import { useEffect, useState, useCallback } from "react";
import {
  Sparkles, Check, X, Edit3, AlertTriangle, Lock, Loader2, ChevronDown, ChevronUp,
} from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

// -----------------------------------------------------------------------
// Confidence tone mapping. Matches Bucket F's AiAnalysisCard so the
// visual vocabulary stays consistent.
// -----------------------------------------------------------------------
function toneForConfidence(score) {
  const v = Number(score == null ? 0 : score);
  if (v >= 0.85) return {
    band: "high", label: "High confidence",
    border: "border-emerald-400",
    pill: "bg-emerald-100 text-emerald-900 border-emerald-300",
    inputRing: "ring-1 ring-emerald-300 focus-within:ring-emerald-400",
  };
  if (v >= 0.70) return {
    band: "confident", label: "Confident",
    border: "border-sky-400",
    pill: "bg-sky-100 text-sky-900 border-sky-300",
    inputRing: "ring-1 ring-sky-300 focus-within:ring-sky-400",
  };
  if (v >= 0.30) return {
    band: "review", label: "Review suggested",
    border: "border-amber-400",
    pill: "bg-amber-100 text-amber-900 border-amber-300",
    inputRing: "ring-1 ring-amber-300 focus-within:ring-amber-400",
  };
  return {
    band: "low", label: "Low confidence — verify",
    border: "border-rose-400",
    pill: "bg-rose-100 text-rose-900 border-rose-300",
    inputRing: "ring-1 ring-rose-300 focus-within:ring-rose-400",
  };
}

// Human-friendly source labels for the callout. New producers added in
// Phase 2 are registered here; fall through to the raw key.
const SOURCE_LABELS = {
  ai_copilot: "AI Copilot",
  ai_photo_vision: "Photo AI",
  ai_rfi_drafter: "RFI Drafter",
  ai_qc_analyzer: "QC Analyzer",
  ai_safety_analyzer: "Safety Analyzer",
  ai_schedule_analyzer: "Schedule Analyzer",
  system_inference: "System",
  calibration_rag: "Calibration RAG",
};

// Light-green pending input chrome. Plain background + sparkle in the
// top-right corner of the wrapped input. Mobile: hover/active states
// degrade to tap-to-expand below.
function PendingInputChrome({ children, tone, onToggle, expanded, mobile }) {
  return (
    <div
      className={`relative rounded ${tone.inputRing} transition-shadow`}
      data-testid="jigsaw-pending-chrome"
    >
      {children}
      <button
        type="button"
        onClick={onToggle}
        data-testid="jigsaw-pending-expand"
        title={expanded ? "Hide AI suggestion" : "Show AI suggestion"}
        className={
          "absolute top-1 right-1 inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[10px] font-medium border " +
          tone.pill +
          " hover:opacity-90"
        }
      >
        <Sparkles className="h-2.5 w-2.5" />
        {mobile ? null : "AI"}
        {expanded
          ? <ChevronUp className="h-2.5 w-2.5" />
          : <ChevronDown className="h-2.5 w-2.5" />}
      </button>
    </div>
  );
}


// =====================================================================
// Main component.
// =====================================================================
export default function JigsawField({
  entityType,
  entityId,
  fieldPath,
  // Optional: caller's current form value. When the user clicks Accept
  // we call onConfirm with the suggested value so the parent form
  // updates its state. Optional in case the parent just wants the
  // visual chrome without binding.
  currentValue,
  onConfirm,
  onReject,
  // Caller's role. Used to gate Accept/Reject. If absent, we
  // optimistically allow both buttons (the backend will 403 on a
  // role mismatch and toast will surface the message).
  userRole,
  // Optional renderer for the suggested value -- so the callout can
  // pretty-print a date or display a multi-line text. Falls back to
  // String(suggestedValue).
  renderSuggestedValue,
  // Optional title used in the callout instead of the field_path.
  fieldLabel,
  // The wrapped input element. Always rendered. If no entityId we
  // pass through; the component degrades to identity rendering.
  children,
  // Phase 5 will pass an explicit className for mobile-first
  // responsive overrides; for now we detect via window width.
  className = "",
}) {
  const [loading, setLoading] = useState(false);
  const [suggestion, setSuggestion] = useState(null);   // null OR row
  const [terminal, setTerminal] = useState(null);       // confirmed / rejected metadata
  // Bucket G Phase 5 (2026-05-11) -- reactive mobile detection. The
  // initial state defaults to collapsed-on-mobile so a 380px-wide
  // screen doesn't have the callout taking up most of the viewport.
  // Desktop keeps the callout expanded by default. Resize listener
  // keeps the band accurate across orientation changes.
  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== "undefined" && window.innerWidth < 640,
  );
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onResize = () => setIsMobile(window.innerWidth < 640);
    window.addEventListener("resize", onResize, { passive: true });
    return () => window.removeEventListener("resize", onResize);
  }, []);
  // Default expanded only on desktop; mobile users tap the chevron
  // to expand on demand.
  const [expanded, setExpanded] = useState(() => {
    if (typeof window === "undefined") return true;
    return window.innerWidth >= 640;
  });
  const [busy, setBusy] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [editedFromAi, setEditedFromAi] = useState(false);

  const noEntity = !entityId;

  const load = useCallback(async () => {
    if (noEntity) { setSuggestion(null); return; }
    setLoading(true);
    try {
      // include_terminal so we can surface "confirmed by X at Y" /
      // "rejected" without a second fetch. Suggestions arrive newest-
      // first per the server's sort; we filter to this field_path.
      const { data } = await api.get(
        `/entities/${entityType}/${entityId}/suggestions?include_terminal=true`,
      );
      const list = (data?.suggestions || []).filter(
        (s) => s.field_path === fieldPath,
      );
      const pending = list.find((s) => s.status === "pending");
      const lastTerminal = list.find(
        (s) => ["confirmed", "rejected", "superseded", "expired"].includes(s.status),
      );
      setSuggestion(pending || null);
      setTerminal(lastTerminal || null);
      setEditedFromAi(false);
    } catch (e) {
      // Non-fatal -- field still renders. Don't toast (would spam
      // the user with one error per JigsawField on the page).
      setSuggestion(null); setTerminal(null);
    } finally { setLoading(false); }
  }, [noEntity, entityType, entityId, fieldPath]);

  useEffect(() => { load(); }, [load]);

  // -------------------------------------------------------------------
  // Role gating helper.
  // -------------------------------------------------------------------
  const allowedRoles = suggestion?.authorized_confirmer_roles || [];
  const roleAllowed = !userRole || allowedRoles.length === 0 || allowedRoles.includes(userRole);

  const accept = async () => {
    if (!suggestion || busy) return;
    setBusy(true);
    try {
      await api.post(`/field-suggestions/${suggestion.id}/confirm`);
      // Push value into the form via the callback.
      try { onConfirm?.(suggestion.suggested_value); } catch {}
      toast.success("AI suggestion accepted");
      // Reload to pull the freshly-confirmed terminal row.
      await load();
    } catch (e) {
      const detail = e.response?.data?.detail;
      const msg = typeof detail === "string"
        ? detail
        : detail?.reason || "Confirm failed";
      toast.error(msg);
    } finally { setBusy(false); }
  };

  const reject = async () => {
    if (!suggestion || busy) return;
    if (!rejectReason || rejectReason.trim().length < 10) {
      toast.error("Reject reason must be at least 10 characters");
      return;
    }
    setBusy(true);
    try {
      await api.post(`/field-suggestions/${suggestion.id}/reject`, {
        reason: rejectReason.trim(),
      });
      try { onReject?.(); } catch {}
      toast.success("AI suggestion rejected");
      setRejectOpen(false);
      setRejectReason("");
      await load();
    } catch (e) {
      const detail = e.response?.data?.detail;
      const msg = typeof detail === "string"
        ? detail
        : detail?.reason || "Reject failed";
      toast.error(msg);
    } finally { setBusy(false); }
  };

  const editInline = () => {
    if (!suggestion) return;
    try { onConfirm?.(suggestion.suggested_value); } catch {}
    setEditedFromAi(true);
    // Suggestion stays pending in the audit. The "Edited from AI"
    // marker on the input shows the lineage. The user will either
    // explicitly confirm later or the suggestion will be auto-
    // superseded if AI re-runs.
  };

  // -------------------------------------------------------------------
  // STATE 1: no entity yet (create form) -- pass through.
  // -------------------------------------------------------------------
  if (noEntity) return <>{children}</>;
  // (mobile detection now lives in component state via useState +
  //  resize listener; see hooks above.)

  // -------------------------------------------------------------------
  // STATE 2: pending AI suggestion exists.
  // -------------------------------------------------------------------
  if (suggestion) {
    const tone = toneForConfidence(suggestion.confidence_score);
    const sourceLabel = SOURCE_LABELS[suggestion.suggested_by] || suggestion.suggested_by;
    return (
      <div className={`space-y-1 ${className}`}
        data-testid={`jigsaw-pending-${fieldPath}`}>
        <PendingInputChrome
          tone={tone}
          expanded={expanded}
          mobile={isMobile}
          onToggle={() => setExpanded((v) => !v)}
        >
          {children}
        </PendingInputChrome>
        {expanded && (
          <div
            className={`rounded p-2 text-xs border ${tone.pill}`}
            data-testid={`jigsaw-callout-${fieldPath}`}
          >
            <div className="flex items-center gap-2 mb-1">
              <Sparkles className="h-3 w-3" />
              <span className="font-medium">
                {sourceLabel} suggested
                {fieldLabel ? ` ${fieldLabel}` : ""}:
              </span>
              {suggestion.confidence_score != null && (
                <span className="text-[10px] opacity-70">
                  {Math.round(suggestion.confidence_score * 100)}% · {tone.label}
                </span>
              )}
            </div>
            <div className="text-slate-800 bg-white/60 rounded p-1.5 mb-1.5 break-words"
              data-testid={`jigsaw-suggested-value-${fieldPath}`}>
              {renderSuggestedValue
                ? renderSuggestedValue(suggestion.suggested_value)
                : (
                  typeof suggestion.suggested_value === "object"
                    ? JSON.stringify(suggestion.suggested_value)
                    : String(suggestion.suggested_value ?? "")
                )}
            </div>
            {editedFromAi && (
              <div className="text-[10px] text-slate-600 italic mb-1">
                Edited from AI suggestion — input value differs from above.
              </div>
            )}
            {/* Phase 5 mobile polish: thumb-friendly button heights
                on touch surfaces. Desktop keeps the compact h-7. */}
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                disabled={!roleAllowed || busy}
                onClick={accept}
                data-testid={`jigsaw-accept-${fieldPath}`}
                title={
                  !roleAllowed
                    ? `Only ${allowedRoles.join(", ")} can confirm this field`
                    : "Accept the AI suggestion"
                }
                className={
                  `${isMobile ? "h-9 px-3" : "h-7 px-2"} ` +
                  "rounded bg-emerald-600 text-white text-xs inline-flex items-center gap-1 hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed"
                }
              >
                {busy
                  ? <Loader2 className="h-3 w-3 animate-spin" />
                  : <Check className="h-3 w-3" />}
                Accept
              </button>
              <button
                type="button"
                disabled={!roleAllowed || busy}
                onClick={() => setRejectOpen(true)}
                data-testid={`jigsaw-reject-${fieldPath}`}
                title={
                  !roleAllowed
                    ? `Only ${allowedRoles.join(", ")} can reject this field`
                    : "Reject (requires a reason)"
                }
                className={
                  `${isMobile ? "h-9 px-3" : "h-7 px-2"} ` +
                  "rounded border border-rose-300 bg-white text-rose-700 text-xs inline-flex items-center gap-1 hover:bg-rose-50 disabled:opacity-50 disabled:cursor-not-allowed"
                }
              >
                <X className="h-3 w-3" />
                Reject
              </button>
              <button
                type="button"
                onClick={editInline}
                data-testid={`jigsaw-edit-${fieldPath}`}
                title="Copy the AI value into the input, then edit it manually"
                className={
                  `${isMobile ? "h-9 px-3" : "h-7 px-2"} ` +
                  "rounded border border-slate-300 bg-white text-slate-700 text-xs inline-flex items-center gap-1 hover:bg-slate-50"
                }
              >
                <Edit3 className="h-3 w-3" />
                Edit
              </button>
              {!roleAllowed && (
                <span className="text-[10px] text-slate-600 inline-flex items-center gap-1 ml-1">
                  <Lock className="h-2.5 w-2.5" />
                  Restricted to {allowedRoles.join(", ")}
                </span>
              )}
            </div>
            {suggestion.source_context && (
              <div className="text-[10px] text-slate-600 mt-1 italic truncate"
                title={suggestion.source_context}>
                Source: {suggestion.source_context}
              </div>
            )}
          </div>
        )}
        {rejectOpen && (
          <div
            className="rounded p-2 text-xs border border-rose-300 bg-rose-50 space-y-1"
            data-testid={`jigsaw-reject-dialog-${fieldPath}`}
          >
            <label className="block text-[10px] uppercase text-rose-800">
              Rejection reason (required, min 10 chars)
            </label>
            <textarea
              data-testid={`jigsaw-reject-reason-${fieldPath}`}
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              rows={2}
              placeholder="Explain why this suggestion is wrong (calibration RAG learns from your reason)…"
              className="w-full p-1.5 text-xs rounded border border-rose-300 bg-white"
            />
            {/* Bug #48 (2026-05-14) -- char counter + gated submit button.
                Mirrors the Bug #54 DelayForm.jsx edit_reason UX so the
                user sees the gate up-front instead of finding out via
                toast error after click. Also defends against future
                bypass attempts if the toast-error path is ever skipped. */}
            <div className="text-[10px] text-rose-800/70 text-right">
              {(rejectReason || "").trim().length} / 10 min
              {(rejectReason || "").trim().length < 10
                ? " — required for TIA defensibility"
                : " — ready"}
            </div>
            <div className="flex gap-1">
              <button
                type="button"
                disabled={busy || (rejectReason || "").trim().length < 10}
                onClick={reject}
                data-testid={`jigsaw-reject-submit-${fieldPath}`}
                title={(rejectReason || "").trim().length < 10
                  ? "Rejection reason must be at least 10 characters."
                  : undefined}
                className="h-7 px-2 rounded bg-rose-600 text-white text-xs hover:bg-rose-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Submit
              </button>
              <button
                type="button" disabled={busy}
                onClick={() => { setRejectOpen(false); setRejectReason(""); }}
                className="h-7 px-2 rounded border border-slate-300 bg-white text-xs"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  // -------------------------------------------------------------------
  // STATE 3: terminal (confirmed / rejected / superseded / expired).
  // We render the input plain + a small badge BELOW it for transparency.
  // -------------------------------------------------------------------
  if (terminal) {
    const isConfirmed = terminal.status === "confirmed";
    const isRejected = terminal.status === "rejected";
    const isSuperseded = terminal.status === "superseded";
    const isExpired = terminal.status === "expired";
    let badgeText = "";
    let badgeTone = "";
    let Icon = Check;
    if (isConfirmed) {
      badgeText = "Confirmed";
      badgeTone = "bg-emerald-100 text-emerald-900 border-emerald-300";
      Icon = Check;
    } else if (isRejected) {
      badgeText = terminal.rejection_reason === "entity_deleted"
        ? "Auto-rejected (entity removed)"
        : "AI suggestion rejected";
      badgeTone = "bg-slate-100 text-slate-700 border-slate-300";
      Icon = X;
    } else if (isSuperseded) {
      badgeText = "Replaced by newer suggestion";
      badgeTone = "bg-slate-100 text-slate-700 border-slate-300";
      Icon = AlertTriangle;
    } else if (isExpired) {
      badgeText = "AI suggestion expired";
      badgeTone = "bg-slate-100 text-slate-500 border-slate-200";
      Icon = AlertTriangle;
    }
    return (
      <div className={`space-y-0.5 ${className}`}
        data-testid={`jigsaw-terminal-${fieldPath}`}>
        {children}
        <span
          data-testid={`jigsaw-terminal-badge-${fieldPath}`}
          className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border ${badgeTone}`}
          title={
            isConfirmed
              ? `Confirmed by ${terminal.confirmed_by || "system"} at ${terminal.confirmed_at || ""}`
              : terminal.rejection_reason || badgeText
          }
        >
          <Icon className="h-2.5 w-2.5" />
          {badgeText}
        </span>
      </div>
    );
  }

  // -------------------------------------------------------------------
  // STATE 4: loading OR no suggestion present.
  // -------------------------------------------------------------------
  if (loading) {
    return (
      <div className={`space-y-0.5 ${className}`}
        data-testid={`jigsaw-loading-${fieldPath}`}>
        {children}
      </div>
    );
  }

  // No suggestion -> pass through cleanly.
  return <>{children}</>;
}
