/**
 * Daily Reports — Phase 2 full rebuild.
 *
 * One page surfaces every signal a federal-construction Super needs to file:
 *   header · weather · crew presence · roster (from presence) · activity grid
 *   inline events (delays, safety, qc, rfis, deliveries) · photos · voice · renovation
 *   (only for renovation/tenant_improvement) · footer w/ submit + approve + audit log
 *
 * Copilot is given the active daily_report_id when this page is open, so it can
 * call DR-only tools: record_presence, mark_activity_status, create_unplanned_activity.
 */
import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { setActiveDailyReportId, subscribeDescriptionInsert } from "../lib/copilotContext";
import { localTodayISO, formatDateDisplay } from "../lib/dateUtil";
import { toast } from "../components/ui/sonner";
import { confirmAction } from "../lib/confirmAction";
import { deleteWithUndo } from "../lib/deleteWithUndo";
import BackButton from "../components/BackButton";
import SubmitProgress from "../components/SubmitProgress";
import { getUser } from "../lib/auth";
import DelayForm from "../components/forms/DelayForm";
import FieldLabel from "../components/forms/FieldLabel";
import CoordinationEventForm from "../components/forms/CoordinationEventForm";
// Addition 4 -- locked-DR edit modal (reusable across event types).
import LockedEventEditModal from "../components/forms/LockedEventEditModal";
// Bucket D Addition 5 (2026-05-12) -- SupplementWizard import removed.
// The component file (frontend/src/components/forms/SupplementWizard.jsx)
// is preserved for legacy reference but no longer imported anywhere on
// this page. New events on locked DRs use the post_submit_added flow
// via LockedAddReasonBlock + the standard create endpoints.
import DrActivityPicker from "../components/forms/DrActivityPicker";
// Bucket D Corrective (2026-05-08) -- master-schedule task picker.
// Used by RFI / QC / Safety / Delivery / Coord forms below in place of
// DrActivityPicker. DrActivityPicker stays imported because nothing else
// uses it on this page yet -- safe to remove once nothing references it.
import TaskPicker from "../components/forms/TaskPicker";
import { BlockerPopover } from "../components/BlockerPopover";
// CPM Part J-6 (2026-05-15) -- per-task RFI impact input.
import PerTaskImpactsInput from "../components/forms/PerTaskImpactsInput";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../components/ui/tabs";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from "../components/ui/dropdown-menu";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "../components/ui/select";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  Download, FilePlus2, RefreshCw, Cloud, Users, ClipboardList, Image as ImageIcon,
  Mic, AlertTriangle, ShieldAlert, ClipboardCheck, MessageSquare, Truck, Plus,
  Lock, CheckCircle2, ChevronLeft, ChevronRight, Trash2, Wand2, Pencil, Sparkles, Loader2,
  Upload, FileSpreadsheet, Pin, Copy as CopyIcon, Building2, X,
} from "lucide-react";
// Bucket F Phase 4 -- DR upload now opens the 3-step tag wizard after
// Bucket G Phase 3 -- Jigsaw pre-fill widget on the DR narrative.
import JigsawField from "../components/forms/JigsawField";
// 5-Route DR voice intake (Phase A 2026-05-25): renders the voice memo
// recorder and live-conversational launcher when the user picked one
// of the voice modes at New-DR time. Imported lazily-friendly default
// from /components/VoiceIntakePanel.jsx.
import VoiceIntakePanel from "../components/VoiceIntakePanel";
// v7 guided flow — EodSlider extracted VERBATIM to components/dr/EodSlider
// so the guided flow (GuidedDrFlow at the bottom of this file) reuses the
// exact B1-gated component. Desktop grid rendering is unchanged (same
// component, new home).
import EodSlider from "../components/dr/EodSlider";
// v7 guided flow — COMMON_TRADES moved to components/dr/drFlowShared.jsx
// (values unchanged; pt.47/pt.62 notes preserved there) so the flow's crew
// sheet and the Manpower Log share one list. LockedReasonCard +
// isFlowReasonValid drive the locked-DR (spec B) reason plumbing on the
// flow's activity screens.
import { COMMON_TRADES, LockedReasonCard, isFlowReasonValid } from "../components/dr/drFlowShared";
// v7 voice notes (spec D) — voice-note-as-record (kind="voice_note", NO
// transcription/extraction pipeline) + IndexedDB offline queue + playback.
import DrVoiceNotes, { listPendingNotes as listPendingVoiceNotes } from "../components/dr/DrVoiceNotes";

const RENOVATION_TYPES = ["renovation", "tenant_improvement"];

// Bucket D Addition 5 (2026-05-12) -- "add to locked DR" reason capture.
// Reused by every event panel + the activity grid when the user adds a
// new event/row AFTER the parent DR was submitted. The reason text is
// passed inline to the create() API call as `add_reason`; backend
// stamps post_submit_added=true + writes EVENT_ADDED_POST_SUBMIT audit
// when min-10-char threshold is met. Admin/PM bypass the reason check
// server-side but the FIELD STILL RENDERS AS REQUIRED CLIENT-SIDE for
// them so audit captures intent uniformly across roles (consistent UX,
// no role-conditional UI behavior). Submission is blocked client-side
// until the field meets the min-char threshold.
//
// Test 2D follow-up (2026-05-12): the prior implementation didn't have
// client-side enforcement -- empty submit succeeded for admin (correct
// per policy spec but inconsistent UX) and for non-admin if the form
// somehow reached the backend. The visual red-border + disabled-submit
// combo now makes the field unambiguously required at the UI layer.
const ADD_REASON_MIN = 10;
function isAddReasonValid(value) {
  return (value || "").trim().length >= ADD_REASON_MIN;
}
function LockedAddReasonBlock({ value, onChange, eventLabel = "this event", testid = "locked-add-reason" }) {
  const length = (value || "").trim().length;
  const ok = length >= ADD_REASON_MIN;
  return (
    <div className={`rounded p-2 text-xs space-y-1.5 ${
      ok
        ? "bg-amber-50 border border-amber-200 text-amber-900"
        : "bg-rose-50 border-2 border-rose-300 text-rose-900"
    }`} data-testid={`${testid}-block`}>
      <div className="flex items-start gap-1.5">
        <Lock className="h-3.5 w-3.5 mt-0.5 flex-shrink-0" />
        <div>
          <strong>Adding {eventLabel} to a locked daily report.</strong>{" "}
          <span className="font-semibold">Reason required (min {ADD_REASON_MIN} chars).</span>{" "}
          Captured in the audit chain.
        </div>
      </div>
      <textarea data-testid={testid} value={value || ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Why is this being added after the DR was submitted? (e.g. 'forgot to log discovery during demo')"
        aria-required="true"
        aria-invalid={!ok}
        className={`w-full p-2 rounded text-sm bg-white h-16 ${
          ok ? "border border-amber-300" : "border-2 border-rose-400 ring-1 ring-rose-200"
        }`} />
      <div className={`flex items-center justify-between text-xs ${
        ok ? "text-emerald-700" : "text-rose-700 font-semibold"
      }`}>
        <span>{ok ? "✓ ready" : "⚠ required field"}</span>
        <span>{length} / {ADD_REASON_MIN} min</span>
      </div>
    </div>
  );
}

// MOBILE add-form sheets (Sid-approved add-forms-as-dialogs, 2026-07-05).
// The crew / unplanned-activity add-forms and the issue quick-adds open as
// slide-up bottom sheets on phones. Rather than duplicate the existing form
// markup + handlers, the EXISTING inline containers are LIFTED into a fixed
// bottom sheet on mobile via CSS (.dr-crew-entry--open / the active events
// TabsContent under [data-issue-sheet="open"]) — see index.css. The sheet
// chrome (grip / title / close / Cancel-Add footer) uses the .dr-msheet-*
// classes and is gated .md:hidden so desktop keeps its inline forms
// byte-identical. No save logic is duplicated; the same state + endpoints
// drive both surfaces.

// Helper for create() handlers: inspects an API error response and, if
// the backend returned 403 with edit_request_required, points the user
// at the edit-request flow. Returns true when handled (caller skips
// generic toast); false otherwise.
function handleLockedAddError(e, _eventType) {
  const status = e.response?.status;
  const detail = e.response?.data?.detail;
  if (
    status === 403 && detail && typeof detail === "object"
    && detail.error === "edit_request_required"
  ) {
    toast.error(
      "Self-edit window (24h) has closed. File an edit request from the "
      + "edit pencil on any existing row -- approval opens a 1-hour window "
      + "for both edits AND adds.",
    );
    return true;
  }
  return false;
}

// pt.65: PHOTO_CLASSES / PHOTO_CLASS_BY_VALUE removed — they were only used
// by the photo-class badge that pt.57 dropped from AuthPhoto (dead code).

// ------------------------------------------------------------
//  Top-level list — pick a date (any past/future), open or create
// ------------------------------------------------------------
// 5-Route DR Entry — three user-facing intake modes selected when
// opening a new daily report. Standard, short names per Abhi:
//   - manual      → fill the form by hand (default; always available)
//   - voice_memo  → record a voice recap; AI transcribes + populates DR
//   - live_voice  → Gemini Live conversational walkthrough
//
// Offline rules:
//   - manual stays available (autosaves draft to localStorage)
//   - voice_memo stays available (recording queues to IndexedDB, syncs
//     when connection returns)
//   - live_voice REQUIRES connection (Gemini Live WebSocket) → grayed
//     out + tooltip when offline OR when GEMINI_API_KEY isn't configured.
const DR_INTAKE_MODES = [
  { value: "manual",     label: "Manual",     hint: "Type values into the form",                 requiresOnline: false, requiresGemini: false },
  { value: "voice_memo", label: "Voice Memo", hint: "Record voice recap, AI fills DR",          requiresOnline: false, requiresGemini: true  },
  { value: "live_voice", label: "Live Voice", hint: "AI guides you with questions in real time", requiresOnline: true,  requiresGemini: true  },
];

export default function DailyReports() {
  const { activeId } = useProjects();
  const [reports, setReports] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [openMode, setOpenMode] = useState("manual");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerDate, setPickerDate] = useState(localTodayISO());
  const [pickerMode, setPickerMode] = useState("manual");
  const [voiceConfig, setVoiceConfig] = useState({ gemini_configured: false, voice_memo_enabled: false, live_voice_enabled: false });
  const [online, setOnline] = useState(typeof navigator !== "undefined" ? navigator.onLine : true);
  useEffect(() => {
    const setOn = () => setOnline(true);
    const setOff = () => setOnline(false);
    window.addEventListener("online", setOn);
    window.addEventListener("offline", setOff);
    return () => {
      window.removeEventListener("online", setOn);
      window.removeEventListener("offline", setOff);
    };
  }, []);
  useEffect(() => {
    api.get("/daily-reports/voice/config")
      .then(({ data }) => setVoiceConfig(data || {}))
      .catch(() => setVoiceConfig({ gemini_configured: false, voice_memo_enabled: false, live_voice_enabled: false }));
  }, []);
  // Feature Client DR Template Export (2026-05-20) -- the role gate
  // here is for the "Upload DR Template" button in the list header.
  // The per-DR "Client Format" export now lives on the DR detail view
  // (next to the existing Internal / External export buttons), so the
  // list page no longer needs templateMeta or per-row export state.
  const navigate = useNavigate();
  const userRole = (getUser()?.role || "").toLowerCase();
  const isAdminOrPm = userRole === "admin" || userRole === "pm";

  // Bucket D Phase 3 -- gallery chips + date links route here with
  // ?openId=<dr-id>; honor it on first mount so the DR opens directly.
  // Bucket D Fix F -- ALSO honor ?openEvent + ?openEventType so the
  // gallery click-through scrolls to and pulse-highlights the specific
  // event row. Stripped from the URL after first mount so refresh
  // doesn't re-trigger.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const target = params.get("openId");
    if (target) setOpenId(target);
  }, []);

  // pt.39 (2026-05-27): write openId back to the URL so refreshing
  // while inside a DR detail keeps that DR open (the bug being fixed
  // here was: refresh /daily-reports?openId=abc would re-render the
  // list because openId state reset to null on the second render, and
  // nothing pushed the param back). Uses replaceState so we don't
  // bloat browser history with every row click; means browser back
  // skips through the SPA-internal nav and returns the user to
  // wherever they came from before this page -- predictable enough,
  // matches how Linear / Notion handle detail-via-search-param.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const have = params.get("openId");
    if (openId === have) return;
    if (openId) params.set("openId", openId);
    else params.delete("openId");
    const qs = params.toString();
    const newUrl = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    window.history.replaceState(null, "", newUrl);
  }, [openId]);

  const load = useCallback(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/daily-reports`).then(({ data }) => setReports(data));
  }, [activeId]);
  useEffect(() => { load(); }, [load]);

  const handleUploadTemplate = () => {
    if (!activeId) return toast.error("Pick a project first");
    if (!isAdminOrPm) {
      return toast.error(
        "Only project admins can upload a client template. Contact your project admin.",
      );
    }
    navigate(`/projects/${activeId}/dr-template`);
  };

  // Tell the copilot which DR we're scoped to.
  useEffect(() => {
    setActiveDailyReportId(openId);
    return () => setActiveDailyReportId(null);
  }, [openId]);

  // v7 post-submit landing (spec E) — when the guided flow submits and
  // navigates back it stamps sessionStorage; read + clear it here and pulse
  // that report's mobile card so the new status badge is unmissable.
  const [hotId, setHotId] = useState(null);
  useEffect(() => {
    if (openId) return undefined;
    let t;
    try {
      const v = sessionStorage.getItem("ice-dr-just-submitted");
      if (v) {
        sessionStorage.removeItem("ice-dr-just-submitted");
        setHotId(v);
        t = setTimeout(() => setHotId(null), 4000);
      }
    } catch { /* ignore */ }
    return () => clearTimeout(t);
  }, [openId]);

  // Item 8 — date-picker driven create. Backend POST is idempotent on
  // (project_id, report_date) so picking a date that already exists
  // just returns that DR; no duplicates.
  const createForDate = async () => {
    if (!pickerDate) return toast.error("Pick a date first");
    try {
      const { data } = await api.post("/daily-reports", {
        project_id: activeId, report_date: pickerDate,
      });
      const existed = reports.some((r) => r.report_date === pickerDate);
      // CPM Part G (2026-05-15) -- format report_date MM/DD/YYYY in
      // toast text. The DB-level comparison above stays ISO.
      toast.success(existed ? `Opened existing report for ${formatDateDisplay(data.report_date)}`
                            : `Daily report opened for ${formatDateDisplay(data.report_date)}`);
      setPickerOpen(false);
      setOpenMode(pickerMode || "manual");
      setOpenId(data.id);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const removeReport = async (r) => {
    if (r.status !== "draft") return;
    // pt.37: themed modal (was window.confirm). Medium-stakes since
    // it touches child records' daily_report_id.
    const ok = await confirmAction({
      title: `Delete draft daily report for ${formatDateDisplay(r.report_date)}?`,
      message: "Linked delays / QC / safety / RFIs / photos keep their records, but their daily_report_id will be cleared.",
      confirmText: "Delete draft",
      icon: "trash",
    });
    if (!ok) return;
    try {
      await api.delete(`/daily-reports/${r.id}`);
      toast.success(`Deleted daily report for ${formatDateDisplay(r.report_date)}`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  // Post-submit landing (spec E) — Export the report PDF straight from
  // the list card (mobile). Reuses the same /pdf-rich endpoint the DR
  // detail uses; only offered once a report is submitted/approved.
  const exportReportPdf = async (r) => {
    try {
      const res = await api.get(`/daily-reports/${r.id}/pdf-rich`, {
        params: { mode: "internal" }, responseType: "blob",
      });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = `daily_report_${r.report_date}_internal.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Export failed");
    }
  };

  if (openId) {
    // onBack also refreshes the list so a just-submitted report shows its
    // new status badge + Export on return (spec E / post-submit landing).
    return <DailyReportDetail id={openId} intakeMode={openMode}
      onBack={() => { setOpenId(null); setOpenMode("manual"); load(); }} />;
  }

  return (
    <div data-testid="daily-reports-page" className="space-y-4">
      <div className="flex items-center justify-between max-md:flex-col max-md:items-start max-md:gap-2">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Daily reports</h1>
        </div>
        <div className="flex items-center gap-2 max-md:w-full">
          {/* Feature Client DR Template Export (2026-05-20) -- visible
              to every user (so they SEE the feature exists), but the
              navigate only happens for admin / PM; other roles get a
              toast pointing them at their project admin. */}
          {/* pt.40 button-system sweep: page-header tier means sbtn-md
              (32px) since these sit on the DR-list page header. */}
          <button data-testid="dr-upload-template-button"
            onClick={handleUploadTemplate}
            title={isAdminOrPm
              ? "Upload your client's xlsx daily report template"
              : "Only project admins can upload a client template"}
            className="sbtn sbtn-md sbtn-secondary">
            <Upload /> Upload DR Template
          </button>
          <button data-testid="dr-new-button"
            onClick={() => { setPickerDate(localTodayISO()); setPickerOpen(true); }}
            className="sbtn sbtn-md sbtn-primary">
            New daily report
          </button>
        </div>
      </div>

      {pickerOpen && (
        <div data-testid="dr-new-dialog"
          className="surface p-4 border-l-4 border-blue-500 bg-blue-50/30">
          <div className="font-medium text-sm mb-2">Open or create a daily report</div>
          <div className="text-xs text-slate-600 mb-3">
            If a daily report already exists for the selected date, it will be opened. Otherwise a new draft will be created.
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-xs">
              <div className="text-slate-500">Date</div>
              <input data-testid="dr-new-date" type="date" value={pickerDate}
                onChange={(e) => setPickerDate(e.target.value)}
                className="mt-1 h-9 px-2 dr-mp-input rounded text-sm" />
            </label>
            {/* pt.43 (2026-05-27): removed the "Intake mode" dropdown
                from the DR open/create picker. Manual is the default
                entry path and is ALWAYS available; the user can
                switch to voice memo from inside the DR via the
                "Add via voice" button. Live Voice (Gemini) is
                planned for v2 -- the backend stays wired up but the
                option is hidden from the frontend for v1. */}
            <button data-testid="dr-new-confirm" onClick={createForDate}
              className="sbtn sbtn-md sbtn-primary">
              Open
            </button>
            <button data-testid="dr-new-cancel" onClick={() => setPickerOpen(false)}
              className="sbtn sbtn-md sbtn-secondary">
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Desktop table (unchanged) — hidden on phones. */}
      <div className="bg-card border border-border rounded-lg overflow-hidden max-md:hidden">
        <table className="w-full dense-table">
          <thead><tr><th>Date</th><th>Status</th><th className="max-md:hidden">Submitted</th><th className="w-32"></th></tr></thead>
          <tbody>
            {reports.map((r) => {
              const canDelete = r.status === "draft";
              return (
                <tr key={r.id} data-testid={`dr-row-${r.id}`}>
                  <td className="font-mono">{formatDateDisplay(r.report_date)}</td>
                  <td><StatusPill status={r.status} /></td>
                  <td className="text-xs max-md:hidden">{r.submitted_at ? new Date(r.submitted_at).toLocaleString() : "—"}</td>
                  <td className="text-right">
                    <button data-testid={`open-dr-${r.id}`} onClick={() => setOpenId(r.id)} className="text-sm text-primary hover:underline mr-3">
                      Open
                    </button>
                    <button
                      data-testid={`delete-dr-${r.id}`}
                      onClick={() => removeReport(r)}
                      disabled={!canDelete}
                      title={canDelete ? "Delete draft" : "Submitted/approved reports cannot be deleted"}
                      className={`h-7 w-7 max-md:min-h-[44px] max-md:min-w-[44px] grid place-items-center rounded inline-flex align-middle ${canDelete ? "text-red-500 hover:bg-red-50" : "text-slate-300 cursor-not-allowed"}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              );
            })}
            {!reports.length && <tr><td colSpan="4" className="text-center py-6 text-sm text-muted-foreground">No reports yet — click "New daily report" to start one for any date.</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Mobile card list (spec E) — status badge is prominent and
          submitted/approved reports carry an Export control on the card.
          md:hidden so desktop is untouched. */}
      <div className="md:hidden flex flex-col gap-2" data-testid="dr-list-mobile">
        {reports.map((r) => {
          const canDelete = r.status === "draft";
          const isLocked = r.status === "submitted" || r.status === "approved";
          return (
            <div key={r.id} data-testid={`dr-card-${r.id}`}
              className={`rounded-xl border p-3${hotId === r.id ? " dr-flow-card-hot" : ""}`}
              style={{ borderColor: "var(--v4-border)", background: "var(--v4-bg-surface)" }}>
              <button data-testid={`open-dr-m-${r.id}`} onClick={() => setOpenId(r.id)}
                className="w-full flex items-center gap-2 text-left">
                <span className="font-mono text-[15px] font-semibold" style={{ color: "var(--v4-fg-primary)" }}>
                  {formatDateDisplay(r.report_date)}
                </span>
                <span className={`v4-pill v4-pill-${r.status === "approved" ? "closed" : r.status === "submitted" ? "submitted" : "draft"} ml-auto`}>
                  {r.status === "approved" ? "Approved" : r.status === "submitted" ? "Submitted" : "Draft"}
                </span>
                <ChevronRight className="h-4 w-4 shrink-0" style={{ color: "var(--v4-fg-muted)" }} />
              </button>
              <div className="mt-2 flex items-center gap-2">
                {r.submitted_at && (
                  <span className="text-xs" style={{ color: "var(--v4-fg-muted)" }}>
                    Submitted {new Date(r.submitted_at).toLocaleDateString()}
                  </span>
                )}
                <div className="ml-auto flex items-center gap-2">
                  {isLocked && (
                    <button data-testid={`export-dr-m-${r.id}`} onClick={() => exportReportPdf(r)}
                      className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg text-sm font-medium"
                      style={{ background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-primary)", border: "1px solid var(--v4-border)" }}>
                      <Download className="h-3.5 w-3.5" /> Export
                    </button>
                  )}
                  <button data-testid={`delete-dr-m-${r.id}`} onClick={() => removeReport(r)} disabled={!canDelete}
                    title={canDelete ? "Delete draft" : "Submitted/approved reports cannot be deleted"}
                    className={`h-9 w-9 grid place-items-center rounded-lg ${canDelete ? "text-red-500" : "text-slate-300 cursor-not-allowed"}`}
                    style={{ border: "1px solid var(--v4-border)" }}>
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
          );
        })}
        {!reports.length && (
          <div className="text-center py-6 text-sm text-muted-foreground">
            No reports yet — tap "New daily report" to start one.
          </div>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------
//  v7 Guided mobile flow — reactive viewport hook.
//  Returns true on phones (<=767.98px). The DR detail renders the
//  guided walkthrough (GuidedDrFlow) when this is true, and the
//  untouched desktop layout otherwise. Kept as a top-level hook so
//  both the detail wrapper and the flow share one source of truth.
// ------------------------------------------------------------
function useIsPhone() {
  const query = "(max-width: 767.98px)";
  const [isPhone, setIsPhone] = useState(
    () => typeof window !== "undefined" && typeof window.matchMedia === "function"
      && window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const onChange = () => setIsPhone(mql.matches);
    onChange();
    // addEventListener is the modern API; addListener is the Safari<14 fallback.
    if (mql.addEventListener) mql.addEventListener("change", onChange);
    else mql.addListener(onChange);
    return () => {
      if (mql.removeEventListener) mql.removeEventListener("change", onChange);
      else mql.removeListener(onChange);
    };
  }, []);
  return isPhone;
}

// ------------------------------------------------------------
//  Detail page (the heart of Phase 2)
// ------------------------------------------------------------
function DailyReportDetail({ id, intakeMode = "manual", onBack }) {
  const [bundle, setBundle] = useState(null);
  const [busy, setBusy] = useState(false);
  // v7 guided flow — phones get the activity-by-activity walkthrough
  // (GuidedDrFlow) instead of the desktop panel stack. Desktop (>=768px)
  // is byte-identical to before this feature.
  const isPhone = useIsPhone();
  // 5-Route DR (2026-05-25) — allow switching to voice mode AFTER the
  // DR is already open. Initial value comes from the New-DR picker;
  // user can flip it from inside the detail page via the header
  // "Voice intake" toggle.
  const [activeIntakeMode, setActiveIntakeMode] = useState(intakeMode);
  // Feature Client DR Template Export (2026-05-20) -- the third export
  // button (next to Internal / External) is gated on the project
  // having a client xlsx template uploaded. We fetch the metadata
  // once the bundle resolves and only render the button when present.
  const [templateMeta, setTemplateMeta] = useState(null);
  const [exportingClient, setExportingClient] = useState(false);
  // Bucket C3a Phase 3 — per-row submit errors keyed by row id. Cleared
  // on next submit attempt or when the user successfully edits the row.
  const [submitRowErrors, setSubmitRowErrors] = useState({});
  // Bucket C3b Phase 1 — quick-log coordination event dialog.
  const [coordOpen, setCoordOpen] = useState(false);
  // Bucket D Addition 5 (2026-05-12) -- locked-DR add reason for the
  // quick-log coordination dialog (lives at DR-detail scope so the
  // header button can open it on locked DRs too).
  const [coordAddReason, setCoordAddReason] = useState("");
  // Addition 3 -- no-work-day modal state. Opened when the server rejects
  // a submit with error="empty_dr_requires_no_work_day". On confirm we
  // resubmit with is_no_work_day=true + the reason/note.
  const [noWorkModal, setNoWorkModal] = useState(null); // null | {reason, note}

  // 5-Route DR Entry (2026-05-25) — online/offline status + pending
  // offline-queue size for the banner shown at the top of the detail.
  const [online, setOnline] = useState(typeof navigator !== "undefined" ? navigator.onLine : true);
  const [queueSize, setQueueSize] = useState(0);
  useEffect(() => {
    const refresh = () => {
      try {
        // Lazy require to avoid a circular dep at module init.
        // eslint-disable-next-line global-require
        const { getQueueSize } = require("../lib/drOfflineSync");
        setQueueSize(getQueueSize());
      } catch { /* ignore */ }
    };
    refresh();
    const setOn = () => { setOnline(true); refresh(); };
    const setOff = () => { setOnline(false); refresh(); };
    window.addEventListener("online", setOn);
    window.addEventListener("offline", setOff);
    const t = setInterval(refresh, 5000);
    return () => {
      window.removeEventListener("online", setOn);
      window.removeEventListener("offline", setOff);
      clearInterval(t);
    };
  }, []);

  const [reminders, setReminders] = useState([]);
  const reload = useCallback(async () => {
    // 2026-05-19 -- /available-tasks call removed along with the
    // planned-activity picker. The grid renders straight from the
    // master schedule via /context.
    const [{ data: ctx }, remindersRes] = await Promise.all([
      api.get(`/daily-reports/${id}/context`),
      api.get(`/daily-reports/${id}/delay-reminders`).catch(() => ({ data: [] })),
    ]);
    setBundle(ctx);
    setReminders(Array.isArray(remindersRes.data) ? remindersRes.data : []);
  }, [id]);
  useEffect(() => { reload(); }, [reload]);

  // 2026-02-XX (Abhi) -- weather auto-fill on first open. Fires once
  // per DR-detail mount IF every weather field is empty AND the
  // backend has never recorded a fetched_at. The ref guard prevents
  // re-firing if the bundle reloads (e.g. after another patch lands)
  // and the fetched_at gate prevents overwriting a super's manual
  // edits when they reopen a draft they already populated by hand.
  const weatherAutofillTriedRef = useRef(false);
  useEffect(() => {
    if (!bundle || weatherAutofillTriedRef.current) return;
    const r = bundle.report;
    const locked = r.status === "submitted" || r.status === "approved";
    if (locked) { weatherAutofillTriedRef.current = true; return; }
    const isBlank = !r.weather_fetched_at
      && !r.weather_conditions
      && r.weather_temp_high_f == null
      && r.weather_temp_low_f == null
      && r.weather_precipitation_in == null
      && r.weather_wind_mph == null;
    weatherAutofillTriedRef.current = true;
    if (!isBlank) return;
    api.post(`/daily-reports/${id}/weather-autofill`)
      .then(() => reload())
      .catch((e) => {
        // 2026-02-XX -- surface a toast when autofill fails so the
        // super knows why the weather card stayed blank (most common:
        // project missing city/state, so geocode returned 422).
        const msg = e?.response?.data?.detail
          || "Weather autofill failed — fill in the project city/state on the Project page, or type values manually.";
        toast.error(`Weather: ${msg}`);
      });
  }, [bundle, id, reload]);

  // Bucket D Fix F -- gallery click-through highlight. After the bundle
  // renders, find the matching event row by data-testid, scroll into
  // view, add the saffron pulse class. Strip the URL params so refresh
  // doesn't re-trigger.
  // Bucket D Fix G -- ALSO honor ?createEvent=<panel-id> by scrolling
  // the named panel into view + pulsing it so the user can immediately
  // hit the panel's "+ Add" button. Panel ids: dr-safety-panel,
  // dr-qc-panel, dr-rfis-panel, dr-deliveries-panel, dr-photos-panel,
  // dr-coordination (handled via quick-log dialog instead, see below).
  useEffect(() => {
    if (!bundle) return;
    const params = new URLSearchParams(window.location.search);
    const eventId = params.get("openEvent");
    const eventType = params.get("openEventType");
    const createEvent = params.get("createEvent");
    if (!eventId && !createEvent) return;
    const t = setTimeout(() => {
      let el = null;
      if (eventId && eventType) {
        const candidates = [
          `[data-testid="${eventType}-row-${eventId}"]`,
          `[data-event-row-type="${eventType}"][data-event-row-id="${eventId}"]`,
        ];
        for (const sel of candidates) {
          el = document.querySelector(sel);
          if (el) break;
        }
      } else if (createEvent) {
        // Coordination uses the quick-log dialog rather than an inline panel.
        if (createEvent === "coordination") {
          setCoordOpen(true);
          return;
        }
        const panelMap = {
          delay: "dr-reminder-banner",
          safety: "dr-safety-panel",
          qc: "dr-qc-panel",
          rfi: "dr-rfis-panel",
          delivery: "dr-deliveries-panel",
          photo: "dr-photos-panel",
        };
        const panelTestId = panelMap[createEvent];
        if (panelTestId) {
          el = document.querySelector(`[data-testid="${panelTestId}"]`);
        }
      }
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("dr-event-highlight");
        setTimeout(() => el.classList.remove("dr-event-highlight"), 2200);
      }
    }, 250);
    // Strip the navigation params so refresh doesn't re-trigger.
    const url = new URL(window.location.href);
    url.searchParams.delete("openEvent");
    url.searchParams.delete("openEventType");
    url.searchParams.delete("createEvent");
    window.history.replaceState({}, "", url.toString());
    return () => clearTimeout(t);
  }, [bundle]);

  // Feature F4 (2026-05-18) -- real-time grid refresh. Poll the grid
  // endpoint every 15s so the F3 blocker badge + F4 auto-status reflect
  // blockers opening / resolving on linked tasks without a manual
  // reload. Lightweight: hits only /grid (not the full /context bundle)
  // and merges the result into bundle.grid. Uncontrolled inputs keep
  // any in-progress edit across the merge (defaultValue is not
  // reapplied to a mounted input). The poll is a no-op until the
  // initial /context load populates bundle.
  useEffect(() => {
    const t = setInterval(() => {
      api.get(`/daily-reports/${id}/grid`)
        .then(({ data }) => {
          if (Array.isArray(data)) {
            setBundle((b) => (b ? { ...b, grid: data } : b));
          }
        })
        .catch(() => { /* poll failure is non-fatal */ });
    }, 15000);
    return () => clearInterval(t);
  }, [id]);

  // Feature Client DR Template Export (2026-05-20) -- fetch the
  // project's template metadata once the bundle loads so the Header
  // can decide whether to render the third export button. A 404 /
  // failure falls back to present=false, which simply hides the
  // button (no error toast on the detail view).
  const projectIdForTemplate = bundle?.project?.id;
  useEffect(() => {
    if (!projectIdForTemplate) { setTemplateMeta(null); return; }
    api.get(`/projects/${projectIdForTemplate}/dr-template`)
      .then(({ data }) => setTemplateMeta(data))
      .catch(() => setTemplateMeta({ present: false }));
  }, [projectIdForTemplate]);

  if (!bundle) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;

  const { report, project, grid, presence, photos, voice_annotations, delays,
          safety_events, qc_issues, rfis, deliveries, audit_log, user_labels } = bundle;
  // Feature DR Header Auto-Population (2026-05-20) -- signature is the
  // current logged-in user's full name per spec ("the logged-in user
  // filling the DR"); the other 5 header fields come from the project
  // record. All read-only, all auto-populated.
  const currentUserName = getUser()?.full_name || "";
  const locked = report.status === "submitted" || report.status === "approved";
  const isRenovation = RENOVATION_TYPES.includes(project.project_type || "");

  const totalHeadcount = presence.reduce((s, p) => s + (p.headcount || 0), 0);
  const totalCrewHours = presence.reduce((s, p) => s + (p.headcount || 0) * (p.hours_per_person || 0), 0);
  const completed = grid.filter((r) => (r.pct_complete_end_of_day || 0) >= 100).length;
  const ppc = grid.length ? Math.round((completed / grid.length) * 100) : 0;

  // Bucket D Addition 5 / Test 2G follow-up (2026-05-12) -- validate()
  // relaxed. Previously this function HARD-BLOCKED submit on three
  // conditions: empty weather, empty crew presence, empty grid. That
  // prevented legitimate no-work days from reaching the backend's
  // no-work-day modal flow (which is the spec'd UX for empty DRs).
  //
  // New rule: only block on LOGICAL CONTRADICTIONS that can't be a
  // no-work day. Empty DRs flow through to the server, which returns
  // 422 with error="empty_dr_requires_no_work_day" so the client
  // opens the no-work-day modal (see _submitWith's 422 handler).
  //
  // Hard-block cases:
  //   - Crew presence empty BUT grid has recorded progress
  //     (someone progressed activities with nobody present -- has to
  //      be one or the other)
  //   - Grid rows exist AND every row is untouched (zero progress,
  //     zero status change) -- that's a "wasted submit"
  //     not a no-work day; user should mark the rows or remove them
  //
  // Soft (non-blocking) hints would go in a future "submit checklist"
  // sidebar -- for now we trust the no-work-day modal flow to handle
  // the empty cases.
  const validate = () => {
    const errors = [];
    const hasProgress = grid.some((r) =>
      (r.pct_complete_end_of_day ?? 0) > (r.pct_complete_start_of_day ?? 0)
      || (r.status_at_end_of_day && r.status_at_end_of_day !== "not_started")
    );
    if (presence.length === 0 && hasProgress) {
      errors.push(
        "Crew presence is empty but activities show recorded progress. "
        + "Either add a trade row to the presence panel, or unmark the activity "
        + "progress (zero progress + no crew = no-work day, which you can submit "
        + "via the no-work-day modal).",
      );
    }
    // The "every row untouched" case: only a hard-block when grid IS
    // non-empty. Empty grid is a valid no-work day candidate.
    const untouched = grid.filter((r) =>
      (r.pct_complete_end_of_day ?? null) === r.pct_complete_start_of_day
      && (!r.status_at_end_of_day || r.status_at_end_of_day === "not_started")
    );
    if (grid.length > 0 && untouched.length === grid.length) {
      errors.push(
        "Activity grid has rows but none show progress. Either record EOD% "
        + "or status on at least one row, or remove the rows and submit "
        + "as a no-work day.",
      );
    }
    return { ok: errors.length === 0, errors };
  };

  // Addition 3 -- shared submit core; payload defaults to {} (normal
  // submit) but the no-work-day modal calls it with is_no_work_day +
  // reason + note populated. Returns true on success.
  const _submitWith = async (payload) => {
    setBusy(true);
    setSubmitRowErrors({});
    try {
      await api.post(`/daily-reports/${id}/submit`, payload);
      toast.success(payload.is_no_work_day
        ? "Daily report submitted as a no-work day."
        : "Daily report submitted and locked.");
      setNoWorkModal(null);
      reload();
      return true;
    } catch (e) {
      const detail = e.response?.data?.detail;
      // Addition 3 + Test 2G follow-up -- empty DR opens the no-work-
      // day modal. We accept both the legacy `error` key and the new
      // `error_code` key (Test 2G renamed it for clarity); either
      // triggers the modal.
      if (
        e.response?.status === 422
        && detail && typeof detail === "object"
        && (detail.error === "empty_dr_requires_no_work_day"
            || detail.error_code === "empty_dr_requires_no_work_day_confirmation")
      ) {
        setNoWorkModal({ reason: "", note: "" });
        toast.info(
          "This DR has no activities or events. Pick a no-work-day reason to submit.",
        );
        return false;
      }
      // Bucket C3a Phase 3 — structured per-row errors render inline.
      if (
        e.response?.status === 422
        && detail && typeof detail === "object"
        && Array.isArray(detail.row_errors)
      ) {
        const byRow = {};
        for (const err of detail.row_errors) {
          if (!byRow[err.row_id]) byRow[err.row_id] = [];
          byRow[err.row_id].push(err);
        }
        setSubmitRowErrors(byRow);
        toast.error(detail.message || "Submit blocked — fix the rows highlighted in red.");
      } else {
        toast.error(detail?.message || detail || e.message || "Submit failed");
      }
      return false;
    } finally { setBusy(false); }
  };

  // v7 guided flow passes onSuccess so it can navigate back to the DR
  // list on a successful submit (spec E). Desktop calls submit() with no
  // args, so onSuccess is undefined there and behavior is unchanged.
  const submit = async (onSuccess) => {
    // Test 2G follow-up (2026-05-12) -- validate() now only flags
    // logical contradictions (crew-empty-with-progress, all-rows-
    // untouched). Empty DRs intentionally pass client validation and
    // get routed to the no-work-day modal via the server's 422
    // response handler in _submitWith (see "empty_dr_requires_no_
    // work_day" / "empty_dr_requires_no_work_day_confirmation").
    const { ok, errors } = validate();
    if (!ok) {
      toast.error("Cannot submit: " + errors[0]);
      return;
    }
    const okSubmit = await _submitWith({});
    if (okSubmit && typeof onSuccess === "function") onSuccess();
  };

  // Addition 3 -- modal confirm: resubmit with is_no_work_day=true.
  const submitNoWorkDay = async (onSuccess) => {
    if (!noWorkModal) return;
    if (!noWorkModal.reason) {
      toast.error("Pick a reason.");
      return;
    }
    if (noWorkModal.reason === "other" && !(noWorkModal.note || "").trim()) {
      toast.error("Note is required when reason is 'other'.");
      return;
    }
    const okSubmit = await _submitWith({
      is_no_work_day: true,
      no_work_reason: noWorkModal.reason,
      no_work_note: (noWorkModal.note || "").trim() || null,
    });
    if (okSubmit && typeof onSuccess === "function") onSuccess();
  };

  const approve = async () => {
    setBusy(true);
    try {
      await api.post(`/daily-reports/${id}/approve`);
      toast.success("Daily report approved.");
      reload();
      return true;
    } catch (e) {
      toast.error(e.response?.data?.detail || "Approve failed");
      return false;
    } finally { setBusy(false); }
  };

  // Phase F — PDF export now takes a mode: "internal" (full) or "external"
  // (omits QC, Safety, and the rolled-forward delay reminders banner).
  const downloadPdf = async (mode = "internal") => {
    const res = await api.get(`/daily-reports/${id}/pdf-rich`, {
      params: { mode },
      responseType: "blob",
    });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = `daily_report_${report.report_date}_${mode}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Feature Client DR Template Export (2026-05-20) -- third export
  // option, adjacent to Internal / External above. The button only
  // renders when templateMeta.present is true (see Header), but we
  // still guard here in case the metadata raced with the click.
  const downloadClientTemplate = async () => {
    if (!templateMeta?.present) {
      toast.error("No client template uploaded for this project.");
      return;
    }
    if (exportingClient) return;
    setExportingClient(true);
    try {
      const res = await api.get(
        `/daily-reports/${id}/export/client-template`,
        { responseType: "blob" },
      );
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = `DR_${report.report_date}_client_format.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      // Server returned an error blob -- decode as text for the toast.
      let detail = "Client-format export failed";
      if (e.response?.data instanceof Blob) {
        try {
          const txt = await e.response.data.text();
          const parsed = JSON.parse(txt);
          detail = parsed.detail || detail;
        } catch { /* leave generic message */ }
      } else {
        detail = e.response?.data?.detail || detail;
      }
      toast.error(detail);
    } finally {
      setExportingClient(false);
    }
  };

  // V4 (2026-05-26): compute per-section "phase" status for the
  // progress bar + jump pips. Each pip = one DR section. Status
  // drives color: complete (emerald), in-progress (emerald hollow),
  // blocked (red), empty (gray). Clicking a pip scrolls the page
  // to the matching section anchor.
  // Sequence (Sid-approved): Activity BEFORE Manpower on BOTH desktop and
  // mobile. This array order drives the spine step order, the desktop pips
  // (numbered 01..07), and the mobile section chips, so putting activity at
  // index 2 / manpower at index 3 reorders every one of them consistently.
  const activityStatus = (() => {
    const rows = grid || [];
    if (rows.length === 0) return "empty";
    const fieldRows = rows.filter((r) => gridSectionOf(r.work_category) === 2);
    if (fieldRows.length === 0) return "empty";
    const withProgress = fieldRows.filter((r) => (r.pct_complete_end_of_day || 0) > 0 || (r.auto_status || r.status_at_end_of_day) === "blocked").length;
    if (withProgress === 0) return "blocked";
    if (withProgress < fieldRows.length) return "in-progress";
    return "complete";
  })();
  const manpowerStatus = (presence || []).length > 0 ? "complete" : "empty";
  const issuesCount = (delays || []).length + (safety_events || []).length + (qc_issues || []).length + (rfis || []).length;
  const drSections = [
    { id: "project",  label: "Project",  status: project ? "complete" : "empty" },
    { id: "weather",  label: "Weather",  status: report?.weather_conditions ? "complete" : "empty" },
    { id: "activity", label: "Activity", status: activityStatus },
    { id: "manpower", label: "Manpower", status: manpowerStatus },
    { id: "events",   label: "Events",   status: issuesCount > 0 ? "in-progress" : "empty" },
    { id: "photos",   label: "Photos",   status: (report?.photo_count ?? 0) > 0 ? "complete" : "empty" },
    { id: "audit",    label: "Audit",    status: "empty" },
  ];
  const completeCount = drSections.filter((s) => s.status === "complete").length;
  const totalSections = drSections.length;
  const progressPct = Math.round((completeCount / totalSections) * 100);

  // MOBILE checklist model (Sid-approved): Project / Weather / Activities /
  // Manpower are REQUIRED; Issues + Photos are OPTIONAL and do NOT count
  // toward the required-completion total. Audit is dropped from the mobile
  // checklist entirely (not part of the super's job). Issues flips to
  // "complete" as soon as >=1 issue is logged so it stops reading as pending.
  const mobileChecklist = [
    { id: "project",  label: "Project",    status: project ? "complete" : "empty",              required: true },
    { id: "weather",  label: "Weather",    status: report?.weather_conditions ? "complete" : "empty", required: true },
    { id: "activity", label: "Activities", status: activityStatus,                              required: true },
    { id: "manpower", label: "Manpower",   status: manpowerStatus,                              required: true },
    { id: "events",   label: "Issues",     status: issuesCount > 0 ? "complete" : "empty",       required: false },
    { id: "photos",   label: "Photos",     status: (report?.photo_count ?? 0) > 0 ? "complete" : "empty", required: false },
  ];
  const requiredSections = mobileChecklist.filter((s) => s.required);
  const requiredDone = requiredSections.filter((s) => s.status === "complete").length;
  const requiredTotal = requiredSections.length;
  const mobileProgressPct = requiredTotal > 0 ? Math.round((requiredDone / requiredTotal) * 100) : 0;

  // V4: tiny date-stepper navigation — flip yesterday/tomorrow without
  // returning to the list view. Stops at boundaries the API will allow.
  const dateStep = (delta) => {
    const d = new Date(report.report_date);
    d.setDate(d.getDate() + delta);
    // Caller: parent passes in onBack which knows how to switch DRs.
    // For now we fire a window event the list page listens to.
    window.dispatchEvent(new CustomEvent("dr-jump-to-date", { detail: { iso: d.toISOString().slice(0, 10) } }));
  };

  // ---- v7 GUIDED FLOW (phones only) ----------------------------------
  // Everything the flow needs is derived above from the same bundle the
  // desktop layout uses; the flow calls the SAME endpoints/handlers so
  // save logic never forks. Desktop falls straight through to the
  // untouched return below.
  if (isPhone) {
    return (
      <GuidedDrFlow
        id={id}
        report={report}
        project={project}
        grid={grid}
        presence={presence}
        photos={photos}
        voiceAnnotations={voice_annotations}
        delays={delays}
        safetyEvents={safety_events}
        qcIssues={qc_issues}
        rfis={rfis}
        deliveries={deliveries}
        locked={locked}
        reload={reload}
        onBack={onBack}
        busy={busy}
        submit={submit}
        noWorkModal={noWorkModal}
        setNoWorkModal={setNoWorkModal}
        submitNoWorkDay={submitNoWorkDay}
        approve={approve}
        onDownloadPdf={downloadPdf}
      />
    );
  }

  return (
    <div data-testid="dr-detail" className="space-y-5 dr-grid">
      {/* V4 title block — pt.73: "Back to Daily Reports" now sits at the TOP
          of this title card (was in the header card below), with the H1 +
          date stepper + status pill on the row beneath it. .surface is
          theme-aware (light + dark). */}
      <div className="surface px-5 py-3 max-md:px-3 max-md:py-2.5 dr-title-card">
        {/* Desktop title block (unchanged) — hidden on phones. */}
        <div className="max-md:hidden">
          <BackButton onClick={onBack} label="Daily Reports" />
          <div className="mt-2 flex items-baseline gap-3 flex-wrap">
            <h1 className="text-xl font-semibold tracking-tight m-0">
              Daily Report &middot;{" "}
              <span className="font-mono">{formatDateDisplay(report.report_date)}</span>
            </h1>
            <span className="date-stepper" data-testid="dr-date-stepper">
              <button onClick={() => dateStep(-1)} title="Previous day" aria-label="Previous day">
                <ChevronLeft className="h-3.5 w-3.5" />
              </button>
              <button onClick={() => dateStep(+1)} title="Next day" aria-label="Next day">
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </span>
            <span className={`v4-pill v4-pill-${
              report.status === "approved" ? "closed" :
              report.status === "submitted" ? "submitted" : "draft"
            }`}>
              {report.status === "approved" ? "Approved" :
               report.status === "submitted" ? "Submitted" : "Draft"}
            </span>
          </div>
        </div>
        {/* Mobile title block (approved mockup): tiny breadcrumb, then
            [‹] [Daily report / date] [status pill] [›]. md:hidden. */}
        <div className="md:hidden">
          {/* user: one title only — the row's left arrow IS the back button. */}
          <div className="flex items-center gap-2">
            <button onClick={onBack} aria-label="Back to daily reports" data-testid="dr-back-m"
              className="h-[30px] w-[30px] grid place-items-center rounded-[9px] border shrink-0"
              style={{ borderColor: "var(--v4-border)", background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-secondary)" }}>
              <ChevronLeft className="h-4 w-4" />
            </button>
            <div className="flex-1 min-w-0">
              <div className="text-[17px] font-semibold leading-tight">Daily report</div>
              {/* mockup subtitle style: long-form date ("May 27, 2026"). */}
              <div className="text-xs" style={{ color: "var(--v4-fg-muted)" }}>
                {(() => { try { return new Date(`${report.report_date}T00:00:00`).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" }); } catch (e) { return formatDateDisplay(report.report_date); } })()}
              </div>
            </div>
            <span className={`v4-pill v4-pill-${
              report.status === "approved" ? "closed" :
              report.status === "submitted" ? "submitted" : "draft"
            }`}>
              {report.status === "approved" ? "Approved" :
               report.status === "submitted" ? "Submitted" : "Draft"}
            </span>
            <button onClick={() => dateStep(+1)} aria-label="Next day" data-testid="dr-next-m"
              className="h-[30px] w-[30px] grid place-items-center rounded-[9px] border shrink-0"
              style={{ borderColor: "var(--v4-border)", background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-secondary)" }}>
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>

      {/* HEADER */}
      <Header report={report} project={project} userLabels={user_labels}
              ppc={ppc}
              totalHeadcount={totalHeadcount} totalCrewHours={totalCrewHours}
              delaysCount={(delays || []).length}
              rfisCount={(rfis || []).length}
              safetyCount={(safety_events || []).length}
              qcCount={(qc_issues || []).length}
              activitiesCount={(grid || []).length}
              locked={locked}
              onSubmit={submit} onApprove={approve} onDownload={downloadPdf}
              busy={busy} validate={validate}
              templateMeta={templateMeta}
              exportingClient={exportingClient}
              onDownloadClientTemplate={downloadClientTemplate}
              onQuickLogCoordination={() => setCoordOpen(true)}
              drSections={drSections}
              completeCount={completeCount}
              totalSections={totalSections}
              progressPct={progressPct}
              mobileChecklist={mobileChecklist}
              requiredDone={requiredDone}
              requiredTotal={requiredTotal}
              mobileProgressPct={mobileProgressPct} />

      {/* 5-Route DR Entry — offline banner. Shows when offline OR when
          there are queued requests waiting to drain. Auto-hides when
          online and queue is empty. */}
      {(!online || queueSize > 0) && (
        <div data-testid="dr-offline-banner"
          className={`rounded-lg border px-3 py-2 text-sm flex items-center justify-between gap-3 ${
            !online
              ? "bg-amber-50 border-amber-300 text-amber-900"
              : "bg-blue-50 border-blue-300 text-blue-900"
          }`}
        >
          <div className="flex items-center gap-2">
            <div className={`h-2 w-2 rounded-full ${!online ? "bg-amber-500 animate-pulse" : "bg-blue-500"}`} />
            {!online
              ? <span><strong>You're offline.</strong> Edits save locally and sync automatically when you reconnect.</span>
              : <span><strong>{queueSize}</strong> pending change{queueSize === 1 ? "" : "s"} syncing…</span>}
          </div>
          {online && queueSize > 0 && (
            <button
              data-testid="dr-offline-drain"
              onClick={async () => {
                try {
                  // eslint-disable-next-line global-require
                  const { drainOfflineQueue } = require("../lib/drOfflineSync");
                  const r = await drainOfflineQueue(api);
                  if (r.synced > 0) toast.success(`Synced ${r.synced} pending change${r.synced === 1 ? "" : "s"}.`);
                  if (r.remaining > 0) toast.error(`${r.remaining} change${r.remaining === 1 ? "" : "s"} still pending — will retry.`);
                  reload();
                } catch (e) {
                  toast.error(`Sync failed: ${e.message}`);
                }
              }}
              className="h-7 px-2 rounded bg-blue-600 text-white text-xs hover:bg-blue-700"
            >Sync now</button>
          )}
        </div>
      )}

      {/* V4 SPINE — vertical rail with numbered status nodes on the
          LEFT of every DR section. Status (.complete / .in-progress
          / .blocked / empty) drives the node color + the connecting
          line segment to the next step. */}
      <div className="dr-spine" data-testid="dr-spine">

      {/* Feature DR Header Auto-Population (2026-05-20) -- the formal
          contract / station / signature block shown on every Daily
          Report. */}
      <div id="dr-section-project" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[0].status === "complete" ? "complete" : drSections[0].status === "in-progress" ? "in-progress" : drSections[0].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">1</span></div>
        <DrAutoHeader project={project} currentUserName={currentUserName} />
      </div>

      {/* 5-Route DR voice intake (Phase A 2026-05-25). The "Voice Memo"
          and "Live Voice" modes selected at New-DR time auto-expand
          the matching panel; Manual mode hides it. Recording / live /
          offline-queue logic lives inside VoiceIntakePanel.
          User can toggle the panel on any open DR via the header
          "Voice intake" button below. */}
      {activeIntakeMode && activeIntakeMode !== "manual" && (
        <div className="relative">
          {/* pt.30: collapse button so the user can close the voice intake
              panel without leaving the DR. Was: only way to make it
              disappear was to navigate away. Now: ✕ button in the top-
              right of the panel container that flips back to manual mode. */}
          {!locked && (
            <button
              data-testid="dr-voice-collapse"
              onClick={() => setActiveIntakeMode("manual")}
              title="Collapse voice intake"
              aria-label="Collapse voice intake"
              className="absolute top-2 right-2 z-10 h-7 w-7 max-md:min-h-[44px] max-md:min-w-[44px] grid place-items-center rounded transition"
              style={{ background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-secondary)", border: "1px solid var(--v4-border)" }}
            >
              <ChevronUp className="h-3.5 w-3.5" />
            </button>
          )}
          <VoiceIntakePanel
            dailyReportId={id}
            initialMode={activeIntakeMode === "voice_memo" ? "voice_async" : "voice_live"}
            locked={locked}
            onApplied={reload}
          />
        </div>
      )}
      {activeIntakeMode === "manual" && !locked && (
        <div className="flex justify-end -mt-2">
          {/* pt.48 (2026-05-28): voice intake is a flagship feature, so
              promote this from the toned-down sbtn-xs secondary to a
              prominent emerald sbtn-md primary CTA. */}
          <button data-testid="dr-open-voice-intake"
            onClick={() => setActiveIntakeMode("voice_memo")}
            className="sbtn sbtn-md sbtn-primary"
          >
            <Mic /> Add via voice
          </button>
        </div>
      )}

      {/* Bucket C3b Phase 1 — quick-log coordination event from this DR.
          Pre-links the event to this daily report on save so it shows up
          on the Coordination page filtered by linked_daily_report_id. */}
      <Dialog open={coordOpen} onOpenChange={(o) => { setCoordOpen(o); if (!o) setCoordAddReason(""); }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {locked ? "Quick-log coordination event (post-submit)" : "Quick-log coordination event"}
            </DialogTitle>
          </DialogHeader>
          {locked && (
            <div className="mb-2">
              <LockedAddReasonBlock value={coordAddReason} onChange={setCoordAddReason}
                eventLabel="a coordination event" testid="coord-add-reason" />
            </div>
          )}
          <CoordinationEventForm
            projectId={project.id}
            linkedDailyReportId={id}
            abbreviated={true}
            isLocked={locked}
            lockedAddReason={locked ? coordAddReason : null}
            onSaved={() => {
              setCoordOpen(false);
              setCoordAddReason("");
              toast.success(locked
                ? "Coordination event logged (post-submit). Audit chain captured."
                : "Coordination event logged");
            }}
            onCancel={() => { setCoordOpen(false); setCoordAddReason(""); }}
          />
        </DialogContent>
      </Dialog>

      {/* Addition 3 -- no-work-day acknowledgment modal. Triggered when
          the server rejects an empty-DR submit; user picks a reason and
          confirms, modal calls submitNoWorkDay() which re-submits with
          is_no_work_day=true. */}
      <Dialog open={!!noWorkModal} onOpenChange={(o) => !o && setNoWorkModal(null)}>
        {/* Sid change #6: on phones this renders as a full-width bottom sheet
            (dr-dialog-sheet) — the old max-w-md centered box leaked a desktop
            width on mobile. Copy trimmed to a one-line helper hint. */}
        <DialogContent className="max-w-md dr-dialog-sheet" data-testid="no-work-day-modal">
          <DialogHeader>
            <DialogTitle>No activities or events</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <p className="text-slate-600 text-xs">
              File as a no-work day so the record shows why it's blank.
            </p>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" checked readOnly disabled
                data-testid="no-work-ack" className="mt-0.5" />
              <span><strong>File as a no-work day</strong> (required to submit a blank DR)</span>
            </label>
            <div>
              <FieldLabel required className="field-label">Reason</FieldLabel>
              <select data-testid="no-work-reason"
                value={noWorkModal?.reason ?? ""}
                onChange={(e) => setNoWorkModal({ ...noWorkModal, reason: e.target.value })}
                className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                <option value="">Select…</option>
                <option value="weather">Weather (storm, extreme cold, snow, etc.)</option>
                <option value="holiday">Holiday</option>
                <option value="scheduled_non_work">Scheduled non-work day</option>
                <option value="crew_unavailable">Crew unavailable</option>
                <option value="tenant_restriction">Tenant or owner access restricted</option>
                <option value="other">Other (note required)</option>
              </select>
            </div>
            <div>
              <FieldLabel required={noWorkModal?.reason === "other"} className="field-label">
                Note
              </FieldLabel>
              <textarea data-testid="no-work-note"
                value={noWorkModal?.note ?? ""}
                onChange={(e) => setNoWorkModal({ ...noWorkModal, note: e.target.value })}
                placeholder={
                  noWorkModal?.reason === "other"
                    ? "Required: describe the reason."
                    : "Optional context."
                }
                className="w-full p-2 rounded border border-slate-200 text-sm h-20" />
            </div>
            <div className="flex justify-end gap-2 pt-2 max-md:flex-col">
              <button data-testid="no-work-cancel" onClick={() => setNoWorkModal(null)}
                className="h-9 px-3 rounded border border-slate-200 text-sm max-md:min-h-[44px] max-md:w-full">
                Cancel
              </button>
              <button data-testid="no-work-confirm" onClick={submitNoWorkDay}
                disabled={busy}
                className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 max-md:min-h-[44px] max-md:w-full">
                {busy ? "Submitting…" : "Submit as no-work day"}
              </button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* WEATHER — section pip jump anchor + spine step 2 */}
      <div id="dr-section-weather" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[1].status === "complete" ? "complete" : drSections[1].status === "in-progress" ? "in-progress" : drSections[1].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">2</span></div>
        <WeatherPanel report={report} dailyReportId={id} locked={locked} reload={reload} />
      </div>

      {/* ACTIVITY GRID — section pip jump anchor + spine step 3.
          Sid-approved sequence change: Activities render BEFORE Manpower
          (parent → child) on desktop AND mobile. drSections[2] is now the
          activity entry (see the reordered array above). */}
      <div id="dr-section-activity" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[2].status === "complete" ? "complete" : drSections[2].status === "in-progress" ? "in-progress" : drSections[2].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">3</span></div>
        <ActivityGridPanel grid={grid} reload={reload}
          dailyReportId={id} locked={locked} reportDate={report.report_date}
          presence={presence} submitRowErrors={submitRowErrors} />
      </div>

      {/* CREW SUMMARY (presence) — section pip jump anchor + spine step 4.
          Manpower now follows Activities. drSections[3] is the manpower entry. */}
      <div id="dr-section-manpower" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[3].status === "complete" ? "complete" : drSections[3].status === "in-progress" ? "in-progress" : drSections[3].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">4</span></div>
        <PresencePanel rows={presence} dailyReportId={id} locked={locked} reload={reload}
          projectId={project.id} grid={grid} reportDate={report.report_date} />
      </div>

      {/* RENOVATION (only for renovation/tenant_improvement) */}
      {isRenovation && (
        <RenovationPanel report={report} dailyReportId={id} locked={locked} reload={reload} />
      )}

      {/* OPEN DELAY REMINDERS — only renders when reminders array
          non-empty. Placed strategically ABOVE the Events tabbed card
          so rolled-forward delays are the FIRST thing the super sees
          before navigating the per-event tabs. */}
      <ReminderBanner reminders={reminders} reload={reload} />

      {/* EVENTS — tabbed card (Delays · Safety · QC · RFIs ·
          Deliveries · Notes). Each panel keeps its full form,
          linking, and audit behavior intact; the tabs are purely a
          structural / layout change so the super can land on the
          right surface in one click instead of scrolling 1500px.
          The "+ Add" button at the right of the tab strip dispatches
          the same `dr-open-event-form` window event that the Activity
          Grid Issues dropdown uses, so the panel-level scroll/focus
          logic continues to work uniformly. */}
      {/* EVENTS — section pip jump anchor + spine step 5 */}
      <div id="dr-section-events" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[4].status === "complete" ? "complete" : drSections[4].status === "in-progress" ? "in-progress" : drSections[4].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">5</span></div>
        <EventsTabsCard
          delays={delays} safetyEvents={safety_events} qcIssues={qc_issues}
          rfis={rfis} deliveries={deliveries}
          dailyReportId={id} projectId={project.id} reportDate={report.report_date}
          locked={locked} reload={reload} grid={grid} report={report} />
      </div>

      {/* PHOTOS — section pip jump anchor + spine step 6 */}
      <div id="dr-section-photos" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[5].status === "complete" ? "complete" : drSections[5].status === "in-progress" ? "in-progress" : drSections[5].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">6</span></div>
        <PhotosPanel rows={photos} dailyReportId={id} projectId={project.id}
                     locked={locked} reload={reload} />
      </div>

      {/* Bucket D Phase 4 -- supplements appendix. Lists every event
          filed against THIS DR after it was submitted (i.e., events
          whose parent_daily_report_id == this DR's id). */}
      <SupplementsAppendix dailyReportId={id} projectId={project.id} />

      {/* AUDIT LOG — section pip jump anchor + spine step 7 (last) */}
      <div id="dr-section-audit" style={{ scrollMarginTop: 70 }}
           className={`step ${drSections[6].status === "complete" ? "complete" : drSections[6].status === "in-progress" ? "in-progress" : drSections[6].status === "blocked" ? "blocked" : ""}`}>
        <div className="step-node"><span className="num">7</span></div>
        <AuditPanel rows={audit_log} userLabels={user_labels} />
      </div>

      </div>{/* /.dr-spine */}

      {/* V4 sticky bottom submit bar — travels with the user as they
          scroll. Shows progress + persistent Submit button. Hides on
          locked / submitted reports. */}
      {!locked && (
        <StickySubmitBar
          completeCount={completeCount}
          total={totalSections}
          progressPct={progressPct}
          remainingLabels={drSections.filter((s) => s.status !== "complete").slice(0, 4).map((s) => s.label)}
          onSubmit={submit}
          busy={busy}
        />
      )}
    </div>
  );
}

// V4 Sticky Submit Bar — adds bottom padding to body so content
// doesn't sit underneath. Auto-cleans up the class on unmount.
function StickySubmitBar({ completeCount, total, progressPct, remainingLabels, onSubmit, busy }) {
  useEffect(() => {
    document.body.classList.add("has-sticky-submit");
    return () => document.body.classList.remove("has-sticky-submit");
  }, []);
  return (
    <div className="sticky-submit" data-testid="dr-sticky-submit">
      {/* pt.37: replaced the linear progress bar + "X to go — A, B, C"
          inline list with Option C: mini progress circle + status text
          + "show me" popover. Same data, ~60% less horizontal weight,
          and the missing-items list moves into a popover so the
          sticky bar stays readable when there are many incomplete
          sections. */}
      <SubmitProgress
        completeCount={completeCount}
        total={total}
        missingLabels={remainingLabels}
      />
      <div style={{ flex: 1 }} />
      {/* pt.39: emerald → primary silver/dark-grey (sbtn-md). This is
          THE action on this surface so it gets the primary treatment
          (32px), not emerald. */}
      <button
        data-testid="dr-sticky-submit-btn"
        onClick={onSubmit}
        disabled={busy}
        className="sbtn sbtn-md sbtn-primary"
      >
        Submit report
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
      </button>
    </div>
  );
}

// ------------------------------------------------------------
//  Panels
// ------------------------------------------------------------
function StatusPill({ status }) {
  const klass = status === "approved" ? "pill-green"
    : status === "submitted" ? "pill-blue"
    : "pill-slate";
  return <span data-testid={`dr-status-${status}`} className={`pill ${klass}`}>{status}</span>;
}

function Header({ report, project, userLabels, ppc,
                  totalHeadcount, totalCrewHours,
                  delaysCount = 0, rfisCount = 0, safetyCount = 0, qcCount = 0,
                  activitiesCount = 0,
                  locked, onSubmit,
                  onApprove, onDownload, busy, validate, onQuickLogCoordination,
                  templateMeta, exportingClient, onDownloadClientTemplate,
                  /* v4 pt.19 — render progress card INSIDE header, ABOVE KPI band */
                  drSections = [], completeCount = 0, totalSections = 7, progressPct = 0,
                  /* mobile checklist (Sid redesign) — computed in DailyReportDetail, passed in */
                  mobileChecklist = [], requiredDone = 0, requiredTotal = 4, mobileProgressPct = 0 }) {
  const v = validate();
  return (
    <div className="surface p-4" data-testid="dr-header">
      {/* v4 (2026-05-26): action buttons (Coordination / Export / Submit)
          moved ABOVE the KPI band to match the v4 mockup. pt.73: the Back
          pill moved up to the title card, so this row is just the actions. */}
      {/* Desktop actions row — hidden on phones (the mobile block below
          carries Submit/Approve; Export stays a desktop task). */}
      <div className="flex flex-wrap items-center gap-3 mb-3 max-md:hidden">
        <div className="ml-auto flex gap-2 items-center">
          {/* pt.38: removed the duplicate "N items blocking submit:
              ..." amber brick that lived here in the header. The same
              info is now in the sticky-bottom SubmitProgress circle +
              "show me" popover (pt.37 Option C). One source of truth
              for what's blocking the submit; no more long wall of
              text at the top of the page. */}
          {/* pt.40 button-system sweep: page-header tier means BIGGER
              (sbtn-md 32px). Coordination/Export sit on the DR detail
              page header alongside the primary Submit action -- they
              should match Submit's tier visually, not the inline
              section buttons (which use sbtn-sm 28px). User
              specifically asked for header buttons to be relatively
              bigger than non-header buttons. */}
          {onQuickLogCoordination && (
            <button data-testid="dr-quick-log-coord" onClick={onQuickLogCoordination}
              className="sbtn sbtn-md sbtn-secondary">
              <MessageSquare />
              {locked ? "Coordination (post-submit)" : "Coordination"}
            </button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button data-testid="dr-export-menu" disabled={busy}
                className="sbtn sbtn-md sbtn-secondary">
                <Download /> Export
                <ChevronDown className="h-3 w-3 -mr-1 opacity-60" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-64">
              <DropdownMenuLabel>Export format</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem data-testid="dr-export-internal"
                disabled={busy}
                onClick={() => onDownload("internal")}
                className="flex items-start gap-2 cursor-pointer">
                <Download className="h-4 w-4 mt-0.5 shrink-0" />
                <div>
                  <div className="font-medium">For Internal</div>
                  <div className="text-xs text-slate-500">Full audit trail, all fields</div>
                </div>
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="dr-export-external"
                disabled={busy}
                onClick={() => onDownload("external")}
                className="flex items-start gap-2 cursor-pointer">
                <Download className="h-4 w-4 mt-0.5 shrink-0" />
                <div>
                  <div className="font-medium">For External</div>
                  <div className="text-xs text-slate-500">Client-safe, redacted internals</div>
                </div>
              </DropdownMenuItem>
              {templateMeta?.present && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem data-testid="dr-export-client-template"
                    disabled={exportingClient || busy}
                    onClick={onDownloadClientTemplate}
                    title={`Export using the project's client template (${templateMeta.file_name || "xlsx"})`}
                    className="flex items-start gap-2 cursor-pointer">
                    {exportingClient ? (
                      <Loader2 className="h-4 w-4 mt-0.5 shrink-0 animate-spin" />
                    ) : (
                      <FileSpreadsheet className="h-4 w-4 mt-0.5 shrink-0" />
                    )}
                    <div>
                      <div className="font-medium">{exportingClient ? "Exporting…" : "Client format (xlsx)"}</div>
                      <div className="text-xs text-slate-500 truncate max-w-[180px]">
                        {templateMeta.file_name || "Uses uploaded template"}
                      </div>
                    </div>
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
          {report.status === "draft" && (
            <button data-testid="dr-submit" onClick={onSubmit} disabled={busy}
              className="sbtn sbtn-md sbtn-primary">
              Submit
            </button>
          )}
          {report.status === "submitted" && (
            <button data-testid="dr-approve" onClick={onApprove} disabled={busy}
              className="sbtn sbtn-md sbtn-primary">
              <CheckCircle2 /> Approve
            </button>
          )}
        </div>
      </div>
      {/* v4 pt.19 (2026-05-26): Progress card moved INTO header, above
          the KPI band so the workflow read order is:
          [Back  ...  Coord | Export | Submit]
          [N/7 sections done  ===bar===  01 Project ... 07 Audit]
          [PPC | Headcount | Crew hours | ... | QC]
          User feedback: "2/4 sections done thing should above that ppc
          and other panels like in v4 mockup". */}
      {/* MOBILE header (approved mockup, md:hidden): slim progress bar with
          the Submit/Approve action inline, then the section chips row. The
          desktop progress card below is max-md:hidden. */}
      {drSections.length > 0 && (
        <div className="md:hidden mb-3 flex flex-col gap-2" data-testid="dr-mobile-head">
          <div className="surface rounded-xl px-3 py-2.5 flex items-center gap-3">
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-2 mb-1.5">
                {/* Sid-approved: progress counts REQUIRED sections only
                    (Project / Weather / Activities / Manpower). Issues +
                    Photos are optional and excluded from the total. */}
                <span className="dr-progress-label min-w-0 overflow-hidden text-ellipsis"><span className="count">{requiredDone} of {requiredTotal}</span>&nbsp;required done</span>
                <span className="text-xs shrink-0" style={{ color: "var(--v4-fg-muted)" }}>{mobileProgressPct}%</span>
              </div>
              <div className="dr-progress-bar" style={{ minWidth: 0 }}>
                <div className="dr-progress-bar-fill" style={{ width: `${mobileProgressPct}%` }} />
              </div>
            </div>
            {report.status === "draft" && (
              <button data-testid="dr-submit-m" onClick={onSubmit} disabled={busy}
                className="sbtn sbtn-md sbtn-primary shrink-0">
                Submit
              </button>
            )}
            {report.status === "submitted" && (
              <button data-testid="dr-approve-m" onClick={onApprove} disabled={busy}
                className="sbtn sbtn-md sbtn-primary shrink-0">
                <CheckCircle2 /> Approve
              </button>
            )}
          </div>
          {/* Sid-approved "show me" checklist: Audit chip removed; Events
              renamed to Issues; Issues + Photos flagged .optional (they don't
              gate submit). A chip flips to the green ✓ state (via .complete)
              the moment its section has data — for Issues that means >=1
              logged issue, the bug Sid hit where it stayed pending. */}
          <div className="dr-pips" data-testid="dr-section-chips">
            {mobileChecklist.map((s) => (
              <button key={s.id} type="button" data-testid={`dr-chip-${s.id}`}
                className={`dr-pip ${s.status}${s.required ? "" : " optional"}`}
                onClick={() => {
                  const el = document.getElementById(`dr-section-${s.id}`);
                  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
                title={`${s.label} — ${s.status}${s.required ? "" : " (optional)"}`}>
                <span className="pip-name">{s.label}</span>
                {!s.required && <span className="pip-opt">optional</span>}
              </button>
            ))}
          </div>
          {/* Mobile-first secondary actions: Coordination + Export as small
              pill chips, pinned TOP-RIGHT above the progress card (user:
              same placement as desktop). -order-1 lifts this row to the top
              of the flex column. */}
          <div className="flex items-center justify-end gap-2 -order-1" data-testid="dr-actions-m">
            {onQuickLogCoordination && (
              <button data-testid="dr-coord-m" onClick={onQuickLogCoordination}
                title={locked ? "Log a coordination event (post-submit)" : "Log a coordination event"}
                className="sbtn sbtn-sm sbtn-secondary" style={{ borderRadius: 999 }}>
                <MessageSquare /> Coordination
              </button>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button data-testid="dr-export-menu-m" disabled={busy}
                  className="sbtn sbtn-sm sbtn-secondary" style={{ borderRadius: 999 }}>
                  <Download /> Export
                  <ChevronDown className="h-3 w-3 -mr-1 opacity-60" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-64">
                <DropdownMenuLabel>Export format</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem data-testid="dr-export-internal-m"
                  disabled={busy}
                  onClick={() => onDownload("internal")}
                  className="flex items-start gap-2 cursor-pointer">
                  <Download className="h-4 w-4 mt-0.5 shrink-0" />
                  <div>
                    <div className="font-medium">For Internal</div>
                    <div className="text-xs text-slate-500">Full audit trail, all fields</div>
                  </div>
                </DropdownMenuItem>
                <DropdownMenuItem data-testid="dr-export-external-m"
                  disabled={busy}
                  onClick={() => onDownload("external")}
                  className="flex items-start gap-2 cursor-pointer">
                  <Download className="h-4 w-4 mt-0.5 shrink-0" />
                  <div>
                    <div className="font-medium">For External</div>
                    <div className="text-xs text-slate-500">Client-safe, redacted internals</div>
                  </div>
                </DropdownMenuItem>
                {templateMeta?.present && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem data-testid="dr-export-client-template-m"
                      disabled={exportingClient || busy}
                      onClick={onDownloadClientTemplate}
                      title={`Export using the project's client template (${templateMeta.file_name || "xlsx"})`}
                      className="flex items-start gap-2 cursor-pointer">
                      {exportingClient ? (
                        <Loader2 className="h-4 w-4 mt-0.5 shrink-0 animate-spin" />
                      ) : (
                        <FileSpreadsheet className="h-4 w-4 mt-0.5 shrink-0" />
                      )}
                      <div>
                        <div className="font-medium">{exportingClient ? "Exporting…" : "Client format (xlsx)"}</div>
                        <div className="text-xs text-slate-500 truncate max-w-[180px]">
                          {templateMeta.file_name || "Uses uploaded template"}
                        </div>
                      </div>
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      )}
      {drSections.length > 0 && (
        <div className="dr-progress-card mb-3 max-md:hidden" data-testid="dr-progress-card" style={{ marginBottom: 12 }}>
          <div className="dr-progress-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
            </svg>
            <span><span className="count">{completeCount} / {totalSections}</span> sections done</span>
          </div>
          <div className="dr-progress-bar"><div className="dr-progress-bar-fill" style={{ width: `${progressPct}%` }} /></div>
          <div className="dr-pips" data-testid="dr-section-pips">
            {drSections.map((s, i) => (
              <button
                key={s.id}
                type="button"
                data-testid={`dr-pip-${s.id}`}
                className={`dr-pip ${s.status}`}
                onClick={() => {
                  const el = document.getElementById(`dr-section-${s.id}`);
                  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
                title={`${s.label} — ${s.status}`}
              >
                <span className="pip-num">{String(i + 1).padStart(2, "0")}</span><span className="pip-name">{s.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}
      {/* KPI band — full width below the progress card. Operational
          stats (PPC / Headcount / Crew hours) on the
          left; event-count stats (Delays / RFIs / Safety / QC) on the
          right. pt.47 (2026-05-28): dropped "Activity hrs" KPI — crew
          hours come from the Manpower Log, per-activity hours retired. */}
      {/* Desktop KPI band (unchanged) — hidden on phones. */}
      <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-7 gap-2 max-md:hidden">
        <KPI label="PPC" value={`${ppc}%`} testid="kpi-ppc" />
        <KPI label="Headcount" value={totalHeadcount} testid="kpi-headcount" />
        <KPI label="Crew hours" value={totalCrewHours.toFixed(1)} testid="kpi-crew-hours" />
        <KPI label="Delays" value={delaysCount} testid="kpi-delays" />
        <KPI label="RFIs" value={rfisCount} testid="kpi-rfis" />
        <KPI label="Safety" value={safetyCount} testid="kpi-safety" />
        <KPI label="QC" value={qcCount} testid="kpi-qc" />
      </div>
      {/* Mobile KPI grid (approved mockup): compact 4-across cells with the
          mockup's short labels. md:hidden. */}
      <div className="md:hidden grid grid-cols-4 gap-1.5 mt-2" data-testid="dr-kpis-m">
        <KPI label="PPC" value={`${ppc}%`} testid="kpi-m-ppc" />
        <KPI label="Workers" value={totalHeadcount} testid="kpi-m-workers" />
        <KPI label="Hours" value={totalCrewHours.toFixed(1)} testid="kpi-m-hours" />
        <KPI label="Delays" value={delaysCount} testid="kpi-m-delays" />
        <KPI label="RFIs" value={rfisCount} testid="kpi-m-rfis" />
        <KPI label="Safety" value={safetyCount} testid="kpi-m-safety" />
        <KPI label="QC" value={qcCount} testid="kpi-m-qc" />
        <KPI label="Activities" value={activitiesCount} testid="kpi-m-activities" />
      </div>
    </div>
  );
}

// Feature DR Header Auto-Population (2026-05-20) -- read-only project
// / contract / signature block. Six fields, all auto-populated, never
// editable. Blank values render as em-dash; absent project fields do
// NOT block DR creation.
//
// 2026-02-XX collapse-by-default refactor (Abhi):
// The full 6-field card is only really needed on the exported
// PDF/xlsx -- on the live DR view, the super only checks these
// values once per DR open. So the card now renders as a single-line
// summary `Client · Station · #Contract · Contractor · Signature`
// with a chevron to expand. Saves ~150px vertical space per DR open.
// Backend export paths render the full block unchanged.
function DrAutoHeader({ project, currentUserName }) {
  const [expanded, setExpanded] = useState(false);
  const fields = [
    { label: "Client",          value: project.client_name,      testid: "dr-hdr-client" },
    { label: "Station",         value: project.station_id,       testid: "dr-hdr-station" },
    { label: "Project title",   value: project.name,             testid: "dr-hdr-project-title" },
    { label: "Contract number", value: project.contract_number,  testid: "dr-hdr-contract" },
    { label: "Contractor",      value: project.contractor_name,  testid: "dr-hdr-contractor" },
    { label: "Signature",       value: currentUserName,          testid: "dr-hdr-signature" },
  ];
  // Compact summary line. Skip nulls so the row reads cleanly when
  // a project record is partially populated. Contract number is
  // prefixed with "#" only when the stored value doesn't already
  // include one (some legacy records store "#1234" while newer ones
  // store "1234").
  const contractLabel = project.contract_number
    ? (String(project.contract_number).trim().startsWith("#")
        ? project.contract_number
        : `#${project.contract_number}`)
    : null;
  const summaryParts = [
    project.client_name,
    project.station_id,
    contractLabel,
    project.contractor_name,
    currentUserName,
  ].filter(Boolean);
  return (
    <div className="surface px-4 py-2.5 dr-section" data-testid="dr-auto-header">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        data-testid="dr-auto-header-toggle"
        aria-expanded={expanded}
        className="w-full flex items-center gap-3 text-left group"
      >
        {/* Mobile (mockup): green section icon leads; chevron moves to the
            right end (max-md:order-3) and the summary right-aligns. Desktop
            keeps chevron-first (order utilities inert >=768px). */}
        <Building2 className="md:hidden h-4 w-4 text-[#34D399] shrink-0" strokeWidth={1.8} />
        <ChevronDown
          className={`h-4 w-4 text-slate-400 shrink-0 transition-transform ${expanded ? "rotate-180" : ""} max-md:order-3`}
        /><div className="text-[11px] uppercase tracking-[0.14em] text-slate-500 font-semibold shrink-0 max-md:normal-case">
          Project / contract
        </div>
        <div className="text-sm text-slate-300 truncate flex-1 max-md:text-right" data-testid="dr-auto-header-summary">
          {summaryParts.length
            ? summaryParts.join("  ·  ")
            : <span className="text-slate-500 italic">No project metadata yet — click to expand</span>}
        </div>
      </button>
      {expanded && (
        <div className="mt-3 pt-3 border-t border-white/[0.06] grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-2"
          data-testid="dr-auto-header-grid">
          {fields.map((f) => (
            <div key={f.label} data-testid={f.testid}>
              <div className="text-xs text-slate-500">{f.label}</div>
              <div className="text-sm text-slate-300">
                {f.value
                  ? f.value
                  : <span className="text-slate-500" data-testid={`${f.testid}-empty`}>—</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function KPI({ label, value, testid }) {
  return (
    <div className="px-3 py-2 bg-slate-50 border border-slate-200 rounded text-center w-full" data-testid={testid}>
      <div className="text-[11px] text-slate-500 truncate">{label}</div>
      <div className="font-semibold tabular-nums text-base leading-tight">{value}</div>
    </div>
  );
}

function PanelShell({ icon: Icon, title, count, testid, children, badge, step }) {
  return (
    <div data-testid={testid} className="surface p-5 dr-section">
      <div
        className="flex items-center gap-2 mb-4 pb-3 max-md:flex-wrap"
        style={{ borderBottom: "1px solid var(--v4-border)" }}
      >
        {/* Inline step badge removed — the .dr-spine wrapper renders
            the numbered nodes on the LEFT rail now. `step` prop kept
            for backwards-compatibility but no longer rendered here. */}
        <Icon className="h-4 w-4 text-[#34D399]" strokeWidth={1.8} />
        <div className="font-semibold text-sm tracking-tight text-foreground">{title}</div>
        {typeof count === "number" && (
          <span
            className="text-[11px] font-medium px-1.5 py-0.5 rounded ml-1"
            style={{ background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-muted)" }}
          >
            {count}
          </span>
        )}
        {badge}
      </div>
      {children}
    </div>
  );
}

function WeatherPanel({ report, dailyReportId, locked, reload }) {
  // 2026-02-XX (Abhi) -- the "Auto-fill from forecast" button was
  // removed; weather now auto-populates ONCE when the DR is first
  // opened with all weather fields blank (see auto-trigger effect
  // in DailyReportDetail). This component is purely render-and-edit
  // so the super still tweaks values manually after autofill lands.
  // 2026-02-XX (Abhi prod-feedback) -- a tiny "Refresh" button is
  // back, in case the project city/state was added AFTER the DR was
  // opened: the auto-trigger fires once-per-mount and can't catch
  // that follow-up edit on its own.
  const [refreshing, setRefreshing] = useState(false);
  // Mobile mockup: phones mount collapsed; desktop mounts expanded and has
  // no visible toggle, so it behaves exactly as before.
  const [expanded, setExpanded] = useState(
    () => typeof window === "undefined" || !window.matchMedia("(max-width: 767.98px)").matches
  );
  const patch = async (fields) => {
    try { await api.patch(`/daily-reports/${dailyReportId}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };
  const refresh = async () => {
    setRefreshing(true);
    try {
      await api.post(`/daily-reports/${dailyReportId}/weather-autofill`);
      toast.success("Weather refreshed from project location");
      reload();
    } catch (e) {
      toast.error(`Weather: ${e?.response?.data?.detail || "Refresh failed — check project city/state"}`);
    } finally {
      setRefreshing(false);
    }
  };

  const fetchedAt = report.weather_fetched_at ? new Date(report.weather_fetched_at).toLocaleTimeString() : null;
  return (
    <div data-testid="dr-weather-panel" className="surface px-4 py-3 dr-section">
      <div className={`flex items-center gap-2 flex-wrap ${expanded ? "mb-2" : "mb-2 max-md:mb-0"}`}><Cloud className="h-4 w-4 text-[#34D399]" strokeWidth={1.8} />
        <div className="font-semibold text-sm tracking-tight text-foreground">Weather</div>
        {fetchedAt && <span className={`text-xs text-slate-400 ml-2 ${expanded ? "" : "max-md:hidden"}`}>auto-filled {fetchedAt}</span>}
        {!expanded && (
          <button type="button" data-testid="weather-summary"
            onClick={() => setExpanded(true)}
            className="md:hidden ml-auto min-w-0 truncate text-right text-xs"
            style={{ color: "var(--v4-fg-muted)" }}>
            {[
              report.weather_conditions
                ? report.weather_conditions.charAt(0).toUpperCase() + report.weather_conditions.slice(1)
                : null,
              (report.weather_temp_high_f ?? null) !== null || (report.weather_temp_low_f ?? null) !== null
                ? `${report.weather_temp_high_f != null ? Math.round(report.weather_temp_high_f) : "—"}°/${report.weather_temp_low_f != null ? Math.round(report.weather_temp_low_f) : "—"}°`
                : null,
            ].filter(Boolean).join(" ") || "No weather yet"}
          </button>
        )}
        <button
          data-testid="weather-refresh-btn"
          type="button"
          onClick={refresh}
          disabled={locked || refreshing}
          className={`ml-auto text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-50 ${expanded ? "" : "max-md:hidden"}`}
          title="Re-fetch weather from project city/state"
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
        {/* Mobile-only collapse toggle (approved DR mobile mockup): weather is
            auto-filled and rarely edited, so phones start collapsed to a
            one-line summary. md:hidden -> invisible >=768px, and desktop
            mounts with expanded=true, so desktop renders exactly as before. */}
        <button
          type="button"
          data-testid="weather-collapse-toggle"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="md:hidden h-8 w-8 inline-flex items-center justify-center rounded text-slate-400 shrink-0"
          title={expanded ? "Collapse weather" : "Expand weather"}
        >
          <ChevronDown className={`h-4 w-4 transition-transform ${expanded ? "rotate-180" : ""}`} />
        </button>
      </div>
      <div className={`grid grid-cols-2 md:grid-cols-5 gap-2 ${expanded ? "" : "max-md:hidden"}`}>
        <Field label="Conditions" value={report.weather_conditions || ""} onChange={(v) => patch({ weather_conditions: v })} disabled={locked} testid="weather-conditions" />
        <Field label="High °F" type="number" value={report.weather_temp_high_f ?? ""} onChange={(v) => patch({ weather_temp_high_f: v ? Number(v) : null })} disabled={locked} testid="weather-high" />
        <Field label="Low °F" type="number" value={report.weather_temp_low_f ?? ""} onChange={(v) => patch({ weather_temp_low_f: v ? Number(v) : null })} disabled={locked} testid="weather-low" />
        <Field label="Precip in" type="number" value={report.weather_precipitation_in ?? ""} onChange={(v) => patch({ weather_precipitation_in: v ? Number(v) : null })} disabled={locked} testid="weather-precip" />
        <Field label="Wind mph" type="number" value={report.weather_wind_mph ?? ""} onChange={(v) => patch({ weather_wind_mph: v ? Number(v) : null })} disabled={locked} testid="weather-wind" />
      </div>
    </div>
  );
}

const PRESENCE_SHIFTS = ["first", "second", "third", "weekend", "holiday", "off_hours"];

// pt.48 (2026-05-28): manpower-log dropdowns (Company / Trade / Task) now use
// the same Radix Select primitive as the top-bar project switcher, so they open
// downward into a themed panel. The old native <datalist> (Company) + <select>
// (Trade/Task) popups rendered white and mis-positioned (to the side / above).
// The trigger is styled with the theme-aware .dr-mp-input (NOT .v4-topbar-select,
// which hardcodes white-on-dark for the always-dark topbar and would be invisible
// on the light-theme page surface). MP_NONE is the sentinel for the Task
// "— none —" choice, since a Radix SelectItem cannot have an empty-string value.
const MP_NONE = "__none__";
function MpSelect({ value, onValueChange, options, placeholder, disabled, testid, className = "w-40" }) {
  return (
    <div className={className}>
      <Select value={value || ""} onValueChange={onValueChange} disabled={disabled}>
        <SelectTrigger data-testid={testid} className="dr-mp-input h-8 w-full text-xs rounded">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent className="max-h-72">
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value} className="text-xs">{o.label}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function PresencePanel({ rows, dailyReportId, locked, reload, projectId, grid = [], reportDate }) {
  // Bucket H Phase 4 -- demand-vs-actual comparison. Fetch per-project
  // demand for this DR's date, group by trade, render alongside actual
  // headcount totals from presence rows.
  const [demandByTrade, setDemandByTrade] = useState(null);
  useEffect(() => {
    if (!projectId || !reportDate) { setDemandByTrade(null); return; }
    api.get(`/resource-leveling/projects/${projectId}/demand`, {
      params: { date_range_start: reportDate, date_range_end: reportDate },
    }).then((r) => {
      const matrix = r.data?.matrix || {};
      const byTrade = {};
      Object.entries(matrix).forEach(([trade, byDate]) => {
        const slot = byDate[reportDate];
        if (slot && slot.demanded > 0) byTrade[trade] = slot;
      });
      setDemandByTrade(byTrade);
    }).catch(() => setDemandByTrade(null));
  }, [projectId, reportDate]);
  const actualByTrade = rows.reduce((acc, r) => {
    const t = r.trade;
    if (!t) return acc;
    acc[t] = (acc[t] || 0) + (r.headcount || 0);
    return acc;
  }, {});

  const total = rows.reduce((s, r) => s + (r.headcount || 0), 0);
  const totalHours = rows.reduce((s, r) => s + (r.headcount || 0) * (r.hours_per_person || 0), 0);

  // 2026-05-26 inline-row redesign (Abhi).
  // Old pattern: persisted rows + a separate "Add trade" form at the bottom.
  // New pattern: persisted rows + one ALWAYS-VISIBLE blank draft row at
  // the end. User types directly into the draft; clicking "+ Add more
  // trade" on the last row's right edge saves the current draft AND
  // appends a new blank draft below. Deleting a row keeps the
  // always-visible blank draft on whichever row is now last.
  // 2026-02-XX (Abhi) -- the manpower log no longer auto-shows a
  // blank draft row. Once the super saves a trade, the table sits
  // clean; clicking the dedicated "+ Add trade" button below opens
  // one editable draft. Same button switches to "Save" when the
  // draft has a trade typed in, then disappears again on success.
  // pt.50 (2026-05-28): when the manpower log is empty, open with ONE blank
  // draft row already in place (the pre-reskin behavior) so the super can type
  // immediately. Additional rows come from the "Add trade" button.
  const [draftsCount, setDraftsCount] = useState(() => (rows && rows.length === 0 && !locked) ? 1 : 0);
  // Each draft has a stable local id so input components don't lose
  // focus on every keystroke as the array re-mounts.
  const draftIdsRef = useRef([]);
  if (draftIdsRef.current.length < draftsCount) {
    while (draftIdsRef.current.length < draftsCount) {
      draftIdsRef.current.push(`d_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    }
  }
  const [drafts, setDrafts] = useState(() => (rows && rows.length === 0 && !locked)
    ? [{ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "", schedule_activity_id: "" }]
    : []);
  // 2026-02-XX (Abhi) -- Subcontractor central repo integration.
  // pt.48 (2026-05-28): the Company dropdown now lists ONLY the subs
  // attached to THIS project (chosen during project create / edit), not the
  // whole tenant repo. Fetch the project for its subcontractor_ids and
  // intersect with the repo. Empty -> the draft row shows a "manage in
  // project" hint instead of an empty dropdown.
  const [subs, setSubs] = useState([]);
  useEffect(() => {
    if (!projectId) { setSubs([]); return; }
    Promise.all([
      api.get("/subcontractors").catch(() => ({ data: [] })),
      api.get(`/projects/${projectId}`).catch(() => ({ data: {} })),
    ]).then(([subsRes, projRes]) => {
      const all = Array.isArray(subsRes.data) ? subsRes.data : [];
      const ids = new Set((projRes.data && projRes.data.subcontractor_ids) || []);
      setSubs(ids.size ? all.filter((s) => ids.has(s.id)) : []);
    });
  }, [projectId]);
  // Keep drafts array length in sync with draftsCount (additions only).
  if (drafts.length < draftsCount) {
    setDrafts((arr) => {
      const out = [...arr];
      while (out.length < draftsCount) {
        out.push({ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "", schedule_activity_id: "" });
      }
      return out;
    });
  }

  const patch = async (rid, fields) => {
    try { await api.patch(`/daily-presence/${rid}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };
  const remove = (rid) => {
    // pt.37: Tier-1 toast + Undo (was window.confirm). Cheap delete —
    // a manpower row can be re-added in seconds, so Gmail/Slack-style
    // instant-feel UX wins over a confirmation modal.
    deleteWithUndo({
      label: "Manpower row deleted",
      onCommit: async () => {
        await api.delete(`/daily-presence/${rid}`);
        reload();
      },
    });
  };
  // Save a single draft row. Returns true on success.
  const [busySaveIdx, setBusySaveIdx] = useState(-1);
  const saveDraft = async (idx) => {
    const d = drafts[idx];
    if (!d) return false;
    if (!d.trade?.trim()) {
      toast.error("Trade is required");
      return false;
    }
    setBusySaveIdx(idx);
    try {
      await api.post(`/daily-reports/${dailyReportId}/presence`, {
        trade: d.trade.trim(),
        company_name: d.company_name?.trim() || "",
        headcount: Number(d.headcount) || 0,
        hours_per_person: Number(d.hours_per_person) || 0,
        notes: d.notes || "",
        schedule_activity_id: d.schedule_activity_id || null,
        shift: "first",
        daily_report_id: dailyReportId,
      });
      reload();
      // mobile: a successful save collapses the entry table back to the
      // "Add crew" ghost (the saved crew shows as a card above).
      if (typeof window !== "undefined" && window.matchMedia("(max-width: 767.98px)").matches) setMEntryOpen(false);
      return true;
    } catch (e) {
      toast.error(e.response?.data?.detail || "Add failed");
      return false;
    } finally {
      setBusySaveIdx(-1);
    }
  };
  // "+ Add trade" (when drafts is empty) or "Save trade" (when a draft
  // has a trade typed). Saves the current LAST draft (if filled) and
  // clears the drafts array — no auto-blank below.
  const addAnotherRow = async () => {
    if (drafts.length === 0) {
      // Open one fresh draft.
      setDrafts([{ trade: "", company_name: "", headcount: 0, hours_per_person: 8, notes: "", schedule_activity_id: "" }]);
      draftIdsRef.current.push(`d_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
      setDraftsCount((n) => n + 1);
      return;
    }
    const lastIdx = drafts.length - 1;
    const last = drafts[lastIdx];
    if (last?.trade?.trim()) {
      const ok = await saveDraft(lastIdx);
      if (!ok) return;
      // Wipe drafts entirely — the saved row will appear via reload().
      setDrafts([]);
      draftIdsRef.current = [];
      setDraftsCount(0);
      // mobile: collapse the entry table back to the "Add crew" ghost —
      // the saved crew renders as a display card above.
      if (typeof window !== "undefined" && window.matchMedia("(max-width: 767.98px)").matches) setMEntryOpen(false);
    } else {
      toast.error("Type a trade first, or click the trash icon to discard the empty row.");
    }
  };
  const removeDraft = (idx) => {
    setDrafts((arr) => arr.filter((_, i) => i !== idx));
    draftIdsRef.current = draftIdsRef.current.filter((_, i) => i !== idx);
    setDraftsCount((n) => Math.max(0, n - 1));
  };

  const setDraftField = (idx, field, value) => {
    setDrafts((arr) => arr.map((d, i) => i === idx ? { ...d, [field]: value } : d));
  };

  const taskOptions = grid.map((g) => ({
    id: g.schedule_activity_id, code: g.activity_code, desc: g.activity_description,
  })).filter((t) => t.id);

  // pt.48 (2026-05-28): option builders for the manpower-log Radix dropdowns.
  // Company is restricted to this project's subs; a value already stored that
  // isn't in the (filtered) list is injected as a leading option so existing
  // rows still display it. Same injection pattern for free-text trades.
  const subNames = subs.map((s) => s.name).filter(Boolean);
  const companyOptions = (current) => {
    const opts = subNames.map((n) => ({ value: n, label: n }));
    if (current && !subNames.includes(current)) opts.unshift({ value: current, label: current });
    return opts;
  };
  const tradeOptions = (current) => {
    const opts = COMMON_TRADES.map((t) => ({ value: t, label: t }));
    if (current && !COMMON_TRADES.includes(current)) opts.unshift({ value: current, label: current });
    return opts;
  };
  const taskSelectOptions = [
    // Sid: task DESCRIPTION only in this file's dropdowns — no [activity_code]
    // prefix (the standalone code column on the desktop grid keeps the code).
    // Placeholder cleaned "— none —" -> "None".
    { value: MP_NONE, label: "None" },
    ...taskOptions.map((t) => ({ value: t.id, label: (t.desc || "").slice(0, 40) || t.code })),
  ];

  // Total column count for empty-state rowspan calc -- header below has:
  // Company, Trade, Task, Headcount, Hrs/person, Sub-total, Notes, actions = 8
  const COL_COUNT = 8;

  // MOBILE (approved mockup): saved crews render as display CARDS and the
  // editable table stays hidden until the super taps "Add crew" (or a card's
  // pencil). Desktop always shows the table (the toggle only gates a
  // max-md:hidden class). Unconditional hook -- rules of hooks safe.
  const [mEntryOpen, setMEntryOpen] = useState(false);

  return (
    <PanelShell icon={Users} title="Manpower log" testid="dr-presence-panel" count={rows.length} step={3}
      badge={
        <div className="ml-2 flex items-center gap-3">
          <span className="text-xs text-slate-500 max-md:hidden">total: {total} workers · {totalHours.toFixed(1)} hours</span>
          {/* mockup wording on phones: "N crews · N · N.N h" */}
          <span className="text-xs text-slate-500 md:hidden">{rows.length} crews · {total} · {totalHours.toFixed(1)} h</span>
        </div>
      }>
      {/* Bucket H Phase 4 -- demand-vs-actual comparison. */}
      {demandByTrade && Object.keys(demandByTrade).length > 0 && (
        <div data-testid="dr-presence-demand-vs-actual"
          className="mb-2 p-2 rounded bg-slate-50 border border-slate-200 text-xs">
          <div className="flex items-center gap-2 mb-1 text-slate-700">
            <Users className="h-3 w-3" />
            <strong>Demand vs actual today</strong>
            <Link to={`/resource-leveling?project_id=${projectId}`}
              className="ml-auto text-blue-600 hover:underline">
              Open Resource Leveling →
            </Link>
          </div>
          <table className="w-full text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="text-left">Trade</th>
                <th className="text-right">Demanded</th>
                <th className="text-right">Actual</th>
                <th className="text-right">Delta</th>
                <th className="text-left">Status</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(demandByTrade).map(([trade, info]) => {
                const actual = actualByTrade[trade] || 0;
                const delta = actual - info.demanded;
                const tone = delta >= 0 ? "text-emerald-700"
                  : delta >= -1 ? "text-amber-700"
                  : "text-rose-700 font-semibold";
                const label = delta >= 0 ? "fully staffed"
                  : delta >= -1 ? "1 short"
                  : `${Math.abs(delta)} short`;
                return (
                  <tr key={trade} data-testid={`dr-presence-trade-${trade}`} className="border-t border-slate-100">
                    <td className="py-0.5">{trade}{info.on_cp && <span className="ml-1 text-rose-600 text-xs">CP</span>}</td>
                    <td className="text-right tabular-nums">{info.demanded}</td>
                    <td className="text-right tabular-nums">{actual}</td>
                    <td className={`text-right tabular-nums ${tone}`}>{delta >= 0 ? `+${delta}` : delta}</td>
                    <td className={tone}>{label}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {/* MOBILE crew cards (approved mockup): company + trade pill, big
          hours with the workers × hours breakdown, task line + pencil.
          "Add crew" reveals the entry table below; pencil does the same
          for edits. md:hidden -> desktop unchanged. */}
      <div className="md:hidden flex flex-col gap-2 mb-2" data-testid="presence-cards-m">
        {rows.map((r) => {
          const hrs = (Number(r.headcount) || 0) * (Number(r.hours_per_person) || 0);
          const task = taskOptions.find((t) => t.id === r.schedule_activity_id);
          return (
            <div key={`m-${r.id}`} data-testid={`presence-card-${r.id}`}
              className="rounded-xl border p-3 flex flex-col gap-1.5"
              style={{ borderColor: "var(--v4-border)", background: "var(--v4-bg-surface)" }}>
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold truncate">{r.company_name || "—"}</span>
                <span className="pill pill-slate ml-auto shrink-0">{r.trade}</span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-lg font-semibold tabular-nums">{hrs.toFixed(1)}</span>
                <span className="text-xs" style={{ color: "var(--v4-fg-secondary)" }}>hrs</span>
                <span className="text-xs ml-1.5" style={{ color: "var(--v4-fg-muted)" }}>
                  {Number(r.headcount) || 0} workers × {(Number(r.hours_per_person) || 0).toFixed(1)} h
                </span>
              </div>
              <div className="flex items-center min-h-[20px]">
                {/* mobile: task NAME only — no activity_code beside it (Sid). */}
                <span className="text-xs truncate" style={{ color: "var(--v4-fg-muted)" }}>
                  {task ? `Task: ${(task.desc || "").slice(0, 40)}` : (r.notes || "")}
                </span>
                {!locked && (
                  <button type="button" data-testid={`presence-card-edit-${r.id}`}
                    onClick={() => setMEntryOpen(true)}
                    aria-label="Edit crews"
                    className="ml-auto h-8 w-8 grid place-items-center rounded shrink-0"
                    style={{ color: "var(--v4-fg-muted)" }}>
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {!locked && (
          <button type="button" data-testid="presence-add-crew-m"
            onClick={() => { if (drafts.length === 0) addAnotherRow(); setMEntryOpen(true); }}
            className="w-full rounded-xl border border-dashed py-2.5 text-sm inline-flex items-center justify-center gap-1.5"
            style={{ borderColor: "var(--v4-border-strong)", color: "var(--v4-fg-secondary)", background: "transparent" }}>
            <Plus className="h-4 w-4" /> Add crew
          </button>
        )}
      </div>
      {/* MOBILE backdrop for the crew entry sheet (Sid #5). md:hidden so
          desktop never renders it; only visible when the sheet is open. */}
      {mEntryOpen && (
        <div className="dr-msheet-ov md:hidden" data-testid="presence-crew-sheet-ov"
          onClick={() => setMEntryOpen(false)} />
      )}
      {/* The crew entry TABLE is the single source of truth for both surfaces:
          inline on desktop, and on phones it is lifted into a bottom sheet via
          .dr-crew-entry--open (CSS, mobile-only). Same draft rows + same
          addAnotherRow/setDraftField/patch handlers drive both — no dup logic.
          On phones the table is hidden until "Add crew" opens the sheet. */}
      <div className={`overflow-x-auto dr-crew-entry ${mEntryOpen ? "dr-crew-entry--open" : "max-md:hidden"}`}>
        {/* mobile sheet chrome (grip + title + close). md:hidden -> desktop unchanged. */}
        <div className="dr-crew-entry-head md:hidden">
          <div className="dr-msheet-grip" />
          <div className="dr-msheet-head">
            <div className="dr-msheet-title"><Users className="h-4 w-4 text-[#34D399] shrink-0" strokeWidth={1.8} /><span>Add crew</span></div>
            <button type="button" onClick={() => setMEntryOpen(false)} aria-label="Close"
              data-testid="presence-crew-sheet-close" className="dr-msheet-x"><X className="h-4 w-4" /></button>
          </div>
          <p className="dr-msheet-sub">Log a crew's hours and link them to an activity.</p>
        </div>
        <table className="dense-table">
          <thead>
            <tr>
              <th>Company</th>
              <th>Trade <span className="text-rose-500">*</span></th>
              <th title="Optional: link this crew to a scheduled activity on today's DR">Task</th>
              <th>Headcount</th>
              <th>Hrs/person</th>
              <th>Sub-total</th>
              <th>Notes</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {/* Persisted rows (edit-in-place). */}
            {rows.map((r) => {
              const isAuto = (r.notes || "").startsWith("Auto-created");
              return (
                <tr key={r.id} data-testid={`presence-row-${r.id}`} className={isAuto ? "bg-blue-50/30" : undefined}>
                  <td>
                    <MpSelect testid={`presence-company-${r.id}`} className="w-44"
                      value={r.company_name || ""} disabled={locked}
                      onValueChange={(v) => patch(r.id, { company_name: v })}
                      placeholder=""
                      options={companyOptions(r.company_name)} />
                  </td>
                  <td>
                    <MpSelect testid={`presence-trade-${r.id}`} className="w-36"
                      value={r.trade || ""} disabled={locked}
                      onValueChange={(v) => patch(r.id, { trade: v })}
                      placeholder=""
                      options={tradeOptions(r.trade)} />
                  </td>
                  <td>
                    <MpSelect testid={`presence-task-${r.id}`} className="min-w-[150px]"
                      value={r.schedule_activity_id || ""} disabled={locked}
                      onValueChange={(v) => patch(r.id, { schedule_activity_id: v === MP_NONE ? null : v })}
                      placeholder=""
                      options={taskSelectOptions} />
                  </td>
                  <td><input type="number" defaultValue={r.headcount} onBlur={(e) => patch(r.id, { headcount: Number(e.target.value) })} disabled={locked} className="w-20 px-2 py-1 dr-mp-input rounded text-sm tabular-nums" /></td>
                  <td><input type="number" inputMode="decimal" step="0.5" defaultValue={r.hours_per_person} onBlur={(e) => patch(r.id, { hours_per_person: Number(e.target.value) })} disabled={locked} className="w-20 px-2 py-1 dr-mp-input rounded text-sm tabular-nums" /></td>
                  <td className="tabular-nums text-sm">{(r.total_hours ?? (r.headcount || 0) * (r.hours_per_person || 0)).toFixed(1)}</td>
                  <td><input defaultValue={r.notes || ""} onBlur={(e) => patch(r.id, { notes: e.target.value })} disabled={locked} className="w-44 px-2 py-1 dr-mp-input rounded text-sm" /></td>
                  <td>
                    {!locked && <button data-testid={`presence-delete-${r.id}`} onClick={() => remove(r.id)} className="text-red-600 hover:bg-red-50 p-1 rounded" title="Remove row"><Trash2 className="h-3.5 w-3.5" /></button>}
                  </td>
                </tr>
              );
            })}
            {/* Always-visible blank draft row(s) at the end. */}
            {!locked && drafts.map((d, idx) => {
              const localId = draftIdsRef.current[idx] || `d_${idx}`;
              const subtotal = (Number(d.headcount) || 0) * (Number(d.hours_per_person) || 0);
              const isLast = idx === drafts.length - 1;
              return (
                <tr key={localId} data-testid={`presence-draft-${idx}`} className="bg-emerald-50/30">
                  <td>
                    {/* pt.48 (2026-05-28): Company is a Radix Select scoped to
                        the project's subs (see companyOptions). When the
                        project has no subs attached, show a hint pointing to
                        the project rather than an empty dropdown. */}
                    {subNames.length > 0 ? (
                      <MpSelect testid={`presence-draft-company-${idx}`} className="w-44"
                        value={d.company_name || ""}
                        onValueChange={(v) => setDraftField(idx, "company_name", v)}
                        placeholder=""
                        options={companyOptions(d.company_name)} />
                    ) : (
                      <div className="w-44">
                        <a href="/projects" target="_blank" rel="noreferrer"
                           className="block text-[11px] text-emerald-600 hover:underline"
                           data-testid="presence-add-sub-link">
                          No subs on this project — add them in the project
                        </a>
                      </div>
                    )}
                  </td>
                  <td>
                    <MpSelect testid={`presence-draft-trade-${idx}`} className="w-36"
                      value={d.trade || ""}
                      onValueChange={(v) => setDraftField(idx, "trade", v)}
                      placeholder=""
                      options={tradeOptions(d.trade)} />
                  </td>
                  <td>
                    <MpSelect testid={`presence-draft-task-${idx}`} className="min-w-[150px]"
                      value={d.schedule_activity_id || ""}
                      onValueChange={(v) => setDraftField(idx, "schedule_activity_id", v === MP_NONE ? "" : v)}
                      placeholder=""
                      options={taskSelectOptions} />
                  </td>
                  <td>
                    <input data-testid={`presence-draft-headcount-${idx}`} type="number" value={d.headcount}
                      onChange={(e) => setDraftField(idx, "headcount", e.target.value)}
                      className="w-20 px-2 py-1 dr-mp-input dr-mp-input--draft rounded text-sm tabular-nums" />
                  </td>
                  <td>
                    <input data-testid={`presence-draft-hours-${idx}`} type="number" inputMode="decimal" step="0.5" value={d.hours_per_person}
                      onChange={(e) => setDraftField(idx, "hours_per_person", e.target.value)}
                      className="w-20 px-2 py-1 dr-mp-input dr-mp-input--draft rounded text-sm tabular-nums" />
                  </td>
                  <td className="tabular-nums text-sm text-slate-500">{subtotal.toFixed(1)}</td>
                  <td>
                    <input data-testid={`presence-draft-notes-${idx}`} value={d.notes}
                      onChange={(e) => setDraftField(idx, "notes", e.target.value)}
                      placeholder=""
                      className="w-44 px-2 py-1 dr-mp-input dr-mp-input--draft rounded text-sm" />
                  </td>
                  <td>
                    <div className="flex items-center gap-1">
                      <button data-testid={`presence-draft-delete-${idx}`} onClick={() => removeDraft(idx)}
                        className="text-red-600 hover:bg-red-50 p-1 rounded" title="Discard this row">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                      {isLast && (
                        <button data-testid="presence-save-row" onClick={addAnotherRow} disabled={busySaveIdx === idx}
                          className="shrink-0 h-8 px-2 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-500 disabled:opacity-50 inline-flex items-center justify-center gap-1"
                          title="Save this row"
                          aria-label="Save row">
                          {busySaveIdx === idx
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <><CheckCircle2 className="h-3.5 w-3.5" /> Save</>}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {!rows.length && drafts.length === 0 && (
              <tr><td colSpan={COL_COUNT} className="text-center py-4 text-sm text-slate-500">No manpower logged yet — click "Add trade" below.</td></tr>
            )}
          </tbody>
        </table>
        {/* MOBILE sheet footer (Sid #5): Cancel closes the sheet; "Add crew"
            appends another draft row via the SAME addAnotherRow handler the
            desktop "Add trade" button uses. md:hidden -> desktop untouched. */}
        {!locked && (
          <div className="dr-msheet-foot md:hidden">
            <button type="button" data-testid="presence-crew-sheet-cancel"
              onClick={() => setMEntryOpen(false)} className="dr-msheet-btn-sl">Cancel</button>
            <button type="button" data-testid="presence-crew-sheet-add"
              onClick={addAnotherRow} className="sbtn sbtn-md sbtn-primary flex-1 justify-center">
              <Plus className="h-4 w-4" /> Add crew
            </button>
          </div>
        )}
      </div>
      {!locked && drafts.length === 0 && (
        <div className="mt-3 flex justify-start max-md:hidden">
          {/* pt.40: inline section button -> sbtn-sm (28px). Hidden on
              phones — the mobile "Add crew" ghost is the entry point. */}
          <button data-testid="presence-add-row" onClick={addAnotherRow}
            className="sbtn sbtn-sm sbtn-primary"
            aria-label="Add a new trade row">
            Add trade
          </button>
        </div>
      )}
    </PanelShell>
  );
}

// Phase E — Crew column on the activity grid: trade dropdown sourced from
// the Crew presence panel + an auto-filled-but-editable headcount input.
// Picking a trade sets linked_crew_id AND crew_count (server PATCH does both).
function CrewSelector({ row, presence, locked, updateRow }) {
  const onPickTrade = (e) => {
    const presenceId = e.target.value || null;
    const picked = presence.find((p) => p.id === presenceId);
    const fields = { linked_crew_id: presenceId };
    // Auto-fill headcount from picked presence row (super can override after)
    if (picked && typeof picked.headcount === "number") {
      fields.crew_count = picked.headcount;
    } else if (!presenceId) {
      // Cleared selection — leave crew_count as-is so the user doesn't lose
      // a manual override they typed earlier.
    }
    updateRow(row.id, fields);
  };
  const onChangeHeadcount = (e) => {
    updateRow(row.id, { crew_count: Number(e.target.value) });
  };
  return (
    <div className="flex flex-col gap-1 min-w-[140px]">
      <select
        data-testid={`dr-crew-trade-${row.activity_code}`}
        value={row.linked_crew_id || ""}
        onChange={onPickTrade}
        disabled={locked}
        className="h-7 text-xs px-1 border border-slate-200 rounded bg-white"
      >
        <option value="">— no crew assigned —</option>
        {presence.map((p) => (
          <option key={p.id} value={p.id}>
            {p.trade}{p.company_name ? ` — ${p.company_name}` : ""}
          </option>
        ))}
      </select>
      <input
        data-testid={`dr-crew-${row.activity_code}`}
        type="number"
        defaultValue={row.crew_count}
        key={`hc-${row.linked_crew_id || "none"}-${row.crew_count}`}
        onBlur={onChangeHeadcount}
        disabled={locked}
        title="Headcount on this activity (auto-filled from picked crew, but editable)"
        className="w-full h-6 text-xs px-1 border border-slate-200 rounded tabular-nums"
      />
    </div>
  );
}

// Bucket C3a Phase 2 — controlled constraint vocabulary; mirrors the
// backend ConstraintType Literal in models.py.
// Features F2/F3/F4 (2026-05-18) -- DR Activity Grid view layer.
//
// F2 -- the grid is split into 3 sections by the linked task's
// work_category (the F1 tag). Two paperwork tags collapse into
// Section 1; a null / unknown tag falls into Field Work so no row
// ever disappears. Only Field Work (Section 2) is editable -- the
// super logs progress there; Sections 1 & 3 are visible but
// read-only. Mirrors backend services/dr_activity_grid.section_for.
const GRID_SECTIONS = [
  { n: 1, title: "Preconstruction & Construction Paperwork",
    editable: false, empty: "No paperwork scheduled on this date." },
  { n: 2, title: "Today's planned activity",
    editable: true, empty: "No activities scheduled on this date." },
  { n: 3, title: "Closeout Paperwork",
    editable: false, empty: "No closeout paperwork scheduled on this date." },
];

function gridSectionOf(workCategory) {
  if (workCategory === "preconstruction_paperwork"
      || workCategory === "construction_paperwork") return 1;
  if (workCategory === "closeout_paperwork") return 3;
  return 2; // field_work + null / unknown -> Field Work
}

// F4 -- status is AUTO-computed (blocked > complete > in_progress >
// not_started) and shown read-only; it is no longer a manual dropdown
// (auto overrides manual). The value comes from the enriched read
// (row.auto_status); status_at_end_of_day is the stored fallback.
const STATUS_PILL_CLASS = {
  not_started: "bg-slate-100 text-slate-600 border-slate-200",
  in_progress: "bg-blue-50 text-blue-700 border-blue-200",
  blocked: "bg-rose-50 text-rose-700 border-rose-200",
  complete: "bg-emerald-50 text-emerald-700 border-emerald-200",
};

function GridStatusPill({ status, testid }) {
  const s = status || "not_started";
  return (
    <span data-testid={testid}
      className={`inline-block px-1.5 py-0.5 rounded border text-xs ${STATUS_PILL_CLASS[s] || STATUS_PILL_CLASS.not_started}`}>
      {s.replace(/_/g, " ")}
    </span>
  );
}

// F3 -- blocker badge with click-popover lives in
// components/BlockerPopover.jsx (shared with the Tasks page so both
// views render an identical UX). The local BlockerBadge that used to
// live here was display-only; it is gone now that DR uses the shared
// click-popover version.

// F2/F3/F4 -- one Activity Grid row. `editable` (true only for the
// Field Work section) decides whether the progress cells render as
// inputs or as read-only text. Status is always a read-only pill
// (F4 auto-status); the blocker badge (F3) is always shown.
// Helper hook (2026-05-26) — listens for the Activity Grid Issues
// dropdown event and, if the kind matches this panel, opens the
// inline-add form, scrolls the panel into view, focuses the first
// input, and pre-fills linked_task_ids with the activity that the
// super flagged. Returns a `consumed` ref the panel can read after
// mount to apply the prefill.
function useIssueDropdownListener(kind, panelTestid, onOpen) {
  useEffect(() => {
    const handler = (e) => {
      const d = e?.detail || {};
      if (d.kind !== kind) return;
      // Defer one frame so the form can mount before we scroll/focus.
      requestAnimationFrame(() => {
        const panel = document.querySelector(`[data-testid="${panelTestid}"]`);
        if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
        // Form may not be visible until onOpen() runs, so wait a tick.
        setTimeout(() => {
          const target = panel?.querySelector("input[data-issue-firstfield], textarea[data-issue-firstfield], select[data-issue-firstfield]");
          (target || panel?.querySelector("input,textarea,select"))?.focus();
        }, 60);
      });
      onOpen?.(d);
    };
    window.addEventListener("dr-open-event-form", handler);
    return () => window.removeEventListener("dr-open-event-form", handler);
  }, [kind, panelTestid, onOpen]);
}

// CPM B1 (2026-06-12) -- relationship-aware EOD-slider gating.
// v7 guided flow (2026-07-05): the EodSlider component moved VERBATIM to
// components/dr/EodSlider.jsx (imported at the top of this file) so the
// mobile guided flow reuses the exact same B1-gated slider. No behavior
// change on the desktop grid.

function GridRow({ r, editable, locked, staleRows, refreshStaleRow,
                   submitRowErrors, presence, updateRow, removeRow,
                   dailyReportId, reload }) {
  const delta = (r.pct_complete_end_of_day || 0) - (r.pct_complete_start_of_day || 0);
  const variance = delta < 0 ? "neg" : delta === 0 ? "zero" : "pos";
  const isUnplanned = (r.activity_code || "").startsWith("AF");
  const isStale = staleRows.has(r.id);
  const isProvisional = r.sync_state === "provisional";
  const rowErrs = submitRowErrors[r.id] || [];
  const hasErr = rowErrs.length > 0;
  const rowClass = isStale ? "bg-amber-50/40"
    : hasErr ? "bg-red-50/40" : undefined;
  // F4 -- auto_status from the enriched read; status_at_end_of_day is
  // the stored fallback for an un-enriched row.
  const status = r.auto_status || r.status_at_end_of_day || "not_started";
  return (
    <tr data-testid={`dr-row-${r.activity_code}`} className={rowClass}>
      <td className="font-mono text-xs">
        {/* user: no activity codes on phones — the name is the identity. */}
        <span className="max-md:hidden">{r.activity_code}</span>
        {isUnplanned && <span className="ml-1 px-1 rounded bg-amber-100 text-amber-700 text-xs">field</span>}
        {isProvisional && !isStale && (
          <span title="Provisional sync — confirmed on submit"
            className="ml-1 inline-block w-1.5 h-1.5 rounded-full bg-blue-400 align-middle max-md:hidden" />
        )}
        {isStale && (
          <button data-testid={`dr-row-refresh-${r.activity_code}`}
            onClick={refreshStaleRow}
            title="Schedule was updated externally — click to refresh row"
            className="ml-1 inline-flex items-center gap-1 text-xs px-1 rounded bg-amber-100 text-amber-800 border border-amber-200 hover:bg-amber-200">
            <RefreshCw className="h-2.5 w-2.5" /> refresh
          </button>
        )}
        {hasErr && (
          <div data-testid={`dr-row-err-${r.activity_code}`}
            className="mt-1 text-xs text-red-700 normal-case font-sans">
            {rowErrs.map((e, i) => <div key={i}>⚠ {e.message}</div>)}
          </div>
        )}
      </td>
      <td className="truncate max-w-[260px] max-md:whitespace-normal max-md:break-words max-md:max-w-none">{r.activity_description}</td>
      <td className="text-xs">{r.location_name || "—"}</td>
      <td className="text-xs tabular-nums text-slate-500">{r.pct_complete_start_of_day || 0}</td>
      <td>
        {editable ? (
          <EodSlider rowId={r.id} initialValue={r.pct_complete_end_of_day || 0}
            locked={locked} updateRow={updateRow} activityCode={r.activity_code}
            gate={r.slider_gate} />
        ) : (
          <span className="text-xs tabular-nums text-slate-500">{r.pct_complete_end_of_day || 0}</span>
        )}
      </td>
      <td className={`text-xs tabular-nums font-medium ${variance === "neg" ? "text-red-600" : variance === "pos" ? "text-emerald-600" : "text-slate-400"}`}>
        {delta > 0 ? "+" : ""}{delta}
      </td>
      <td><GridStatusPill status={status} testid={`dr-status-${r.activity_code}`} /></td>
      <td><BlockerPopover count={r.open_blocker_count} summary={r.open_blocker_summary} testid={`dr-blocker-${r.activity_code}`} /></td>
      <td>
        {/* Issues dropdown (2026-05-26) -- lets the super flag a new
            delay / safety / QC / RFI / delivery against THIS activity
            without scrolling. Selection emits a global custom event
            that the relevant Panel listens for: it opens the panel's
            inline-add form, scrolls into view, focuses the first
            input, and pre-fills linked_task_ids with this activity's
            schedule_activity_id. */}
        {editable && !locked && r.schedule_activity_id ? (
          <select
            data-testid={`dr-row-issue-${r.activity_code}`}
            defaultValue=""
            onChange={(e) => {
              const kind = e.target.value;
              if (!kind) return;
              window.dispatchEvent(new CustomEvent("dr-open-event-form", {
                detail: {
                  kind,
                  activityId: r.schedule_activity_id,
                  activityCode: r.activity_code,
                  activityDescription: r.activity_description,
                },
              }));
              // Reset the dropdown so the same activity can flag
              // multiple issues in a row.
              e.target.value = "";
            }}
            className="text-xs px-1.5 py-0.5 border border-slate-200 rounded bg-white"
          >
            {/* disabled+hidden: stays as the collapsed label but never
                appears as a pickable row in the opened list (it isn't a
                real event kind — user feedback). */}
            <option value="" disabled hidden>Flag issue…</option>
            <option value="delay">Log delay</option>
            <option value="safety">Log safety event</option>
            <option value="qc">Log QC issue</option>
            <option value="delivery">Log delivery</option>
            <option value="rfi">Log RFI</option>
          </select>
        ) : (
          <span className="text-xs text-slate-300">—</span>
        )}
      </td>
      <td>
        {editable && !locked && (
          <button data-testid={`dr-remove-${r.activity_code}`} onClick={() => removeRow(r.id)}
            className="text-red-600 hover:bg-red-50 p-1 rounded text-xs">remove</button>
        )}
        {editable && locked && (
          <ActivityRowLockedEditButton row={r} dailyReportId={dailyReportId} reload={reload} />
        )}
      </td>
    </tr>
  );
}

// pt.46 (2026-05-27) — read-only row for the BEFORE (preconstruction +
// construction paperwork) and AFTER (closeout paperwork) sections.
// pt.62 (2026-05-28): mirror Today's Planned Activity columns minus the
// editable/interactive ones (EOD slider, Δ, Blocker, Issues). pt.78: dropped
// the Loc column for these reference sections (paperwork has no location) —
// columns are now Code, Description, SOD %, Status. CRITICAL FIX: read the
// SAME fields the editable GridRow reads (activity_description) — the old row
// read r.description which doesn't exist on grid rows, so every reference row
// rendered a blank name. These rows are informational: other teams (PM/admin)
// update submittals & paperwork; the super just sees them for reference.
function ReadOnlyGridRow({ r }) {
  const status = r.auto_status || r.status_at_end_of_day || "not_started";
  return (
    <tr data-testid={`dr-readonly-row-${r.activity_code}`}>
      <td className="font-mono text-xs">{r.activity_code}</td>
      <td>
        <div className="text-sm whitespace-normal break-words">{r.activity_description || "—"}</div>
      </td>
      <td className="text-xs tabular-nums text-slate-500">{r.pct_complete_start_of_day || 0}</td>
      <td><GridStatusPill status={status} testid={`dr-readonly-status-${r.activity_code}`} /></td>
    </tr>
  );
}

function ActivityGridPanel({ grid, reload, dailyReportId, locked, reportDate, presence = [], submitRowErrors = {} }) {
  const [unplanned, setUnplanned] = useState({ description: "", trade_code: "", duration_days: 1 });
  // MOBILE (mockup): the unplanned-activity form hides behind a ghost
  // "+ Add unplanned activity" button; desktop always shows the form.
  const [mUnplannedOpen, setMUnplannedOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // 2026-02-XX (Abhi) -- empty sections collapse by default; the
  // user can click any section header to flip the override.
  const [sectionOpenOverride, setSectionOpenOverride] = useState({});
  // pt.78 (2026-05-29) -- the BEFORE (preconstruction/construction paperwork)
  // and AFTER (closeout paperwork) read-only reference sections collapse by
  // DEFAULT even when they have rows. Click the one-line header (end arrow) to
  // expand — same affordance as the photo-review stack.
  const [refSecOpen, setRefSecOpen] = useState({ before: false, after: false });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture
  // for activity grid rows (both planned + unplanned). Reused across the
  // two add forms below so a single reason captures both kinds.
  const [addReason, setAddReason] = useState("");
  // Bucket C1 Phase 1 — rows that the backend rejected with 409 because
  // the linked task was modified externally between grid load and PATCH.
  // Cleared by reload() (which pulls a fresh task_updated_at via /context).
  const [staleRows, setStaleRows] = useState(() => new Set());

  const refreshFromSchedule = async () => {
    setRefreshing(true);
    try {
      const { data } = await api.post(`/daily-reports/${dailyReportId}/refresh-grid`);
      toast.success(data.inserted ? `${data.inserted} activity row(s) added from schedule.` : "Already up to date.");
      setStaleRows(new Set());
      reload();
    } catch (e) { toast.error(formatErr(e, "Refresh grid")); }
    finally { setRefreshing(false); }
  };

  const updateRow = async (rowId, fields) => {
    // Bucket C1 Phase 1 — attach the optimistic-lock token captured
    // from /context. If the linked task has moved underneath us, the
    // backend rejects with 409 and we mark the row stale so the user
    // sees a "Schedule updated — refresh row" hint.
    //
    // Bucket D Addition 5 (2026-05-12) -- on a LOCKED DR, the inline
    // grid inputs route through this updater. The PATCH endpoint now
    // requires an edit_reason on locked DRs; quick inline edits won't
    // carry one, so this code path is intended for DRAFT DRs only.
    // Locked-DR edits go through the row's "edit" button which opens
    // LockedEventEditModal with the edit_reason capture (see the
    // table-row `locked && <button>edit</button>` chunk further down).
    const row = grid.find((r) => r.id === rowId) || {};
    const payload = { ...fields };
    if (row.task_updated_at) payload.task_last_modified_at = row.task_updated_at;
    try {
      await api.patch(`/daily-report-activities/${rowId}`, payload);
      // Successful write; clear any stale flag and pull fresh data.
      if (staleRows.has(rowId)) {
        const next = new Set(staleRows); next.delete(rowId); setStaleRows(next);
      }
      reload();
    } catch (e) {
      if (e.response?.status === 409) {
        const next = new Set(staleRows); next.add(rowId); setStaleRows(next);
        toast.error("Schedule was updated externally — click Refresh on the row to pull the latest before re-entering.");
      } else {
        toast.error(formatErr(e, "Update row"));
      }
    }
  };
  const refreshStaleRow = () => {
    // Easiest reliable refresh: pull /context again. Clears the
    // stale set and gets a fresh task_updated_at for every row.
    setStaleRows(new Set());
    reload();
  };
  const removeRow = (rowId) => {
    // pt.37: Tier-1 toast + Undo (was window.confirm). Activity grid
    // row is auto-populated from schedule and easy to re-add — same
    // reasoning as the manpower row above.
    deleteWithUndo({
      label: "Activity row removed from grid",
      onCommit: async () => {
        try { await api.delete(`/daily-report-activities/${rowId}`); reload(); }
        catch (e) { toast.error(formatErr(e, "Remove row")); }
      },
    });
  };
  // 2026-05-19 -- the "Add planned activity" picker was removed from
  // the UI. The grid auto-populates from the master schedule for the
  // report date (see refreshFromSchedule above + the per-DR cron in
  // services/dr_activity_grid.py), so a manual planned-row picker was
  // redundant. The handler and `picking` state are gone. Unplanned
  // adds remain -- those represent work the schedule didn't predict.
  // Bucket D Fix E -- double-click guard for unplanned-activity create.
  const [creatingUnplanned, setCreatingUnplanned] = useState(false);
  const createUnplanned = async () => {
    if (creatingUnplanned) return;
    if (!unplanned.description) return toast.error("Describe the unplanned activity first");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setCreatingUnplanned(true);
    try {
      await api.post(`/daily-reports/${dailyReportId}/unplanned-activity`, {
        ...unplanned,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setUnplanned({ description: "", trade_code: "", duration_days: 1 });
      if (locked) { setAddReason(""); toast.success("Unplanned activity created (post-submit). Audit chain captured."); }
      else toast.success("Unplanned activity created — flagged for PM review.");
      // mobile: collapse back to the ghost after a successful create.
      if (typeof window !== "undefined" && window.matchMedia("(max-width: 767.98px)").matches) setMUnplannedOpen(false);
      reload();
    } catch (e) {
      if (!handleLockedAddError(e, "activity")) {
        toast.error(formatErr(e, "Create unplanned"));
      }
    }
    finally { setCreatingUnplanned(false); }
  };

  return (
    <PanelShell icon={ClipboardList} title="Activity log" testid="dr-activity-grid" count={grid.length} step={4}
      badge={
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-slate-500">date: {formatDateDisplay(reportDate)}</span>
          {!locked && (
            <button data-testid="dr-refresh-grid" onClick={refreshFromSchedule} disabled={refreshing}
              className="h-7 px-2 rounded border border-slate-200 bg-white text-xs hover:bg-slate-50 flex items-center gap-1 disabled:opacity-50">
              <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} /> Refresh from schedule
            </button>
          )}
        </div>
      }>
      {/* v4 pt.21 (2026-05-26): Activity log redesigned to match v4
          mockup Lifecycle Track. Three sections:
          - BEFORE · REFERENCE: Preconstruction Paperwork (one-line
            read-only summary, NOT collapsible)
          - TODAY · ACTION REQUIRED: Today's Planned Activity (full
            table ALWAYS visible — supers can't accidentally collapse
            their core workflow)
          - AFTER · REFERENCE: Closeout Paperwork (one-line read-only)
          The old design had all three collapsible which hid the
          "Today's Planned" table and confused supers. */}
      {(() => {
        const sec1 = GRID_SECTIONS[0]; // Preconstruction
        const sec2 = GRID_SECTIONS[1]; // Today's Planned (editable)
        const sec3 = GRID_SECTIONS[2]; // Closeout
        const sec1Rows = grid.filter((r) => gridSectionOf(r.work_category) === 1);
        const sec2Rows = grid.filter((r) => gridSectionOf(r.work_category) === 2);
        const sec3Rows = grid.filter((r) => gridSectionOf(r.work_category) === 3);
        const sec2Reported = sec2Rows.filter((r) => (r.pct_complete_end_of_day || 0) > 0 || (r.auto_status || r.status_at_end_of_day) === "blocked").length;
        const sec2Pct = sec2Rows.length === 0 ? 0 : Math.round((sec2Reported / sec2Rows.length) * 100);
        return (
          <div className="lifecycle">
            {/* pt.46: BEFORE section.
                - Empty (no preconstruction / construction-paperwork
                  tasks scheduled today) → minimal one-line locked bar
                  (current behavior).
                - Populated → full read-only card mirroring the
                  Today's Planned Activity card structure but
                  desaturated (no green badge, no edit affordances)
                  so supers can SEE the scheduled paperwork without
                  it competing for attention with their own action
                  items. Supers don't action this — it's PMs'/admins'
                  work — but they get visibility. */}
            <div className="phase-divider">Before · Reference</div>
            {sec1Rows.length === 0 ? (
              <div className="phase-ref" data-testid="dr-grid-section-1">
                <Lock className="lock-icon" />
                <span className="title">{sec1.title}</span>
                <span className="count">· 0 rows</span>
                <span className="ref-tag">Read-Only</span>
                <span className="meta">no paperwork scheduled for today</span>
              </div>
            ) : (
              <div className="phase-readonly" data-testid="dr-grid-section-1">
                <div className="phase-readonly-head phase-readonly-toggleable" role="button" tabIndex={0}
                  data-testid="dr-grid-section-1-toggle"
                  onClick={() => setRefSecOpen((s) => ({ ...s, before: !s.before }))}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setRefSecOpen((s) => ({ ...s, before: !s.before })); } }}>
                  <Lock className="lock-icon" />
                  <span className="phase-readonly-title">{sec1.title}</span>
                  <span className="phase-readonly-count">
                    · {sec1Rows.length} {sec1Rows.length === 1 ? "row" : "rows"} scheduled
                  </span>
                  <span className="phase-readonly-tag">Read-only · for reference</span>
                  <span className="phase-readonly-toggle" aria-hidden>{refSecOpen.before ? "▴" : "▾"}</span>
                </div>
                {refSecOpen.before && (
                  <div className="phase-readonly-body">
                    <div className="overflow-x-auto">
                      <table className="dense-table">
                        <thead>
                          <tr>
                            <th>Code</th><th>Description</th>
                            <th>SOD %</th><th>Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sec1Rows.map((r) => (
                            <ReadOnlyGridRow key={r.id} r={r} />
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* pt.43: dropped "Today ·" prefix per user — just
                "Action Required" centered. */}
            <div className="phase-divider now">Action Required</div>
            <div className="phase-now" data-testid="dr-grid-section-2">
              <div className="phase-now-head">
                <span className="phase-now-badge">
                  <Pencil className="h-3 w-3" /> Your Job
                </span>
                <span className="phase-now-title">Today's Planned Activity</span>
                <span className="phase-now-count">
                  {sec2Reported} of {sec2Rows.length} {sec2Rows.length === 1 ? "row" : "rows"} reported · {sec2Pct}% complete
                </span>
              </div>
              <div className="phase-now-helper">
                Mark today's work — SOD %, EOD %, Δ, status, blockers. Submit blocks until every row has progress.
              </div>
              <div className="phase-now-body">
                <div className="overflow-x-auto">
                  <table className="dense-table">
                    <thead>
                      <tr>
                        <th>Code</th><th>Description</th><th>Loc</th>
                        <th>SOD %</th><th>EOD %</th><th>Δ</th>
                        <th>Status</th>
                        <th title="Open blockers on the linked task — open RFIs, delays, over-SLA submittals. Same count the Tasks page shows.">Blocker</th>
                        <th title="Flag a new delay, safety event, QC issue, RFI, or delivery against this activity. Selecting jumps to that section.">Issues</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {sec2Rows.length === 0 ? (
                        <tr>
                          <td colSpan="10" data-testid="dr-grid-section-2-empty"
                            className="text-center py-4 text-xs italic" style={{ color: "var(--v4-fg-faint)" }}>
                            {sec2.empty}
                          </td>
                        </tr>
                      ) : sec2Rows.map((r) => (
                        <GridRow key={r.id} r={r} editable={true}
                          locked={locked} staleRows={staleRows} refreshStaleRow={refreshStaleRow}
                          submitRowErrors={submitRowErrors} presence={presence}
                          updateRow={updateRow} removeRow={removeRow}
                          dailyReportId={dailyReportId} reload={reload} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            {/* pt.46: AFTER section — same pattern as BEFORE.
                Empty → minimal locked bar. Populated → read-only
                card showing the closeout paperwork scheduled today. */}
            <div className="phase-divider">After · Reference</div>
            {sec3Rows.length === 0 ? (
              <div className="phase-ref" data-testid="dr-grid-section-3">
                <Lock className="lock-icon" />
                <span className="title">{sec3.title}</span>
                <span className="count">· 0 rows</span>
                <span className="ref-tag">Read-Only</span>
                <span className="meta">no closeout paperwork scheduled for today</span>
              </div>
            ) : (
              <div className="phase-readonly" data-testid="dr-grid-section-3">
                <div className="phase-readonly-head phase-readonly-toggleable" role="button" tabIndex={0}
                  data-testid="dr-grid-section-3-toggle"
                  onClick={() => setRefSecOpen((s) => ({ ...s, after: !s.after }))}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setRefSecOpen((s) => ({ ...s, after: !s.after })); } }}>
                  <Lock className="lock-icon" />
                  <span className="phase-readonly-title">{sec3.title}</span>
                  <span className="phase-readonly-count">
                    · {sec3Rows.length} {sec3Rows.length === 1 ? "row" : "rows"} scheduled
                  </span>
                  <span className="phase-readonly-tag">Read-only · for reference</span>
                  <span className="phase-readonly-toggle" aria-hidden>{refSecOpen.after ? "▴" : "▾"}</span>
                </div>
                {refSecOpen.after && (
                  <div className="phase-readonly-body">
                    <div className="overflow-x-auto">
                      <table className="dense-table">
                        <thead>
                          <tr>
                            <th>Code</th><th>Description</th>
                            <th>SOD %</th><th>Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sec3Rows.map((r) => (
                            <ReadOnlyGridRow key={r.id} r={r} />
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })()}

      <div className="mt-4 pt-3 border-t border-slate-100 space-y-3">
        {locked && (
          <LockedAddReasonBlock value={addReason} onChange={setAddReason}
            eventLabel="an activity row" testid="activity-add-reason" />
        )}
        {/* UNPLANNED ADD only -- the planned-activity picker was
            removed (2026-05-19) since the grid auto-populates from
            the schedule. Unplanned activities still need a manual
            entry path because the schedule by definition didn't
            anticipate them. */}
        {/* MOBILE: ghost affordance (mockup); tapping opens the bottom sheet. */}
        {!locked && (
          <button type="button" data-testid="unplanned-ghost-m"
            onClick={() => setMUnplannedOpen(true)}
            className="md:hidden w-full rounded-xl border border-dashed py-2.5 text-sm inline-flex items-center justify-center gap-1.5"
            style={{ borderColor: "var(--v4-border-strong)", color: "var(--v4-fg-secondary)", background: "transparent" }}>
            <Plus className="h-4 w-4" /> Add unplanned activity
          </button>
        )}
        {/* MOBILE backdrop for the unplanned-activity sheet (Sid #5). */}
        {mUnplannedOpen && (
          <div className="dr-msheet-ov md:hidden" data-testid="unplanned-sheet-ov"
            onClick={() => setMUnplannedOpen(false)} />
        )}
        {/* Same amber form fields + same createUnplanned handler drive desktop
            inline AND the mobile bottom sheet (dr-crew-entry--open lifts it on
            phones; desktop keeps the inline box). No duplicated save logic. */}
        <div className={`bg-amber-50 border border-amber-200 rounded p-3 space-y-2 dr-crew-entry ${mUnplannedOpen ? "dr-crew-entry--open" : "max-md:hidden"}`}>
          {/* mobile sheet chrome */}
          <div className="dr-crew-entry-head md:hidden">
            <div className="dr-msheet-grip" />
            <div className="dr-msheet-head">
              <div className="dr-msheet-title"><Wand2 className="h-4 w-4 text-[#34D399] shrink-0" strokeWidth={1.8} /><span>Add unplanned activity</span></div>
              <button type="button" onClick={() => setMUnplannedOpen(false)} aria-label="Close"
                data-testid="unplanned-sheet-close" className="dr-msheet-x"><X className="h-4 w-4" /></button>
            </div>
            <p className="dr-msheet-sub">Work done today that wasn't on the plan.</p>
          </div>
          <div className="text-xs font-medium text-amber-800 flex items-center gap-1 max-md:hidden"><Wand2 className="h-3 w-3" /> Create unplanned activity (flagged for PM review)</div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <input data-testid="unplanned-desc" placeholder="What was done?" value={unplanned.description} onChange={(e) => setUnplanned({ ...unplanned, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-amber-200 text-sm bg-white" />
            <input data-testid="unplanned-trade" placeholder="Trade" value={unplanned.trade_code} onChange={(e) => setUnplanned({ ...unplanned, trade_code: e.target.value })} className="h-8 px-2 rounded border border-amber-200 text-sm bg-white" />
          </div>
          {/* desktop inline add button (mobile uses the sheet footer below). */}
          <button data-testid="unplanned-add" onClick={createUnplanned}
            disabled={creatingUnplanned || (locked && !isAddReasonValid(addReason))}
            title={locked && !isAddReasonValid(addReason)
              ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
              : undefined}
            className="sbtn sbtn-sm sbtn-primary max-md:hidden">
            {creatingUnplanned ? "Creating…" : (locked ? "Create unplanned (post-submit)" : "Create unplanned activity")}
          </button>
          {/* MOBILE sheet footer: Cancel + primary Add (same createUnplanned). */}
          <div className="dr-msheet-foot md:hidden">
            <button type="button" data-testid="unplanned-sheet-cancel"
              onClick={() => setMUnplannedOpen(false)} className="dr-msheet-btn-sl">Cancel</button>
            <button type="button" data-testid="unplanned-sheet-add" onClick={createUnplanned}
              disabled={creatingUnplanned}
              className="sbtn sbtn-md sbtn-primary flex-1 justify-center">
              {creatingUnplanned ? "Adding…" : (<><Plus className="h-4 w-4" /> Add</>)}
            </button>
          </div>
        </div>
      </div>
    </PanelShell>
  );
}

function RenovationPanel({ report, dailyReportId, locked, reload }) {
  const patch = async (fields) => {
    try { await api.patch(`/daily-reports/${dailyReportId}`, fields); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Update failed"); }
  };
  return (
    <PanelShell icon={ShieldAlert} title="Renovation / occupied building" testid="dr-renovation-panel">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <CheckField label="Building still occupied during work" value={!!report.occupancy_active} onChange={(v) => patch({ occupancy_active: v })} disabled={locked} testid="ren-occupancy" />
        <CheckField label="Dust / containment controls in place" value={!!report.dust_controls_in_place} onChange={(v) => patch({ dust_controls_in_place: v })} disabled={locked} testid="ren-dust" />
        <CheckField label="After-hours work (noise / vibration window)" value={!!report.after_hours_work} onChange={(v) => patch({ after_hours_work: v })} disabled={locked} testid="ren-after-hours" />
        <Field label="Occupied areas active today" value={report.occupied_areas || ""} onChange={(v) => patch({ occupied_areas: v })} disabled={locked} testid="ren-occupied-areas" />
        <Field label="Noise complaints" type="number" value={report.noise_complaints_count ?? 0} onChange={(v) => patch({ noise_complaints_count: Number(v) })} disabled={locked} testid="ren-noise-count" />
        <Field label="Dust violations" type="number" value={report.dust_violations_count ?? 0} onChange={(v) => patch({ dust_violations_count: Number(v) })} disabled={locked} testid="ren-dust-count" />
      </div>
    </PanelShell>
  );
}

// Bucket D Addition 5 (2026-05-12) -- per-row Edit button on the
// activity grid when the parent DR is locked. Renders an amber
// "edit" link that opens LockedEventEditModal targeting
// /daily-report-activities/{rowId} with the SOD%/EOD%/hours/status/
// notes/constraint fields. The modal already handles the edit_reason
// prompt + 403 needs_request flip into edit-request mode. Backend
// PATCH fires ACTIVITY_GRID_EDITED_POST_SUBMIT (instead of
// EVENT_EDITED_POST_SUBMIT) so counsel can filter grid edits
// separately in the Audit Log dropdown.
function ActivityRowLockedEditButton({ row, dailyReportId, reload }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button data-testid={`dr-row-edit-locked-${row.activity_code}`}
        onClick={() => setOpen(true)}
        className="text-amber-700 hover:bg-amber-50 p-1 rounded text-xs">
        edit
      </button>
      {/* Post-submit grid-row edit modal. Features F3/F4 (2026-05-18):
          the Constraint field was removed (the Constraints column is
          gone -- redundant with the F3 blocker badge) and the Status
          field was removed (status is now F4 auto-computed from
          blockers + EOD%, so a manual pick would just be overridden).
          The editable post-submit field set is SOD% / EOD% /
          notes; editing EOD% re-derives the row's auto-status on the
          backend PATCH. Δ stays excluded -- it's derived (EOD% - SOD%),
          not patchable. */}
      <LockedEventEditModal
        open={open} onOpenChange={setOpen}
        eventType="activity" eventId={row.id} dailyReportId={dailyReportId}
        patchUrl={`/daily-report-activities/${row.id}`}
        title={`Edit grid row [${row.activity_code}] (post-submit)`}
        fields={[
          { key: "pct_complete_start_of_day", label: "SOD %", type: "number" },
          { key: "pct_complete_end_of_day", label: "EOD %", type: "number" },
          { key: "notes", label: "Notes", type: "textarea" },
        ]}
        currentValues={{
          pct_complete_start_of_day: row.pct_complete_start_of_day,
          pct_complete_end_of_day: row.pct_complete_end_of_day,
          notes: row.notes,
        }}
        onSaved={reload}
      />
    </>
  );
}

// ------------------------------------------------------------
//  Events tabbed card (2026-02-XX)
//
//  Consolidates Delays · Safety · QC · RFIs · Deliveries · Notes
//  into a single tabbed surface so the super can land on the right
//  event surface in one click instead of scrolling ~1500px. Each
//  underlying panel keeps its full form / linking / audit behavior
//  intact -- this is a STRUCTURAL change only, no data is removed.
//
//  Activity Grid "Issues" dropdown integration:
//   - The grid emits `dr-open-event-form` with a `kind` (delay /
//     safety / qc / rfi / delivery).
//   - We intercept first; switch the active tab to the matching
//     surface; then RE-DISPATCH the event with __replayed=true so
//     the freshly-mounted panel's existing scroll/focus/prelink
//     listener picks it up after React commits. Re-dispatch is
//     filtered by __replayed so we don't loop.
//
//  Inline "+ Add" button on the tab strip:
//   - Dispatches the same `dr-open-event-form` event so every panel
//     reacts identically to the grid-driven flow. Notes is special-
//     cased (no event) -- focuses the notes textarea instead.
// ------------------------------------------------------------
function EventsTabsCard({ delays, safetyEvents, qcIssues, rfis, deliveries,
                          dailyReportId, projectId, reportDate, locked, reload, grid, report }) {
  // Mobile unified-feed redesign (approved mockup): phones start with NO
  // tab selected -- the feed below the chips is the default view, and
  // tapping a chip opens that kind's add/list panel. Desktop keeps the
  // original "delays" initial tab.
  const [activeTab, setActiveTab] = useState(() => (
    (typeof window !== "undefined" && window.matchMedia("(max-width: 767.98px)").matches)
      ? "" : "delays"
  ));
  const [pendingDetail, setPendingDetail] = useState(null);

  useEffect(() => {
    const KIND_TO_TAB = {
      delay: "delays", safety: "safety", qc: "qc",
      rfi: "rfis", delivery: "deliveries",
    };
    const handler = (e) => {
      const detail = e?.detail || {};
      if (detail.__replayed) return;
      const target = KIND_TO_TAB[detail.kind];
      if (!target) return;
      if (target !== activeTab) {
        setActiveTab(target);
        setPendingDetail(detail);
      }
    };
    window.addEventListener("dr-open-event-form", handler);
    return () => window.removeEventListener("dr-open-event-form", handler);
  }, [activeTab]);

  useEffect(() => {
    if (!pendingDetail) return;
    // Wait one tick so the newly-active TabsContent mounts and the
    // panel's useIssueDropdownListener is registered before we
    // re-fire the event.
    const t = setTimeout(() => {
      window.dispatchEvent(new CustomEvent("dr-open-event-form", {
        detail: { ...pendingDetail, __replayed: true },
      }));
      setPendingDetail(null);
    }, 30);
    return () => clearTimeout(t);
  }, [pendingDetail, activeTab]);

  const TABS = [
    { value: "delays",     label: "Delays",     icon: AlertTriangle,  count: (delays || []).length },
    { value: "safety",     label: "Safety",     icon: ShieldAlert,    count: (safetyEvents || []).length },
    { value: "qc",         label: "QC",         icon: ClipboardCheck, count: (qcIssues || []).length },
    { value: "rfis",       label: "RFIs",       icon: MessageSquare,  count: (rfis || []).length },
    { value: "deliveries", label: "Deliveries", icon: Truck,          count: (deliveries || []).length },
    { value: "notes",      label: "Notes",      icon: ClipboardList,  count: null },
  ];

  const eventsTotal = (delays || []).length + (safetyEvents || []).length
    + (qcIssues || []).length + (rfis || []).length + (deliveries || []).length;

  // MOBILE (user request): after a successful log of ANY kind the open
  // panel/form closes and the page returns to chips + feed. Detected as an
  // events-count increase while a tab is open — one hook covers all five
  // kind panels without touching their internals. Desktop unaffected.
  const prevEventsTotalRef = useRef(eventsTotal);
  useEffect(() => {
    const prev = prevEventsTotalRef.current;
    prevEventsTotalRef.current = eventsTotal;
    if (eventsTotal > prev && activeTab
        && typeof window !== "undefined"
        && window.matchMedia("(max-width: 767.98px)").matches) {
      setActiveTab("");
    }
  }, [eventsTotal, activeTab]);

  // Mobile unified feed (approved mockup) -- every event kind merged into
  // one newest-first card list. Desktop never renders it (md:hidden), and
  // the arrays are small (<=200 rows each), so plain recompute is fine.
  const feedTime = (raw) => {
    if (!raw) return { ts: null, timeLabel: "" };
    const ms = Date.parse(raw);
    if (!Number.isFinite(ms)) return { ts: null, timeLabel: String(raw) };
    // Date-only strings ("2026-06-30") render as-is; datetimes as h:mm.
    const hasClock = /[T ]\d{2}:/.test(String(raw));
    return {
      ts: ms,
      timeLabel: hasClock
        ? new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
        : String(raw),
    };
  };
  const feedItems = [
    ...(delays || []).map((r) => ({
      key: `delay-${r.id}`, kindLabel: "Delay", kindPill: "pill-amber",
      raw: r.created_at || r.reported_at,
      title: r.description,
      meta: [r.severity, (r.status || "open").replace(/_/g, " "), r.reason_category]
        .filter(Boolean).join(" · "),
    })),
    ...(safetyEvents || []).map((r) => ({
      key: `safety-${r.id}`, kindLabel: "Safety", kindPill: "pill-red",
      raw: r.created_at || r.reported_at,
      title: r.description,
      meta: [(r.event_type || "").replace(/_/g, " "), r.severity].filter(Boolean).join(" · "),
    })),
    ...(qcIssues || []).map((r) => ({
      key: `qc-${r.id}`, kindLabel: "QC", kindPill: "pill-blue",
      raw: r.created_at || r.reported_at,
      title: r.description,
      meta: [(r.severity || "").replace(/_/g, " "), (r.status || "open").replace(/_/g, " ")]
        .filter(Boolean).join(" · "),
    })),
    ...(rfis || []).map((r) => ({
      key: `rfi-${r.id}`, kindLabel: "RFI", kindPill: "pill-blue",
      raw: r.created_at || r.date_initiated,
      title: r.subject,
      meta: [r.rfi_number, r.priority, r.status].filter(Boolean).join(" · "),
    })),
    ...(deliveries || []).map((r) => ({
      key: `delivery-${r.id}`, kindLabel: "Delivery", kindPill: "pill-slate",
      raw: r.created_at || r.delivery_date,
      title: r.description || r.vendor,
      meta: [r.vendor, r.status].filter(Boolean).join(" · "),
    })),
    ...((report?.notes || "").trim()
      ? [{ key: "notes", kindLabel: "Note", kindPill: "pill-slate", raw: null,
           title: report.notes, meta: "" }]
      : []),
  ]
    .map((it, i) => ({ ...it, ...feedTime(it.raw), order: i }))
    .sort((a, b) => {
      // Newest first; items without a parseable timestamp keep their
      // original order at the end.
      if (a.ts == null && b.ts == null) return a.order - b.order;
      if (a.ts == null) return 1;
      if (b.ts == null) return -1;
      return b.ts - a.ts;
    });

  return (
    <PanelShell icon={AlertTriangle}
      title={<><span className="max-md:hidden">Events</span><span className="md:hidden">Issues</span></>}
      count={eventsTotal} testid="dr-events-tabs">
      {/* MOBILE (Sid #5): tapping an issue chip opens that kind's add-form as a
          bottom-sheet dialog. The panel already auto-opens its form on mount
          and auto-closes (activeTab→"") after a successful log, so this only
          adds the sheet CHROME — a dim backdrop + a close control — while
          reusing every existing panel handler. Desktop (md:hidden) unchanged.
          The active TabsContent is lifted into the sheet via CSS when the
          card carries data-issue-sheet="open". */}
      {activeTab && (
        <div className="dr-msheet-ov md:hidden" data-testid="dr-issue-sheet-ov"
          onClick={() => setActiveTab("")} />
      )}
      {activeTab && (
        <button type="button" onClick={() => setActiveTab("")} aria-label="Close"
          data-testid="dr-issue-sheet-close" className="dr-issue-sheet-close md:hidden">
          <X className="h-4 w-4" />
        </button>
      )}
      <Tabs value={activeTab} onValueChange={setActiveTab}
        data-issue-sheet={activeTab ? "open" : "closed"}>
        <div className="surface p-2 flex items-center gap-2 flex-wrap">
        <TabsList className="dr-events-tabs flex flex-wrap h-auto p-1 gap-1 max-md:grid max-md:grid-cols-3 max-md:gap-2">
            {TABS.map((t) => {
              const Icon = t.icon;
              return (
                <TabsTrigger key={t.value} value={t.value}
                  data-testid={`dr-events-tab-${t.value}`}
                  className="dr-events-tab gap-1.5 px-3 max-md:px-1.5 max-md:gap-1 max-md:text-xs max-md:justify-center">
                  {/* icon hidden on phones so the 3-col grid fits label + count
                      without cramping (real-device feedback). */}
                  <Icon className="h-3.5 w-3.5 max-md:hidden" strokeWidth={1.8} />
                  <span className="truncate">{t.label}</span>
                  {typeof t.count === "number" && (
                    <span className="dr-events-tab-count ml-1 text-[10px] font-medium px-1.5 py-0.5 rounded">
                      {t.count}
                    </span>
                  )}
                </TabsTrigger>
              );
            })}
        </TabsList>
        {/* Mobile-only unified feed (w-full drops it below the chip grid
            inside this flex-wrap row; hidden entirely on md+). */}
        {feedItems.length > 0 && (
          <div className="md:hidden w-full flex flex-col gap-2 mt-2" data-testid="dr-events-feed-m">
            {feedItems.map((it) => (
              <div key={it.key} className="rounded-xl border p-3 flex flex-col gap-1.5"
                style={{ borderColor: "var(--v4-border)", background: "var(--v4-bg-surface)" }}>
                <div className="flex items-center gap-2">
                  <span className={`pill ${it.kindPill}`}>{it.kindLabel}</span>
                  <span className="text-xs ml-auto" style={{ color: "var(--v4-fg-muted)" }}>{it.timeLabel}</span>
                </div>
                <div className="text-sm">{it.title}</div>
                {it.meta && (
                  <div className="text-xs flex items-center gap-2" style={{ color: "var(--v4-fg-muted)" }}>
                    <span className="min-w-0 truncate">{it.meta}</span>
                    <Pencil className="h-3.5 w-3.5 ml-auto shrink-0 opacity-60" aria-hidden="true" />
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        </div>
        <TabsContent value="delays" className="mt-3" data-testid="dr-events-tab-content-delays">
          <DelaysPanel rows={delays} dailyReportId={dailyReportId} projectId={projectId}
                       reportDate={reportDate} locked={locked} reload={reload} />
        </TabsContent>
        <TabsContent value="safety" className="mt-3" data-testid="dr-events-tab-content-safety">
          <SafetyPanel rows={safetyEvents} dailyReportId={dailyReportId} projectId={projectId}
                       reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </TabsContent>
        <TabsContent value="qc" className="mt-3" data-testid="dr-events-tab-content-qc">
          <QcPanel rows={qcIssues} dailyReportId={dailyReportId} projectId={projectId}
                   reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </TabsContent>
        <TabsContent value="rfis" className="mt-3" data-testid="dr-events-tab-content-rfis">
          <RfisPanel rows={rfis} dailyReportId={dailyReportId} projectId={projectId}
                     reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </TabsContent>
        <TabsContent value="deliveries" className="mt-3" data-testid="dr-events-tab-content-deliveries">
          <DeliveriesPanel rows={deliveries} dailyReportId={dailyReportId} projectId={projectId}
                           reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </TabsContent>
        <TabsContent value="notes" className="mt-3" data-testid="dr-events-tab-content-notes">
          <NotesPanel report={report} dailyReportId={dailyReportId} locked={locked} reload={reload} />
        </TabsContent>
      </Tabs>
    </PanelShell>
  );
}


// ---------- Inline event panels (delays / safety / qc / rfis / deliveries) ----------
function DelaysPanel({ rows, dailyReportId, projectId, reportDate, locked, reload }) {
  // MOBILE (user: chip tap should open the form directly, no extra click):
  // the panel mounts when its chip activates the tab, so an open-on-mobile
  // initializer IS the one-tap flow. Locked DRs keep the button (the
  // post-submit reason flow stays deliberate). Desktop starts closed as
  // before. After the log-and-auto-close, the next chip tap remounts the
  // panel and the form opens fresh.
  const [showForm, setShowForm] = useState(() => (
    typeof window !== "undefined"
    && window.matchMedia("(max-width: 767.98px)").matches
    && !locked
  ));
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // 2026-05-26 -- Activity Grid Issues dropdown integration.
  const [prelinkedTaskId, setPrelinkedTaskId] = useState(null);
  useIssueDropdownListener("delay", "dr-delays-panel", (detail) => {
    setShowForm(true);
    setPrelinkedTaskId(detail.activityId || null);
  });

  return (
    <PanelShell icon={AlertTriangle} title="Delays" testid="dr-delays-panel" count={rows.length}>
      {rows.map((r) => (
        <DelayRowInline key={r.id} delay={r} reload={reload}
          locked={locked} dailyReportId={dailyReportId} />
      ))}
      {!rows.length && <div data-testid="delays-empty" className="text-xs text-slate-400 py-2"><span className="max-md:hidden">Nothing logged today — say so by clicking + at right.</span><span className="md:hidden">Nothing logged today — add one with the + chips above.</span></div>}
      <div className="mt-3 pt-3 border-t border-slate-100">
        {!showForm ? (
          <button data-testid="delay-add-open" onClick={() => setShowForm(true)}
            className="sbtn sbtn-sm sbtn-primary">
            {locked ? "Log a delay (post-submit)" : "Log a delay"}
          </button>
        ) : (
          <div className="space-y-2">
            {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
              eventLabel="a delay" testid="delay-add-reason" />}
            <DelayForm
              projectId={projectId}
              reportDate={reportDate}
              dailyReportId={dailyReportId}
              isLocked={locked}
              lockedAddReason={locked ? addReason : null}
              prelinkedTaskId={prelinkedTaskId}
              onSaved={() => {
                setShowForm(false);
                setAddReason("");
                setPrelinkedTaskId(null);
                reload();
                toast.success(locked ? "Delay logged (post-submit). Audit chain captured." : "Delay logged");
              }}
              onCancel={() => { setShowForm(false); setAddReason(""); setPrelinkedTaskId(null); }}
            />
          </div>
        )}
      </div>
    </PanelShell>
  );
}

function DelayRowInline({ delay, reload, locked, dailyReportId }) {
  const [resolving, setResolving] = useState(false);
  const [resolveDate, setResolveDate] = useState(localTodayISO());
  const [resolveNote, setResolveNote] = useState("");
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);

  const resolve = async () => {
    try {
      await api.patch(`/delays/${delay.id}`, {
        status: "resolved",
        resolved_date: resolveDate,
        resolved_note: resolveNote || null,
      });
      setResolving(false);
      reload();
      toast.success("Delay marked resolved");
    } catch (e) { toast.error(formatErr(e, "Resolve delay")); }
  };

  const sevColor = delay.severity === "critical" || delay.severity === "major" ? "pill-red" : "pill-slate";
  const statusColor = delay.status === "resolved" ? "pill-green" : delay.status === "in_progress" ? "pill-amber" : "pill-slate";
  const linkedCount = (delay.linked_task_ids || []).length;
  return (
    <div data-testid={`delay-row-${delay.id}`} className="border-b border-slate-100 py-2 text-xs space-y-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={`pill ${sevColor}`}>{delay.severity}</span>
        <span className={`pill ${statusColor}`}>{(delay.status || "open").replace(/_/g, " ")}</span>
        {delay.reason_category && <span className="text-slate-400">{delay.reason_category}</span>}
        {linkedCount > 0 && <span className="text-slate-500">· {linkedCount} task{linkedCount === 1 ? "" : "s"}</span>}
        {delay.duration_days != null && <span className="text-slate-500">· {delay.duration_days}d open</span>}
        <SupplementBadge row={delay} />
        {delay.status !== "resolved" && (
          <button data-testid={`delay-resolve-open-${delay.id}`} onClick={() => setResolving((v) => !v)}
            className="ml-auto text-blue-600 hover:underline">
            {resolving ? "cancel" : "mark resolved"}
          </button>
        )}
        {locked && (
          <button data-testid={`delay-edit-locked-${delay.id}`} onClick={() => setEditOpen(true)}
            className={`${delay.status !== "resolved" ? "ml-2" : "ml-auto"} text-amber-700 hover:underline`}>
            edit
          </button>
        )}
        {delay.post_submit_edited && (
          <span className="text-xs text-amber-700"
            title={`Last edit: ${(delay.last_edit_reason || "").slice(0, 80)}`}>
            (edited {delay.edit_count || 0}× post-submit)
          </span>
        )}
      </div>
      <div className="text-slate-700 whitespace-pre-wrap">{delay.description}</div>
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="delay" eventId={delay.id} dailyReportId={dailyReportId}
          patchUrl={`/delays/${delay.id}`}
          fields={[
            { key: "description", label: "Description", type: "textarea" },
            { key: "severity", label: "Severity", type: "select",
              options: [
                { value: "minor", label: "minor" }, { value: "moderate", label: "moderate" },
                { value: "major", label: "major" }, { value: "critical", label: "critical" },
              ] },
            { key: "reason_category", label: "Reason category" },
            { key: "owner_attribution", label: "Owner attribution", type: "select",
              options: [
                { value: "none", label: "none" }, { value: "owner", label: "owner" },
                { value: "gc", label: "gc" }, { value: "design", label: "design" },
                { value: "force_majeure", label: "force majeure" },
              ] },
          ]}
          currentValues={{
            description: delay.description, severity: delay.severity,
            reason_category: delay.reason_category, owner_attribution: delay.owner_attribution,
          }}
          onSaved={reload}
        />
      )}
      {resolving && (
        <div className="mt-2 p-2 bg-slate-50 rounded grid grid-cols-1 sm:grid-cols-2 gap-2">
          <input type="date" value={resolveDate} onChange={(e) => setResolveDate(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <input type="text" placeholder="Resolution note (optional)" value={resolveNote}
            onChange={(e) => setResolveNote(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <button data-testid={`delay-resolve-confirm-${delay.id}`} onClick={resolve}
            className="col-span-2 h-7 max-md:min-h-[44px] px-3 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-700">
            Mark resolved
          </button>
        </div>
      )}
    </div>
  );
}

function ReminderBanner({ reminders, reload }) {
  if (!reminders?.length) return null;
  return (
    <div data-testid="dr-reminder-banner" className="surface p-4 mb-6 border-l-4 border-amber-400 bg-amber-50/40">
      <div className="flex items-center gap-2 mb-2 max-md:flex-wrap">
        <AlertTriangle className="h-4 w-4 text-amber-600" />
        <div className="font-medium text-sm text-amber-900">Open delay reminders</div>
        <span className="text-xs text-amber-700">({reminders.length})</span>
        <span className="text-xs text-slate-500 ml-auto max-md:hidden">delays from earlier days that touch tasks scheduled today</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {reminders.map((r) => (
          <ReminderCard key={r.id} delay={r} reload={reload} />
        ))}
      </div>
    </div>
  );
}

function ReminderCard({ delay, reload }) {
  const [resolving, setResolving] = useState(false);
  const [resolveDate, setResolveDate] = useState(localTodayISO());
  const [resolveNote, setResolveNote] = useState("");

  const resolve = async () => {
    try {
      await api.patch(`/delays/${delay.id}`, {
        status: "resolved",
        resolved_date: resolveDate,
        resolved_note: resolveNote || null,
      });
      setResolving(false);
      reload();
      toast.success("Delay marked resolved");
    } catch (e) { toast.error(formatErr(e, "Resolve delay")); }
  };

  const sevColor = delay.severity === "critical" || delay.severity === "major" ? "pill-red" : "pill-slate";
  const linked = delay.linked_task_ids || [];
  const firstLine = (delay.description || "").split("\n")[0].slice(0, 140);
  return (
    <div data-testid={`reminder-card-${delay.id}`} className="bg-white rounded border border-amber-200 p-2 text-xs space-y-1">
      <div className="flex items-center gap-1.5">
        <span className={`pill ${sevColor}`}>{delay.severity}</span>
        {delay.reason_category && <span className="text-slate-400">{delay.reason_category}</span>}
        {delay.duration_days != null && <span className="text-slate-500">· {delay.duration_days}d open</span>}
        {!resolving && (
          <button data-testid={`reminder-resolve-open-${delay.id}`} onClick={() => setResolving(true)}
            className="ml-auto text-blue-600 hover:underline">mark resolved</button>
        )}
      </div>
      <div className="text-slate-800 truncate">{firstLine}</div>
      {linked.length > 0 && (
        <div className="text-xs text-slate-500">tasks: {linked.length} linked</div>
      )}
      {resolving && (
        <div className="mt-1 p-2 bg-amber-50 rounded grid grid-cols-2 gap-2">
          <input type="date" value={resolveDate} onChange={(e) => setResolveDate(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <input type="text" placeholder="Resolution note" value={resolveNote}
            onChange={(e) => setResolveNote(e.target.value)}
            className="h-7 px-2 border border-slate-200 rounded text-xs" />
          <button onClick={() => setResolving(false)} className="h-7 px-2 rounded border border-slate-200 text-xs">Cancel</button>
          <button data-testid={`reminder-resolve-confirm-${delay.id}`} onClick={resolve}
            className="h-7 px-2 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-700">Save</button>
        </div>
      )}
    </div>
  );
}

function SafetyPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Phase C — simplified: description + type + severity. OSHA recordable
  // checkbox removed per spec.
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) picker. OPTIONAL for safety per spec (some events area-wide);
  // empty list still saves.
  const [draft, setDraft] = useState({
    // 2026-05-18 -- no pre-selected defaults for event_type/severity.
    // Forces super to consciously pick before saving (validation
    // gate in create() below).
    description: "", severity: "", event_type: "",
    linked_task_ids: [],
  });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  // 2026-05-26 -- Activity Grid Issues dropdown integration.
  useIssueDropdownListener("safety", "dr-safety-panel", (detail) => {
    setDraft((d) => {
      const next = { ...d };
      if (detail.description) next.description = detail.description;
      if (detail.severity) next.severity = detail.severity;
      if (detail.activityId && !d.linked_task_ids.includes(detail.activityId)) {
        next.linked_task_ids = [...d.linked_task_ids, detail.activityId];
      }
      return next;
    });
  });
  // Phase D — copilot can push a drafted description into this form.
  useEffect(() => subscribeDescriptionInsert(({ target, text }) => {
    if (target !== "safety" || !text) return;
    setDraft((d) => ({ ...d, description: d.description ? `${d.description}\n\n${text}` : text }));
  }), []);
  const create = async () => {
    if (busy) return;
    if (!draft.description) return toast.error("Description required");
    if (!draft.event_type) return toast.error("Event type is required");
    if (!draft.severity) return toast.error("Severity is required");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      await api.post(`/safety-events`, {
        project_id: projectId, daily_report_id: dailyReportId, date: reportDate,
        description: draft.description, severity: draft.severity, event_type: draft.event_type,
        linked_task_ids: draft.linked_task_ids,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setDraft({ description: "", severity: "", event_type: "",
                  linked_task_ids: [] });
      setAddReason("");
      reload();
      if (locked) toast.success("Safety event added (post-submit). Audit chain captured.");
    } catch (e) {
      if (!handleLockedAddError(e, "safety")) {
        toast.error(formatErr(e, "Create safety event"));
      }
    }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={ShieldAlert} title="Safety events" testid="dr-safety-panel" count={rows.length}>
      {rows.map((r) => (
        <SafetyRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div data-testid="safety-empty" className="text-xs text-slate-400 py-2"><span className="max-md:hidden">Nothing logged today — say so by clicking + at right.</span><span className="md:hidden">Nothing logged today — add one with the + chips above.</span></div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="safety-form">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a safety event" testid="safety-add-reason" />}
        <textarea data-testid="safety-new-desc" rows={4} placeholder="Describe the event"
          value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          className="w-full p-2 rounded border border-slate-200 text-sm" />
        <TaskPicker projectId={projectId}
          value={draft.linked_task_ids}
          onChange={(v) => setDraft({ ...draft, linked_task_ids: v })}
          required={false} label="Linked tasks (optional)"
          testidPrefix="safety-activity" />
        <div className="grid grid-cols-2 gap-2">
          <select data-testid="safety-new-type" value={draft.event_type} onChange={(e) => setDraft({ ...draft, event_type: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            <option value="">Select…</option>
            {["near_miss", "first_aid", "recordable", "lost_time", "fatality", "other"].map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
          </select>
          <select data-testid="safety-new-severity" value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            <option value="">Select…</option>
            {["minor", "moderate", "major", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        {/* pt.40: inline section button -> sbtn-sm (28px). */}
        <button data-testid="safety-add" onClick={create}
          disabled={busy || (locked && !isAddReasonValid(addReason))}
          title={locked && !isAddReasonValid(addReason)
            ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
            : undefined}
          className="sbtn sbtn-sm sbtn-primary">
          {busy ? "Saving…" : locked ? "Add (post-submit)" : "Add safety event"}
        </button>
      </div>
    </PanelShell>
  );
}

function SafetyRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`safety-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className="pill pill-slate mr-2">{row.event_type}</span>
      <span className={`pill mr-2 ${row.severity === "critical" || row.severity === "major" ? "pill-red" : "pill-slate"}`}>{row.severity}</span>
      <span className="text-slate-700 whitespace-pre-wrap">{row.description}</span>
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`safety-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-xs text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="safety" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/safety-events/${row.id}`}
          fields={[
            { key: "description", label: "Description", type: "textarea" },
            { key: "severity", label: "Severity", type: "select",
              options: ["minor", "moderate", "major", "critical"].map((s) => ({ value: s, label: s })) },
            { key: "event_type", label: "Event type", type: "select",
              options: ["near_miss", "first_aid", "recordable", "lost_time", "fatality"].map((s) => ({ value: s, label: s.replace(/_/g, " ") })) },
          ]}
          currentValues={{
            description: row.description, severity: row.severity, event_type: row.event_type,
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}

function QcPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Phase 5 -- QC severity gets the rework_required tier.
  // When picked, super enters rework_duration_days + basis;
  // calibration RAG suggests a benchmark from prior outcomes (advisory).
  // Submit -> backend creates synthetic delay + runs CPM cascade.
  // QC rework days (2026-05-17) -- rework duration is entered in
  // WORKING DAYS (0.5-day increments), consistent with delays / RFIs.
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) replaces the old per-DR linked_dr_activity_ids picker. The
  // synthetic delay and CPM cascade walk these task ids directly.
  const [draft, setDraft] = useState({
    description: "", severity: "minor",
    rework_duration_days: "", rework_duration_basis: "",
    linked_task_ids: [],
  });
  const [benchmark, setBenchmark] = useState(null);
  // Phase D — copilot can push a drafted description into this form.
  useEffect(() => subscribeDescriptionInsert(({ target, text }) => {
    if (target !== "qc" || !text) return;
    setDraft((d) => ({ ...d, description: d.description ? `${d.description}\n\n${text}` : text }));
  }), []);

  useEffect(() => {
    if (draft.severity !== "rework_required" || !projectId) { setBenchmark(null); return; }
    api.get(`/projects/${projectId}/qc-issues/rework-benchmark`)
      .then(({ data }) => setBenchmark(data))
      .catch(() => setBenchmark(null));
  }, [draft.severity, projectId]);

  const isRework = draft.severity === "rework_required";
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  // 2026-05-26 -- Activity Grid Issues dropdown integration.
  useIssueDropdownListener("qc", "dr-qc-panel", (detail) => {
    setDraft((d) => {
      const next = { ...d };
      if (detail.description) next.description = detail.description;
      if (detail.severity) next.severity = detail.severity;
      if (detail.activityId && !d.linked_task_ids.includes(detail.activityId)) {
        next.linked_task_ids = [...d.linked_task_ids, detail.activityId];
      }
      return next;
    });
  });

  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  const create = async () => {
    if (busy) return;
    if (!draft.description) return toast.error("Description required");
    if (!draft.linked_task_ids.length) return toast.error("Pick at least one linked task");
    if (isRework) {
      if (!draft.rework_duration_days || Number(draft.rework_duration_days) <= 0) {
        return toast.error("Rework duration (days) required when severity=rework_required");
      }
      if (!draft.rework_duration_basis.trim()) {
        return toast.error("Rework basis (short note) required");
      }
    }
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      const payload = {
        project_id: projectId,
        daily_report_id: dailyReportId,
        date: reportDate,
        description: draft.description,
        severity: draft.severity,
        linked_task_ids: draft.linked_task_ids,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      };
      if (isRework) {
        payload.rework_duration_days = Number(draft.rework_duration_days);
        payload.rework_duration_basis = draft.rework_duration_basis.trim();
      }
      await api.post(`/qc-issues`, payload);
      setDraft({ description: "", severity: "minor",
                  rework_duration_days: "", rework_duration_basis: "",
                  linked_task_ids: [] });
      setAddReason("");
      reload();
      toast.success(isRework
        ? "QC rework filed -- synthetic delay created and CPM cascade running."
        : (locked ? "QC issue added (post-submit). Audit chain captured." : "QC issue logged."));
    } catch (e) {
      if (!handleLockedAddError(e, "qc")) {
        toast.error(formatErr(e, "Create QC issue"));
      }
    }
    finally { setBusy(false); }
  };

  return (
    <PanelShell icon={ClipboardCheck} title="QC issues" testid="dr-qc-panel" count={rows.length}>
      {rows.map((r) => (
        <QcRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div data-testid="qc-empty" className="text-xs text-slate-400 py-2"><span className="max-md:hidden">Nothing logged today — say so by clicking + at right.</span><span className="md:hidden">Nothing logged today — add one with the + chips above.</span></div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2" data-testid="qc-form">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a QC issue" testid="qc-add-reason" />}
        <div className="space-y-2">
          <textarea data-testid="qc-new-desc" rows={4} placeholder="Describe the issue"
            value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            className="w-full p-2 rounded border border-slate-200 text-sm" />

          {/* Bucket D Corrective -- master-schedule task picker. */}
          <TaskPicker projectId={projectId}
            value={draft.linked_task_ids}
            onChange={(v) => setDraft({ ...draft, linked_task_ids: v })}
            required={true} label="Linked tasks (required)"
            testidPrefix="qc-activity" />

          <div className="flex items-center gap-2">
            <select data-testid="qc-new-severity" value={draft.severity}
              onChange={(e) => setDraft({ ...draft, severity: e.target.value })}
              className="h-8 px-2 rounded border border-slate-200 text-sm">
              {["observation", "minor", "moderate", "major", "critical", "rework_required", "stop_work"]
                .map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
          </div>

          {/* Bucket D Phase 5 -- rework block.
              QC rework days (2026-05-17) -- duration entered in
              working days, not hours. */}
          {isRework && (
            <div className="bg-rose-50 border border-rose-200 rounded p-2 space-y-2" data-testid="qc-rework-block">
              <div className="text-xs font-semibold text-rose-900">Rework duration estimate (super)</div>
              {benchmark?.benchmark_days != null && (
                <div className="text-xs text-slate-600 italic" data-testid="qc-rework-benchmark">
                  {benchmark.basis}
                </div>
              )}
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <FieldLabel required>Rework duration (days)</FieldLabel>
                  <input data-testid="qc-rework-days" type="number" inputMode="decimal" step="0.5" min="0"
                    value={draft.rework_duration_days}
                    onChange={(e) => setDraft({ ...draft, rework_duration_days: e.target.value })}
                    className="w-full h-8 px-2 rounded border border-rose-300 text-sm" />
                </div>
                <div className="col-span-2">
                  <FieldLabel required>Basis (short note)</FieldLabel>
                  <input data-testid="qc-rework-basis" type="text"
                    value={draft.rework_duration_basis}
                    onChange={(e) => setDraft({ ...draft, rework_duration_basis: e.target.value })}
                    placeholder="e.g. re-rough 11A south wall, 2 plumbers ~1 day"
                    className="w-full h-8 px-2 rounded border border-rose-300 text-sm" />
                </div>
              </div>
              {/* QC rework days (2026-05-17) -- silent-zero guard. The
                  CPM cascade applies WHOLE-day impacts, so a sub-1-day
                  rework rounds to 0 working days of schedule shift.
                  Warn the super (NON-BLOCKING -- save still proceeds)
                  so a half-day entry is not a silent no-op. */}
              {Number(draft.rework_duration_days) > 0
                && Number(draft.rework_duration_days) <= 0.5 && (
                <div data-testid="qc-rework-subday-warning"
                  className="text-xs text-amber-900 bg-amber-50 border border-amber-300 rounded px-2 py-1">
                  A {draft.rework_duration_days}-day rework will be recorded on the
                  QC issue but will not shift the schedule — the cascade currently
                  applies whole-day impacts (a sub-1-day rework rounds to 0).
                </div>
              )}
              <div className="text-xs text-rose-800">
                Duration is in working days (0.5-day increments). On save, a
                synthetic delay (cause=quality_rework) of that many days is
                created and the CPM cascade run for the affected activities.
              </div>
            </div>
          )}

          <div className="flex justify-end">
            {/* pt.40: inline section button -> sbtn-sm (28px). */}
            <button data-testid="qc-add" onClick={create}
              disabled={busy || (locked && !isAddReasonValid(addReason))}
              title={locked && !isAddReasonValid(addReason)
                ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
                : undefined}
              className="sbtn sbtn-sm sbtn-primary">
              {busy ? "Saving…" : locked ? "Add QC (post-submit)" : "Add QC issue"}
            </button>
          </div>
        </div>
      </div>
    </PanelShell>
  );
}

function QcRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`qc-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className={`pill mr-2 ${
        row.severity === "rework_required" || row.severity === "stop_work" ? "pill-red"
        : row.severity === "critical" || row.severity === "major" ? "pill-red"
        : "pill-slate"
      }`}>{row.severity}</span>
      <span className="text-slate-700 whitespace-pre-wrap">{row.description}</span>
      {row.linked_synthetic_delay_id && (
        <span className="ml-2 text-xs text-rose-700 bg-rose-50 border border-rose-200 px-1 py-0.5 rounded">
          rework {row.rework_duration_days}d
          {row.rework_actual_days != null && ` (actual ${row.rework_actual_days}d)`}
        </span>
      )}
      {/* Task 3 fix D -- outcome capture button. Visible when QC has
          a synthetic delay AND no actual rework yet recorded. */}
      {row.linked_synthetic_delay_id && row.rework_actual_days == null && (
        <ReworkOutcomeButton qcIssue={row} reload={reload} />
      )}
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`qc-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-xs text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="qc" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/qc-issues/${row.id}`}
          fields={[
            { key: "description", label: "Description", type: "textarea" },
            { key: "severity", label: "Severity", type: "select",
              options: ["observation", "minor", "moderate", "major", "critical", "rework_required", "stop_work"]
                .map((s) => ({ value: s, label: s.replace(/_/g, " ") })) },
            { key: "status", label: "Status", type: "select",
              options: ["open", "in_progress", "resolved", "closed"].map((s) => ({ value: s, label: s.replace(/_/g, " ") })) },
            { key: "resolution_notes", label: "Resolution notes", type: "textarea" },
          ]}
          currentValues={{
            description: row.description, severity: row.severity,
            status: row.status, resolution_notes: row.resolution_notes,
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}


function RfisPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) picker. RFIs are REQUIRED to link >=1 task (gates a CPM
  // node via the gating_entities back-pointer).
  // CPM Part J-6 (2026-05-15) -- per_task_impacts captures the super's
  // estimated schedule impact per linked task. Optional on create;
  // empty array means "no impact captured yet" (backend treats the
  // RFI as a binary blocker via count_open_blockers, doesn't push
  // Slip on the linked tasks).
  // Explicit impact start (2026-07-03) -- when the estimated impact
  // begins; defaults like DelayForm's date (reportDate || local today).
  const [draft, setDraft] = useState({
    subject: "", description: "", priority: "normal",
    linked_task_ids: [],
    per_task_impacts: [],
    impact_start_date: reportDate || localTodayISO(),
  });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  // 2026-05-26 -- Activity Grid Issues dropdown integration.
  useIssueDropdownListener("rfi", "dr-rfis-panel", (detail) => {
    if (!detail.activityId) return;
    setDraft((d) => ({
      ...d,
      linked_task_ids: d.linked_task_ids.includes(detail.activityId)
        ? d.linked_task_ids : [...d.linked_task_ids, detail.activityId],
    }));
  });
  const create = async () => {
    if (busy) return;
    if (!draft.subject || !draft.description) return toast.error("Subject + question required");
    if (!draft.linked_task_ids.length) return toast.error("Pick at least one linked task");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      // CPM Part J-6 -- include per_task_impacts (cleaned by
      // PerTaskImpactsInput: empty / 0 inputs already pruned, only
      // positive integers + task_ids remain). Empty array is fine;
      // backend treats it as "no impact captured yet".
      await api.post(`/rfis`, {
        project_id: projectId, daily_report_id: dailyReportId,
        subject: draft.subject,
        question: draft.description,  // backend alias -> description
        priority: draft.priority,
        linked_task_ids: draft.linked_task_ids,
        per_task_impacts: draft.per_task_impacts || [],
        // Explicit impact start (2026-07-03) -- when the estimated
        // impact begins; the elapsed-growth anchor on the backend.
        impact_start_date: draft.impact_start_date || reportDate || localTodayISO(),
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setDraft({
        subject: "", description: "", priority: "normal",
        linked_task_ids: [], per_task_impacts: [],
        impact_start_date: reportDate || localTodayISO(),
      });
      setAddReason("");
      reload();
      if (locked) toast.success("RFI added (post-submit). Audit chain captured.");
    } catch (e) {
      if (!handleLockedAddError(e, "rfi")) {
        toast.error(formatErr(e, "Open RFI"));
      }
    }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={MessageSquare} title="RFIs" testid="dr-rfis-panel" count={rows.length}>
      {rows.map((r) => (
        <RfiRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div data-testid="rfis-empty" className="text-xs text-slate-400 py-2"><span className="max-md:hidden">Nothing logged today — say so by clicking + at right.</span><span className="md:hidden">Nothing logged today — add one with the + chips above.</span></div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="an RFI" testid="rfi-add-reason" />}
        <input data-testid="rfi-new-subject" placeholder="Subject" value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} className="w-full h-8 px-2 rounded border border-slate-200 text-sm" />
        <textarea data-testid="rfi-new-desc" placeholder="Question / context" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="w-full px-2 py-1 rounded border border-slate-200 text-sm" rows={2} />
        <TaskPicker projectId={projectId}
          value={draft.linked_task_ids}
          onChange={(v) => setDraft((d) => ({
            // CPM Part J-6 -- when super removes a task from linked
            // tasks, prune its per_task_impacts entry so the cleaned
            // array PerTaskImpactsInput later receives doesn't carry
            // stale rows. (PerTaskImpactsInput's own auto-sync useEffect
            // catches this defensively too, but pruning at the source
            // avoids a transient inconsistent state.)
            ...d,
            linked_task_ids: v,
            per_task_impacts: (d.per_task_impacts || []).filter(
              (imp) => (v || []).includes(imp.task_id),
            ),
          }))}
          required={true} label="Linked tasks"
          testidPrefix="rfi-activity" />
        {/* CPM Part J-6 -- per-task schedule-impact input. Only
            renders when there are linked tasks. Empty array (super
            hasn't entered any impact_days yet) is a valid state and
            keeps the RFI as a binary blocker only. */}
        <PerTaskImpactsInput
          linkedTaskIds={draft.linked_task_ids}
          value={draft.per_task_impacts}
          onChange={(v) => setDraft((d) => ({ ...d, per_task_impacts: v }))}
          projectId={projectId}
          testidPrefix="rfi-impacts"
        />
        {/* Explicit impact start (2026-07-03) -- when the estimated
            impact begins; anchors elapsed-day growth on the backend. */}
        <label className="block text-xs text-slate-500">
          Impact starts on
          <input data-testid="rfi-new-impact-start" type="date"
            value={draft.impact_start_date}
            onChange={(e) => setDraft({ ...draft, impact_start_date: e.target.value })}
            className="mt-1 block w-full h-8 px-2 rounded border border-slate-200 text-sm text-slate-700" />
          <span className="block mt-0.5 text-xs text-slate-400">
            Pick today if the RFI already affects today&apos;s work; pick
            tomorrow if the impact begins then.
          </span>
        </label>
        <div className="flex items-center gap-2">
          <select data-testid="rfi-new-priority" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm">
            {["low", "normal", "high", "urgent"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <button data-testid="rfi-add" onClick={create}
            disabled={busy || (locked && !isAddReasonValid(addReason))}
            title={locked && !isAddReasonValid(addReason)
              ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
              : undefined}
            className="sbtn sbtn-sm sbtn-primary">{busy ? "Saving…" : locked ? "Open RFI (post-submit)" : "Open RFI"}</button>
        </div>
      </div>
    </PanelShell>
  );
}

function RfiRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`rfi-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className="font-mono mr-2 text-blue-700">{row.rfi_number}</span>
      <span className="pill pill-slate mr-2">{row.priority}</span>
      <span className="text-slate-700">{row.subject}</span>
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`rfi-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-xs text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="rfi" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/rfis/${row.id}`}
          fields={[
            { key: "subject", label: "Subject" },
            { key: "description", label: "Question / context", type: "textarea" },
            { key: "status", label: "Status", type: "select",
              options: ["draft", "open", "responded", "closed"].map((s) => ({ value: s, label: s })) },
            { key: "response_text", label: "Response (when status=responded)", type: "textarea" },
            // CPM Part J-6 (2026-05-15) -- per_task_impacts editor.
            // Linked tasks are FIXED in this modal (the modal only
            // edits impact magnitudes; linkage edits live on the
            // standalone /rfis page). Empty value renders zero inputs;
            // user adding a value emits the cleaned array on submit.
            { key: "per_task_impacts", label: "Schedule impact (optional)",
              type: "per_task_impacts",
              linkedTaskIds: row.linked_task_ids || [],
              projectId: row.project_id },
          ]}
          currentValues={{
            subject: row.subject, description: row.description,
            status: row.status, response_text: row.response_text,
            per_task_impacts: row.per_task_impacts || [],
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}


function DeliveriesPanel({ rows, dailyReportId, projectId, reportDate, locked, reload, grid }) {
  // Bucket D Corrective (2026-05-08) -- linked_task_ids (master schedule
  // scope) picker. REQUIRED min-1 -- deliveries gate the tasks
  // consuming them so the cascade can attribute slip if a delivery is
  // late.
  const [draft, setDraft] = useState({
    vendor: "", description: "", po_number: "",
    linked_task_ids: [],
  });
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture.
  const [addReason, setAddReason] = useState("");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  // 2026-05-26 -- Activity Grid Issues dropdown integration.
  useIssueDropdownListener("delivery", "dr-deliveries-panel", (detail) => {
    if (!detail.activityId) return;
    setDraft((d) => ({
      ...d,
      linked_task_ids: d.linked_task_ids.includes(detail.activityId)
        ? d.linked_task_ids : [...d.linked_task_ids, detail.activityId],
    }));
  });
  const create = async () => {
    if (busy) return;
    if (!draft.vendor || !draft.description) return toast.error("Vendor + description required");
    if (!draft.linked_task_ids.length) return toast.error("Pick at least one task consuming this delivery");
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      await api.post(`/deliveries`, {
        project_id: projectId, delivery_date: reportDate,
        daily_report_id: dailyReportId,
        vendor: draft.vendor, description: draft.description,
        po_number: draft.po_number || null,
        linked_task_ids: draft.linked_task_ids,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      setDraft({ vendor: "", description: "", po_number: "", linked_task_ids: [] });
      setAddReason("");
      reload();
      if (locked) toast.success("Delivery added (post-submit). Audit chain captured.");
    } catch (e) {
      if (!handleLockedAddError(e, "delivery")) {
        toast.error(formatErr(e, "Log delivery"));
      }
    }
    finally { setBusy(false); }
  };
  return (
    <PanelShell icon={Truck} title="Deliveries today" testid="dr-deliveries-panel" count={rows.length}>
      {rows.map((r) => (
        <DeliveryRowInline key={r.id} row={r} locked={locked}
          dailyReportId={dailyReportId} reload={reload} />
      ))}
      {!rows.length && <div data-testid="deliveries-empty" className="text-xs text-slate-400 py-2"><span className="max-md:hidden">Nothing logged today — say so by clicking + at right.</span><span className="md:hidden">Nothing logged today — add one with the + chips above.</span></div>}
      <div className="mt-3 pt-3 border-t border-slate-100 space-y-2">
        {locked && <LockedAddReasonBlock value={addReason} onChange={setAddReason}
          eventLabel="a delivery" testid="delivery-add-reason" />}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <input data-testid="delivery-new-vendor" placeholder="Vendor" value={draft.vendor} onChange={(e) => setDraft({ ...draft, vendor: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
          <input data-testid="delivery-new-desc" placeholder="What was delivered" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} className="col-span-2 h-8 px-2 rounded border border-slate-200 text-sm" />
          <input data-testid="delivery-new-po" placeholder="PO #" value={draft.po_number} onChange={(e) => setDraft({ ...draft, po_number: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-sm" />
          <button data-testid="delivery-add" onClick={create}
            disabled={busy || (locked && !isAddReasonValid(addReason))}
            title={locked && !isAddReasonValid(addReason)
              ? `Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`
              : undefined}
            className="sbtn sbtn-sm sbtn-primary col-span-2">{busy ? "Saving…" : locked ? "Log delivery (post-submit)" : "Log delivery"}</button>
        </div>
        <TaskPicker projectId={projectId}
          value={draft.linked_task_ids}
          onChange={(v) => setDraft({ ...draft, linked_task_ids: v })}
          required={true} label="Tasks consuming this delivery"
          testidPrefix="delivery-activity" />
      </div>
    </PanelShell>
  );
}

function NotesPanel({ report, dailyReportId, locked, reload }) {
  const [text, setText] = useState(report.notes || "");
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  useEffect(() => { setText(report.notes || ""); }, [report.notes]);
  const save = async () => {
    if (busy) return;
    setBusy(true);
    try { await api.patch(`/daily-reports/${dailyReportId}`, { notes: text }); reload(); toast.success("Notes saved"); }
    catch (e) { toast.error(e.response?.data?.detail || "Save failed"); }
    finally { setBusy(false); }
  };
  // Bucket G Phase 3 (2026-05-11) -- Jigsaw pre-fill on the DR narrative.
  const userRole = (typeof window !== "undefined" && JSON.parse(localStorage.getItem("ice.user") || "null")?.role || "").toLowerCase();
  return (
    <PanelShell icon={ClipboardList} title="Daily notes / narrative" testid="dr-notes-panel">
      <JigsawField entityType="daily_report" entityId={dailyReportId}
        fieldPath="notes" userRole={userRole} fieldLabel="notes"
        onConfirm={(v) => setText(String(v ?? ""))}>
        <textarea data-testid="dr-notes-textarea" disabled={locked} value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Free-text notes that go on the PDF (visitors, accidents-near-misses summary, manpower changes, etc.)"
          className="w-full h-28 px-3 py-2 rounded border border-slate-200 text-sm" />
      </JigsawField>
      {!locked && (
        <div className="flex justify-end mt-2">
          <button data-testid="dr-notes-save" onClick={save} disabled={busy}
            className="h-8 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
            {busy ? "Saving…" : "Save notes"}
          </button>
        </div>
      )}
    </PanelShell>
  );
}

function PhotosPanel({ rows, dailyReportId, projectId, locked, reload }) {
  const [busy, setBusy] = useState(false);
  // Bucket D Addition 5 (2026-05-12) -- add-to-locked-DR reason capture
  // for photo uploads; forwarded to /photo-tray/attach-to-dr.
  const [addReason, setAddReason] = useState("");

  // pt.54 (2026-05-28) / pt.75 (2026-05-29): Photo AI Review — pin uploaded
  // photos, run ONE combined "AI analysis" (POST /photo-tray/review, lens
  // "combined") that reports safety violations (with validated OSHA 1926
  // codes) AND QC / workmanship issues in a single pass, or says it's clean.
  // Newest review shows in full; older ones collapse to a single line,
  // re-openable in a dialog to edit / copy / retry / delete. (pt.75 replaced
  // the separate Safety + QC buttons with one button and removed the
  // "Send to Safety / QC" actions entirely.)
  const [pinned, setPinned] = useState(() => new Set());
  const [reviews, setReviews] = useState([]);       // {id,lens,severity,label,text,n,ids,time}
  const [runningLens, setRunningLens] = useState(null);
  const [collapsedIds, setCollapsedIds] = useState(() => new Set());
  const [dialogId, setDialogId] = useState(null);
  const [previewSrc, setPreviewSrc] = useState(null);
  const pinnedIds = [...pinned];
  const togglePin = (id) => setPinned((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n;
  });
  // pt.65: prune pins to the live gallery whenever rows change (e.g. a pinned
  // photo is deleted) so the count, the run-spinner, and the review request
  // never carry a stale, no-longer-present photo id.
  useEffect(() => {
    setPinned((prev) => {
      if (!prev.size) return prev;
      const live = new Set(rows.map((p) => p.id));
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [rows]); // eslint-disable-line react-hooks/exhaustive-deps
  const toReview = (d) => ({
    id: d.id, lens: d.lens, severity: d.severity, label: d.label, text: d.text,
    n: d.photo_count ?? (d.photo_ids || []).length, ids: d.photo_ids || [],
    time: new Date(d.created_at || Date.now()).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
  });
  // pt.61: load persisted reviews on open so Safety/QC reviews survive a
  // refresh / re-open days later — no re-spending tokens to regenerate.
  useEffect(() => {
    if (!dailyReportId) return;
    let cancelled = false;
    api.get(`/daily-reports/${dailyReportId}/photo-reviews`)
      .then(({ data }) => { if (!cancelled) setReviews((data || []).map(toReview)); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [dailyReportId]); // eslint-disable-line react-hooks/exhaustive-deps
  const runReview = async (lens) => {
    if (!pinnedIds.length || runningLens) return;
    setRunningLens(lens);
    try {
      const { data } = await api.post(`/photo-tray/review`, { tray_ids: pinnedIds, lens, daily_report_id: dailyReportId });
      setReviews((r) => [toReview({ ...data, photo_ids: data.photo_ids || pinnedIds }), ...r]);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Review failed — try again.");
    } finally { setRunningLens(null); }
  };
  const setReviewText = (id, text) => setReviews((r) => r.map((x) => x.id === id ? { ...x, text } : x));
  const deleteReview = async (rev) => {
    try { await api.delete(`/photo-reviews/${rev.id}`); } catch { /* drop locally regardless */ }
    setReviews((r) => r.filter((x) => x.id !== rev.id));
    if (dialogId === rev.id) setDialogId(null);
  };
  // pt.61: retry re-runs the SAME lens on the SAME photos and REPLACES the
  // review (so the super doesn't stack up rejected versions).
  const retryReview = async (rev) => {
    if (runningLens) return;
    setRunningLens(rev.lens);
    try {
      // pt.65: delete the old review FIRST (awaited, not swallowed) so a
      // failed delete can't leave two persisted copies — the POST always
      // inserts a fresh row, so create-before-delete risked a duplicate on
      // next mount. If the delete fails we abort before creating a second.
      await api.delete(`/photo-reviews/${rev.id}`);
      const { data } = await api.post(`/photo-tray/review`, { tray_ids: rev.ids, lens: rev.lens, daily_report_id: dailyReportId });
      setReviews((r) => [toReview({ ...data, photo_ids: data.photo_ids || rev.ids }), ...r.filter((x) => x.id !== rev.id)]);
      if (dialogId === rev.id) setDialogId(null);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Re-run failed — try again.");
    } finally { setRunningLens(null); }
  };
  // pt.62: theme-aware classes (see .rev-* in index.css) instead of inline
  // dark-only colors, so the chips + buttons read correctly in light mode too.
  const sevCls = (s) => "rev-sev " + (s === "clear" ? "rev-sev--clear"
    : (s === "critical" || s === "major") ? "rev-sev--crit" : "rev-sev--warn");
  const dialogReview = reviews.find((r) => r.id === dialogId) || null;

  // pt.63 (2026-05-28): batch upload, no tagging in the middle. The super
  // picks MANY photos at once; each uploads to the tray, then all are
  // attached to THIS daily report in one call (POST /photo-tray/attach-to-dr)
  // — no per-photo TagWizard, no AI caption/class step. The photos appear in
  // the gallery with the delete + pin controls; tagging to an event / running
  // an AI review is a separate, optional step the super does later.
  const onUpload = async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    if (locked && (addReason || "").trim().length < ADD_REASON_MIN) {
      toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) before uploading to a locked DR.`);
      e.target.value = "";
      return;
    }
    setBusy(true);
    try {
      // pt.67: upload each file to the tray, retrying ONCE on a transient
      // failure (network error or 5xx — e.g. the Emergent preview waking from
      // idle, or a flaky site connection) so a momentary blip doesn't silently
      // drop the whole batch. Capture the real error so the toast is actionable
      // instead of a generic "upload failed".
      // Offline queue (spec F.4): if the tray POST fails with NO response
      // object (network down / offline), the photo isn't lost — it's stashed
      // in lib/drPhotoQueue and re-uploaded + attached to this DR on the next
      // 'online' event. A response-bearing failure (4xx/5xx) is a real reject
      // and still surfaces to the user. Online behavior is byte-identical.
      const queuePhotoOffline = async (file) => {
        try {
          // eslint-disable-next-line global-require
          const pq = require("../lib/drPhotoQueue");
          await pq.queuePhoto({
            blob: file,
            mimeType: file.type || "image/jpeg",
            dailyReportId,
            projectId,
            photoClass: "progress",
          });
          return true;
        } catch { return false; }
      };
      const uploadOne = async (file) => {
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            const fd = new FormData();
            fd.append("file", file);
            fd.append("project_id", projectId);
            fd.append("capture_source", "manual_upload");
            const r = await api.post(`/photo-tray`, fd, { headers: { "Content-Type": "multipart/form-data" } });
            return { id: r.data?.id || null };
          } catch (err) {
            const offline = !err.response;
            const transient = offline || (err.response.status >= 500);
            if (transient && attempt === 0) { await new Promise((res) => setTimeout(res, 900)); continue; }
            // Offline (no response even after the retry): queue, never drop.
            if (offline && await queuePhotoOffline(file)) return { id: null, queued: true };
            return { id: null, err };
          }
        }
        return { id: null };
      };
      const results = await Promise.all(files.map(uploadOne));
      const trayIds = results.map((r) => r.id).filter(Boolean);
      const queued = results.filter((r) => r.queued).length;
      const failed = results.length - trayIds.length - queued;
      if (!trayIds.length) {
        if (queued > 0) {
          // Nothing reached the server, but every failure was offline and is
          // now safely queued — treat as a local save, not a loss.
          toast(`${queued} photo${queued !== 1 ? "s" : ""} saved — will upload when you reconnect.`);
          if (locked) setAddReason("");
          return;
        }
        const firstErr = results.find((r) => r.err)?.err;
        const status = firstErr?.response?.status;
        const detail = firstErr?.response?.data?.detail || firstErr?.message;
        // pt.68: a 413 is a deterministic size rejection — retrying won't help,
        // so don't tell the user to retry; tell them to use a smaller photo.
        let msg;
        if (status === 413) {
          msg = `Upload failed: ${detail || "photo too large"}. Use photos under 25MB.`;
        } else if (detail) {
          msg = `Upload failed: ${detail}. The server may have been waking up — please try again.`;
        } else {
          msg = "Upload failed — no photos were saved. The server may have been waking up; please try again.";
        }
        toast.error(msg);
        return;
      }
      // Attach all to this DR in one shot (no event, no AI).
      const { data } = await api.post(`/photo-tray/attach-to-dr`, {
        tray_ids: trayIds, daily_report_id: dailyReportId,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      reload();
      const n = data?.created ?? trayIds.length;
      // pt.65: surface partial failures — don't show a clean success toast
      // when some files silently failed to upload (easy to miss on a phone).
      // A mixed batch may also have offline-queued photos (queued>0) that will
      // upload on reconnect; call those out so the count isn't confusing.
      if (failed > 0) {
        toast.error(`${n} photo${n !== 1 ? "s" : ""} added · ${failed} failed to upload — please retry those.`);
      } else if (queued > 0) {
        toast.success(`${n} added · ${queued} saved to upload when you reconnect.`);
      } else {
        toast.success(`${n} photo${n !== 1 ? "s" : ""} added to this report.`);
      }
      if (locked) setAddReason("");
    } catch (err) {
      if (!handleLockedAddError(err, "photo")) {
        toast.error(err.response?.data?.detail || "Upload failed");
      }
    }
    finally { setBusy(false); e.target.value = ""; }
  };

  return (
    <PanelShell icon={ImageIcon} title="Photos" testid="dr-photos-panel" count={rows.length} step={6}
      badge={
        <label data-testid="photo-upload-label" className="ml-auto inline-block max-md:hidden">
          {/* pt.63 (2026-05-28): batch multi-select upload, no tagging step. */}
          <span className="sbtn sbtn-sm sbtn-primary cursor-pointer">
            <Upload className="h-3.5 w-3.5" /> {busy ? "Uploading…" : (locked ? "Upload photos (post-submit)" : "Upload photos")}
          </span>
          <input data-testid="photo-upload" type="file" accept="image/*" multiple onChange={onUpload} className="hidden" />
        </label>
      }>
      {locked && (
        <div className="mb-3">
          <LockedAddReasonBlock value={addReason} onChange={setAddReason}
            eventLabel="a photo" testid="photo-add-reason" />
        </div>
      )}
      <div className="grid grid-cols-3 sm:grid-cols-6 md:grid-cols-8 gap-2">
        {/* MOBILE (mockup): dashed camera "Add" tile as the LAST grid cell
            (order 9999); the header upload button is max-md:hidden. Same
            onUpload handler, respects the locked flow. */}
        {!busy && (
          <label className="md:hidden aspect-square rounded-[10px] border border-dashed grid place-items-center cursor-pointer"
            style={{ borderColor: "var(--v4-border-strong)", color: "var(--v4-fg-secondary)", order: 9999 }}
            data-testid="photo-add-tile-m">
            <span className="flex flex-col items-center gap-1">
              <Upload className="h-5 w-5" />
              <span className="text-[11px]">Add</span>
            </span>
            <input type="file" accept="image/*" multiple onChange={onUpload} className="hidden" />
          </label>
        )}
        {rows.map((p) => {
          const isPinned = pinned.has(p.id);
          return (
            <div key={p.id} className={`relative rounded ${isPinned ? "ring-2 ring-emerald-500" : ""}`}>
              <AuthPhoto photo={p} onPreview={setPreviewSrc}
                locked={locked} dailyReportId={dailyReportId} reload={reload} />
              <button type="button" data-testid={`photo-pin-${p.id}`}
                onClick={() => togglePin(p.id)}
                title={isPinned ? "Unpin from review" : "Pin for AI review"}
                className="absolute top-1 right-1 h-6 w-6 max-md:min-h-[40px] max-md:min-w-[40px] grid place-items-center rounded-full transition z-10"
                style={isPinned
                  ? { background: "#10b981", color: "#06281d", border: "1px solid transparent" }
                  : { background: "rgba(10,10,11,.6)", color: "#fff", border: "1px solid rgba(255,255,255,.28)" }}>
                <Pin className="h-3 w-3" />
              </button>
            </div>
          );
        })}
        {!rows.length && <div className="col-span-full text-xs text-slate-400 py-4 text-center">No photos yet.</div>}
      </div>

      {rows.length > 0 && (
        <div className="mt-3 pt-3 border-t border-slate-100">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: "var(--v4-fg-primary)" }}>
              <span className="h-2 w-2 rounded-full" style={{ background: pinnedIds.length ? "#10b981" : "var(--v4-fg-faint)" }} />
              {pinnedIds.length} pinned
            </span>
            <button type="button" className="text-[11px] px-2 py-1 rounded border max-md:hidden"
              style={{ color: "var(--v4-fg-muted)", borderColor: "var(--v4-border)" }}
              onClick={() => setPinned(new Set(rows.map((p) => p.id)))}>Pin all</button>
            <button type="button" className="text-[11px] px-2 py-1 rounded border max-md:hidden"
              style={{ color: "var(--v4-fg-muted)", borderColor: "var(--v4-border)" }}
              onClick={() => setPinned(new Set())}>Clear</button>
            <span className="flex-1" />
            {/* pt.75: ONE combined "AI analysis" button (safety + QC in a
                single vision pass). It stays solid + clearly visible in BOTH
                themes whether idle or running — the old per-lens buttons faded
                to near-invisible in light theme when disabled. When nothing is
                pinned we don't disable it (that was the invisibility trap);
                instead a click shows a clear toast. */}
            <button type="button" data-testid="photo-review-run" disabled={!!runningLens}
              title={pinnedIds.length
                ? `Run AI safety + QC analysis on ${pinnedIds.length} pinned photo${pinnedIds.length !== 1 ? "s" : ""}`
                : "Pin one or more photos, then run AI safety + QC analysis"}
              onClick={() => {
                if (!pinnedIds.length) { toast.error("Pin at least one photo to analyze."); return; }
                runReview("combined");
              }}
              className="rev-analyze">
              {runningLens ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
              {runningLens ? "Analyzing…" : "AI analysis"}
            </button>
          </div>

          {(reviews.length > 0 || runningLens) && (
            <div className="mt-3 space-y-2">
              {runningLens && (
                <div className="flex items-center gap-2 text-xs px-3 py-2 rounded"
                  style={{ color: "var(--v4-fg-muted)", background: "var(--v4-bg-surface-2)", border: "1px solid var(--v4-border)" }}>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Foreman is analyzing {pinnedIds.length} photo{pinnedIds.length !== 1 ? "s" : ""} for {runningLens === "safety" ? "safety" : runningLens === "qc" ? "QC" : "safety & QC"}…
                </div>
              )}
              {reviews.map((rev, idx) => {
                const full = idx === 0 && !collapsedIds.has(rev.id);
                const isSafety = rev.lens === "safety";
                const isQc = rev.lens === "qc";
                const isCombined = !isSafety && !isQc; // pt.75: combined is the default lens
                const HeadIcon = isCombined ? Sparkles : (isSafety ? ShieldAlert : ClipboardCheck);
                const headFull = isCombined ? "AI analysis" : (isSafety ? "Safety review" : "QC review");
                const headMini = isCombined ? "AI" : (isSafety ? "Safety" : "QC");
                const lensCls = isCombined ? "rev-lens--ai" : (isSafety ? "rev-lens--safety" : "rev-lens--qc");
                const accent = isCombined ? "#6366f1" : (isSafety ? "#f59e0b" : "#38bdf8");
                if (!full) {
                  return (
                    <button key={rev.id} type="button" onClick={() => setDialogId(rev.id)}
                      data-testid={`photo-review-mini-${rev.id}`}
                      className="w-full flex items-center gap-2 text-left px-3 py-2 rounded text-xs"
                      style={{ background: "var(--v4-bg-surface)", border: "1px solid var(--v4-border)" }}>
                      <span className={`inline-flex items-center gap-1 font-semibold shrink-0 ${lensCls}`}>
                        <HeadIcon className="h-3.5 w-3.5" />{headMini}
                      </span>
                      <span className={`shrink-0 ${sevCls(rev.severity)}`}>{rev.label}</span>
                      <span className="flex-1 truncate" style={{ color: "var(--v4-fg-muted)" }}>{rev.text}</span>
                      <span className="shrink-0 max-md:hidden" style={{ color: "var(--v4-fg-faint)" }}>{rev.n} photo{rev.n !== 1 ? "s" : ""} · {rev.time}</span>
                      <span className="rev-open font-semibold shrink-0">Open ›</span>
                    </button>
                  );
                }
                return (
                  <div key={rev.id} className="rounded overflow-hidden"
                    style={{ background: "var(--v4-bg-surface-2)", border: "1px solid var(--v4-border)", borderLeft: `3px solid ${accent}` }}>
                    <div className="flex items-center gap-2 px-3 py-2 flex-wrap" style={{ borderBottom: "1px solid var(--v4-border)" }}>
                      <span className={`inline-flex items-center gap-1.5 text-xs font-bold ${lensCls}`}>
                        <HeadIcon className="h-4 w-4" />{headFull}
                      </span>
                      <span className="text-xs" style={{ color: "var(--v4-fg-muted)" }}>· Foreman · {rev.n} photo{rev.n !== 1 ? "s" : ""}</span>
                      <span className={sevCls(rev.severity)}>{rev.label}</span>
                      <span className="ml-auto text-[11px]" style={{ color: "var(--v4-fg-faint)" }}>{rev.time}</span>
                      <button type="button" title="Collapse" onClick={() => setCollapsedIds((s) => new Set(s).add(rev.id))}
                        className="text-sm leading-none px-1" style={{ color: "var(--v4-fg-faint)" }}>▴</button>
                    </div>
                    <div className="px-3 py-2">
                      <p className="text-[13px] leading-relaxed" style={{ color: "var(--v4-fg-primary)" }}>{rev.text}</p>
                      <div className="flex items-center gap-2 flex-wrap mt-3 pt-2" style={{ borderTop: "1px solid var(--v4-border)" }}>
                        <button type="button" onClick={() => setDialogId(rev.id)} className="rev-btn rev-ghost">
                          <Pencil className="h-3.5 w-3.5" /> Edit
                        </button>
                        <button type="button" onClick={() => { navigator.clipboard?.writeText(rev.text); toast.success("Review copied"); }}
                          className="rev-btn rev-ghost">
                          <CopyIcon className="h-3.5 w-3.5" /> Copy
                        </button>
                        <button type="button" onClick={() => retryReview(rev)} disabled={!!runningLens}
                          title="Re-run this review (replaces it)" className="rev-btn rev-ghost">
                          <RefreshCw className="h-3.5 w-3.5" /> Retry
                        </button>
                        <button type="button" onClick={() => deleteReview(rev)}
                          title="Delete this review" className="rev-btn rev-danger ml-auto">
                          <Trash2 className="h-3.5 w-3.5" /> Delete
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
      {previewSrc && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-6"
          style={{ background: "rgba(0,0,0,.8)" }}
          onClick={() => setPreviewSrc(null)}>
          <img src={previewSrc} alt="" data-testid="photo-preview-img"
            className="max-w-full max-h-full rounded-lg object-contain"
            style={{ boxShadow: "0 8px 40px rgba(0,0,0,.6)" }} />
          <button type="button" onClick={() => setPreviewSrc(null)}
            className="absolute top-4 right-5 text-2xl leading-none max-md:min-h-[44px] max-md:min-w-[44px] grid place-items-center"
            style={{ color: "rgba(255,255,255,.85)" }}>✕</button>
        </div>
      )}
      {dialogReview && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-6"
          style={{ background: "rgba(0,0,0,.6)" }}
          onClick={(e) => { if (e.target === e.currentTarget) setDialogId(null); }}>
          <div className="w-full max-w-[560px] rounded-xl overflow-hidden"
            style={{ background: "var(--v4-bg-surface)", border: "1px solid var(--v4-border)", boxShadow: "0 8px 28px rgba(0,0,0,.45)" }}>
            <div className="flex items-center gap-2 px-4 py-3 flex-wrap" style={{ borderBottom: "1px solid var(--v4-border)" }}>
              <span className={`inline-flex items-center gap-1.5 text-sm font-bold ${dialogReview.lens === "safety" ? "rev-lens--safety" : dialogReview.lens === "qc" ? "rev-lens--qc" : "rev-lens--ai"}`}>
                {dialogReview.lens === "safety" ? <ShieldAlert className="h-4 w-4" /> : dialogReview.lens === "qc" ? <ClipboardCheck className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}{dialogReview.lens === "safety" ? "Safety review" : dialogReview.lens === "qc" ? "QC review" : "AI analysis"}
              </span>
              <span className="text-xs" style={{ color: "var(--v4-fg-muted)" }}>· Foreman · {dialogReview.n} photo{dialogReview.n !== 1 ? "s" : ""}</span>
              <span className={sevCls(dialogReview.severity)}>{dialogReview.label}</span>
              <button type="button" onClick={() => setDialogId(null)} className="ml-auto text-base leading-none px-1"
                style={{ color: "var(--v4-fg-faint)" }}>✕</button>
            </div>
            <div className="px-4 py-3">
              <textarea value={dialogReview.text} data-testid="photo-review-edit"
                onChange={(e) => setReviewText(dialogReview.id, e.target.value)}
                rows={6} className="w-full rounded p-3 text-[13px] leading-relaxed dr-mp-input" />
              <p className="text-[11px] mt-2" style={{ color: "var(--v4-fg-faint)" }}>
                Edit the analysis here, then copy it — the “{dialogReview.label}” severity is kept with this review.
              </p>
            </div>
            <div className="flex items-center gap-2 px-4 py-3" style={{ borderTop: "1px solid var(--v4-border)" }}>
              <button type="button" onClick={() => { navigator.clipboard?.writeText(dialogReview.text); toast.success("Review copied"); }}
                className="rev-btn rev-ghost">
                <CopyIcon className="h-3.5 w-3.5" /> Copy
              </button>
              <button type="button" onClick={() => retryReview(dialogReview)} disabled={!!runningLens}
                title="Re-run this review (replaces it)" className="rev-btn rev-ghost">
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </button>
              <button type="button" onClick={() => deleteReview(dialogReview)}
                title="Delete this review" className="rev-btn rev-danger ml-auto">
                <Trash2 className="h-3.5 w-3.5" /> Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </PanelShell>
  );
}

function AuthPhoto({ photo, locked, dailyReportId, reload, onPreview }) {
  const [src, setSrc] = useState(null);
  // Addition 4 follow-up -- locked-DR edit modal toggle for the photo
  // card. Pencil icon overlays in the top-right when locked; opens
  // LockedEventEditModal pre-filled with caption + photo_class.
  const [editOpen, setEditOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let blobUrl = null;
    api.get(`/photos/${photo.id}/file`, { responseType: "blob" })
      .then((res) => { if (cancelled) return; blobUrl = URL.createObjectURL(res.data); setSrc(blobUrl); })
      .catch(() => { /* ignore — show placeholder */ });
    return () => { cancelled = true; if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [photo.id]);
  return (
    <div data-testid={`photo-${photo.id}`} data-event-row-id={photo.id} data-event-row-type="photo"
      className="relative block aspect-square bg-slate-100 rounded overflow-hidden border border-slate-200 group">
      {src ? <img src={src} alt={photo.caption || ""} onClick={() => onPreview && onPreview(src)}
        className="w-full h-full object-cover cursor-zoom-in" /> :
        <div className="w-full h-full grid place-items-center text-xs text-slate-400">…</div>}
      <div className="absolute top-1 left-1 flex items-center gap-1">
        {/* pt.56: photo_class label dropped (the Photo tray shows full
            metadata) — replaced with a small delete control on the left. */}
        {!locked && (
          <button data-testid={`photo-delete-${photo.id}`}
            onClick={async (e) => {
              e.preventDefault(); e.stopPropagation();
              const ok = await confirmAction({
                title: "Delete this photo?",
                message: "It's removed from this daily report; the original stays in the Photo tray.",
                confirmText: "Delete photo",
                icon: "trash",
              });
              if (!ok) return;
              try { await api.delete(`/photo-tags/${photo.id}`); reload(); }
              catch (err) { toast.error(err.response?.data?.detail || "Delete failed"); }
            }}
            title="Delete photo from this report"
            className="bg-black/55 hover:bg-red-600 text-white rounded p-1 max-md:min-h-[40px] max-md:min-w-[40px] max-md:grid max-md:place-items-center transition">
            <Trash2 className="h-3 w-3" />
          </button>
        )}
        {photo.evidentiary_flag && (
          <span data-testid={`photo-evidentiary-${photo.id}`} title="Evidentiary — locked record" className="bg-black/70 text-white rounded p-0.5">
            <Lock className="h-2.5 w-2.5" />
          </span>
        )}
        {photo.timestamp_mismatch_flag && (
          <span data-testid={`photo-tsmismatch-${photo.id}`} title="Device vs. server timestamp mismatch" className="bg-amber-500 text-white rounded p-0.5">
            <AlertTriangle className="h-2.5 w-2.5" />
          </span>
        )}
        {/* Bucket D Task 2 -- supplement marker overlaid on the
            photo card. Saffron pill matches SupplementBadge style. */}
        {photo.is_supplement && (
          <span data-testid={`photo-supplement-${photo.id}`}
            title={`Supplement to DR ${photo.parent_daily_report_date ? formatDateDisplay(photo.parent_daily_report_date) : (photo.parent_daily_report_id?.slice(0, 8) || "")}`}
            className="bg-amber-200 text-amber-900 rounded px-1 text-xs font-mono">
            SUPP
          </span>
        )}
        {/* Addition 4 follow-up -- post-submit-edited annotation.
            Saffron square stays visible; the edit-pencil only shows
            on hover when the parent DR is locked. */}
        {photo.post_submit_edited && (
          <span data-testid={`photo-edited-${photo.id}`}
            title={`Edited ${photo.edit_count || 0}x post-submit. ${photo.last_edit_reason ? `Last reason: ${photo.last_edit_reason}` : ""}`}
            className="bg-amber-300 text-amber-900 rounded px-1 text-xs font-mono">
            EDITED {photo.edit_count || 0}x
          </span>
        )}
        {/* Bucket F Phase 3 pulled-forward (Risk #4) -- AI lineage
            badge. Stamps when the tag row's created_via_ai_suggestion
            is true. Renders alongside the post-submit-edited badge so
            counsel + supers can see both lineage facts at a glance. */}
        {photo.created_via_ai_suggestion && (
          <span data-testid={`photo-ai-${photo.id}`}
            title={
              photo.ai_confidence_at_tag_time != null
                ? `Tagged from AI suggestion at ${Math.round(photo.ai_confidence_at_tag_time * 100)}% confidence`
                : "Tagged from AI suggestion"
            }
            className="bg-blue-100 text-blue-800 border border-blue-200 rounded px-1 text-xs font-mono inline-flex items-center gap-0.5">
            <Sparkles className="h-2 w-2" /> AI
          </span>
        )}
      </div>
      {locked && (
        <button data-testid={`photo-edit-locked-${photo.id}`}
          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setEditOpen(true); }}
          title="Edit photo (post-submit -- captures audit trail)"
          className="absolute top-1 right-1 bg-amber-600/90 hover:bg-amber-700 text-white rounded p-1 opacity-0 group-hover:opacity-100 max-md:opacity-100 transition-opacity">
          <Pencil className="h-2.5 w-2.5" />
        </button>
      )}
      {photo.caption && <div className="absolute bottom-0 left-0 right-0 bg-black/60 text-white text-xs px-1 py-0.5 truncate">{photo.caption}</div>}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="photo" eventId={photo.id} dailyReportId={dailyReportId}
          patchUrl={`/photos/${photo.id}`}
          fields={[
            { key: "caption", label: "Caption", type: "textarea" },
            { key: "photo_class", label: "Photo class", type: "select",
              options: ["progress", "discovery", "damage", "quality", "safety", "coordination", "closeout"]
                .map((c) => ({ value: c, label: c })) },
          ]}
          currentValues={{
            caption: photo.caption || "", photo_class: photo.photo_class || "progress",
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}

function VoicePanel({ rows }) {
  return (
    <PanelShell icon={Mic} title="Voice annotations" testid="dr-voice-panel" count={rows.length}>
      <div className="space-y-2">
        {rows.map((v) => (
          <div key={v.id} data-testid={`voice-${v.id}`} className="bg-slate-50 border border-slate-200 rounded p-2 text-xs">
            <div className="flex items-center gap-2 mb-1">
              <span className="pill pill-slate">{v.kind}</span>
              <span className="text-slate-400">{new Date(v.created_at).toLocaleTimeString()}</span>
            </div>
            <div className="text-slate-700">{v.transcript || <em className="text-slate-400">(no transcript)</em>}</div>
          </div>
        ))}
        {!rows.length && <div className="text-xs text-slate-400 py-4 text-center">
          No voice memos linked to this report. Use the floating mic to record one — then link it to this DR via the result sheet.
        </div>}
      </div>
    </PanelShell>
  );
}

function AuditPanel({ rows, userLabels }) {
  // 2026-02-XX (Abhi) -- audit trail collapses by default to a single
  // count chip; the rolling 30-row list expands on click. For most
  // DRs the trail is 95% empty rows of low-signal events, so the
  // collapsed default reclaims footer real estate.
  const [expanded, setExpanded] = useState(false);
  const count = rows.length;
  return (
    <PanelShell icon={ClipboardList} title="Audit trail" testid="dr-audit-panel" count={count} step={7}>
      {!expanded ? (
        <button
          data-testid="dr-audit-expand"
          onClick={() => setExpanded(true)}
          className="w-full flex items-center justify-between gap-3 text-left px-3 py-2 rounded border border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.05] transition"
        >
          <span className="text-sm text-slate-300 max-md:hidden">
            {count === 0
              ? "No audit entries yet."
              : <><strong className="tabular-nums">{count}</strong> audit {count === 1 ? "entry" : "entries"} — click to expand</>}
          </span>
          {/* mockup's one-line acc row on phones: just "Auto ▾" at right. */}
          <span className="md:hidden text-xs" style={{ color: "var(--v4-fg-muted)" }}>Auto ▾</span>
          {count > 0 && <ChevronDown className="h-4 w-4 text-slate-400 -rotate-90 transition-transform max-md:hidden" />}
        </button>
      ) : (
        <>
          <button
            data-testid="dr-audit-collapse"
            onClick={() => setExpanded(false)}
            className="mb-2 text-xs text-slate-400 hover:text-slate-200 inline-flex items-center gap-1"
          >
            <ChevronDown className="h-3.5 w-3.5" /> Collapse
          </button>
          <div className="space-y-1 max-h-60 overflow-y-auto">
            {rows.slice(0, 30).map((a) => (
              <div key={a.id} data-testid={`audit-${a.id}`} className="text-xs text-slate-600 flex gap-2 border-b border-slate-100 py-1">
                <span className="font-mono text-slate-400 w-32 shrink-0">{new Date(a.created_at).toLocaleTimeString()}</span>
                <span className="pill pill-slate">{a.op}</span>
                <span className="font-mono">{a.table_name}</span>
                <span className="text-slate-500">by {userLabels[a.actor_id] || (a.actor_id || "—").slice(0, 8)}</span>
              </div>
            ))}
            {!rows.length && <div className="text-xs text-slate-400 text-center py-3">No audit entries yet.</div>}
          </div>
        </>
      )}
    </PanelShell>
  );
}

// ------------------------------------------------------------
function DeliveryRowInline({ row, locked, dailyReportId, reload }) {
  // Addition 4 -- locked-DR edit modal toggle.
  const [editOpen, setEditOpen] = useState(false);
  return (
    <div data-testid={`delivery-row-${row.id}`} className="border-b border-slate-100 py-1.5 text-xs">
      <span className="font-medium mr-2">{row.vendor}</span>
      <span className="text-slate-700">{row.description}</span>
      {row.po_number && <span className="text-slate-400 ml-2 font-mono">{row.po_number}</span>}
      <span className={`pill ml-2 ${row.status === "received" ? "pill-green" : "pill-slate"}`}>{row.status}</span>
      <SupplementBadge row={row} />
      {locked && (
        <button data-testid={`delivery-edit-locked-${row.id}`} onClick={() => setEditOpen(true)}
          className="ml-2 text-amber-700 hover:underline">edit</button>
      )}
      {row.post_submit_edited && (
        <span className="ml-2 text-xs text-amber-700">
          (edited {row.edit_count || 0}× post-submit)
        </span>
      )}
      {locked && (
        <LockedEventEditModal
          open={editOpen} onOpenChange={setEditOpen}
          eventType="delivery" eventId={row.id} dailyReportId={dailyReportId}
          patchUrl={`/deliveries/${row.id}`}
          fields={[
            { key: "vendor", label: "Vendor" },
            { key: "description", label: "Description", type: "textarea" },
            { key: "po_number", label: "PO number" },
            { key: "delivery_date", label: "Delivery date", type: "text" },
            { key: "status", label: "Status", type: "select",
              options: ["scheduled", "received", "delayed", "cancelled"].map((s) => ({ value: s, label: s })) },
          ]}
          currentValues={{
            vendor: row.vendor, description: row.description,
            po_number: row.po_number, delivery_date: row.delivery_date,
            status: row.status,
          }}
          onSaved={reload}
        />
      )}
    </div>
  );
}


// Bucket D Task 3 fix D -- outcome capture button on QC rows that
// triggered a rework cascade. Modal collects actual rework days +
// notes, POSTs /qc-issues/{id}/record-rework-outcome which updates
// the QC, flips the synthetic delay to resolved, re-runs cascade+RAG.
// QC rework days (2026-05-17) -- actual rework captured in WORKING
// DAYS (was hours), consistent with the days-based estimate.
// ------------------------------------------------------------
function ReworkOutcomeButton({ qcIssue, reload }) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (days === "" || Number(days) < 0) {
      toast.error("Actual rework (days) required (>=0)"); return;
    }
    setBusy(true);
    try {
      await api.post(`/qc-issues/${qcIssue.id}/record-rework-outcome`, {
        rework_actual_days: Number(days),
        rework_outcome_notes: notes || null,
      });
      toast.success("Rework outcome recorded -- cascade re-running");
      setOpen(false);
      reload();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Outcome capture failed");
    } finally { setBusy(false); }
  };
  return (
    <>
      <button data-testid={`qc-rework-outcome-${qcIssue.id}`} onClick={() => setOpen(true)}
        className="ml-2 text-xs px-2 py-0.5 rounded bg-emerald-600 text-white hover:bg-emerald-700">
        Record outcome
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>Record rework outcome</DialogTitle></DialogHeader>
          <div className="space-y-3 text-sm">
            <div className="text-xs text-slate-500">
              QC: "{(qcIssue.description || "").slice(0, 80)}…"
              <br />
              Estimated: {qcIssue.rework_duration_days}d.
              Synthetic delay: <span className="font-mono">{(qcIssue.linked_synthetic_delay_id || "").slice(0, 8)}</span>.
            </div>
            <div>
              <FieldLabel required>Actual rework (days)</FieldLabel>
              <input data-testid="qc-rework-actual-days" type="number" inputMode="decimal" step="0.5" min="0"
                value={days} onChange={(e) => setDays(e.target.value)}
                className="block w-full h-9 px-2 rounded border border-slate-300 text-sm" />
            </div>
            <div>
              <FieldLabel optional>Notes</FieldLabel>
              <textarea data-testid="qc-rework-outcome-notes" rows={3}
                value={notes} onChange={(e) => setNotes(e.target.value)}
                placeholder="What went better or worse than expected?"
                className="block w-full px-2 py-1 rounded border border-slate-300 text-sm" />
            </div>
            <div className="flex justify-end gap-2">
              <button onClick={() => setOpen(false)} className="h-9 px-3 rounded bg-slate-200 text-slate-700 text-sm hover:bg-slate-300">Cancel</button>
              <button data-testid="qc-rework-outcome-submit" onClick={submit} disabled={busy}
                className="h-9 px-3 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-50">
                {busy ? "Recording…" : "Record outcome"}
              </button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ------------------------------------------------------------
// Bucket D Task 2 -- per-row supplement badge. Container DR panels
// display this next to event-row titles when the row is is_supplement
// =true, so the user can tell at a glance which rows are filed back
// against an older locked DR. Backend enriches parent_daily_report_
// date in the /context bundle (Task 2) so this renders without an
// extra fetch.
// ------------------------------------------------------------
function SupplementBadge({ row }) {
  if (!row?.is_supplement) return null;
  const parentDate = row.parent_daily_report_date
    ? formatDateDisplay(row.parent_daily_report_date)
    : row.parent_daily_report_id?.slice(0, 8);
  return (
    <span data-testid={`supplement-badge-${row.id}`}
      title="Filed after the parent daily report was submitted"
      className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded bg-amber-200 text-amber-900 text-xs font-mono">
      SUPP→{parentDate || "?"}
    </span>
  );
}

// ------------------------------------------------------------
// Bucket D Addition 5 (2026-05-12) -- Supplements section HIDDEN.
//
// The supplement flow (file new events against a "container DR"
// pointing back to the locked parent) was replaced by the
// post_submit_added flag (file events directly on the locked DR
// with an add_reason). Existing supplement records stay in the DB
// for audit + TIA preservation, but the UI no longer renders them
// here -- they appear inline on each event panel like any other
// row, with their SUPP→<date> badge per SupplementBadge above.
//
// The function below is preserved (commented-out call site in the
// DR detail render) for forensic recovery if we ever need to expose
// the legacy supplement list as a debug surface. The early return
// keeps it from rendering anything in the meantime.
function SupplementsAppendix(_props) {
  return null;
}
// Legacy SupplementsAppendix implementation removed (2026-05-12). The
// supplement flow is replaced by post_submit_added on each event
// collection. Pre-existing supplement records still surface inline on
// their event panels with the SUPP→<date> badge (see SupplementBadge
// above) and are preserved in the DB / audit chain. Git history holds
// the legacy implementation if it ever needs to be revived as a debug
// surface.

// ------------------------------------------------------------
//  Tiny field helpers
// ------------------------------------------------------------
function Field({ label, value, onChange, type = "text", disabled, testid }) {
  return (
    <label className="text-xs flex flex-col gap-1">
      <span className="text-slate-500">{label}</span>
      <input data-testid={testid} type={type} defaultValue={value} disabled={disabled}
        onBlur={(e) => onChange(e.target.value)}
        className="h-8 px-2 rounded border border-slate-200 bg-white text-sm" />
    </label>
  );
}
function CheckField({ label, value, onChange, disabled, testid }) {
  return (
    <label className="text-xs flex items-center gap-2 bg-slate-50 border border-slate-200 rounded px-2 py-1.5 cursor-pointer">
      <input data-testid={testid} type="checkbox" checked={value} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

// Surface backend errors as helpfully as possible — never "Load failed".
function formatErr(e, action = "Action") {
  const r = e.response;
  if (!r) return `${action} failed: ${e.message || "network error"}`;
  const detail = r.data?.detail;
  if (Array.isArray(detail)) {
    const lines = detail.map((d) => `${(d.loc || []).slice(-1)[0] || "?"}: ${d.msg || d}`);
    return `${action} failed (${r.status}): ${lines.join("; ").slice(0, 240)}`;
  }
  if (typeof detail === "string") return `${action} failed (${r.status}): ${detail}`;
  return `${action} failed (${r.status})`;
}

/* ============================================================
 *  v7 GUIDED DAILY-REPORT FLOW  (phones only)
 *  ------------------------------------------------------------
 *  A client state machine layered over the SAME bundle + handlers
 *  the desktop layout uses. Screens:
 *     landing → activity[0..N-1] → unplanned → review
 *  Every write hits an existing endpoint:
 *     grid PATCH      /daily-report-activities/{rowId}
 *     presence create /daily-reports/{id}/presence
 *     presence delete /daily-presence/{rid}
 *     unplanned       /daily-reports/{id}/unplanned-activity
 *     issues          existing Delay/Safety/QC/RFI/Coord panels
 *     photos          existing PhotosPanel upload/tag plumbing
 *     voice notes     POST /voice/annotate + GET /voice/annotations
 *     submit          the parent's submit()/no-work/ locked rules
 *  Desktop (>=768px) never mounts this — DailyReportDetail returns
 *  its original layout there.
 * ============================================================ */

// Optional offline photo queue owned by the parallel offline agent
// (lib/drPhotoQueue.js). We lazy-require it so this file compiles and
// runs whether or not that module exists yet; when present we use
// queuePhoto()/listPendingPhotos(). Mirrors the existing lazy-require
// pattern used for lib/drOfflineSync above.
function _photoQueue() {
  try {
    // eslint-disable-next-line global-require
    return require("../lib/drPhotoQueue");
  } catch { return null; }
}

// Pill tint for an activity's rollup status (matches GridStatusPill vocab).
function flowStatusOf(row) {
  const s = row.auto_status || row.status_at_end_of_day || "not_started";
  if (s === "blocked") return { label: "Blocked", cls: "dr-flow-pill-rd" };
  if (s === "complete") return { label: "Complete", cls: "dr-flow-pill-gr" };
  if (s === "in_progress") return { label: "In progress", cls: "dr-flow-pill-bl" };
  const pct = row.pct_complete_end_of_day || 0;
  if (pct >= 100) return { label: "Complete", cls: "dr-flow-pill-gr" };
  if (pct > 0) return { label: "In progress", cls: "dr-flow-pill-bl" };
  return { label: "Not started", cls: "dr-flow-pill-sl" };
}
function flowFillColor(row) {
  const s = row.auto_status || row.status_at_end_of_day || "not_started";
  const pct = row.pct_complete_end_of_day || 0;
  if (s === "blocked") return "var(--v4-fg-faint, #868a93)";
  if (s === "complete" || pct >= 100) return "#34d399";
  if (pct > 0) return "#6cb0f2";
  return "#868a93";
}

// A reusable bottom sheet (dim backdrop + slide-up panel). Reuses the
// existing dr-msheet CSS conventions; new dr-flow-* classes are added
// to index.css. Backdrop click + the grip close it.
function FlowSheet({ open, onClose, title, subtitle, children, testid }) {
  if (!open) return null;
  return (
    <div className="dr-flow-ov" data-testid={testid} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dr-flow-sheet" role="dialog" aria-modal="true">
        <div className="dr-flow-grip" onClick={onClose} />
        {title && (
          <div className="dr-flow-sheet-head">
            <div className="dr-flow-sheet-title">{title}</div>
            {subtitle && <div className="dr-flow-sheet-sub">{subtitle}</div>}
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

// Voice notes: the flow renders components/dr/DrVoiceNotes (spec D —
// kind="voice_note", NO transcription/extraction, IndexedDB offline queue
// that drains to /voice/annotate, blob playback). Imported at the top.

function GuidedDrFlow({
  id, report, project, grid, presence, photos, voiceAnnotations,
  delays, safetyEvents, qcIssues, rfis, deliveries,
  locked, reload, onBack, busy, submit, noWorkModal, setNoWorkModal,
  submitNoWorkDay, approve, onDownloadPdf,
}) {
  // Editable "today's planned" activities (Field Work section) in grid order.
  const activities = (grid || []).filter((r) => gridSectionOf(r.work_category) === 2);
  const unplannedRows = activities.filter((r) => (r.activity_code || "").startsWith("AF"));
  // Screens: 0..N-1 activities, then "unplanned", then "review".
  const N = activities.length;
  const UNPLANNED = N;
  const REVIEW = N + 1;

  // "landing" | number(screen index)
  const [screen, setScreen] = useState("landing");
  const [returnToReview, setReturnToReview] = useState(false);
  // Which bottom sheet is open on the current activity screen.
  const [sheet, setSheet] = useState(null); // {kind, activity} | null
  const [queueCount, setQueueCount] = useState(0);
  // Locked-DR (spec B): ONE reason, entered once on any activity screen,
  // feeds edit_reason on grid PATCHes and add_reason on photo tags. Event
  // sheets keep their own LockedAddReasonBlock (existing panel behavior).
  const [lockedReason, setLockedReason] = useState("");
  const lockedReasonOk = isFlowReasonValid(lockedReason);
  // Per-activity photo upload busy flag.
  const [photoBusy, setPhotoBusy] = useState(false);
  // Live big-% mirror while the slider is being dragged (EodSlider owns the
  // drag; its bubbled input event keeps the mockup's big readout live).
  // Screen changes also reset scroll so each step starts at its title.
  const [livePct, setLivePct] = useState(null);
  useEffect(() => {
    setLivePct(null);
    try { window.scrollTo({ top: 0 }); } catch { /* ignore */ }
  }, [screen]);
  // Crew chips flip "off" optimistically during the delete-with-undo window
  // (mockup behavior); Undo restores them instantly.
  const [pendingOffIds, setPendingOffIds] = useState(() => new Set());
  // Queued v7 voice notes (offline) — counted into the sync chip and used
  // to decide whether the landing shows the Voice notes section.
  const [voiceNotePending, setVoiceNotePending] = useState(0);
  // Spec D: only voice-note-as-record rows surface in the flow (intake
  // memos with other kinds stay a separate feature).
  const voiceNoteRows = (voiceAnnotations || []).filter((v) => v.kind === "voice_note");

  // Pending-sync chip: photos + voice + queued requests combined.
  const refreshQueueCount = useCallback(async () => {
    let total = 0;
    try {
      // eslint-disable-next-line global-require
      const { getQueueSize } = require("../lib/drOfflineSync");
      total += getQueueSize() || 0;
    } catch { /* ignore */ }
    try {
      // eslint-disable-next-line global-require
      const vq = require("../lib/drVoiceQueue");
      const clips = await vq.listPending(id);
      total += (clips || []).length;
    } catch { /* ignore */ }
    try {
      // v7 voice notes queue their own store (drains to /voice/annotate,
      // NOT the transcribe-and-extract intake) — count those too.
      const noteClips = await listPendingVoiceNotes(id);
      total += (noteClips || []).length;
      setVoiceNotePending((noteClips || []).length);
    } catch { /* ignore */ }
    try {
      const pq = _photoQueue();
      if (pq && pq.listPendingPhotos) {
        const pend = await pq.listPendingPhotos(id);
        total += (pend || []).length;
      }
    } catch { /* ignore */ }
    setQueueCount(total);
  }, [id]);
  useEffect(() => {
    refreshQueueCount();
    const on = () => refreshQueueCount();
    window.addEventListener("online", on);
    window.addEventListener("offline", on);
    const t = setInterval(refreshQueueCount, 6000);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", on);
      clearInterval(t);
    };
  }, [refreshQueueCount]);

  // --- CARRY-FORWARD CREW (spec A) --------------------------------
  // On a DRAFT report's first mobile open with zero presence rows,
  // fetch the project's most recent PRIOR report's presence and clone
  // those rows into THIS report once. Guard flag lives in localStorage.
  const [carriedWeekday, setCarriedWeekday] = useState(null);
  useEffect(() => {
    if (locked) return;
    if (!id || !project?.id) return;
    if ((presence || []).length > 0) return;
    // Guard key per spec: "ice-dr-carried-<reportId>".
    const guardKey = `ice-dr-carried-${id}`;
    if (localStorage.getItem(guardKey)) return;
    let cancelled = false;
    (async () => {
      try {
        const { data: reports } = await api.get(`/projects/${project.id}/daily-reports`);
        const prior = (reports || [])
          .filter((r) => r.id !== id && r.report_date < report.report_date)
          .sort((a, b) => (a.report_date < b.report_date ? 1 : -1))[0];
        if (!prior) { localStorage.setItem(guardKey, "none"); return; }
        const { data: prevPresence } = await api.get(`/daily-reports/${prior.id}/presence`)
          .catch(() => ({ data: [] }));
        if (cancelled || !Array.isArray(prevPresence) || !prevPresence.length) {
          localStorage.setItem(guardKey, "none"); return;
        }
        // Mark the guard BEFORE the writes so a mid-flight remount can't
        // double-carry; if a write fails the super can add crew manually.
        localStorage.setItem(guardKey, "done");
        // Only keep a task link if that task is on TODAY's grid — a stale
        // link would make the row invisible on every activity screen.
        const todaysTaskIds = new Set((grid || []).map((g) => g.schedule_activity_id).filter(Boolean));
        for (const p of prevPresence) {
          await api.post(`/daily-reports/${id}/presence`, {
            trade: p.trade || "",
            company_name: p.company_name || "",
            headcount: Number(p.headcount) || 0,
            hours_per_person: Number(p.hours_per_person) || 0,
            notes: p.notes || "",
            schedule_activity_id: todaysTaskIds.has(p.schedule_activity_id) ? p.schedule_activity_id : null,
            shift: p.shift || "first",
            daily_report_id: id,
          }).catch(() => { /* skip a bad row, keep going */ });
        }
        if (cancelled) return;
        let weekday = "";
        try { weekday = new Date(`${prior.report_date}T00:00:00`).toLocaleDateString(undefined, { weekday: "long" }); } catch { /* ignore */ }
        setCarriedWeekday(weekday || "the prior report");
        reload();
        toast.success(`Crew carried forward from ${weekday || "the prior report"}. Tap a chip to remove anyone who's off.`);
      } catch {
        // Non-fatal: leave presence empty, super adds crew by hand.
        try { localStorage.setItem(guardKey, "error"); } catch { /* ignore */ }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, project?.id, locked]);

  // ---- shared write handlers (SAME endpoints as desktop) ----------
  const updateRow = async (rowId, fields) => {
    const row = (grid || []).find((r) => r.id === rowId) || {};
    const payload = { ...fields };
    if (row.task_updated_at) payload.task_last_modified_at = row.task_updated_at;
    // Spec B — on a locked DR the grid PATCH requires an edit_reason; the
    // activity screen's reason card supplies it (same field the desktop
    // LockedEventEditModal sends).
    if (locked) {
      if (!lockedReasonOk) {
        toast.error("Enter the edit reason at the top of the screen first.");
        return;
      }
      payload.edit_reason = lockedReason.trim();
    }
    try {
      await api.patch(`/daily-report-activities/${rowId}`, payload);
      reload();
    } catch (e) {
      if (e.response?.status === 409) {
        toast.error("Schedule changed externally — reopen the report to pull the latest.");
      } else {
        toast.error(formatErr(e, "Update activity"));
      }
    }
  };
  const deletePresence = (rid) => {
    if (pendingOffIds.has(rid)) return; // delete already queued
    setPendingOffIds((s) => { const n = new Set(s); n.add(rid); return n; });
    deleteWithUndo({
      label: "Crew removed",
      onCommit: async () => {
        try { await api.delete(`/daily-presence/${rid}`); reload(); }
        catch (e) {
          toast.error(formatErr(e, "Remove crew"));
          setPendingOffIds((s) => { const n = new Set(s); n.delete(rid); return n; });
        }
      },
      onRestore: () => setPendingOffIds((s) => { const n = new Set(s); n.delete(rid); return n; }),
    });
  };

  // Photos (spec A) — upload to the tray, then tag each photo to THIS grid
  // row via the EXISTING POST /photo-tags plumbing (daily_report_activity_id
  // is the tag's event ref). Locked DRs pass the reason as add_reason.
  const uploadActivityPhotos = async (row, files) => {
    if (!files.length) return;
    if (locked && !lockedReasonOk) {
      toast.error("Enter the edit reason at the top of the screen first.");
      return;
    }
    setPhotoBusy(true);
    // Offline queue (spec F.4): a network failure (no err.response) means we're
    // offline — stash the bytes in lib/drPhotoQueue so they upload + tag to
    // this activity on reconnect, instead of silently losing the photo. Any
    // other error (validation / locked-add / server reject) still surfaces.
    const pq = _photoQueue();
    let tagged = 0;
    let queued = 0;
    let hardErr = null;
    try {
      for (const file of files) {
        try {
          const fd = new FormData();
          fd.append("file", file);
          fd.append("project_id", project.id);
          fd.append("capture_source", "manual_upload");
          const r = await api.post(`/photo-tray`, fd, { headers: { "Content-Type": "multipart/form-data" } });
          const trayId = r.data?.id;
          if (!trayId) continue;
          await api.post(`/photo-tags`, {
            tray_photo_id: trayId,
            daily_report_id: id,
            daily_report_activity_id: row.id,
            photo_class: "progress",
            ...(locked ? { add_reason: lockedReason.trim() } : {}),
          });
          tagged += 1;
        } catch (e) {
          // No response object => network/offline. Never lose the photo:
          // queue it (tagged to THIS activity) for a reconnect drain.
          if (!e?.response && pq?.queuePhoto) {
            try {
              await pq.queuePhoto({
                blob: file,
                mimeType: file.type || "image/jpeg",
                dailyReportId: id,
                projectId: project.id,
                activityRowId: row.id,
                photoClass: "progress",
              });
              queued += 1;
              continue;
            } catch { /* fall through to hardErr */ }
          }
          hardErr = e;
        }
      }
    } finally { setPhotoBusy(false); }
    if (queued > 0) {
      toast(`${queued} photo${queued === 1 ? "" : "s"} saved — will upload when you reconnect.`);
    }
    if (tagged > 0) { reload(); toast.success(`${tagged} photo${tagged === 1 ? "" : "s"} added.`); }
    if (hardErr) {
      if (!handleLockedAddError(hardErr, "photo")) {
        toast.error(formatErr(hardErr, "Photo upload"));
      }
    } else if (tagged === 0 && queued === 0) {
      toast.error("Upload failed — try again.");
    }
  };

  const goActivity = (idx, opts = {}) => {
    setReturnToReview(!!opts.returnToReview);
    setSheet(null);
    setScreen(idx);
  };

  // Submit wrappers that navigate back to the DR list on success (spec E).
  // A successful submit/approve stamps sessionStorage so the list can flag
  // the just-submitted card.
  const backToList = () => {
    try { sessionStorage.setItem("ice-dr-just-submitted", id); } catch { /* ignore */ }
    onBack();
  };
  const submitAndReturn = () => submit(backToList);
  const submitNoWorkAndReturn = () => submitNoWorkDay(backToList);
  // Spec B — review on a SUBMITTED report keeps the existing next action:
  // Approve (same handler the desktop header button calls).
  const approveAndReturn = async () => {
    const ok = await approve();
    if (ok) backToList();
  };

  // Progress dots + step label.
  const stepBar = (activeIdx) => {
    const steps = [];
    for (let i = 0; i < N; i++) steps.push(i);
    steps.push(UNPLANNED, REVIEW);
    const label = activeIdx < N
      ? <><b>Activity {activeIdx + 1}</b> of {N}</>
      : activeIdx === UNPLANNED ? <b>Unplanned work</b> : <b>Review</b>;
    return (
      <div className="dr-flow-steps" data-testid="dr-flow-steps">
        <button className="dr-flow-steps-x" onClick={() => { setSheet(null); setScreen("landing"); }}
          aria-label="Back to start" data-testid="dr-flow-exit"><X className="h-4.5 w-4.5" /></button>
        <span className="dr-flow-steps-lbl">{label}</span>
        <div className="dr-flow-dots">
          {steps.map((s, k) => (
            <span key={k} className={`dr-flow-dot${s === activeIdx ? " on" : (k < steps.indexOf(activeIdx) ? " done" : "")}`} />
          ))}
        </div>
      </div>
    );
  };

  const longDate = (() => {
    try { return new Date(`${report.report_date}T00:00:00`).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" }); }
    catch { return formatDateDisplay(report.report_date); }
  })();
  const statusPill = (
    <span className={`v4-pill v4-pill-${report.status === "approved" ? "closed" : report.status === "submitted" ? "submitted" : "draft"}`}>
      {report.status === "approved" ? "Approved" : report.status === "submitted" ? "Submitted" : "Draft"}
    </span>
  );

  // ============ LANDING ============
  if (screen === "landing") {
    return (
      <div className="dr-flow" data-testid="dr-flow-landing">
        {queueCount > 0 && (
          <div className="dr-flow-syncchip" data-testid="dr-flow-syncchip">
            <span className="dr-flow-rec-dot" /> {queueCount} item{queueCount === 1 ? "" : "s"} will sync
          </div>
        )}
        <div className="dr-flow-hero">
          {/* back-to-list — the landing is the flow's root, so it carries
              the exit to the DR list (the step-bar X only returns HERE). */}
          <div className="dr-flow-hero-row" style={{ marginBottom: 8 }}>
            <button type="button" className="dr-flow-back" data-testid="dr-flow-back-list"
              onClick={onBack} aria-label="Back to daily reports" style={{ padding: "8px 12px" }}>
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span style={{ marginLeft: "auto" }}>{statusPill}</span>
          </div>
          <div className="dr-flow-hero-t">Daily report</div>
          <div className="dr-flow-hero-s">
            {longDate} · {project?.name || "Project"} · {N} {N === 1 ? "activity" : "activities"}
            {carriedWeekday ? `, carried from ${carriedWeekday}` : ""}
          </div>
          {locked && (
            <div className="dr-flow-hero-row">
              <span className="dr-flow-locked-tag"><Lock className="h-3 w-3" /> Submitted</span>
            </div>
          )}
          {N > 0 ? (
            <button className="dr-flow-cta" data-testid="dr-flow-start" onClick={() => goActivity(0)}>
              {locked ? "Review report" : "Start"} <ChevronRight className="h-4 w-4" />
            </button>
          ) : (
            <div className="dr-flow-empty">
              <ClipboardList className="h-8 w-8" style={{ color: "var(--v4-fg-faint)" }} />
              <div className="dr-flow-empty-t">No activities scheduled</div>
              <div className="dr-flow-empty-s">Nothing on the plan today. Log work done off-schedule, or file a no-work day.</div>
              <button className="dr-flow-cta" data-testid="dr-flow-empty-unplanned" onClick={() => setSheet({ kind: "unplanned" })}>
                <Plus className="h-4 w-4" /> Add unplanned activity
              </button>
              {!locked && (
                <button className="dr-flow-alt" data-testid="dr-flow-empty-nowork" onClick={submitAndReturn}>
                  File as no-work day
                </button>
              )}
            </div>
          )}
        </div>

        {N > 0 && (
          <>
            <div className="dr-flow-rowlbl">Today's activities</div>
            <div className="dr-flow-mini">
              {activities.map((r, i) => {
                const st = flowStatusOf(r);
                return (
                  <button key={r.id} className="dr-flow-minirow" data-testid={`dr-flow-minirow-${i}`} onClick={() => goActivity(i)}>
                    <span className="dr-flow-mini-dot" style={{ background: flowFillColor(r) }} />
                    <span className="dr-flow-mini-name">{r.activity_description || r.activity_code}</span>
                    <span className="dr-flow-mini-r">
                      <span className={`dr-flow-pill ${st.cls}`}>{r.pct_complete_end_of_day || 0}%</span>
                      <ChevronRight className="h-3.5 w-3.5" />
                    </span>
                  </button>
                );
              })}
            </div>
          </>
        )}

        <div className="dr-flow-rowlbl">Site-wide</div>
        <div className="dr-flow-bigcard" style={{ cursor: "default" }}>
          <Cloud className="h-6 w-6" style={{ color: "#34d399" }} />
          <div className="dr-flow-big-body">
            <div className="dr-flow-big-t">Weather</div>
            <div className="dr-flow-big-s">
              {[
                report.weather_conditions
                  ? report.weather_conditions.charAt(0).toUpperCase() + report.weather_conditions.slice(1)
                  : null,
                (report.weather_temp_high_f != null || report.weather_temp_low_f != null)
                  ? `${report.weather_temp_high_f != null ? Math.round(report.weather_temp_high_f) : "—"}° / ${report.weather_temp_low_f != null ? Math.round(report.weather_temp_low_f) : "—"}°`
                  : null,
                report.weather_fetched_at ? "auto-added" : null,
              ].filter(Boolean).join(" · ") || "Not set"}
            </div>
          </div>
          {report.weather_conditions && <CheckCircle2 className="h-4 w-4" style={{ color: "#34d399" }} />}
        </div>
        <button className="dr-flow-bigcard" data-testid="dr-flow-unplanned-card" onClick={() => setSheet({ kind: "unplanned" })}>
          <Plus className="h-6 w-6" style={{ color: "#34d399" }} />
          <div className="dr-flow-big-body">
            <div className="dr-flow-big-t">Unplanned work</div>
            <div className="dr-flow-big-s">Log anything done off the schedule</div>
          </div>
          <ChevronRight className="h-4 w-4" style={{ color: "var(--v4-fg-muted)" }} />
        </button>

        {/* spec D — voice notes list (playable) on the landing whenever the
            report has notes; recording lives on the Review step. */}
        {(voiceNoteRows.length > 0 || voiceNotePending > 0) && (
          <>
            <div className="dr-flow-rowlbl">Voice notes ({voiceNoteRows.length + voiceNotePending})</div>
            <DrVoiceNotes reportId={id} notes={voiceNoteRows} reload={reload}
              allowRecord={false} onPendingChange={setVoiceNotePending} />
          </>
        )}

        {/* Sheets reachable from the landing */}
        <FlowSheets
          sheet={sheet} setSheet={setSheet}
          id={id} project={project} report={report} grid={grid}
          delays={delays} safetyEvents={safetyEvents} qcIssues={qcIssues} rfis={rfis}
          locked={locked} reload={reload}
        />
        {/* No-work-day dialog (reuses the parent's state + handler) */}
        <FlowNoWorkDialog noWorkModal={noWorkModal} setNoWorkModal={setNoWorkModal}
          submitNoWorkDay={submitNoWorkAndReturn} busy={busy} />
      </div>
    );
  }

  // ============ ACTIVITY SCREEN ============
  if (typeof screen === "number" && screen < N) {
    const r = activities[screen];
    const st = flowStatusOf(r);
    const thisPresence = (presence || []).filter((p) => p.schedule_activity_id && p.schedule_activity_id === r.schedule_activity_id);
    // Photos are tagged to a grid row via daily_report_activity_id (the
    // photo-tag event ref uploadActivityPhotos writes).
    const taskPhotos = (photos || []).filter((p) => p.daily_report_activity_id === r.id);
    const issueItems = listIssuesForTask(r.schedule_activity_id, { delays, safetyEvents, qcIssues, rfis, deliveries });
    const thisIssues = issueItems.length;
    const plannedTick = typeof r.planned_pct_complete === "number" ? r.planned_pct_complete
      : (typeof r.planned_pct === "number" ? r.planned_pct : null);

    const footerNext = () => {
      if (returnToReview) { setReturnToReview(false); setScreen(REVIEW); return; }
      if (screen < N - 1) setScreen(screen + 1);
      else setScreen(UNPLANNED);
    };
    const footerBack = () => {
      if (screen > 0) setScreen(screen - 1);
      else setScreen("landing");
    };

    return (
      <div className="dr-flow" data-testid={`dr-flow-activity-${screen}`}>
        {stepBar(screen)}
        <div className="dr-flow-body">
          {/* spec B — compact reason card; its value feeds edit_reason on
              grid PATCHes + add_reason on photo tags (event sheets keep
              their own LockedAddReasonBlock). */}
          {locked && (
            <LockedReasonCard value={lockedReason} onChange={setLockedReason} />
          )}
          <div className="dr-flow-actname">{r.activity_description || r.activity_code}</div>
          <div className="dr-flow-subline">
            {project?.name || "Project"} · {longDate} · <span className={`dr-flow-pill ${st.cls}`}>{st.label}</span>
          </div>

          {/* Progress card — reuses EodSlider (B1 gating, clamps, override).
              The bubbled input event mirrors the drag into the big readout. */}
          <div className="dr-flow-block">
            <div className="dr-flow-block-h"><span className="dr-flow-progress-ic">%</span> Progress today</div>
            <div className="dr-flow-prog"
              onInput={(e) => { const t = e.target; if (t && t.type === "range") setLivePct(Number(t.value) || 0); }}>
              <div className="dr-flow-prog-big" data-testid={`dr-flow-pct-${screen}`}>{livePct ?? (r.pct_complete_end_of_day || 0)}%</div>
              <div className="dr-flow-slider-wrap">
                {plannedTick != null && (
                  <span className="dr-flow-sug" style={{ left: `${Math.max(0, Math.min(100, plannedTick))}%` }} />
                )}
                {/* Locked DR: the slider unlocks once the reason card is
                    valid — updateRow then PATCHes with edit_reason (the
                    exact desktop locked-edit plumbing). */}
                <EodSlider rowId={r.id} initialValue={r.pct_complete_end_of_day || 0}
                  locked={locked && !lockedReasonOk} updateRow={updateRow}
                  activityCode={r.activity_code}
                  gate={r.slider_gate} />
              </div>
              <div className="dr-flow-prog-meta">
                was {r.pct_complete_start_of_day || 0}%
                {plannedTick != null ? ` · planned ${plannedTick}%` : ""}
              </div>
            </div>
          </div>

          {/* Crew card */}
          <div className="dr-flow-block">
            <div className="dr-flow-block-h">
              <Users className="h-4 w-4" style={{ color: "#34d399" }} /> Crew
              {carriedWeekday && thisPresence.length > 0 && <span className="dr-flow-block-n">carried from {carriedWeekday}</span>}
            </div>
            <div className="dr-flow-crewline">
              {thisPresence.map((p) => (
                <button key={p.id} type="button" data-testid={`dr-flow-crewchip-${p.id}`}
                  className={`dr-flow-crewchip${pendingOffIds.has(p.id) ? " off" : ""}`}
                  onClick={() => !locked && deletePresence(p.id)}
                  title={locked ? "Locked report" : "Tap to remove"}>
                  <CheckCircle2 className="h-3.5 w-3.5 dr-flow-ck" />
                  {(p.company_name || p.trade)}{p.headcount ? ` · ${p.headcount}×${p.hours_per_person || 0}h` : ""}
                </button>
              ))}
              {!locked && (
                <button type="button" className="dr-flow-addc" data-testid="dr-flow-crew-add"
                  onClick={() => setSheet({ kind: "crew", activity: r })}>
                  <Plus className="h-3.5 w-3.5" /> Add
                </button>
              )}
            </div>
            {thisPresence.length === 0 && (
              <div className="dr-flow-muted-note">No crew on this activity yet.</div>
            )}
          </div>

          {/* Issues card */}
          <div className="dr-flow-block">
            <div className="dr-flow-block-h"><AlertTriangle className="h-4 w-4" style={{ color: "#34d399" }} /> Anything wrong?</div>
            <div className="dr-flow-issuebar">
              {thisIssues === 0
                ? <span className="dr-flow-noissue"><CheckCircle2 className="h-3.5 w-3.5" /> No issues</span>
                : <span className="dr-flow-hasissue"><AlertTriangle className="h-3.5 w-3.5" /> {thisIssues} logged</span>}
              <span className="dr-flow-qadd">
                <button type="button" className="dr-flow-qa" data-testid="dr-flow-issue-delay" onClick={() => setSheet({ kind: "delay", activity: r })}><AlertTriangle className="h-3 w-3" /> Delay</button>
                <button type="button" className="dr-flow-qa" data-testid="dr-flow-issue-rfi" onClick={() => setSheet({ kind: "rfi", activity: r })}><MessageSquare className="h-3 w-3" /> RFI</button>
                <button type="button" className="dr-flow-qa" data-testid="dr-flow-issue-safety" onClick={() => setSheet({ kind: "safety", activity: r })}><ShieldAlert className="h-3 w-3" /> Safety</button>
                <button type="button" className="dr-flow-qa" data-testid="dr-flow-issue-qc" onClick={() => setSheet({ kind: "qc", activity: r })}><ClipboardCheck className="h-3 w-3" /> QC</button>
                <button type="button" className="dr-flow-qa" data-testid="dr-flow-issue-coord" onClick={() => setSheet({ kind: "coordination", activity: r })}><Truck className="h-3 w-3" /> Coord</button>
              </span>
            </div>
            {/* spec A — this task's logged issues, one line each. */}
            {issueItems.length > 0 && (
              <div className="dr-flow-items" data-testid={`dr-flow-issue-list-${screen}`}>
                {issueItems.map((it) => (
                  <div key={it.key} className="dr-flow-it">
                    <span className={`dr-flow-pill ${it.cls}`}>{it.kind}</span>
                    <span className="dr-flow-it-tx">{it.text}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Photos card — capture/upload goes straight to the tray and is
              tagged to THIS grid row (uploadActivityPhotos). Locked DRs
              stay uploadable once the reason card is valid (add_reason). */}
          <div className="dr-flow-block">
            <div className="dr-flow-block-h">
              <ImageIcon className="h-4 w-4" style={{ color: "#34d399" }} /> Photos
              {taskPhotos.length > 0 && <span className="dr-flow-block-n">{taskPhotos.length}</span>}
            </div>
            <div className="dr-flow-thumbs">
              {taskPhotos.map((p) => <FlowThumb key={p.id} photo={p} />)}
              {(!locked || lockedReasonOk) && (
                <label className="dr-flow-thumb-add" data-testid="dr-flow-photo-add">
                  {photoBusy
                    ? <Loader2 className="h-5 w-5 animate-spin" />
                    : <><ImageIcon className="h-5 w-5" /><span>Add</span></>}
                  <input type="file" accept="image/*" multiple className="hidden" disabled={photoBusy}
                    data-testid="dr-flow-photo-input"
                    onChange={(e) => { uploadActivityPhotos(r, Array.from(e.target.files || [])); e.target.value = ""; }} />
                </label>
              )}
              {locked && !lockedReasonOk && taskPhotos.length === 0 && (
                <div className="dr-flow-muted-note">No photos on this activity.</div>
              )}
            </div>
          </div>
        </div>

        <div className="dr-flow-nav" data-testid="dr-flow-nav">
          <button className="dr-flow-back" data-testid="dr-flow-back" onClick={footerBack}><ChevronLeft className="h-4 w-4" /></button>
          <button className="dr-flow-next" data-testid="dr-flow-next" onClick={footerNext}>
            {returnToReview
              ? <>Finish <CheckCircle2 className="h-4 w-4" /></>
              : screen < N - 1
                ? <>Confirm &amp; next <ChevronRight className="h-4 w-4" /></>
                : <>Continue <ChevronRight className="h-4 w-4" /></>}
          </button>
        </div>

        <FlowSheets
          sheet={sheet} setSheet={setSheet}
          id={id} project={project} report={report} grid={grid}
          delays={delays} safetyEvents={safetyEvents} qcIssues={qcIssues} rfis={rfis}
          locked={locked} reload={reload}
        />
      </div>
    );
  }

  // ============ UNPLANNED STEP ============
  if (screen === UNPLANNED) {
    return (
      <div className="dr-flow" data-testid="dr-flow-unplanned-step">
        {stepBar(UNPLANNED)}
        <div className="dr-flow-body">
          <div className="dr-flow-actname">Unplanned work</div>
          <div className="dr-flow-subline">Anything done today that wasn't on the schedule? Add it, or continue.</div>
          <div className="dr-flow-items">
            {unplannedRows.length === 0
              ? <div className="dr-flow-muted-note" style={{ textAlign: "center", padding: "10px 0" }}>Nothing added — most days this stays empty.</div>
              : unplannedRows.map((r) => (
                <div key={r.id} className="dr-flow-it" data-testid={`dr-flow-unplanned-row-${r.id}`}>
                  <ClipboardList className="h-4 w-4" style={{ color: "#34d399" }} />
                  <span>{r.activity_description || r.activity_code}</span>
                  <span className="dr-flow-pill dr-flow-pill-am">field</span>
                </div>
              ))}
          </div>
          {!locked && (
            <button type="button" className="dr-flow-ghost" data-testid="dr-flow-unplanned-add"
              onClick={() => setSheet({ kind: "unplanned" })}>
              <Plus className="h-4 w-4" /> Add unplanned activity
            </button>
          )}
        </div>
        <div className="dr-flow-nav">
          <button className="dr-flow-back" onClick={() => setScreen(N > 0 ? N - 1 : "landing")}><ChevronLeft className="h-4 w-4" /></button>
          <button className="dr-flow-next" data-testid="dr-flow-to-review" onClick={() => setScreen(REVIEW)}>Review <ChevronRight className="h-4 w-4" /></button>
        </div>
        <FlowSheets
          sheet={sheet} setSheet={setSheet}
          id={id} project={project} report={report} grid={grid}
          delays={delays} safetyEvents={safetyEvents} qcIssues={qcIssues} rfis={rfis}
          locked={locked} reload={reload}
        />
      </div>
    );
  }

  // ============ REVIEW & SUBMIT ============
  return (
    <div className="dr-flow" data-testid="dr-flow-review">
      {stepBar(REVIEW)}
      <div className="dr-flow-body">
        <div className="dr-flow-actname">Review &amp; submit</div>
        <div className="dr-flow-subline">{project?.name || "Project"} · {longDate} · everything for today</div>

        <div className="dr-flow-instr" data-testid="dr-flow-instr">
          <Pencil className="h-3.5 w-3.5" />
          Missed something? Tap the pencil to revisit an activity — photos, issues, crew.
        </div>

        {activities.length === 0 && (
          <div className="dr-flow-emptynote">No scheduled activities today.</div>
        )}
        {activities.map((r, i) => {
          const st = flowStatusOf(r);
          const crewN = (presence || []).filter((p) => p.schedule_activity_id && p.schedule_activity_id === r.schedule_activity_id).length;
          const issuesN = countIssuesForTask(r.schedule_activity_id, { delays, safetyEvents, qcIssues, rfis, deliveries });
          const photoN = (photos || []).filter((p) => p.daily_report_activity_id === r.id).length;
          return (
            <div key={r.id} className="dr-flow-rev" data-testid={`dr-flow-rev-${i}`}>
              <div className="dr-flow-rev-top">
                <CheckCircle2 className="h-4 w-4" style={{ color: "#34d399" }} />
                <span className="dr-flow-rev-name">{r.activity_description || r.activity_code}</span>
                <span className={`dr-flow-pill ${st.cls}`} style={{ marginLeft: "auto" }}>{r.pct_complete_end_of_day || 0}%</span>
                <button type="button" className="dr-flow-rev-edit" data-testid={`dr-flow-rev-edit-${i}`}
                  onClick={() => goActivity(i, { returnToReview: true })} aria-label="Edit activity">
                  <Pencil className="h-4 w-4" />
                </button>
              </div>
              <div className="dr-flow-rev-chips">
                <span className="dr-flow-mc"><Users className="h-3 w-3" /> {crewN} crew</span>
                {issuesN > 0
                  ? <span className="dr-flow-mc dr-flow-mc-am"><AlertTriangle className="h-3 w-3" /> {issuesN} issue{issuesN > 1 ? "s" : ""}</span>
                  : <span className="dr-flow-mc dr-flow-mc-gr"><CheckCircle2 className="h-3 w-3" /> no issues</span>}
                {photoN > 0 && <span className="dr-flow-mc"><ImageIcon className="h-3 w-3" /> {photoN}</span>}
              </div>
            </div>
          );
        })}

        {unplannedRows.length > 0 && (
          <>
            <div className="dr-flow-rowlbl">Unplanned work</div>
            {unplannedRows.map((r) => (
              <div key={r.id} className="dr-flow-rev" style={{ cursor: "default" }}>
                <div className="dr-flow-rev-top">
                  <ClipboardList className="h-4 w-4" style={{ color: "#34d399" }} />
                  <span className="dr-flow-rev-name">{r.activity_description || r.activity_code}</span>
                  <span className="dr-flow-pill dr-flow-pill-am" style={{ marginLeft: "auto" }}>field</span>
                </div>
              </div>
            ))}
          </>
        )}

        <div className="dr-flow-rev" style={{ cursor: "default" }}>
          <div className="dr-flow-rev-top">
            <Cloud className="h-4 w-4" style={{ color: "#34d399" }} />
            <span className="dr-flow-rev-name">Weather</span>
            <span className="dr-flow-muted-note" style={{ marginLeft: "auto" }}>
              {[report.weather_conditions,
                (report.weather_temp_high_f != null || report.weather_temp_low_f != null)
                  ? `${report.weather_temp_high_f != null ? Math.round(report.weather_temp_high_f) : "—"}°/${report.weather_temp_low_f != null ? Math.round(report.weather_temp_low_f) : "—"}°`
                  : null].filter(Boolean).join(" ") || "not set"}
            </span>
          </div>
        </div>

        {/* spec D — record + playable list on the Review step. */}
        <div className="dr-flow-rowlbl">Voice notes</div>
        <DrVoiceNotes reportId={id} notes={voiceNoteRows} reload={reload}
          allowRecord onPendingChange={setVoiceNotePending} />
      </div>

      <div className="dr-flow-nav">
        <button className="dr-flow-back" onClick={() => setScreen(UNPLANNED)}><ChevronLeft className="h-4 w-4" /></button>
        {report.status === "approved" ? (
          <button className="dr-flow-next" data-testid="dr-flow-export" onClick={() => onDownloadPdf("internal")}>
            <Download className="h-4 w-4" /> Export PDF
          </button>
        ) : report.status === "submitted" ? (
          <button className="dr-flow-next" data-testid="dr-flow-approve" disabled={busy} onClick={approveAndReturn}>
            <CheckCircle2 className="h-4 w-4" /> {busy ? "Approving…" : "Approve report"}
          </button>
        ) : (
          <button className="dr-flow-next" data-testid="dr-flow-submit" disabled={busy} onClick={submitAndReturn}>
            <CheckCircle2 className="h-4 w-4" /> {busy ? "Submitting…" : "Submit report"}
          </button>
        )}
      </div>

      <FlowNoWorkDialog noWorkModal={noWorkModal} setNoWorkModal={setNoWorkModal}
        submitNoWorkDay={submitNoWorkAndReturn} busy={busy} />
    </div>
  );
}

// Count issues (delays/safety/qc/rfis/deliveries) linked to one task.
function countIssuesForTask(taskId, { delays, safetyEvents, qcIssues, rfis, deliveries }) {
  if (!taskId) return 0;
  const has = (arr) => (arr || []).filter((x) => Array.isArray(x.linked_task_ids) && x.linked_task_ids.includes(taskId)).length;
  return has(delays) + has(safetyEvents) + has(qcIssues) + has(rfis) + has(deliveries);
}

// Spec A — the activity screen lists this task's logged issues under the
// quick-add chips. Same linkage the count above uses; short one-line rows.
function listIssuesForTask(taskId, { delays, safetyEvents, qcIssues, rfis, deliveries }) {
  if (!taskId) return [];
  const linked = (arr) => (arr || []).filter((x) => Array.isArray(x.linked_task_ids) && x.linked_task_ids.includes(taskId));
  return [
    ...linked(delays).map((d) => ({ key: `d-${d.id}`, kind: "Delay", cls: "dr-flow-pill-am", text: d.description })),
    ...linked(rfis).map((x) => ({ key: `r-${x.id}`, kind: "RFI", cls: "dr-flow-pill-bl", text: x.subject })),
    ...linked(safetyEvents).map((s) => ({ key: `s-${s.id}`, kind: "Safety", cls: "dr-flow-pill-rd", text: s.description })),
    ...linked(qcIssues).map((q) => ({ key: `q-${q.id}`, kind: "QC", cls: "dr-flow-pill-bl", text: q.description })),
    ...linked(deliveries).map((v) => ({ key: `v-${v.id}`, kind: "Delivery", cls: "dr-flow-pill-sl", text: v.description || v.vendor })),
  ];
}

// Locked-report reason: the flow renders components/dr/drFlowShared's
// LockedReasonCard on each activity screen (spec B). Its value feeds
// edit_reason on grid PATCHes + add_reason on photo tags.

// Authenticated photo thumbnail (blob → object URL), reused pattern
// from AuthPhoto but sized for the flow's thumb grid.
function FlowThumb({ photo }) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    let cancelled = false; let blobUrl = null;
    api.get(`/photos/${photo.id}/file`, { responseType: "blob" })
      .then((res) => { if (cancelled) return; blobUrl = URL.createObjectURL(res.data); setSrc(blobUrl); })
      .catch(() => { /* placeholder */ });
    return () => { cancelled = true; if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [photo.id]);
  return (
    <div className="dr-flow-thumb" data-testid={`dr-flow-thumb-${photo.id}`}>
      {src ? <img src={src} alt={photo.caption || ""} /> : <span>…</span>}
    </div>
  );
}

// No-work-day dialog — thin phone-shaped wrapper over the parent's
// noWorkModal state + submitNoWorkDay handler (reused verbatim).
function FlowNoWorkDialog({ noWorkModal, setNoWorkModal, submitNoWorkDay, busy }) {
  if (!noWorkModal) return null;
  return (
    <div className="dr-flow-ov" data-testid="dr-flow-nowork" onClick={(e) => { if (e.target === e.currentTarget) setNoWorkModal(null); }}>
      <div className="dr-flow-sheet" role="dialog" aria-modal="true">
        <div className="dr-flow-grip" onClick={() => setNoWorkModal(null)} />
        <div className="dr-flow-sheet-head">
          <div className="dr-flow-sheet-title">No activities or events</div>
          <div className="dr-flow-sheet-sub">File as a no-work day so the record shows why it's blank.</div>
        </div>
        <div className="dr-flow-fld">
          <label>Reason</label>
          <select value={noWorkModal.reason || ""} data-testid="dr-flow-nowork-reason"
            onChange={(e) => setNoWorkModal({ ...noWorkModal, reason: e.target.value })}>
            <option value="">Select…</option>
            <option value="weather">Weather (storm, extreme cold, snow, etc.)</option>
            <option value="holiday">Holiday</option>
            <option value="scheduled_non_work">Scheduled non-work day</option>
            <option value="crew_unavailable">Crew unavailable</option>
            <option value="tenant_restriction">Tenant or owner access restricted</option>
            <option value="other">Other (note required)</option>
          </select>
        </div>
        <div className="dr-flow-fld">
          <label>Note{noWorkModal.reason === "other" ? " *" : ""}</label>
          <textarea value={noWorkModal.note || ""} data-testid="dr-flow-nowork-note"
            placeholder={noWorkModal.reason === "other" ? "Required: describe the reason." : "Optional context."}
            onChange={(e) => setNoWorkModal({ ...noWorkModal, note: e.target.value })} />
        </div>
        <div className="dr-flow-sheet-foot">
          <button className="dr-flow-sheet-cancel" onClick={() => setNoWorkModal(null)}>Cancel</button>
          <button className="dr-flow-sheet-save" data-testid="dr-flow-nowork-confirm" disabled={busy} onClick={submitNoWorkDay}>
            {busy ? "Submitting…" : "Submit as no-work day"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Bottom sheets for crew / unplanned / issues / photos. Each REUSES an
// existing panel or form component so the create/save logic never
// forks. Issue sheets render the existing panel AND dispatch the
// dr-open-event-form event (with the activity's schedule_activity_id)
// so the panel prelinks + focuses exactly as the desktop grid dropdown
// does.
function FlowSheets({
  sheet, setSheet, id, project, report, grid,
  delays, safetyEvents, qcIssues, rfis,
  locked, reload,
}) {
  const kind = sheet?.kind;
  const activity = sheet?.activity;
  const reportDate = report?.report_date;

  // For issue sheets: after the panel mounts, dispatch the prelink event
  // so the existing useIssueDropdownListener opens the form + links the
  // task. One shot per open.
  useEffect(() => {
    if (!kind || !activity) return;
    const issueKinds = { delay: "delay", rfi: "rfi", safety: "safety", qc: "qc" };
    const k = issueKinds[kind];
    if (!k || !activity.schedule_activity_id) return;
    const t = setTimeout(() => {
      window.dispatchEvent(new CustomEvent("dr-open-event-form", {
        detail: {
          kind: k,
          activityId: activity.schedule_activity_id,
          activityCode: activity.activity_code,
          activityDescription: activity.activity_description,
        },
      }));
    }, 40);
    return () => clearTimeout(t);
  }, [kind, activity]);

  if (!kind) return null;
  const close = () => setSheet(null);
  // A successful save reloads the bundle; auto-close so the super lands
  // back on the activity screen (matches the mockup's log-and-return).
  const doneAndClose = () => { reload(); close(); };

  const titles = {
    crew: "Add crew", unplanned: "Add unplanned activity",
    delay: "Log delay", rfi: "Raise RFI", safety: "Safety event", qc: "QC issue", coordination: "Coordination event",
  };
  const forLine = activity ? (activity.activity_description || activity.activity_code) : null;

  return (
    <FlowSheet open onClose={close} title={titles[kind]} subtitle={forLine ? `on ${forLine}` : null} testid={`dr-flow-sheet-${kind}`}>
      {kind === "crew" && (
        <FlowCrewForm id={id} project={project} activity={activity} grid={grid}
          onSaved={doneAndClose} onCancel={close} />
      )}
      {kind === "unplanned" && (
        <FlowUnplannedForm id={id} locked={locked} onSaved={doneAndClose} onCancel={close} />
      )}
      {/* (photos sheet removed — the activity screen's Add tile uploads +
          tags to the grid row directly via uploadActivityPhotos.) */}
      {kind === "delay" && (
        <div className="dr-flow-sheet-panel">
          <DelaysPanel rows={delays} dailyReportId={id} projectId={project.id}
            reportDate={reportDate} locked={locked} reload={reload} />
        </div>
      )}
      {kind === "rfi" && (
        <div className="dr-flow-sheet-panel">
          <RfisPanel rows={rfis} dailyReportId={id} projectId={project.id}
            reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </div>
      )}
      {kind === "safety" && (
        <div className="dr-flow-sheet-panel">
          <SafetyPanel rows={safetyEvents} dailyReportId={id} projectId={project.id}
            reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </div>
      )}
      {kind === "qc" && (
        <div className="dr-flow-sheet-panel">
          <QcPanel rows={qcIssues} dailyReportId={id} projectId={project.id}
            reportDate={reportDate} locked={locked} reload={reload} grid={grid} />
        </div>
      )}
      {kind === "coordination" && (
        <div className="dr-flow-sheet-panel">
          <FlowCoordinationForm id={id} project={project} activity={activity}
            locked={locked} onSaved={doneAndClose} onCancel={close} />
        </div>
      )}
    </FlowSheet>
  );
}

// Crew add — reuses the SAME POST /daily-reports/{id}/presence contract
// PresencePanel.saveDraft uses. Pre-links this activity's task.
function FlowCrewForm({ id, project, activity, grid, onSaved, onCancel }) {
  const [subs, setSubs] = useState([]);
  const [company, setCompany] = useState("");
  const [trade, setTrade] = useState("");
  const [headcount, setHeadcount] = useState(3);
  const [hours, setHours] = useState(8);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!project?.id) return;
    Promise.all([
      api.get("/subcontractors").catch(() => ({ data: [] })),
      api.get(`/projects/${project.id}`).catch(() => ({ data: {} })),
    ]).then(([subsRes, projRes]) => {
      const all = Array.isArray(subsRes.data) ? subsRes.data : [];
      const ids = new Set((projRes.data && projRes.data.subcontractor_ids) || []);
      setSubs(ids.size ? all.filter((s) => ids.has(s.id)) : []);
    });
  }, [project?.id]);
  const save = async () => {
    if (!trade.trim()) return toast.error("Trade is required");
    setBusy(true);
    try {
      await api.post(`/daily-reports/${id}/presence`, {
        trade: trade.trim(),
        company_name: company.trim(),
        headcount: Number(headcount) || 0,
        hours_per_person: Number(hours) || 0,
        notes: "",
        schedule_activity_id: activity?.schedule_activity_id || null,
        shift: "first",
        daily_report_id: id,
      });
      toast.success("Crew added");
      onSaved();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Add failed");
    } finally { setBusy(false); }
  };
  return (
    <div className="dr-flow-form">
      <div className="dr-flow-fld">
        <label>Company</label>
        <div className="dr-flow-chipset">
          {subs.map((s) => (
            <button key={s.id} type="button" className={`dr-flow-chip${company === s.name ? " on" : ""}`}
              onClick={() => { setCompany(s.name); if (!trade && s.default_trade) setTrade(s.default_trade); }}>{s.name}</button>
          ))}
          {subs.length === 0 && <span className="dr-flow-muted-note">Add subs on the Project page to pick here — or type below.</span>}
        </div>
        <input type="text" value={company} data-testid="dr-flow-crew-company"
          placeholder="Company name" onChange={(e) => setCompany(e.target.value)} />
      </div>
      <div className="dr-flow-fld">
        <label>Trade *</label>
        <div className="dr-flow-chipset">
          {COMMON_TRADES.map((t) => (
            <button key={t} type="button" className={`dr-flow-chip${trade === t ? " on" : ""}`}
              onClick={() => setTrade(t)}>{t}</button>
          ))}
        </div>
      </div>
      <div className="dr-flow-row2">
        <div className="dr-flow-fld">
          <label>Workers *</label>
          <div className="dr-flow-stp">
            <button type="button" onClick={() => setHeadcount((n) => Math.max(0, Number(n) - 1))}>−</button>
            <span className="dr-flow-stp-val" data-testid="dr-flow-crew-headcount">{headcount}</span>
            <button type="button" onClick={() => setHeadcount((n) => Number(n) + 1)}>+</button>
          </div>
        </div>
        <div className="dr-flow-fld">
          <label>Hours each *</label>
          <div className="dr-flow-stp">
            <button type="button" onClick={() => setHours((n) => Math.max(0, Number(n) - 0.5))}>−</button>
            <span className="dr-flow-stp-val">{hours}</span>
            <button type="button" onClick={() => setHours((n) => Number(n) + 0.5)}>+</button>
          </div>
        </div>
      </div>
      <div className="dr-flow-sheet-foot">
        <button className="dr-flow-sheet-cancel" onClick={onCancel}>Cancel</button>
        <button className="dr-flow-sheet-save" data-testid="dr-flow-crew-save" disabled={busy} onClick={save}>
          {busy ? "Adding…" : "Add crew"}
        </button>
      </div>
    </div>
  );
}

// Unplanned add — reuses the SAME POST /daily-reports/{id}/unplanned-activity
// contract as ActivityGridPanel.createUnplanned (incl. locked add_reason).
function FlowUnplannedForm({ id, locked, onSaved, onCancel }) {
  const [description, setDescription] = useState("");
  const [tradeCode, setTradeCode] = useState("");
  const [addReason, setAddReason] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!description.trim()) return toast.error("Describe the unplanned activity first");
    if (locked && addReason.trim().length < ADD_REASON_MIN) {
      return toast.error(`Add reason required (min ${ADD_REASON_MIN} chars) on a locked DR.`);
    }
    setBusy(true);
    try {
      await api.post(`/daily-reports/${id}/unplanned-activity`, {
        description: description.trim(),
        trade_code: tradeCode || "",
        duration_days: 1,
        ...(locked ? { add_reason: addReason.trim() } : {}),
      });
      toast.success(locked ? "Unplanned activity created (post-submit)." : "Unplanned activity created — flagged for PM review.");
      onSaved();
    } catch (e) {
      if (!handleLockedAddError(e, "activity")) toast.error(formatErr(e, "Create unplanned"));
    } finally { setBusy(false); }
  };
  return (
    <div className="dr-flow-form">
      {locked && (
        <div style={{ marginBottom: 10 }}>
          <LockedAddReasonBlock value={addReason} onChange={setAddReason}
            eventLabel="this activity" testid="dr-flow-unplanned-reason" />
        </div>
      )}
      <div className="dr-flow-fld">
        <label>What was done? *</label>
        <input type="text" value={description} data-testid="dr-flow-unplanned-desc"
          placeholder="Describe the work" onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div className="dr-flow-fld">
        <label>Trade</label>
        <div className="dr-flow-chipset">
          {COMMON_TRADES.map((t) => (
            <button key={t} type="button" className={`dr-flow-chip${tradeCode === t ? " on" : ""}`}
              onClick={() => setTradeCode(t)}>{t}</button>
          ))}
        </div>
      </div>
      <div className="dr-flow-sheet-foot">
        <button className="dr-flow-sheet-cancel" onClick={onCancel}>Cancel</button>
        <button className="dr-flow-sheet-save" data-testid="dr-flow-unplanned-save" disabled={busy} onClick={save}>
          {busy ? "Adding…" : "Add"}
        </button>
      </div>
    </div>
  );
}

// Coordination — reuses the EXISTING CoordinationEventForm (same as the
// desktop header quick-log), pre-linked to this DR + locked add_reason.
function FlowCoordinationForm({ id, project, activity, locked, onSaved, onCancel }) {
  const [addReason, setAddReason] = useState("");
  return (
    <div className="dr-flow-form">
      {locked && (
        <div style={{ marginBottom: 10 }}>
          <LockedAddReasonBlock value={addReason} onChange={setAddReason}
            eventLabel="a coordination event" testid="dr-flow-coord-reason" />
        </div>
      )}
      <CoordinationEventForm
        projectId={project.id}
        linkedDailyReportId={id}
        abbreviated
        isLocked={locked}
        lockedAddReason={locked ? addReason : null}
        onSaved={() => { toast.success(locked ? "Coordination event logged (post-submit)." : "Coordination event logged"); onSaved(); }}
        onCancel={onCancel}
      />
    </div>
  );
}
