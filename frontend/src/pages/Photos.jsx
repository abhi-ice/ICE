// Bucket D Phase 3 -- Photos page reworked as a gallery aggregation
// across all DRs in the project. Each card is a `photos` row (the
// tagging junction); filters cover date, class, evidentiary flag,
// tagged-to event type, affected activity. Click a card -> opens the
// parent DR with the relevant event highlighted.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api, { API } from "../lib/api";
import { useProjects } from "../lib/projects";
import { useUrlState } from "../lib/useUrlState";
import { navigateToTodaysDrForCreate } from "../lib/todaysDr";
import { Camera, Filter, Lock, AlertTriangle, FileImage, Plus, Upload, Sparkles, Edit3, HelpCircle, Download, Loader2 } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";
import { getToken, getUser } from "../lib/auth";
import GalleryActivityChips from "../components/photos/GalleryActivityChips";
import { localTodayISO } from "../lib/dateUtil";
import { AiHelpPanel } from "./SystemPages";
import PageHelp from "../components/PageHelp";

const PHOTO_CLASSES = ["progress", "discovery", "damage", "quality",
                        "safety", "coordination", "closeout"];
const PHOTO_CLASS_TONES = {
  progress: "bg-slate-100 text-slate-700 border-slate-300",
  discovery: "bg-amber-100 text-amber-800 border-amber-300",
  damage: "bg-rose-100 text-rose-800 border-rose-300",
  quality: "bg-violet-100 text-violet-800 border-violet-300",
  safety: "bg-orange-100 text-orange-800 border-orange-300",
  coordination: "bg-sky-100 text-sky-800 border-sky-300",
  closeout: "bg-emerald-100 text-emerald-800 border-emerald-300",
};

const TAGGED_TO_OPTIONS = [
  { value: "",            label: "Any tag type" },
  { value: "activity",    label: "Activity" },
  { value: "delay",       label: "Delay" },
  { value: "rfi",         label: "RFI" },
  { value: "qc",          label: "QC issue" },
  { value: "safety",      label: "Safety event" },
  { value: "coordination", label: "Coordination" },
  { value: "delivery",    label: "Delivery" },
  { value: "drawing",     label: "Drawing pin" },
];

function eventLabelForRow(p) {
  if (p.daily_report_activity_id) return { type: "activity", id: p.daily_report_activity_id };
  if (p.delay_id) return { type: "delay", id: p.delay_id };
  if (p.rfi_id) return { type: "RFI", id: p.rfi_id };
  if (p.qc_issue_id) return { type: "QC", id: p.qc_issue_id };
  if (p.safety_event_id) return { type: "safety", id: p.safety_event_id };
  if (p.coordination_event_id) return { type: "coordination", id: p.coordination_event_id };
  if (p.delivery_id) return { type: "delivery", id: p.delivery_id };
  if (p.drawing_pin_id) return { type: "drawing pin", id: p.drawing_pin_id };
  return { type: "untagged", id: null };
}

export default function Photos() {
  const { activeId, projects } = useProjects();
  const [photos, setPhotos] = useState([]);
  const [loading, setLoading] = useState(false);
  // Bucket D Fix H -- filter state mirrored to URL.
  const [filters, setFilters] = useUrlState({
    photo_class: "", evidentiary_only: false, tagged_to: "",
    affected_activity_id: "",
  });
  const project = projects.find((p) => p.id === activeId);
  // Bucket F Phase 3 -- AI stats card. Admin / PM only. Fetched from
  // the per-project endpoint added in Phase 2.
  const user = getUser();
  const showAiStats = ["admin", "pm", "px"].includes((user?.role || "").toLowerCase());
  const [aiStats, setAiStats] = useState(null);
  const [aiStatsLoading, setAiStatsLoading] = useState(false);
  useEffect(() => {
    if (!showAiStats || !activeId) { setAiStats(null); return; }
    setAiStatsLoading(true);
    api.get(`/projects/${activeId}/photo-ai-stats`, { params: { days: 30 } })
      .then((r) => setAiStats(r.data))
      .catch(() => setAiStats(null))
      .finally(() => setAiStatsLoading(false));
  }, [showAiStats, activeId]);

  const load = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      const params = {};
      if (filters.photo_class) params.photo_class = filters.photo_class;
      if (filters.evidentiary_only) params.evidentiary_only = true;
      if (filters.tagged_to) params.tagged_to = filters.tagged_to;
      if (filters.affected_activity_id) params.affected_activity_id = filters.affected_activity_id;
      const r = await api.get(`/projects/${activeId}/photos`, { params });
      setPhotos(Array.isArray(r.data) ? r.data : []);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load photos");
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [activeId, filters]);

  // Bucket F Phase 5 -- detect budget_paused state from the per-
  // project AI stats. Surfaces a page-level banner so users don't
  // have to drill into individual tray photos to understand why
  // their new uploads aren't being analyzed.
  const budgetPausedCount = (aiStats?.by_status?.budget_paused) || 0;
  const [helpOpen, setHelpOpen] = useState(false);

  if (!activeId) return <div className="text-sm text-slate-500">Pick a project to see photos.</div>;

  return (
    /* pt.35: dropped outer p-6 -- AppLayout main already pads p-6. */
    <div data-testid="photos-page">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Camera className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800 inline-flex items-baseline">
              Photos
              {/* pt.36: PageHelp (?) houses the long multi-tag rule
                  explanation that previously lived in a yellow brick
                  below. Subtitle keeps the short A-style line. */}
              <PageHelp title="Photos gallery">
                Each card here is a <b>tag</b> — it connects one photo to one event on one
                daily report. The same image may appear multiple times if it's tagged to
                multiple events (e.g. a delay AND a safety concern).
                <br/><br/>
                Upload + tag new photos from the <Link to="/photo-tray" className="text-emerald-700 underline">Photo tray</Link>.
              </PageHelp>
            </h1>
          </div>
          <p className="text-sm text-slate-500">{project?.name} · gallery view across all daily reports</p>
        </div>
        <div className="flex items-center gap-2">
          {/* pt.39 button-system sweep: secondary 28px sbtn-sm. */}
          <button data-testid="photos-ai-help"
            onClick={() => setHelpOpen(true)} title="How does Photo AI work?"
            className="sbtn sbtn-sm sbtn-secondary">
            <HelpCircle /> AI help
          </button>
          {/* Bucket K Phase 3 (2026-05-11) -- photo evidence pack
              export. Project-scoped; respects the current filter row. */}
          <PhotoEvidenceExport projectId={activeId} filters={filters} />
          {/* Bucket D Fix G [3/4] -- "+ New" routes to today's DR's
              photos panel (consistent with all other galleries). The
              upload+tag flow lives on the Photo Tray page; the link
              below is preserved for users who want to go straight
              to the tray. */}
          <PhotoNewButton projectId={activeId} />
          {/* pt.39: secondary 28px. */}
          <Link to="/photo-tray"
            data-testid="photos-go-to-tray"
            className="sbtn sbtn-sm sbtn-secondary">
            <Upload /> Photo tray
          </Link>
        </div>
      </header>

      {budgetPausedCount > 0 && (
        <div data-testid="photos-budget-paused-banner"
          className="mb-4 bg-rose-50 border border-rose-300 rounded p-3 text-sm text-rose-900 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-medium">
              AI analyses paused — monthly budget reached.
            </div>
            <div className="text-xs mt-0.5">
              {budgetPausedCount} tray photo(s) on this project are queued
              waiting for the budget reset (1st of next month) or for an admin
              to raise the cap in{" "}
              <Link to="/settings" className="underline">Settings → AI Usage</Link>.
            </div>
          </div>
        </div>
      )}

      {/* pt.36: yellow Gallery-view brick removed — content now lives
          in the PageHelp (?) on the title (Option B) + the muted
          subtitle (Option A). Stops the page from looking like a PDF
          with a thick header band on every visit. */}

      {/* Bucket F Phase 3 -- AI stats card (admin / PM only). */}
      {showAiStats && (
        <AiStatsCard stats={aiStats} loading={aiStatsLoading} />
      )}

      <div className="bg-white border border-slate-200 rounded p-3 mb-4 flex items-end gap-3 flex-wrap">
        <Filter className="h-4 w-4 text-slate-500 mb-2" />
        <div>
          <label className="text-xs text-slate-500">Photo class</label>
          <select data-testid="photos-filter-class"
            value={filters.photo_class} onChange={(e) => setFilters({ ...filters, photo_class: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white">
            <option value="">(any)</option>
            {PHOTO_CLASSES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-500">Tagged to</label>
          <select data-testid="photos-filter-tagged-to"
            value={filters.tagged_to} onChange={(e) => setFilters({ ...filters, tagged_to: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white">
            {TAGGED_TO_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-500">Affected activity id</label>
          <input data-testid="photos-filter-activity"
            value={filters.affected_activity_id}
            onChange={(e) => setFilters({ ...filters, affected_activity_id: e.target.value })}
            placeholder="daily_report_activity.id"
            className="block h-8 px-2 rounded border border-slate-300 text-sm w-64" />
        </div>
        <label className="text-xs flex items-center gap-1">
          <input data-testid="photos-filter-evidentiary"
            type="checkbox" checked={filters.evidentiary_only}
            onChange={(e) => setFilters({ ...filters, evidentiary_only: e.target.checked })} />
          <span>Evidentiary only</span>
        </label>
      </div>

      {loading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : !photos.length ? (
        <div className="text-sm text-slate-500 border border-dashed border-slate-300 rounded p-8 text-center">
          No photos match these filters. Upload + tag from the Photo tray.
        </div>
      ) : (
        <div className="grid grid-cols-3 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 gap-2">
          {photos.map((p) => <PhotoCard key={p.id} photo={p} projectId={activeId} />)}
        </div>
      )}

      {/* Bucket F Phase 5 -- in-app AI help (same panel as Settings tab). */}
      {helpOpen && <AiHelpPanel onClose={() => setHelpOpen(false)} />}
    </div>
  );
}

function PhotoCard({ photo, projectId }) {
  const tone = PHOTO_CLASS_TONES[photo.photo_class] || PHOTO_CLASS_TONES.progress;
  const { type: eventType } = eventLabelForRow(photo);
  return (
    <Link to={`/daily-reports?openId=${photo.daily_report_id}&openEvent=${photo.id}&openEventType=photo`}
      data-testid={`photos-card-${photo.id}`}
      className="block bg-white border border-slate-200 rounded overflow-hidden hover:border-blue-400 hover:shadow-sm transition">
      <div className="relative aspect-square bg-slate-100">
        <PhotoImage photoId={photo.id} />
        <span className={`absolute top-1 left-1 text-xs px-1.5 py-0.5 rounded border ${tone}`}>
          {photo.photo_class}
        </span>
        {photo.evidentiary_flag && (
          <span title="Evidentiary -- locked record" className="absolute top-1 right-1 bg-black/70 text-white rounded p-0.5">
            <Lock className="h-2.5 w-2.5" />
          </span>
        )}
        {photo.timestamp_mismatch_flag && (
          <span title="Device/server timestamp mismatch" className="absolute bottom-1 right-1 bg-amber-500 text-white rounded p-0.5">
            <AlertTriangle className="h-2.5 w-2.5" />
          </span>
        )}
        {/* Bucket F Phase 3 + Phase 5 pull-forward Risk #1 -- AI
            lineage indicator. Sparkles icon on photos created via an
            AI suggestion. user_overridden_at_tag_time (denormalized
            snapshot at tag time per migration 011) determines whether
            the override pencil renders, so the indicator stays
            accurate even if the tray row is later edited or
            archived. Hover shows confidence captured at tag time. */}
        {photo.created_via_ai_suggestion && (
          <span data-testid={`photo-ai-${photo.id}`}
            title={
              (photo.user_overridden_at_tag_time
                ? `AI suggestion (overridden at tag time)`
                : `AI-suggested tag`)
              + (photo.ai_confidence_at_tag_time != null
                  ? ` · ${Math.round(photo.ai_confidence_at_tag_time * 100)}% confidence`
                  : "")
            }
            className="absolute top-1 right-6 bg-blue-600 text-white rounded px-1 py-0.5 text-xs inline-flex items-center gap-0.5">
            <Sparkles className="h-2.5 w-2.5" /> AI
            {photo.user_overridden_at_tag_time && (
              <Edit3 className="h-2.5 w-2.5" />
            )}
          </span>
        )}
      </div>
      <div className="p-2 space-y-1 text-xs">
        <div className="flex items-center justify-between">
          <span className="text-slate-500">{photo.daily_report_id ? (photo.daily_report_id.slice(0, 8)) : "no DR"}</span>
          <span className="text-xs text-slate-400">{eventType}</span>
        </div>
        {photo.tag_caption && <div className="text-slate-700 truncate" title={photo.tag_caption}>"{photo.tag_caption}"</div>}
        {photo.daily_report_activity_id && (
          <GalleryActivityChips projectId={projectId} activityIds={[photo.daily_report_activity_id]} max={1} />
        )}
        {photo.is_supplement && (
          <div data-testid={`photo-gallery-supp-${photo.id}`}
            className="text-xs inline-block px-1.5 py-0.5 rounded bg-amber-200 text-amber-900 font-mono">
            SUPP→{photo.parent_daily_report_date || photo.parent_daily_report_id?.slice(0, 8) || "?"}
          </div>
        )}
      </div>
    </Link>
  );
}

// Bucket D Fix G [3/4] -- + New button that routes to today's DR with
// PhotosPanel scrolled into view + pulsed.
function PhotoNewButton({ projectId }) {
  const navigate = useNavigate();
  const [routing, setRouting] = useState(false);
  const click = async () => {
    if (routing || !projectId) return;
    setRouting(true);
    try {
      const { url, locked, dr } = await navigateToTodaysDrForCreate(projectId, "photo");
      if (locked) {
        toast.info(`Today's DR is locked. Upload + tag photos via the Photo tray instead.`);
        navigate(`/daily-reports?openId=${dr.id}`);
      } else {
        navigate(url);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Couldn't resolve today's DR");
    } finally { setRouting(false); }
  };
  return (
    <button data-testid="photos-add-via-dr" onClick={click} disabled={routing}
      className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1">
      <Plus className="h-3.5 w-3.5" /> {routing ? "Opening today's DR…" : "New on today's DR"}
    </button>
  );
}

// Bucket F Phase 3 -- AI usage stats card. Admin / PM only. 30-day
// rollup from /api/projects/{id}/photo-ai-stats. Renders 6 metrics
// + top safety concerns + estimated cost.
function AiStatsCard({ stats, loading }) {
  if (loading) {
    return (
      <div data-testid="ai-stats-loading"
        className="bg-white border border-slate-200 rounded p-3 mb-4 text-xs text-slate-500">
        Loading AI usage stats…
      </div>
    );
  }
  if (!stats || stats.uploaded_in_window === 0) {
    return (
      <div data-testid="ai-stats-empty"
        className="bg-white border border-slate-200 rounded p-3 mb-4 text-xs text-slate-500 inline-flex items-center gap-2">
        <Sparkles className="h-3 w-3" />
        No AI activity in the last 30 days on this project.
      </div>
    );
  }
  // pt.36 (2026-05-27): shrunk from ~140px tall to ~60px. Outer
  // padding p-3 -> px-3 py-2, header mb-2 -> mb-1.5, tile padding
  // p-2 -> px-2 py-1.5, tile layout switched from stacked (label
  // above / value below) to horizontal (label · value on one line)
  // for ~50% vertical savings. Safety + failure rows now inline
  // pills with no border-top section break, separated by a hairline.
  return (
    <div data-testid="ai-stats-card"
      className="bg-white border border-slate-200 rounded px-3 py-2 mb-3">
      <div className="flex items-center gap-2 mb-1.5">
        <Sparkles className="h-3.5 w-3.5 text-blue-700" />
        <div className="font-medium text-xs">AI usage · last 30 days</div>
        <div className="ml-auto text-[11px] text-slate-500">
          {stats.window_days}d · {stats.uploaded_in_window} uploaded
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-1.5">
        <StatTile label="Analyzed" value={stats.analyzed_in_window} />
        <StatTile label="Avg conf"
          value={`${Math.round((stats.avg_confidence_score || 0) * 100)}%`} />
        <StatTile label="Override"
          value={`${Math.round((stats.override_rate || 0) * 100)}%`} />
        <StatTile label="Rejected"
          value={`${Math.round((stats.rejection_rate || 0) * 100)}%`} />
        <StatTile label="Tokens o/i"
          value={`${stats.output_tokens_total || 0}/${stats.input_tokens_total || 0}`} />
        <StatTile label="Cost"
          value={`$${(stats.estimated_cost_usd || 0).toFixed(2)}`} />
      </div>
      {Array.isArray(stats.top_safety_concerns) && stats.top_safety_concerns.length > 0 && (
        <div className="mt-1.5 pt-1.5 border-t border-slate-100 flex items-center gap-2 flex-wrap">
          <span className="text-[10px] uppercase tracking-wide text-slate-500 font-medium shrink-0">Safety</span>
          {stats.top_safety_concerns.map((c, i) => (
            <span key={i} data-testid={`ai-stats-safety-${i}`}
              className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-rose-50 text-rose-900 border border-rose-200">
              <AlertTriangle className="h-2.5 w-2.5" />
              {c.concern} <span className="text-rose-700">({c.count})</span>
            </span>
          ))}
        </div>
      )}
      {Object.keys(stats.failure_reasons || {}).length > 0 && (
        <div className="mt-1.5 pt-1.5 border-t border-slate-100 flex items-center gap-2 flex-wrap">
          <span className="text-[10px] uppercase tracking-wide text-slate-500 font-medium shrink-0">Failed</span>
          {Object.entries(stats.failure_reasons).map(([reason, count]) => (
            <span key={reason}
              className="text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200">
              {reason} <span className="text-slate-500">({count})</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// pt.36: horizontal label · value layout (was vertical stack). Halves
// the tile height while still reading cleanly because the values are
// short (mostly 1-4 chars + %).
function StatTile({ label, value }) {
  return (
    <div className="px-2 py-1.5 rounded border border-slate-200 bg-slate-50 flex items-baseline justify-between gap-2">
      <div className="text-[10px] text-slate-500 uppercase tracking-wide truncate">{label}</div>
      <div className="text-xs font-semibold text-slate-900 tabular-nums whitespace-nowrap">{value}</div>
    </div>
  );
}


function PhotoImage({ photoId }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let urlObj = null;
    (async () => {
      try {
        const res = await fetch(`${API}/photos/${photoId}/file`, {
          headers: { Authorization: `Bearer ${getToken()}` },
          credentials: "include",
        });
        if (res.ok) { urlObj = URL.createObjectURL(await res.blob()); setUrl(urlObj); }
      } catch {}
    })();
    return () => { if (urlObj) URL.revokeObjectURL(urlObj); };
  }, [photoId]);
  if (!url) return <div className="w-full h-full grid place-items-center text-slate-300"><FileImage className="h-8 w-8" /></div>;
  return <img src={url} alt="" className="w-full h-full object-cover" />;
}


// Bucket K Phase 3 (2026-05-11) -- photo evidence pack export dropdown.
// Project-scoped; passes current Photos page filters through to the
// backend export endpoint so users get the exhibit pack matching
// what they see on screen.
function PhotoEvidenceExport({ projectId, filters }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const downloadFmt = async (fmt) => {
    if (!projectId) {
      toast.error("Pick a project first");
      return;
    }
    setBusy(true);
    setOpen(false);
    try {
      const params = { format: fmt };
      if (filters?.photo_class) params.photo_class = filters.photo_class;
      if (filters?.evidentiary_only) params.evidentiary_only = true;
      const res = await api.get(
        `/projects/${projectId}/photo-evidence-pack/export`,
        { params, responseType: "blob" },
      );
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      const ext = fmt === "zip" ? "zip" : fmt;
      a.href = url;
      a.download = `photo_evidence_pack_${projectId.slice(0, 8)}.${ext}`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(`${fmt.toUpperCase()} evidence pack ready`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Export failed");
    } finally { setBusy(false); }
  };

  return (
    <div className="relative">
      <button data-testid="photos-export-toggle"
        onClick={() => setOpen((v) => !v)}
        disabled={busy || !projectId}
        title="Generate exhibit-ready photo evidence pack"
        className="h-9 px-3 rounded border border-slate-300 bg-white text-slate-700 text-sm inline-flex items-center gap-1 hover:bg-slate-50 disabled:opacity-50">
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <Download className="h-3.5 w-3.5" />}
        Evidence pack
      </button>
      {open && (
        <div data-testid="photos-export-menu"
          className="absolute right-0 mt-1 w-32 bg-white border border-slate-200 rounded shadow-lg z-10">
          {["pdf", "docx", "zip"].map((fmt) => (
            <button key={fmt} onClick={() => downloadFmt(fmt)}
              data-testid={`photos-export-${fmt}`}
              className="w-full text-left px-3 py-1.5 text-sm hover:bg-slate-50">
              {fmt.toUpperCase()}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
