// Bucket F Phase 3 (2026-05-08) -- Photo AI analysis card.
//
// Renders the AI section of the tray-detail modal across all 7
// lifecycle states: pending / analyzing / complete / failed /
// skipped / rejected_by_user / budget_paused.
//
// Polls GET /photo-tray/{id}/ai-analysis every 3s while status is
// pending or analyzing, max 30s (10 polls) then stops; user can
// click Re-analyze to retry. Stops polling on tab-hidden (uses the
// same visibilitychange pattern as Tasks page gap icon).
import { useEffect, useState, useCallback } from "react";
import {
  Sparkles, RefreshCw, AlertTriangle, ShieldAlert, X, Edit3, Check,
  Loader2, Lock, ChevronDown, ChevronUp, Info,
} from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";

// Confidence -> tone mapping per spec.
function confidenceBadge(score) {
  const v = Number(score || 0);
  if (v >= 0.85) return { label: "High confidence", tone: "bg-emerald-100 text-emerald-900 border-emerald-300" };
  if (v >= 0.70) return { label: "Confident",      tone: "bg-sky-100 text-sky-900 border-sky-300" };
  if (v >= 0.30) return { label: "Review suggested", tone: "bg-amber-100 text-amber-900 border-amber-300" };
  return { label: "Low confidence — verify", tone: "bg-rose-100 text-rose-900 border-rose-300" };
}

// Reason -> user-facing label for the skipped/failed/budget_paused
// states. Server reason codes mapped to short copy + tone.
const SKIP_REASON_LABELS = {
  cui_blocked: { label: "AI disabled — CUI project", tone: "text-slate-500" },
  tenant_ai_disabled: { label: "AI disabled for this tenant", tone: "text-slate-500" },
  project_ai_disabled: { label: "AI disabled for this project", tone: "text-slate-500" },
  pre_ai_upload: { label: "Legacy photo — AI didn't run", tone: "text-slate-400" },
  tray_deleted_mid_flight: { label: "Photo was deleted during analysis", tone: "text-slate-500" },
};

const FAILURE_LABELS = {
  image_too_large: "Image too large (>20MB)",
  not_an_image: "Not recognized as an image (HEIC plugin missing?)",
  image_conversion_failed: "Image preprocessing failed",
  storage_unavailable: "Couldn't read photo bytes from storage",
  vision_api_error: "Anthropic Vision API returned an error",
  vision_timeout: "Vision call timed out (>30s)",
  vision_malformed_json: "AI response wasn't valid JSON",
};

const POLL_INTERVAL_MS = 3000;
const POLL_MAX_DURATION_MS = 30000;
const POLL_MAX_ITERATIONS = POLL_MAX_DURATION_MS / POLL_INTERVAL_MS;

export default function AiAnalysisCard({ trayId, isAdmin = false, onChanged }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [pollCount, setPollCount] = useState(0);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!trayId) return;
    setLoading(true);
    setError(null);
    try {
      const r = await api.get(`/photo-tray/${trayId}/ai-analysis`);
      setData(r.data);
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "Failed to load AI analysis");
    } finally { setLoading(false); }
  }, [trayId]);

  useEffect(() => { load(); }, [load]);

  // Poll while pending/analyzing. Stops on terminal state, after 30s
  // total, or when the document goes hidden.
  useEffect(() => {
    if (!data) return undefined;
    const status = data.ai_analysis_status;
    if (status !== "pending" && status !== "analyzing") return undefined;
    if (pollCount >= POLL_MAX_ITERATIONS) return undefined;
    let cancelled = false;
    const id = setTimeout(async () => {
      if (cancelled || document.hidden) return;
      setPollCount((p) => p + 1);
      try {
        const r = await api.get(`/photo-tray/${trayId}/ai-analysis`);
        if (!cancelled) setData(r.data);
      } catch { /* keep last data; UI shows stale */ }
    }, POLL_INTERVAL_MS);
    return () => { cancelled = true; clearTimeout(id); };
  }, [data, pollCount, trayId]);

  const triggerAnalyze = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.post(`/photo-tray/${trayId}/analyze`);
      toast.success(`AI re-analyze: ${r.data?.status || "queued"}`);
      setPollCount(0);
      await load();
      onChanged?.();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Re-analyze failed");
    } finally { setBusy(false); }
  };

  if (loading && !data) {
    return (
      <div data-testid="ai-card-loading" className="p-3 border border-slate-200 rounded bg-slate-50 text-xs text-slate-500 flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading AI analysis…
      </div>
    );
  }

  if (error) {
    return (
      <div data-testid="ai-card-error" className="p-3 border border-rose-200 rounded bg-rose-50 text-xs flex items-center gap-2">
        <AlertTriangle className="h-3.5 w-3.5 text-rose-600" />
        <span className="flex-1 text-rose-900">{error}</span>
        <button onClick={load} className="h-7 px-2 rounded bg-rose-600 text-white text-xs hover:bg-rose-700">
          Retry
        </button>
      </div>
    );
  }

  if (!data) return null;

  const status = data.ai_analysis_status;

  // ---- pending / analyzing ----
  if (status === "pending" || status === "analyzing") {
    const stopped = pollCount >= POLL_MAX_ITERATIONS;
    return (
      <div data-testid={`ai-card-${status}`}
        className="p-3 border border-sky-200 rounded bg-sky-50 text-xs space-y-2">
        <div className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-sky-700" />
          <span className="font-medium text-sky-900">
            {status === "pending" ? "AI analysis queued…" : "Analyzing photo…"}
          </span>
          <Loader2 className="h-3 w-3 animate-spin text-sky-700" />
        </div>
        <div className="text-sky-800/70">
          {stopped
            ? "Still working. The analysis usually finishes within 30 seconds."
            : `Polling every ${POLL_INTERVAL_MS / 1000}s (${pollCount}/${POLL_MAX_ITERATIONS}).`}
        </div>
        {stopped && (
          <button onClick={load} className="h-7 px-2 rounded bg-sky-600 text-white text-xs hover:bg-sky-700">
            Refresh now
          </button>
        )}
      </div>
    );
  }

  // ---- failed ----
  if (status === "failed") {
    const reason = data.ai_failure_reason || "unknown";
    const label = FAILURE_LABELS[reason] || `Analysis failed (${reason})`;
    return (
      <div data-testid="ai-card-failed"
        className="p-3 border border-rose-200 rounded bg-rose-50 text-xs space-y-2">
        <div className="flex items-center gap-2 text-rose-900">
          <AlertTriangle className="h-3.5 w-3.5" />
          <span className="font-medium">AI analysis failed</span>
        </div>
        <div className="text-rose-800">{label}</div>
        <div className="text-[10px] text-rose-700 font-mono">{reason}</div>
        <button data-testid="ai-card-retry" onClick={triggerAnalyze} disabled={busy}
          className="h-7 px-2 rounded bg-rose-600 text-white text-xs hover:bg-rose-700 disabled:opacity-50 inline-flex items-center gap-1">
          <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} />
          {busy ? "Retrying…" : "Retry"}
        </button>
      </div>
    );
  }

  // ---- skipped ----
  if (status === "skipped") {
    const reason = data.ai_failure_reason;
    const cfg = SKIP_REASON_LABELS[reason] || { label: `AI skipped (${reason || "no reason"})`, tone: "text-slate-500" };
    return (
      <div data-testid="ai-card-skipped"
        className="p-3 border border-slate-200 rounded bg-slate-50 text-xs flex items-center gap-2">
        <Lock className="h-3.5 w-3.5 text-slate-400" />
        <span className={cfg.tone}>{cfg.label}</span>
      </div>
    );
  }

  // ---- rejected_by_user ----
  if (status === "rejected_by_user") {
    return (
      <div data-testid="ai-card-rejected"
        className="p-3 border border-slate-200 rounded bg-slate-50 text-xs space-y-2">
        <div className="flex items-center gap-2 text-slate-600">
          <X className="h-3.5 w-3.5" />
          <span className="font-medium">AI suggestion rejected</span>
        </div>
        {data.ai_user_override_reason && (
          <div className="text-slate-500 italic">"{data.ai_user_override_reason}"</div>
        )}
        <button data-testid="ai-card-reanalyze" onClick={triggerAnalyze} disabled={busy}
          className="h-7 px-2 rounded bg-slate-200 text-slate-700 text-xs hover:bg-slate-300 disabled:opacity-50 inline-flex items-center gap-1">
          <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} />
          Re-analyze
        </button>
      </div>
    );
  }

  // ---- budget_paused ----
  if (status === "budget_paused") {
    return (
      <div data-testid="ai-card-budget-paused"
        className="p-3 border border-amber-300 rounded bg-amber-50 text-xs space-y-2">
        <div className="flex items-center gap-2 text-amber-900">
          <AlertTriangle className="h-3.5 w-3.5" />
          <span className="font-medium">AI paused — monthly budget reached</span>
        </div>
        <div className="text-amber-800">
          New photo analyses are paused until the budget resets on the 1st
          {isAdmin && " or an admin raises the cap"}.
        </div>
        {isAdmin && (
          <a href="/settings" className="text-xs text-amber-700 underline">
            Settings → AI Usage to increase budget
          </a>
        )}
      </div>
    );
  }

  // ---- complete ----
  // Default complete render: class badge + confidence + caption +
  // event type + trade + safety concerns + action buttons.
  const conf = confidenceBadge(data.ai_confidence_score);
  const safety = Array.isArray(data.ai_safety_concerns_detected)
    ? data.ai_safety_concerns_detected : [];
  return (
    <div data-testid="ai-card-complete"
      className="p-3 border border-blue-200 rounded bg-blue-50/40 text-xs space-y-2">
      {/* Bucket F Phase 4 -- cross-project reuse banner. Renders when
          ai_reused_from_phash is true (the analyzer found a prior
          analysis on the same pHash and copied the result over
          without paying for another Vision call). Promoted from the
          small inline line to a banner at the top so users
          immediately see this analysis cost zero tokens. */}
      {data.ai_reused_from_phash && (
        <div data-testid="ai-card-reused-banner"
          className="px-2 py-1.5 rounded bg-emerald-50 border border-emerald-200 text-emerald-900 text-[11px] flex items-center gap-2">
          <Check className="h-3.5 w-3.5 shrink-0" />
          <span>
            <span className="font-medium">AI analysis reused from previous upload.</span>{" "}
            Same image was analyzed before; no new tokens were spent.
          </span>
        </div>
      )}
      <div className="flex items-center gap-2">
        <Sparkles className="h-3.5 w-3.5 text-blue-700" />
        <span className="font-medium text-blue-900">AI suggestion</span>
        <span className={`ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] ${conf.tone}`}>
          {Math.round((data.ai_confidence_score || 0) * 100)}% · {conf.label}
        </span>
      </div>

      {safety.length > 0 && (
        <div data-testid="ai-card-safety" className="p-2 rounded border border-rose-300 bg-rose-50 space-y-1">
          <div className="flex items-center gap-1 font-medium text-rose-900">
            <ShieldAlert className="h-3 w-3" /> Safety concerns detected
          </div>
          <div className="flex flex-wrap gap-1">
            {safety.map((s, i) => (
              <span key={i} data-testid={`ai-safety-${i}`}
                className="text-[10px] px-1.5 py-0.5 rounded bg-rose-100 text-rose-900 border border-rose-200">
                {s}
              </span>
            ))}
          </div>
          <a href={`/safety-events?from_photo=${trayId}`}
            data-testid="ai-card-create-safety"
            className="inline-flex items-center gap-1 text-[10px] text-rose-700 underline">
            Create safety event from this →
          </a>
        </div>
      )}

      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        <div>
          <div className="text-[10px] uppercase text-slate-500">Class</div>
          <div className="font-medium text-slate-800 capitalize">
            {data.ai_suggested_class || "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">Event type</div>
          <div className="font-medium text-slate-800">
            {(data.ai_suggested_event_type || "none").replace(/_/g, " ")}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">Trade</div>
          <div className="font-medium text-slate-800">
            {data.ai_suggested_trade || "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">Model</div>
          <div className="text-slate-600 text-[10px] font-mono">
            {data.ai_model_version || "—"}
          </div>
        </div>
      </div>

      {data.ai_suggested_caption && (
        <div className="bg-white border border-slate-200 rounded p-2">
          <div className="text-[10px] uppercase text-slate-500 mb-0.5">Caption</div>
          <div className="text-slate-800 italic">"{data.ai_suggested_caption}"</div>
        </div>
      )}

      {data.ai_user_overridden && (
        <div className="flex items-center gap-1 text-amber-700 text-[10px]">
          <Edit3 className="h-3 w-3" />
          Overridden by user{data.ai_user_override_reason ? `: "${data.ai_user_override_reason}"` : ""}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-slate-100">
        <button data-testid="ai-card-override" onClick={() => setOverrideOpen(true)}
          className="h-7 px-2 rounded border border-slate-300 bg-white text-slate-700 text-xs hover:bg-slate-50 inline-flex items-center gap-1">
          <Edit3 className="h-3 w-3" /> Override
        </button>
        <button data-testid="ai-card-reject" onClick={() => setRejectOpen(true)}
          className="h-7 px-2 rounded border border-rose-200 bg-white text-rose-700 text-xs hover:bg-rose-50 inline-flex items-center gap-1">
          <X className="h-3 w-3" /> Reject
        </button>
      </div>

      {overrideOpen && (
        <AiOverrideDialog open={overrideOpen} onOpenChange={setOverrideOpen}
          data={data} trayId={trayId}
          onSaved={() => { setOverrideOpen(false); load(); onChanged?.(); }} />
      )}
      {rejectOpen && (
        <AiRejectDialog open={rejectOpen} onOpenChange={setRejectOpen}
          trayId={trayId}
          onSaved={() => { setRejectOpen(false); load(); onChanged?.(); }} />
      )}
    </div>
  );
}


// ---- Override dialog -----------------------------------------------

const OVERRIDE_CLASSES = ["progress", "discovery", "damage", "quality", "other"];
const OVERRIDE_EVENT_TYPES = [
  "delay", "rfi", "qc_issue", "safety_event",
  "coordination_event", "delivery", "none",
];

function AiOverrideDialog({ open, onOpenChange, data, trayId, onSaved }) {
  const [photoClass, setPhotoClass] = useState(data.ai_suggested_class || "progress");
  const [caption, setCaption] = useState(data.ai_suggested_caption || "");
  const [eventType, setEventType] = useState(data.ai_suggested_event_type || "none");
  const [trade, setTrade] = useState(data.ai_suggested_trade || "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (busy) return;
    if (!reason || reason.trim().length < 5) {
      toast.error("Override reason (5+ chars) required"); return;
    }
    setBusy(true);
    try {
      const body = {
        override_reason: reason.trim(),
      };
      if (photoClass !== data.ai_suggested_class) body.photo_class = photoClass;
      if (caption !== (data.ai_suggested_caption || "")) body.caption = caption;
      if (eventType !== data.ai_suggested_event_type) body.event_type = eventType;
      if (trade !== (data.ai_suggested_trade || "")) body.trade = trade;
      await api.post(`/photo-tray/${trayId}/override-ai`, body);
      toast.success("AI suggestion overridden");
      onSaved?.();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Override failed");
    } finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Override AI suggestion</DialogTitle></DialogHeader>
        <div className="space-y-2 text-sm">
          <div>
            <label className="text-[10px] uppercase text-slate-500">Photo class</label>
            <select value={photoClass} onChange={(e) => setPhotoClass(e.target.value)}
              className="w-full h-8 px-2 rounded border border-slate-300 bg-white text-sm">
              {OVERRIDE_CLASSES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500">Caption</label>
            <input value={caption} onChange={(e) => setCaption(e.target.value)}
              className="w-full h-8 px-2 rounded border border-slate-300 text-sm" />
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500">Event type</label>
            <select value={eventType} onChange={(e) => setEventType(e.target.value)}
              className="w-full h-8 px-2 rounded border border-slate-300 bg-white text-sm">
              {OVERRIDE_EVENT_TYPES.map((c) => (
                <option key={c} value={c}>{c.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500">Trade</label>
            <input value={trade} onChange={(e) => setTrade(e.target.value)}
              placeholder="plumber, electrician, drywall…"
              className="w-full h-8 px-2 rounded border border-slate-300 text-sm" />
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500">Why are you overriding? (5+ chars) *</label>
            <textarea value={reason} onChange={(e) => setReason(e.target.value)}
              rows={2}
              placeholder="e.g. AI labeled progress but this is rework"
              className="w-full px-2 py-1 rounded border border-slate-300 text-sm" />
          </div>
          <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
            <button onClick={() => onOpenChange(false)} disabled={busy}
              className="h-9 px-3 rounded border border-slate-300 bg-white text-sm hover:bg-slate-50">
              Cancel
            </button>
            <button data-testid="ai-override-submit" onClick={submit} disabled={busy}
              className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
              {busy ? "Saving…" : "Save override"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}


// ---- Reject dialog -------------------------------------------------

function AiRejectDialog({ open, onOpenChange, trayId, onSaved }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (busy) return;
    if (!reason || reason.trim().length < 10) {
      toast.error("Reject reason must be at least 10 characters"); return;
    }
    setBusy(true);
    try {
      await api.post(`/photo-tray/${trayId}/reject-ai`, { reason: reason.trim() });
      toast.success("AI suggestion rejected");
      onSaved?.();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Reject failed");
    } finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Reject AI suggestion</DialogTitle></DialogHeader>
        <div className="space-y-2 text-sm">
          <div className="text-xs text-slate-600">
            The photo stays in the tray; manual tagging is still available.
          </div>
          <div>
            <label className="text-[10px] uppercase text-slate-500">Why? (10+ chars) *</label>
            <textarea value={reason} onChange={(e) => setReason(e.target.value)}
              rows={3}
              placeholder="e.g. AI completely misread; this is a delivery photo not a QC issue"
              className="w-full px-2 py-1 rounded border border-slate-300 text-sm" />
          </div>
          <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
            <button onClick={() => onOpenChange(false)} disabled={busy}
              className="h-9 px-3 rounded border border-slate-300 bg-white text-sm hover:bg-slate-50">
              Cancel
            </button>
            <button data-testid="ai-reject-submit" onClick={submit} disabled={busy}
              className="h-9 px-3 rounded bg-rose-600 text-white text-sm hover:bg-rose-700 disabled:opacity-50">
              {busy ? "Saving…" : "Reject"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
