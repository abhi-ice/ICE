// Desktop guided Daily Report — 1:1 implementation of the approved mockup
// (ICE-DR-guided.html), wired to the real endpoints the mobile guided flow
// already uses so save logic never forks.
//
// Page layout (strict containment): report header · Weather · Today's
// activities (accordion cards, ONE open at a time — each card owns its
// Progress today / Crew / Issues & deliveries / Photos) · Unplanned work ·
// Voice notes · sticky submit bar. Coordination is only reachable from
// inside an activity card. Every form fills through the guided prompt
// dialog (GuidedDialog.jsx) one question at a time.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";
import { getUser } from "../../lib/auth";
import { localTodayISO } from "../../lib/dateUtil";
import { queuePhoto } from "../../lib/drPhotoQueue";
import { compressImage } from "../../lib/imageCompress";
import { drainOfflineQueue } from "../../lib/drOfflineSync";
import DrVoiceNotes from "./DrVoiceNotes";
import GuidedDialog from "./GuidedDialog";
import { COMMON_TRADES, LockedReasonCard, isFlowReasonValid } from "./drFlowShared";
import "./drGuidedDesktop.css";

/* ================= option lists — mockup labels, real enum values ================= */
const opt = (pairs) => pairs.map(([label, value]) => ({ label, value }));
const OPT = {
  trade: COMMON_TRADES.map((t) => ({ label: t, value: t })),
  severity: opt([["Minor", "minor"], ["Moderate", "moderate"], ["Major", "major"], ["Critical", "critical"]]),
  delayCause: opt([
    ["Weather", "weather"], ["Site not as expected", "differing_site_condition"],
    ["Found a hidden condition", "concealed_condition"], ["Waiting on a design answer", "design_clarification"],
    ["Design changed", "design_change"], ["Waiting on an RFI answer", "rfi_pending"],
    ["Waiting on submittal approval", "submittal_pending"], ["Materials or equipment late", "procurement"],
    ["Short on workers", "manpower"], ["Utility problem", "utility"], ["Permit or inspection", "regulatory"],
    ["Tenant coordination", "tenant_coordination"], ["Building coordination", "building_coordination"], ["Other", "other"],
  ]),
  owner: opt([
    ["None", "none"], ["GC", "gc"], ["A/E", "a_e"], ["Owner/Gov", "owner"],
    ["Sub", "sub"], ["External", "external_authority"], ["Tenant", "tenant"],
  ]),
  delayStatus: opt([["Open", "open"], ["In progress", "in_progress"], ["Resolved", "resolved"]]),
  rfiPriority: opt([["Low", "low"], ["Normal", "normal"], ["High", "high"], ["Urgent", "urgent"]]),
  qcSeverity: opt([
    ["Observation", "observation"], ["Minor", "minor"], ["Moderate", "moderate"], ["Major", "major"],
    ["Critical", "critical"], ["Rework required", "rework_required"], ["Stop work", "stop_work"],
  ]),
  safetyType: opt([
    ["Near miss", "near_miss"], ["First aid", "first_aid"], ["Recordable", "recordable"],
    ["Lost time", "lost_time"], ["Fatality", "fatality"], ["Other", "other"],
  ]),
  channel: opt([
    ["Phone", "phone"], ["Email", "email"], ["In person", "in_person"], ["Meeting", "meeting"],
    ["Text message", "text_message"], ["Formal letter", "formal_letter"], ["System notification", "system_notification"],
  ]),
  direction: opt([["Inbound", "inbound"], ["Outbound", "outbound"], ["Internal", "internal"]]),
  topic: opt([
    ["Tenant access", "tenant_access"], ["Building coordination", "building_coordination"],
    ["A e clarification", "a_e_clarification"], ["Owner direction", "owner_direction"],
    ["Sub coordination", "sub_coordination"], ["Regulatory", "regulatory"], ["Safety", "safety"],
    ["Quality", "quality"], ["Scope", "scope"], ["Payment", "payment"], ["Other", "other"],
  ]),
  outcome: opt([
    ["Agreement", "agreement"], ["Disagreement", "disagreement"], ["Pending", "pending"],
    ["Informational", "informational"], ["Escalation required", "escalation_required"],
  ]),
  coordTrade: opt([
    ["General", "general"], ["Sitework", "sitework"], ["Demolition", "demolition"], ["Concrete", "concrete"],
    ["Masonry", "masonry"], ["Steel", "steel"], ["Carpentry", "carpentry"], ["Drywall", "drywall"],
    ["Finishes", "finishes"], ["Doors & windows", "doors_windows"], ["Roofing", "roofing"],
    ["Plumbing", "plumbing"], ["Mechanical", "mechanical"], ["Electrical", "electrical"],
    ["Fire protection", "fire_protection"], ["Elevators", "elevators"], ["Abatement", "abatement"], ["Other", "other"],
  ]),
  photoClass: opt([
    ["Progress", "progress"], ["Discovery", "discovery"], ["Damage", "damage"], ["Quality", "quality"],
    ["Safety", "safety"], ["Coordination", "coordination"], ["Closeout", "closeout"],
  ]),
  photoEvent: opt([
    ["General progress", "progress"], ["Delay", "delay"], ["RFI", "rfi"], ["QC", "qc"],
    ["Safety", "safety"], ["Coord", "coord"], ["Delivery", "delivery"],
  ]),
  noWork: opt([
    ["Weather (storm, extreme cold, snow, etc.)", "weather"], ["Holiday", "holiday"],
    ["Scheduled non-work day", "scheduled_non_work"], ["Crew unavailable", "crew_unavailable"],
    ["Tenant or owner access restricted", "tenant_restriction"], ["Other (note required)", "other"],
  ]),
};

/* ================= local helpers (mirrors of DailyReports.jsx module helpers) ================= */
function gridSectionOf(cat) {
  if (cat === "preconstruction_paperwork" || cat === "construction_paperwork") return 1;
  if (cat === "closeout_paperwork") return 3;
  return 2;
}
function trunc(s, n) { s = String(s || "").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function fmtErr(e, what) {
  const d = e?.response?.data?.detail;
  if (typeof d === "string") return `${what} failed: ${d}`;
  if (d?.message) return `${what} failed: ${d.message}`;
  return `${what} failed: ${e?.message || "unknown error"}`;
}
function savedToast(res, msg) {
  if (res?.data?._offline) toast.success(`${msg} — saved offline, will sync when you reconnect.`);
  else toast.success(msg);
}
// Chip text mirrors what the mockup baked in at creation time:
// "what · Severity", "subject · Priority", "vendor (PO n)" etc.
const capWords = (s) => String(s || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
function listIssuesForTask(taskId, { delays, safetyEvents, qcIssues, rfis, deliveries, coordEvents }) {
  if (!taskId) return [];
  const has = (x) => Array.isArray(x?.linked_task_ids) && x.linked_task_ids.includes(taskId);
  const out = [];
  (delays || []).filter(has).forEach((x) => out.push({
    key: `d${x.id}`, kind: "Delay", cls: "prob",
    text: `${trunc(x.description, 44)}${x.severity ? ` · ${capWords(x.severity)}` : ""}` }));
  (rfis || []).filter(has).forEach((x) => out.push({
    key: `r${x.id}`, kind: "RFI", cls: "info",
    text: `${trunc(x.subject, 44)}${x.priority ? ` · ${capWords(x.priority)}` : ""}` }));
  (safetyEvents || []).filter(has).forEach((x) => out.push({
    key: `s${x.id}`, kind: "Safety", cls: "prob",
    text: x.event_type ? `${capWords(x.event_type)}${x.severity ? ` · ${capWords(x.severity)}` : ""}` : trunc(x.description, 44) }));
  (qcIssues || []).filter(has).forEach((x) => out.push({
    key: `q${x.id}`, kind: "QC", cls: "prob",
    text: `${capWords(x.severity) || trunc(x.description, 44)}${x.rework_duration_days ? ` · rework ${x.rework_duration_days}d` : ""}` }));
  (deliveries || []).filter(has).forEach((x) => out.push({
    key: `v${x.id}`, kind: "Delivery", cls: "ok",
    text: `${x.vendor || x.description || ""}${x.po_number ? ` (PO ${x.po_number})` : ""}` }));
  (coordEvents || []).filter(has).forEach((x) => out.push({
    key: `c${x.id}`, kind: "Coord", cls: "info",
    text: `${capWords(x.topic)}${x.summary ? ` · ${trunc(x.summary, 36)}` : ""}` }));
  return out;
}
function statusOf(row, pct) {
  const s = row.auto_status || row.status_at_end_of_day;
  if (s === "blocked") return { cls: "blocker", word: "Blocked" };
  if (s === "complete" || pct >= 100) return { cls: "done", word: "Complete" };
  if (s === "in_progress" || pct > 0) return { cls: "open", word: "In progress" };
  return { cls: "draft", word: "Not started" };
}

/* Authenticated photo thumbnail (bytes require the bearer token). */
function AuthThumb({ photoId }) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    let alive = true; let url = null;
    api.get(`/photos/${photoId}/file`, { responseType: "blob" })
      .then((r) => { if (!alive) return; url = URL.createObjectURL(r.data); setSrc(url); })
      .catch(() => {});
    return () => { alive = false; if (url) URL.revokeObjectURL(url); };
  }, [photoId]);
  return src ? <img src={src} alt="" /> : null;
}

const WeatherIcon = () => (
  <svg className="sun" viewBox="0 0 48 48" aria-hidden="true">
    <circle cx="17" cy="20" r="9" fill="#f2c14e" />
    <path d="M8 34a9 9 0 0 1 1-18 12 12 0 0 1 23 2 8 8 0 0 1-2 16z" fill="#cdd8e2" />
    <line x1="16" y1="40" x2="14" y2="45" stroke="#9aa1a8" strokeWidth="3" strokeLinecap="round" />
    <line x1="26" y1="40" x2="24" y2="45" stroke="#9aa1a8" strokeWidth="3" strokeLinecap="round" />
    <line x1="36" y1="40" x2="34" y2="45" stroke="#9aa1a8" strokeWidth="3" strokeLinecap="round" />
  </svg>
);

/* ================================================================== */
export default function DrGuidedDesktop({
  id, report, project, grid, presence, photos, voiceAnnotations,
  delays, safetyEvents, qcIssues, rfis, deliveries,
  locked, reload, onBack, busy, submit, submitNoWorkDay,
  noWorkModal, setNoWorkModal, approve, onDownloadPdf,
  onDownloadClientTemplate, clientTemplatePresent,
  submitRowErrors, online, queueSize,
}) {
  const [openActId, setOpenActId] = useState(null);
  const [dlg, setDlg] = useState(null);
  const [wxEdit, setWxEdit] = useState(false);
  const [wx, setWx] = useState({});
  const [exportOpen, setExportOpen] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesText, setNotesText] = useState(report?.notes || "");
  const [pinned, setPinned] = useState(() => new Set());
  const [aiOpen, setAiOpen] = useState(() => new Set());
  const [reviews, setReviews] = useState([]);
  const [aiBusy, setAiBusy] = useState(false);
  const [touched, setTouched] = useState(() => new Set()); // rowIds interacted with this session (mockup's touch())
  const [coordEvents, setCoordEvents] = useState([]);
  const [overrides, setOverrides] = useState({});   // rowId -> reason (gate unlocked this session)
  const [ovOpen, setOvOpen] = useState({});          // rowId -> bool (override box revealed)
  const [ovDraft, setOvDraft] = useState({});        // rowId -> draft reason text
  const [pctDraft, setPctDraft] = useState({});      // rowId -> local slider value while dragging
  const [lockedReason, setLockedReason] = useState("");
  const commitTimers = useRef({});
  const barRef = useRef(null);

  const role = getUser()?.role;
  const canOverride = role === "admin" || role === "pm";
  const reportDate = report?.report_date;
  const dateSub = useMemo(() => {
    try {
      return new Date(`${reportDate}T00:00:00`).toLocaleDateString(undefined,
        { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    } catch (e) { return reportDate; }
  }, [reportDate]);

  const touch = useCallback((rowId) => {
    setTouched((p) => { if (p.has(rowId)) return p; const n = new Set(p); n.add(rowId); return n; });
  }, []);

  // Coordination events aren't in the /context bundle — fetch them so a
  // logged Coord shows up as a chip on its activity (mockup parity).
  const loadCoords = useCallback(async () => {
    try {
      const r = await api.get(`/coordination-events`, { params: { project_id: project?.id } });
      const rows = Array.isArray(r.data) ? r.data : (Array.isArray(r.data?.items) ? r.data.items : []);
      setCoordEvents(rows.filter((x) => x.linked_daily_report_id === id));
    } catch (e) { /* endpoint variant — chips simply stay absent */ }
  }, [id, project?.id]);
  useEffect(() => { loadCoords(); }, [loadCoords]);

  /* ---------- per-activity model ---------- */
  const activities = useMemo(() => (grid || []).filter((r) => gridSectionOf(r.work_category) === 2), [grid]);
  const unplannedRows = useMemo(() => activities.filter((r) => (r.activity_code || "").startsWith("AF")), [activities]);
  // Mockup containment: unplanned items live ONLY in the Unplanned work
  // card — they are not activity cards and don't count toward "N of M".
  const cardRows = useMemo(() => activities.filter((r) => !(r.activity_code || "").startsWith("AF")), [activities]);
  const voiceNotes = useMemo(() => (voiceAnnotations || []).filter((v) => v.kind === "voice_note"), [voiceAnnotations]);
  const issueBags = { delays, safetyEvents, qcIssues, rfis, deliveries, coordEvents };

  const model = useMemo(() => cardRows.map((r) => {
    const sod = r.pct_complete_start_of_day ?? 0;
    const pct = pctDraft[r.id] !== undefined ? pctDraft[r.id] : (r.pct_complete_end_of_day ?? sod);
    const crew = (presence || []).filter((p) => p.schedule_activity_id && p.schedule_activity_id === r.schedule_activity_id);
    const rowPhotos = (photos || []).filter((p) => p.daily_report_activity_id === r.id);
    const issues = listIssuesForTask(r.schedule_activity_id, issueBags);
    const gate = r.slider_gate;
    const overridden = !!overrides[r.id];
    const gateBlocked = !!(gate && gate.locked && !overridden && (gate.max_pct ?? 0) <= sod);
    const planned = r.planned_pct_complete ?? r.planned_pct ?? null;
    // Mockup's touch(): ANY interaction this session counts as updated,
    // in addition to persisted signals.
    const updated = touched.has(r.id)
      || (r.pct_complete_end_of_day != null && r.pct_complete_end_of_day !== sod)
      || crew.length > 0 || issues.length > 0 || rowPhotos.length > 0;
    return { row: r, sod, pct, crew, rowPhotos, issues, gate, overridden, gateBlocked, planned, updated,
      status: gateBlocked ? { cls: "blocker", word: "Blocked" } : statusOf(r, pct) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [cardRows, presence, photos, delays, safetyEvents, qcIssues, rfis, deliveries, coordEvents, pctDraft, overrides, touched]);

  const updatedCount = model.filter((m) => m.updated).length;
  const taskOpts = useMemo(() => cardRows
    .filter((r) => r.schedule_activity_id)
    .map((r) => ({ name: r.activity_description, id: r.schedule_activity_id, done: (r.auto_status || r.status_at_end_of_day) === "complete" })), [cardRows]);

  const lockedReasonOk = isFlowReasonValid(lockedReason);
  const needReason = () => {
    if (locked && !lockedReasonOk) {
      toast.error("This report is submitted — enter the edit reason at the top first.");
      return true;
    }
    return false;
  };
  const addReasonPatch = locked ? { add_reason: (lockedReason || "").trim() } : {};

  /* ---------- accordion ---------- */
  const toggleAct = (rowId) => {
    setOpenActId((cur) => {
      const next = cur === rowId ? null : rowId;
      if (next) setTimeout(() => {
        const el = document.getElementById(`drgx-act-${rowId}`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 30);
      return next;
    });
  };
  const cardClick = (ev, rowId) => {
    if (openActId === rowId) {
      if (ev.target.closest(".ahead")) toggleAct(rowId);
      return;
    }
    if (ev.target.closest("button,input,textarea,select,a")) return;
    toggleAct(rowId);
  };

  /* ---------- progress ---------- */
  const commitPct = useCallback(async (m, v) => {
    const n = Math.round(Number(v));
    const fields = { pct_complete_end_of_day: n, task_last_modified_at: m.row.task_updated_at };
    const gate = m.row.slider_gate;
    if (gate && gate.locked && n > Math.max(gate.max_pct ?? 0, m.sod) && overrides[m.row.id]) {
      fields.slider_gate_override_reason = overrides[m.row.id];
    }
    if (locked) {
      if (!lockedReasonOk) { toast.error("This report is submitted — enter the edit reason at the top first."); return; }
      fields.edit_reason = lockedReason.trim();
    }
    try {
      const res = await api.patch(`/daily-report-activities/${m.row.id}`, fields);
      if (res?.data?._offline) toast.success("Progress saved offline — will sync when you reconnect.");
      reload();
    } catch (e) {
      if (e?.response?.status === 409) { toast.error("This row changed elsewhere — refreshing."); reload(); }
      else toast.error(fmtErr(e, "Save progress"));
    }
  }, [locked, lockedReason, lockedReasonOk, overrides, reload]);

  const setProg = (m, v, immediate) => {
    const gate = m.row.slider_gate;
    const effMax = m.overridden ? 100 : (gate && gate.locked ? Math.max(gate.max_pct ?? 100, m.sod) : 100);
    let n = Math.round(Number(v));
    if (n > effMax) {
      n = effMax;
      toast.error(gate?.reason || "Progress is gated by this activity's predecessors.");
    }
    if (n < 0) n = 0;
    touch(m.row.id);
    setPctDraft((p) => ({ ...p, [m.row.id]: n }));
    clearTimeout(commitTimers.current[m.row.id]);
    commitTimers.current[m.row.id] = setTimeout(() => commitPct(m, n), immediate ? 0 : 650);
  };

  /* ---------- flows (real payloads — same endpoints as the mobile flow) ---------- */
  const openFlow = (flow, fromChooserAct) => setDlg({
    kind: "flow", flow, flowKey: `${flow.title}-${Date.now()}`,
    fromChooser: fromChooserAct ? () => openChooser(fromChooserAct) : undefined,
  });

  const FLOW_crew = (m) => ({
    title: "Add crew", sub: m.row.activity_description, save: "Add crew", saving: "Adding…",
    steps: [
      { q: "Company", gloss: "Type the company name — your own crew or a subcontractor.",
        fields: [{ k: "co", label: "Company", t: "text", ph: "e.g. Metro Flooring LLC" }] },
      { q: "Trade", gloss: "Which trade was this crew? Start typing to filter.",
        fields: [{ k: "trade", label: "Trade", t: "chips", opts: OPT.trade, req: 1 }] },
      { q: "How big was the crew?", gloss: "Workers on this activity, and the hours each one worked.",
        fields: [{ k: "hc", label: "Workers", t: "stepper", def: 3, min: 1, stp: 1, req: 1 },
                 { k: "hr", label: "Hours each", t: "stepper", def: 8, min: 0.5, stp: 0.5, req: 1 }] },
      { q: "Notes — optional", optional: 1, gloss: "Anything the office should know about this crew? Skip if not.",
        fields: [{ k: "cnotes", label: "Notes", t: "text", ph: "e.g. left early for another job at 2 PM" }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const res = await api.post(`/daily-reports/${id}/presence`, {
        trade: a.trade, company_name: (a.co || "").trim(), headcount: Number(a.hc) || 1,
        hours_per_person: Number(a.hr) || 8, notes: (a.cnotes || "").trim(),
        schedule_activity_id: m.row.schedule_activity_id || null, shift: "first",
        daily_report_id: id, ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Add crew")); throw e; });
      savedToast(res, "Crew added.");
      touch(m.row.id);
      reload();
    },
  });

  const FLOW_delay = (m) => ({
    title: "Log delay", sub: `Attached to ${m.row.activity_description}`, save: "Log delay", saving: "Logging…",
    steps: [
      { q: "What happened?", gloss: "Tell us what caused the delay and who it affected.",
        fields: [{ k: "what", label: "What happened?", t: "textarea", req: 1, ph: "Tell us what caused the delay and who it affected" }] },
      { q: "Severity", gloss: "How bad is it?",
        fields: [{ k: "dsev", label: "Severity", t: "choice", opts: OPT.severity, req: 1 }] },
      { q: "Cause", gloss: "Pick the closest cause. Start typing to filter.",
        fields: [{ k: "cause", label: "Cause", t: "chips", opts: OPT.delayCause, req: 1 }] },
      { q: "Which tasks does it set back? — optional", optional: 1,
        gloss: "This activity is already picked. Tap to add or remove tasks, and say how many days each one loses. Skip if you're not sure — the office can fill it in later.",
        fields: [{ k: "timp", label: "Tasks set back", t: "taskimpact", tasks: taskOpts,
          def: m.row.schedule_activity_id ? [{ task: m.row.activity_description, id: m.row.schedule_activity_id, days: "" }] : [] }] },
      { q: "When does the impact start? — optional", optional: 1,
        gloss: "Skip if you're not sure — the office can fill it in later.",
        fields: [{ k: "starts", label: "Impact starts on", t: "date", def: reportDate }] },
      { q: "Is it fixed yet? — optional", optional: 1,
        gloss: "Skip if you're not sure — the office can fill it in later.",
        fields: [{ k: "dstat", label: "Status", t: "choice", opts: OPT.delayStatus }] },
      { q: "Whose side does this fall on? — optional", optional: 1,
        gloss: "Who is responsible for the delay. Skip if you're not sure — the office can fill it in later.",
        fields: [{ k: "owner", label: "Responsible party", t: "choice", opts: OPT.owner }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const picked = Array.isArray(a.timp) && a.timp.length
        ? a.timp
        : (m.row.schedule_activity_id ? [{ task: m.row.activity_description, id: m.row.schedule_activity_id, days: "" }] : []);
      if (!picked.length) { toast.error("This activity has no linked schedule task"); throw new Error("no-task"); }
      const res = await api.post(`/delays`, {
        project_id: project.id, daily_report_id: id, date: reportDate || localTodayISO(),
        description: a.what.trim(), severity: a.dsev, status: a.dstat || "open",
        cause_class: a.cause, reason_category: a.cause,
        owner_attribution: a.owner || "none",
        linked_task_ids: picked.map((x) => x.id),
        per_task_impacts: picked.map((x) => ({ task_id: x.id, projected_impact_days: Number(x.days) || 0 })),
        impact_start_date: a.starts || reportDate || localTodayISO(),
        ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Log delay")); throw e; });
      savedToast(res, "Delay logged.");
      touch(m.row.id);
      reload();
    },
  });

  const FLOW_rfi = (m) => ({
    title: "Raise RFI", sub: `Attached to ${m.row.activity_description}`, save: "Raise RFI", saving: "Raising…",
    steps: [
      { q: "Subject", gloss: "A short headline for the question.",
        fields: [{ k: "subj", label: "Subject", t: "text", req: 1, ph: "e.g. Corridor soffit height" }] },
      { q: "Question / context", gloss: "Explain the question.",
        fields: [{ k: "ctx", label: "Question / context", t: "textarea", req: 1, ph: "Explain the question" }] },
      { q: "Priority", gloss: "How fast do you need the answer?",
        fields: [{ k: "pri", label: "Priority", t: "choice", opts: OPT.rfiPriority, req: 1 }] },
      { q: "More details (for the office) — optional", optional: 1,
        gloss: "Skip this if you're not sure — the office can fill it in later.",
        fields: [{ k: "simp", label: "Schedule impact (days)", t: "number", stp: 0.5, ph: "days" },
                 { k: "sstart", label: "Impact starts on", t: "date", def: reportDate }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const taskId = m.row.schedule_activity_id;
      if (!taskId) { toast.error("This activity has no linked schedule task"); throw new Error("no-task"); }
      const days = Number(a.simp) || 0;
      const res = await api.post(`/rfis`, {
        project_id: project.id, daily_report_id: id,
        subject: a.subj.trim(), question: a.ctx.trim(), priority: a.pri,
        linked_task_ids: [taskId],
        per_task_impacts: days > 0 ? [{ task_id: taskId, projected_impact_days: days }] : [],
        impact_start_date: a.sstart || reportDate || localTodayISO(),
        ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Raise RFI")); throw e; });
      savedToast(res, "RFI logged.");
      touch(m.row.id);
      reload();
    },
  });

  const FLOW_safety = (m) => ({
    title: "Safety event", sub: `Attached to ${m.row.activity_description}`, save: "Log safety", saving: "Logging…",
    steps: [
      { q: "Event type", gloss: "What kind of safety event was it?",
        fields: [{ k: "stype", label: "Event type", t: "choice", opts: OPT.safetyType, req: 1 }] },
      { q: "Severity", gloss: "How bad is it?",
        fields: [{ k: "ssev", label: "Severity", t: "choice", opts: OPT.severity, req: 1 }] },
      { q: "Describe", gloss: "Describe the event.",
        fields: [{ k: "sdesc", label: "Describe", t: "textarea", req: 1, ph: "Describe the event" }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const res = await api.post(`/safety-events`, {
        project_id: project.id, daily_report_id: id, date: reportDate || localTodayISO(),
        description: a.sdesc.trim(), severity: a.ssev, event_type: a.stype,
        linked_task_ids: m.row.schedule_activity_id ? [m.row.schedule_activity_id] : [],
        ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Log safety")); throw e; });
      savedToast(res, "Safety logged.");
      touch(m.row.id);
      reload();
    },
  });

  const FLOW_qc = (m) => ({
    title: "QC issue", sub: `Attached to ${m.row.activity_description}`, save: "Log QC", saving: "Logging…",
    steps: [
      { q: "Severity", gloss: "How serious is the quality problem?",
        fields: [{ k: "qcsev", label: "Severity", t: "choice", opts: OPT.qcSeverity, req: 1 }] },
      { q: "Describe", gloss: "What needs fixing?",
        fields: [{ k: "qdesc", label: "Describe", t: "textarea", req: 1, ph: "What needs fixing?" }] },
      { q: "How much rework will it take?", when: (a) => a.qcsev === "rework_required",
        gloss: "You picked “Rework required”, so the office needs these two.",
        fields: [{ k: "rw", label: "Rework (days)", t: "number", stp: 0.5, req: 1, ph: "0" },
                 { k: "basis", label: "Basis", t: "text", req: 1, ph: "e.g. re-do south wall, 2 workers ~1 day" }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const taskId = m.row.schedule_activity_id;
      if (!taskId) { toast.error("This activity has no linked schedule task"); throw new Error("no-task"); }
      const res = await api.post(`/qc-issues`, {
        project_id: project.id, daily_report_id: id, date: reportDate || localTodayISO(),
        description: a.qdesc.trim(), severity: a.qcsev,
        linked_task_ids: [taskId],
        ...(a.qcsev === "rework_required"
          ? { rework_duration_days: Number(a.rw) || 0, rework_duration_basis: (a.basis || "").trim() } : {}),
        ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Log QC")); throw e; });
      savedToast(res, "QC logged.");
      touch(m.row.id);
      reload();
    },
  });

  const FLOW_delivery = (m) => ({
    title: "Log delivery", sub: `Attached to ${m.row.activity_description}`, save: "Log delivery", saving: "Logging…",
    steps: [
      { q: "Vendor", gloss: "Who delivered it?",
        fields: [{ k: "vendor", label: "Vendor", t: "text", req: 1, ph: "Vendor / supplier" }] },
      { q: "What was delivered?", gloss: "Materials or equipment — add the PO number if you have it.",
        fields: [{ k: "what_d", label: "What was delivered?", t: "textarea", req: 1, ph: "Materials or equipment" },
                 { k: "po", label: "PO #", t: "text", ph: "e.g. 4471" }] },
      { q: "Tasks consuming this delivery", gloss: "Pick every task that will use it.",
        fields: [{ k: "tasks", label: "Tasks consuming this delivery", t: "multichips", req: 1,
          opts: taskOpts.map((t) => ({ label: t.name, value: t.id })) }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const res = await api.post(`/deliveries`, {
        project_id: project.id, delivery_date: reportDate || localTodayISO(), daily_report_id: id,
        vendor: a.vendor.trim(), description: a.what_d.trim(), po_number: (a.po || "").trim() || null,
        linked_task_ids: a.tasks, ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Log delivery")); throw e; });
      savedToast(res, "Delivery logged.");
      touch(m.row.id);
      reload();
    },
  });

  const FLOW_coord = (m) => ({
    title: "Coordination event", sub: `Attached to ${m.row.activity_description}`, save: "Log coordination", saving: "Logging…",
    steps: [
      { q: "Who did you talk to?", gloss: "The person or organization on the other side.",
        fields: [{ k: "party", label: "Other party", t: "text", req: 1, ph: "Person or organization" },
                 { k: "org", label: "Org", t: "text", ph: "e.g. GSA, A/E firm" }] },
      { q: "Channel", gloss: "How did the conversation happen?",
        fields: [{ k: "chan", label: "Channel", t: "choice", opts: OPT.channel, req: 1 }] },
      { q: "Direction", gloss: "Who reached out?",
        fields: [{ k: "dir", label: "Direction", t: "choice", opts: OPT.direction, req: 1 }] },
      { q: "Topic", gloss: "What was it about?",
        fields: [{ k: "topic", label: "Topic", t: "chips", opts: OPT.topic, req: 1 }] },
      { q: "Describe topic", when: (a) => a.topic === "other",
        gloss: "You picked “Other”, so a few words are required.",
        fields: [{ k: "topicOther", label: "Describe topic", t: "text", req: 1, ph: "Topic" }] },
      { q: "Summary", gloss: "One or two lines the office can read at a glance.",
        fields: [{ k: "csum", label: "Summary", t: "textarea", req: 1, ph: "e.g. No second-shift work in the FCC suite Friday — email coming." }] },
      { q: "Outcome", gloss: "Where did the conversation land?",
        fields: [{ k: "outc", label: "Outcome", t: "choice", opts: OPT.outcome, req: 1 }] },
      { q: "More details (for the office) — optional", optional: 1,
        gloss: "Skip this if you're not sure — the office can fill it in later.",
        fields: [{ k: "when", label: "When", mini: "(when the conversation happened)", t: "datetime",
                   def: `${reportDate}T14:35` },
                 { k: "ctrade", label: "Trade", t: "chips", opts: OPT.coordTrade },
                 { k: "detail", label: "Detail", t: "textarea", ph: "A longer narrative — what was discussed, what was agreed, next steps." },
                 { k: "ltasks", label: "Linked tasks (optional)", t: "multichips",
                   opts: taskOpts.map((t) => ({ label: t.name, value: t.id })) }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const linked = Array.isArray(a.ltasks) && a.ltasks.length
        ? a.ltasks
        : (m.row.schedule_activity_id ? [m.row.schedule_activity_id] : []);
      const res = await api.post(`/coordination-events`, {
        project_id: project.id, linked_daily_report_id: id,
        occurred_at: a.when || `${reportDate}T14:35`,
        channel: a.chan, direction: a.dir,
        other_party_name: a.party.trim(), other_party_org: (a.org || "").trim() || null,
        other_party_role: null, other_party_email: null, other_party_phone: null,
        topic: a.topic, topic_other_description: a.topic === "other" ? (a.topicOther || "").trim() : null,
        summary: a.csum.trim(), detail_text: (a.detail || "").trim() || null,
        outcome: a.outc, outcome_notes: null,
        linked_task_ids: linked, trade_code: a.ctrade || null,
        ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Log coordination")); throw e; });
      savedToast(res, "Coord logged.");
      touch(m.row.id);
      reload();
      loadCoords();
    },
  });

  const FLOW_photo = (m) => ({
    title: "Photos", sub: m.row.activity_description, save: "Add photo", saving: "Uploading…",
    steps: [
      { q: "Add a photo", gloss: "Photos are usually added from your phone. You can also pick a file from this computer. Pick one to continue.",
        fields: [{ k: "pfile", label: "Photo", t: "file", req: 1 }] },
      { q: "Add a caption", gloss: "A few words about the photo — you can leave it blank.",
        fields: [{ k: "cap", label: "Caption", t: "text", ph: "A few words about the photo" }] },
      { q: "Tag this photo — optional", optional: 1, gloss: "Helps the office file it. Skip if you're not sure.",
        fields: [{ k: "pevent", label: "What was happening in the photo?", mini: "(the event it belongs to)", t: "choice", opts: OPT.photoEvent },
                 { k: "pclass", label: "Where should the office file it?", mini: "(the report section)", t: "choice", opts: OPT.photoClass }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const file = a.pfile;
      const photoClass = a.pclass || "progress";
      let blob = file;
      try { blob = await compressImage(file); } catch (e) { blob = file; }
      try {
        const fd = new FormData();
        fd.append("file", blob, file.name || "photo.jpg");
        fd.append("project_id", project.id);
        fd.append("capture_source", "manual_upload");
        const up = await api.post(`/photo-tray`, fd, { headers: { "Content-Type": "multipart/form-data" } });
        const tag = await api.post(`/photo-tags`, {
          tray_photo_id: up.data.id, daily_report_id: id,
          daily_report_activity_id: m.row.id, photo_class: photoClass, ...addReasonPatch,
        });
        const cap = (a.cap || "").trim();
        if (cap && tag?.data?.id) {
          await api.patch(`/photos/${tag.data.id}`, { caption: cap }).catch(() => {});
        }
        toast.success("Photo added.");
        touch(m.row.id);
        reload();
      } catch (e) {
        if (!e?.response) {
          await queuePhoto({
            blob, mimeType: blob.type || file.type || "image/jpeg",
            dailyReportId: id, projectId: project.id,
            activityRowId: m.row.id, taskId: m.row.schedule_activity_id, photoClass,
          });
          toast.success("Photo saved offline — it will upload when you reconnect.");
          touch(m.row.id);
          reload();
          return;
        }
        toast.error(fmtErr(e, "Add photo"));
        throw e;
      }
    },
  });

  const FLOW_unplanned = () => ({
    title: "Unplanned work", sub: dateSub, save: "Add", saving: "Adding…",
    steps: [
      { q: "What was done?", gloss: "Describe the extra work. The office will review it.",
        fields: [{ k: "udesc", label: "What was done?", t: "textarea", req: 1, ph: "Describe the extra work" }] },
      { q: "Trade", optional: 1, gloss: "Which trade did it? Skip if you're not sure.",
        fields: [{ k: "utrade", label: "Trade", t: "chips", opts: OPT.trade }] },
    ],
    onSave: async (a) => {
      if (needReason()) throw new Error("reason");
      const res = await api.post(`/daily-reports/${id}/unplanned-activity`, {
        description: a.udesc.trim(), trade_code: a.utrade || "", duration_days: 1, ...addReasonPatch,
      }).catch((e) => { toast.error(fmtErr(e, "Create unplanned")); throw e; });
      savedToast(res, "Unplanned work added.");
      reload();
    },
  });

  const FLOW_nowork = () => ({
    title: "No-work day", sub: dateSub, save: "Submit as no-work day", saving: "Submitting…",
    steps: [
      { q: "Reason", gloss: "If no work happened today, pick why.",
        fields: [{ k: "reason", label: "Reason", t: "choice", opts: OPT.noWork, req: 1 }] },
      { q: "Note", when: (a) => a.reason === "other",
        gloss: "You picked “Other”, so a short note is required.",
        fields: [{ k: "nnote", label: "Note", t: "textarea", req: 1, ph: "Add any detail" }] },
    ],
    onSave: async (a) => {
      setNoWorkModal({ reason: a.reason, note: a.nnote || "", __auto: true });
    },
  });

  // No-work flow hands its answers to the parent's state, then the parent's
  // submitNoWorkDay (which reads that state) fires exactly once.
  useEffect(() => {
    if (noWorkModal && noWorkModal.__auto) {
      setNoWorkModal({ reason: noWorkModal.reason, note: noWorkModal.note });
      submitNoWorkDay(() => setNoWorkModal(null));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [noWorkModal]);

  /* ---------- issue chooser ---------- */
  const ISSUE_TYPES = [
    ["Delay", "Something set the work back", FLOW_delay],
    ["RFI", "A question that needs an answer", FLOW_rfi],
    ["Safety", "A near miss, injury, or hazard", FLOW_safety],
    ["QC", "Quality — work that needs fixing", FLOW_qc],
    ["Delivery", "Materials or equipment that arrived", FLOW_delivery],
    ["Coord", "Log an important conversation", FLOW_coord],
  ];
  const openChooser = (m) => setDlg({
    kind: "chooser", title: "Issues & deliveries", sub: `Attached to ${m.row.activity_description}`,
    body: (
      <>
        <div className="gq">Pick what kind of event it was.</div>
        <div className="ggloss">Only the questions you need will follow.</div>
        <div className="opts two">
          {ISSUE_TYPES.map(([label, gloss, mk]) => (
            <button key={label} className="opt" onClick={() => openFlow(mk(m), m)}>
              <span>{label}</span><span className="og">{gloss}</span>
            </button>
          ))}
        </div>
      </>
    ),
    foot: <button className="btn" onClick={() => setDlg(null)}>Cancel</button>,
  });

  /* ---------- photos: pin / delete / AI ---------- */
  const togglePin = (rowId, photoId) => {
    touch(rowId);
    setPinned((p) => {
      const n = new Set(p);
      if (n.has(photoId)) n.delete(photoId); else n.add(photoId);
      return n;
    });
  };
  const deletePhoto = async (rowId, photoId) => {
    if (locked && !lockedReasonOk) { toast.error("This report is submitted — enter the edit reason at the top first."); return; }
    try {
      await api.delete(`/photo-tags/${photoId}`);
      toast.success("Photo removed.");
      touch(rowId);
      reload();
    } catch (e) { toast.error(fmtErr(e, "Remove photo")); }
  };
  const loadReviews = useCallback(async () => {
    try {
      const r = await api.get(`/daily-reports/${id}/photo-reviews`);
      setReviews(Array.isArray(r.data) ? r.data : []);
    } catch (e) { /* silent */ }
  }, [id]);
  // Mockup parity: the AI button TOGGLES the box. First open (no review
  // yet) runs the analysis; after that, clicking just shows/hides it.
  const runAI = async (m) => {
    if (aiOpen.has(m.row.id)) {
      setAiOpen((p) => { const n = new Set(p); n.delete(m.row.id); return n; });
      return;
    }
    if (reviews.length) { setAiOpen((p) => new Set(p).add(m.row.id)); return; }
    const ids = m.rowPhotos.filter((p) => pinned.has(p.id)).map((p) => p.id);
    if (!ids.length) { toast.error("Pin at least one photo to analyze."); return; }
    setAiBusy(true);
    try {
      await api.post(`/photo-tray/review`, { tray_ids: ids, lens: "combined", daily_report_id: id });
      await loadReviews();
      setAiOpen((p) => new Set(p).add(m.row.id));
      touch(m.row.id);
    } catch (e) { toast.error(fmtErr(e, "AI analysis")); }
    finally { setAiBusy(false); }
  };
  useEffect(() => { loadReviews(); }, [loadReviews]);

  // The app shell's <main> is overflow-x:hidden, which (per spec) turns it
  // into the containing block for position:sticky while the real scroller
  // is <html> — so the submit bar would never stick. Lift the overflow only
  // while this view is mounted.
  useEffect(() => {
    const m = document.querySelector("main");
    if (m) m.classList.add("drgx-host");
    return () => { if (m) m.classList.remove("drgx-host"); };
  }, []);

  /* ---------- weather ---------- */
  const wxLine = report?.weather_conditions
    ? <>{report.weather_conditions}{report.weather_temp_high_f != null && <>, high <b>{Math.round(report.weather_temp_high_f)}&deg;F</b></>}.</>
    : <>Not set.</>;
  const wxTime = useMemo(() => {
    if (!report?.weather_fetched_at) return null;
    try { return new Date(report.weather_fetched_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return null; }
  }, [report?.weather_fetched_at]);
  const openWxEdit = () => {
    setWx({
      cond: report?.weather_conditions || "", hi: report?.weather_temp_high_f ?? "",
      lo: report?.weather_temp_low_f ?? "", pr: report?.weather_precipitation_in ?? "",
      wi: report?.weather_wind_mph ?? "",
    });
    setWxEdit((v) => !v);
  };
  const saveWx = async () => {
    if (needReason()) return;
    try {
      const num = (v) => (v === "" || v === null ? null : Number(v));
      const res = await api.patch(`/daily-reports/${id}`, {
        weather_conditions: wx.cond, weather_temp_high_f: num(wx.hi), weather_temp_low_f: num(wx.lo),
        weather_precipitation_in: num(wx.pr), weather_wind_mph: num(wx.wi),
        ...(locked ? { edit_reason: lockedReason.trim() } : {}),
      });
      savedToast(res, "Weather saved.");
      setWxEdit(false);
      reload();
    } catch (e) { toast.error(fmtErr(e, "Save weather")); }
  };
  const refreshWx = async () => {
    try {
      await api.post(`/daily-reports/${id}/weather-autofill`);
      toast.success("Weather refreshed from the local station.");
      reload();
    } catch (e) { toast.error(fmtErr(e, "Refresh weather")); }
  };

  /* ---------- notes / export / submit ---------- */
  const saveNotes = async () => {
    if (needReason()) return;
    try {
      const res = await api.patch(`/daily-reports/${id}`, {
        notes: notesText, ...(locked ? { edit_reason: lockedReason.trim() } : {}),
      });
      savedToast(res, "Note saved to the report.");
      setNotesOpen(false);
      reload();
    } catch (e) { toast.error(fmtErr(e, "Save notes")); }
  };

  const openReview = () => {
    if (report.status === "submitted") { approve(); return; }
    if (report.status === "approved") { onDownloadPdf("internal"); return; }
    const un = model.filter((mm) => !mm.updated);
    setDlg({
      kind: "custom", title: "Submit report", sub: dateSub,
      body: (
        <>
          <div className="gq">Does this look right?</div>
          <div className="ggloss">One last look before it goes to the office. Nothing here blocks you.</div>
          <div className="sumwrap">
            <div className="sumrow"><span className="sl">Weather</span>
              <span className="sv">{report?.weather_conditions
                ? `${report.weather_conditions}${report.weather_temp_high_f != null ? `, high ${Math.round(report.weather_temp_high_f)}°F` : ""}`
                : "Not set."}</span></div>
            {model.map((mm) => (
              <div className="sumrow" key={mm.row.id}>
                <span className="sl">{mm.row.activity_description}</span>
                <span className="sv">{mm.pct}% done · {mm.crew.length} crew · {mm.issues.length} logged · {mm.rowPhotos.length} photo{mm.rowPhotos.length === 1 ? "" : "s"}</span>
              </div>
            ))}
            <div className="sumrow"><span className="sl">Unplanned work</span>
              <span className="sv">{unplannedRows.length} item{unplannedRows.length === 1 ? "" : "s"}</span></div>
            <div className="sumrow"><span className="sl">Voice notes</span>
              <span className="sv">{voiceNotes.length} note{voiceNotes.length === 1 ? "" : "s"}</span></div>
          </div>
          {un.length > 0 && (
            <div className="reviewwarn">
              You haven't looked at:{" "}
              {un.map((mm, i) => (
                <span key={mm.row.id}>
                  {i > 0 && " · "}
                  <b>{mm.row.activity_description}</b>{" "}
                  <button className="sedit" onClick={() => {
                    setDlg(null);
                    setTimeout(() => {
                      const el = document.getElementById(`drgx-act-${mm.row.id}`);
                      if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("hilite"); setTimeout(() => el.classList.remove("hilite"), 1800); }
                    }, 60);
                  }}>Go there</button>
                </span>
              ))}
              . That's okay — it won't stop the report.
            </div>
          )}
        </>
      ),
      foot: (
        <>
          <button className="btn" onClick={() => setDlg(null)}>Keep editing</button>
          <span style={{ flex: 1 }} />
          <button className="btn green" disabled={busy} onClick={() => { setDlg(null); submit(() => {}); }}>
            {busy ? "Submitting…" : "Submit report"}
          </button>
        </>
      ),
    });
  };

  /* close export/notes popovers on outside click + Escape (mockup parity) */
  useEffect(() => {
    const onDoc = (e) => {
      if (!e.target.closest(".exportmenu") && !e.target.closest("[data-export-toggle]")) setExportOpen(false);
      if (!e.target.closest(".notespop") && !e.target.closest("[data-notes-toggle]")) setNotesOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { setExportOpen(false); setNotesOpen(false); }
    };
    document.addEventListener("click", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("click", onDoc); document.removeEventListener("keydown", onKey); };
  }, []);

  const greenLabel = report.status === "approved" ? "Export PDF"
    : report.status === "submitted" ? (busy ? "Approving…" : "Approve report")
    : (busy ? "Submitting…" : "Submit report");

  /* ================= RENDER ================= */
  const actSummary = (m) => {
    if (m.gateBlocked) {
      return (
        <>
          <span className="sumwait">Waiting on: {m.gate?.reason || "a predecessor activity"}</span>
          <span className="sumtap">Click to open</span>
        </>
      );
    }
    return (
      <>
        <span>
          <b>{m.pct}%</b> done · {m.crew.length ? `${m.crew.length} crew` : "no crew yet"} ·{" "}
          {m.issues.length ? `${m.issues.length} logged` : "nothing logged"} ·{" "}
          {m.rowPhotos.length ? `${m.rowPhotos.length} photo${m.rowPhotos.length > 1 ? "s" : ""}` : "no photos"}
        </span>
        <span className="sumtap">Click to open</span>
      </>
    );
  };

  const renderCard = (m) => {
    const r = m.row;
    const isOpen = openActId === r.id;
    const rowErr = submitRowErrors && submitRowErrors[r.id];
    const gateLockedUi = m.gateBlocked;
    const pinnedCount = m.rowPhotos.filter((p) => pinned.has(p.id)).length;
    const latestReview = reviews.length ? reviews[0] : null;
    return (
      <section key={r.id} id={`drgx-act-${r.id}`} data-testid={`drgx-act-${r.id}`}
        className={`card act${isOpen ? " open" : ""}`} onClick={(e) => cardClick(e, r.id)}>
        <div className="ahead">
          <div>
            <div className="anm">{r.activity_description}</div>
            <div className="aloc">{r.location_name || r.activity_code || ""}</div>
          </div>
          <span className={`pill ${m.status.cls}`}>{m.status.word}</span>
          <span className="chev">&#9662;</span>
        </div>
        <div className="asum">{actSummary(m)}</div>
        <div className="abody">
          {rowErr && <div className="waitplain"><span className="wdot" /><div>{rowErr}</div></div>}
          {gateLockedUi && (
            <>
              <div className="waitplain">
                <span className="wdot" />
                <div>
                  Waiting on: <b>{m.gate?.reason || "a predecessor activity"}</b>
                  <div className="whint">That's okay — it won't stop you submitting the report.</div>
                </div>
              </div>
              {canOverride && !ovOpen[r.id] && (
                <button className="btn link" onClick={() => setOvOpen((p) => ({ ...p, [r.id]: true }))}>
                  Someone did start it &#9662;
                </button>
              )}
              {canOverride && ovOpen[r.id] && (
                <div className="ovbox">
                  <label className="f">Why is it okay to start early? <span className="mini">(for the office)</span></label>
                  <input className="gi" placeholder="A few words so the office knows"
                    value={ovDraft[r.id] || ""}
                    onChange={(e) => setOvDraft((p) => ({ ...p, [r.id]: e.target.value }))} />
                  <div className="hint">At least 8 characters — one line is fine.</div>
                  <div style={{ marginTop: 12 }}>
                    <button className="btn small accent" disabled={(ovDraft[r.id] || "").trim().length < 8}
                      onClick={() => {
                        setOverrides((p) => ({ ...p, [r.id]: (ovDraft[r.id] || "").trim() }));
                        touch(r.id);
                        toast.success("Noted for the office — progress unlocked.");
                      }}>Unlock progress</button>
                  </div>
                </div>
              )}
            </>
          )}
          {m.overridden && (
            <div className="ovnote">Started early — noted for the office: &ldquo;{overrides[r.id]}&rdquo;</div>
          )}

          {/* Row 1 — Progress today (inline, no dialog) */}
          <div className="arow first">
            <div className="rl">Progress today</div>
            <div className={`rc ${gateLockedUi ? "lockdim" : ""}`}>
              <div className={`progval${m.pct === 0 ? " zero" : ""}`}>{m.pct}% done</div>
              <div className="progmeta">was {m.sod}%{m.planned != null ? ` · planned ${m.planned}%` : ""}</div>
              <input type="range" className="progrange" min="0" max="100" step="5" value={m.pct}
                disabled={gateLockedUi} onChange={(e) => setProg(m, e.target.value, false)} />
              <div className="quick">
                {[["Nothing yet", 0], ["A little", 25], ["Halfway", 50], ["Most of it", 75], ["All done", 100]].map(([q, v]) => (
                  <button key={q} disabled={gateLockedUi} onClick={() => setProg(m, v, true)}>{q}</button>
                ))}
              </div>
            </div>
            <div className="ra" />
          </div>

          {/* Row 2 — Crew */}
          <div className="arow">
            <div className="rl">Crew</div>
            <div className="rc">
              {m.crew.length === 0 ? <span className="none">No crew added yet.</span> : (
                <div className="chiprow">
                  {m.crew.map((c) => (
                    <span className="crewchip" key={c.id}>
                      {[c.company_name || "(company not named)", c.trade].filter(Boolean).join(" · ")} · {c.headcount} × {c.hours_per_person}h
                      {c.notes ? ` · “${trunc(c.notes, 22)}”` : ""}
                      <button className="cx" title="Remove" onClick={async () => {
                        if (needReason()) return;
                        try { await api.delete(`/daily-presence/${c.id}`); touch(m.row.id); reload(); }
                        catch (e2) { toast.error(fmtErr(e2, "Remove crew")); }
                      }}>&times;</button>
                    </span>
                  ))}
                </div>
              )}
            </div>
            <div className="ra">
              <button className="btn small" onClick={() => openFlow(FLOW_crew(m))}>Add</button>
            </div>
          </div>

          {/* Row 3 — Issues & deliveries */}
          <div className="arow">
            <div className="rl">Issues &amp; deliveries</div>
            <div className="rc">
              {m.issues.length === 0 ? <span className="none">Nothing logged on this activity yet.</span> : (
                <div className="chiprow">
                  {m.issues.map((p) => (
                    <span key={p.key} className={`lchip ${p.cls}`}>{p.kind}: {p.text}</span>
                  ))}
                </div>
              )}
            </div>
            <div className="ra">
              <button className="btn small" onClick={() => openChooser(m)}>Add</button>
            </div>
          </div>

          {/* Row 4 — Photos */}
          <div className="arow">
            <div className="rl">Photos</div>
            <div className="rc">
              <div className="photos">
                {m.rowPhotos.map((p) => (
                  <div key={p.id} className={`photo${pinned.has(p.id) ? " pinned" : ""}`}>
                    <AuthThumb photoId={p.id} />
                    <span className="pins">
                      <button type="button" className={`iconbtn${pinned.has(p.id) ? " on" : ""}`}
                        title={pinned.has(p.id) ? "Unpin" : "Pin"} onClick={() => togglePin(r.id, p.id)}>&#128204;</button>
                      <button type="button" className="iconbtn del" title="Remove photo"
                        onClick={() => deletePhoto(r.id, p.id)}>&#128465;</button>
                    </span>
                    {p.caption ? <span className="pcap">{p.caption}</span> : <span />}
                    {pinned.has(p.id) && <span className="pintag">Pinned</span>}
                  </div>
                ))}
                <button type="button" className="addphoto" onClick={() => openFlow(FLOW_photo(m))}>+ Add</button>
              </div>
              {m.rowPhotos.length > 0 && (
                <>
                  <button className="btn small aibtn" disabled={aiBusy} onClick={() => runAI(m)}>
                    &#10024; {aiBusy ? "Analyzing…" : `AI analysis · ${pinnedCount} pinned`}
                  </button>
                  {aiOpen.has(r.id) && latestReview && (
                    <div className="ai">
                      <div className="h">&#10024; AI analysis</div>
                      <div className="hint" style={{ marginTop: 6 }}>{latestReview.summary_text || latestReview.text || latestReview.label || "Analysis complete."}</div>
                    </div>
                  )}
                </>
              )}
            </div>
            <div className="ra">
              <button className="btn small" onClick={() => openFlow(FLOW_photo(m))}>Add</button>
            </div>
          </div>
        </div>
      </section>
    );
  };

  return (
    <div className="drgx" data-testid="drgx-desktop">
      {/* ============ report header ============ */}
      <div className="rephead">
        <div className="top-in">
          <div className="proj">
            <div className="kicker">
              <button className="btn link" style={{ padding: 0, minHeight: 0, fontSize: 12, letterSpacing: ".08em", fontWeight: 700, textTransform: "uppercase" }}
                onClick={onBack} data-testid="drgx-back">&larr; Daily report</button>
            </div>
            <h1>{project?.name || "Daily report"}</h1>
            <div className="addr">
              {[project?.client_name, project?.contractor_name, dateSub].filter(Boolean).join(" · ")}
            </div>
          </div>
          <div className="saved">Saved automatically</div>
        </div>
      </div>

      <div className="wrap">
        {(!online || queueSize > 0) && (
          <div className="card" style={{ borderColor: "var(--open-line)", background: "var(--open-bg)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", color: "var(--open-fg)", fontWeight: 600 }}>
              <span>{!online ? "You're offline — everything still saves, and syncs when you reconnect." : `${queueSize} change${queueSize === 1 ? "" : "s"} waiting to sync.`}</span>
              {online && queueSize > 0 && (
                <button className="btn small" style={{ marginLeft: "auto" }} onClick={() => drainOfflineQueue(api)}>Sync now</button>
              )}
            </div>
          </div>
        )}

        {locked && (
          <div className="card">
            <div className="sec-title">{report.status === "approved" ? "Approved report" : "Submitted report"}</div>
            <div className="sec-sub">Editing a submitted report — reason required. It's captured in the audit chain.</div>
            <LockedReasonCard value={lockedReason} onChange={setLockedReason} />
          </div>
        )}

        {/* ============ Weather (site-wide) ============ */}
        <div className="card" data-testid="drgx-weather">
          <div className="sec-title">Weather</div>
          <div className="weather">
            <WeatherIcon />
            <div className="now">
              {wxLine}
              <button className="wok" onClick={() => toast.success("Weather confirmed. Thanks!")}>&#10003; Looks right</button>
            </div>
            <div className="push"><button className="btn link" onClick={openWxEdit}>Change</button></div>
          </div>
          <div className="hint">
            {wxTime ? `Filled in automatically at ${wxTime}. Tap "Change" only if it looks wrong.`
              : `Tap "Change" to fill in today's weather.`}
          </div>
          <div className={`wedit${wxEdit ? " open" : ""}`}>
            <div className="fld"><label className="f">Conditions</label>
              <input className="i" value={wx.cond || ""} onChange={(e) => setWx((p) => ({ ...p, cond: e.target.value }))} /></div>
            <div className="g2">
              <div className="fld"><label className="f">High temperature (&deg;F)</label>
                <input className="i" type="number" value={wx.hi} onChange={(e) => setWx((p) => ({ ...p, hi: e.target.value }))} /></div>
              <div className="fld"><label className="f">Low temperature (&deg;F)</label>
                <input className="i" type="number" value={wx.lo} onChange={(e) => setWx((p) => ({ ...p, lo: e.target.value }))} /></div>
            </div>
            <div className="g2">
              <div className="fld"><label className="f">Rain (inches)</label>
                <input className="i" type="number" step="0.1" value={wx.pr} onChange={(e) => setWx((p) => ({ ...p, pr: e.target.value }))} /></div>
              <div className="fld"><label className="f">Wind (mph)</label>
                <input className="i" type="number" value={wx.wi} onChange={(e) => setWx((p) => ({ ...p, wi: e.target.value }))} /></div>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
              <button className="btn accent small" onClick={saveWx}>Save</button>
              <button className="btn small" onClick={() => setWxEdit(false)}>Cancel</button>
              <button className="btn small" onClick={refreshWx}>Get the latest again</button>
            </div>
          </div>
        </div>

        {/* ============ Today's activities ============ */}
        <div style={{ marginTop: 30 }}>
          <div className="sec-title" style={{ fontSize: 22 }}>Today's activities</div>
          <div className="sec-sub" style={{ marginBottom: 0 }}>
            Everything for each activity lives on its card — progress, crew, issues &amp; deliveries, photos.
          </div>
        </div>
        {model.length === 0 && (
          <div className="card"><span className="none">No activities scheduled today. Log unplanned work below, or file a no-work day.</span></div>
        )}
        {model.map(renderCard)}

        {/* ============ Unplanned work (site-wide) ============ */}
        <div className="card" data-testid="drgx-unplanned">
          <div className="sec-title">Unplanned work</div>
          <div className="sec-sub">Did the crew do something that wasn't on the schedule? Log it here. The office will review it.</div>
          <div className="listgap">
            {unplannedRows.length === 0
              ? <div className="none" style={{ padding: "4px 0 10px" }}>Nothing logged yet.</div>
              : unplannedRows.map((u) => (
                <div className="urow" key={u.id}>
                  <span style={{ flex: 1 }}>{u.activity_description}</span>
                  {(u.trade_code || u.location_name) ? <span className="utag">{u.trade_code || u.location_name}</span> : null}
                </div>
              ))}
          </div>
          <button className="btn ghost" onClick={() => openFlow(FLOW_unplanned())}>Add unplanned activity</button>
        </div>

        {/* ============ Voice notes (site-wide) ============ */}
        <div className="card" data-testid="drgx-voice">
          <div className="sec-title">Voice notes</div>
          <div className="sec-sub">Prefer to talk instead of type? Record a short voice note.</div>
          <DrVoiceNotes reportId={id} notes={voiceNotes} reload={reload} allowRecord />
        </div>
      </div>

      {/* ============ sticky submit bar ============ */}
      <div className="submitbar" ref={barRef}>
        <div className="in">
          <div className="status">
            <b>{updatedCount} of {model.length}</b> activities updated today · report saves as you go
          </div>
          <span className="push" />
          <div style={{ position: "relative" }}>
            <div className={`notespop${notesOpen ? " show" : ""}`}>
              <div className="nt">Daily notes</div>
              <div className="hint" style={{ margin: "0 0 10px" }}>Write anything worth remembering about today. This goes on the printed report.</div>
              <textarea className="i" style={{ minHeight: 120 }} value={notesText}
                placeholder="Visitors, weather effects, crew changes, anything notable..."
                onChange={(e) => setNotesText(e.target.value)} />
              <div style={{ display: "flex", gap: 10, marginTop: 12, justifyContent: "flex-end" }}>
                <button className="btn small" onClick={() => setNotesOpen(false)}>Cancel</button>
                <button className="btn small accent" onClick={saveNotes}>Save note</button>
              </div>
            </div>
            <button className="btn link" data-notes-toggle onClick={() => { setNotesOpen((v) => !v); setExportOpen(false); }}>Daily notes</button>
          </div>
          {!locked && (
            <button className="btn link" onClick={() => openFlow(FLOW_nowork())}>File as no-work day</button>
          )}
          <button className="btn small" onClick={() => onDownloadPdf("internal")}>&#11015; PDF</button>
          <div style={{ position: "relative" }}>
            <div className={`exportmenu${exportOpen ? " show" : ""}`}>
              <button onClick={() => { setExportOpen(false); onDownloadPdf("internal"); }}>Internal</button>
              <button onClick={() => { setExportOpen(false); onDownloadPdf("external"); }}>External</button>
              {clientTemplatePresent && (
                <button onClick={() => { setExportOpen(false); onDownloadClientTemplate(); }}>Client template</button>
              )}
            </div>
            <button className="btn small" data-export-toggle onClick={() => { setExportOpen((v) => !v); setNotesOpen(false); }}>
              Export &#9662;
            </button>
          </div>
          <button className="btn green" disabled={busy} onClick={openReview} data-testid="drgx-submit">
            {greenLabel}
          </button>
        </div>
      </div>

      <GuidedDialog session={dlg} onClose={() => setDlg(null)} />
    </div>
  );
}
