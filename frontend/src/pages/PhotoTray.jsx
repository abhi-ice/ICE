// Bucket D Phase 2 -- Photo tray page. Staging area for photos before
// they're tagged to specific DR events. Grid of tray photos with
// status filter; click a photo to open the tagging wizard.
import { useEffect, useState, useCallback, useMemo } from "react";
import {
  Camera, Upload, Filter, Archive, Check, MapPin, Clock, FileImage,
  Sparkles, X as XIcon, AlertTriangle, HelpCircle,
} from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import TagWizard from "../components/photos/TagWizard";
import AiAnalysisCard from "../components/photos/AiAnalysisCard";
import { useUrlState } from "../lib/useUrlState";
import { AiHelpPanel } from "./SystemPages";

// Bucket F Phase 3 -- first-run tooltip dismissable per user via
// localStorage. Shown the first time a user opens the tray modal
// after the AI feature ships.
const AI_FIRSTRUN_KEY = "ice.photoai.firstrun.v1";

const STATUS_LABELS = {
  untagged:           { label: "Untagged",           tone: "bg-slate-100 text-slate-700" },
  partially_tagged:   { label: "Partially tagged",   tone: "bg-amber-100 text-amber-800" },
  fully_tagged:       { label: "Fully tagged",       tone: "bg-emerald-100 text-emerald-800" },
  archived:           { label: "Archived",           tone: "bg-slate-200 text-slate-500" },
};

const SOURCE_LABELS = {
  mobile_camera: "Mobile camera",
  manual_upload: "Manual upload",
  drawing_pin:   "Drawing pin",
};

function isoDaysAgo(n) {
  const d = new Date(); d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

export default function PhotoTray() {
  const { activeId } = useProjects();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  // Bucket D Fix H -- filter state mirrored to URL.
  const [filters, setFilters] = useUrlState({
    status: "", capture_source: "", date_from: isoDaysAgo(30),
    date_to: "", untagged_only: false,
  });
  const [openDetail, setOpenDetail] = useState(null);
  const [openWizard, setOpenWizard] = useState(false);
  const [uploading, setUploading] = useState(false);

  const load = useCallback(async () => {
    if (!activeId) { setRows([]); return; }
    setLoading(true);
    try {
      const params = {};
      if (filters.untagged_only) params.untagged_only = true;
      else if (filters.status) params.status = filters.status;
      if (filters.capture_source) params.capture_source = filters.capture_source;
      if (filters.date_from) params.date_from = filters.date_from;
      if (filters.date_to) params.date_to = filters.date_to;
      const r = await api.get(`/projects/${activeId}/photo-tray`, { params });
      setRows(r.data || []);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load tray");
    } finally { setLoading(false); }
  }, [activeId, filters]);

  useEffect(() => { load(); }, [load]);

  const onUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file || !activeId) return;
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("project_id", activeId);
      fd.append("capture_source", "manual_upload");
      const r = await api.post("/photo-tray", fd, { headers: { "Content-Type": "multipart/form-data" } });
      toast.success(`Uploaded (${r.data.status})`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Upload failed");
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  };

  const archive = async (id) => {
    if (!window.confirm("Archive this tray entry? It will hide from the default view.")) return;
    try {
      await api.post(`/photo-tray/${id}/archive`);
      load();
    } catch (e) { toast.error(e.response?.data?.detail || "Archive failed"); }
  };

  // Bucket F Phase 5 -- show a budget-paused banner whenever any
  // tray row on this page has status=budget_paused. The pill on
  // individual cards is easy to miss when scrolling; the banner is
  // a clear top-of-page signal that AI is currently disabled
  // tenant-wide and explains what to do.
  const budgetPausedCount = useMemo(
    () => rows.filter((r) => r.ai_analysis_status === "budget_paused").length,
    [rows],
  );
  const [helpOpen, setHelpOpen] = useState(false);

  if (!activeId) return <div className="p-6 text-sm text-slate-500">Pick a project to see the photo tray.</div>;

  return (
    <div className="p-6 max-w-7xl mx-auto" data-testid="photo-tray-page">
      <header className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Camera className="h-5 w-5 text-slate-700" />
          <h1 className="text-xl font-semibold text-slate-800">Photo tray</h1>
          <span className="text-xs text-slate-400 ml-2">
            Untagged + partially-tagged staging. Tag photos to events from here.
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button data-testid="photo-tray-ai-help"
            onClick={() => setHelpOpen(true)} title="How does Photo AI work?"
            className="h-9 px-2 rounded border border-slate-300 bg-white text-slate-600 text-sm inline-flex items-center gap-1 hover:bg-slate-50">
            <HelpCircle className="h-3.5 w-3.5" /> AI help
          </button>
          <label data-testid="photo-tray-upload" className="block">
            <span className="h-9 px-3 rounded bg-blue-600 text-white text-sm inline-flex items-center gap-1 hover:bg-blue-700 cursor-pointer">
              <Upload className="h-3.5 w-3.5" /> {uploading ? "Uploading…" : "Upload photo"}
            </span>
            <input type="file" accept="image/*" onChange={onUpload} className="hidden" />
          </label>
        </div>
      </header>

      {budgetPausedCount > 0 && (
        <div data-testid="tray-budget-paused-banner"
          className="mb-4 bg-rose-50 border border-rose-300 rounded p-3 text-sm text-rose-900 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div className="flex-1">
            <div className="font-medium">
              AI analyses paused — monthly budget reached.
            </div>
            <div className="text-xs mt-0.5">
              {budgetPausedCount} photo(s) on this page are queued in
              <code className="mx-1 px-1 bg-rose-100 rounded">budget_paused</code>
              state. They'll auto-process when the budget resets on the 1st,
              or admins can raise the cap in{" "}
              <a href="/settings" className="underline">Settings → AI Usage</a>.
            </div>
          </div>
        </div>
      )}

      <div className="bg-white border border-slate-200 rounded p-3 mb-4 flex items-end gap-3 flex-wrap">
        <Filter className="h-4 w-4 text-slate-500 mb-2" />
        <div>
          <label className="text-[10px] text-slate-500 uppercase">Status</label>
          <select data-testid="tray-filter-status" value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value, untagged_only: false })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white" disabled={filters.untagged_only}>
            <option value="">(any)</option>
            <option value="untagged">Untagged</option>
            <option value="partially_tagged">Partially tagged</option>
            <option value="fully_tagged">Fully tagged</option>
            <option value="archived">Archived</option>
          </select>
        </div>
        <div>
          <label className="text-[10px] text-slate-500 uppercase">Source</label>
          <select data-testid="tray-filter-source" value={filters.capture_source} onChange={(e) => setFilters({ ...filters, capture_source: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white">
            <option value="">(any)</option>
            <option value="mobile_camera">Mobile camera</option>
            <option value="manual_upload">Manual upload</option>
            <option value="drawing_pin">Drawing pin</option>
          </select>
        </div>
        <div>
          <label className="text-[10px] text-slate-500 uppercase">From</label>
          <input data-testid="tray-filter-from" type="date" value={filters.date_from} onChange={(e) => setFilters({ ...filters, date_from: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm" />
        </div>
        <div>
          <label className="text-[10px] text-slate-500 uppercase">To</label>
          <input data-testid="tray-filter-to" type="date" value={filters.date_to} onChange={(e) => setFilters({ ...filters, date_to: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm" />
        </div>
        <label className="text-xs flex items-center gap-1">
          <input data-testid="tray-filter-untagged" type="checkbox" checked={filters.untagged_only}
            onChange={(e) => setFilters({ ...filters, untagged_only: e.target.checked, status: "" })} />
          <span>Untagged only</span>
        </label>
      </div>

      {loading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : !rows.length ? (
        <div className="text-sm text-slate-500 border border-dashed border-slate-300 rounded p-8 text-center">
          No photos in tray. Upload one with the button above, or take photos from the daily report page.
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3">
          {rows.map((r) => (
            <TrayCard key={r.id} row={r} onClick={() => setOpenDetail(r)} />
          ))}
        </div>
      )}

      {/* Detail dialog */}
      <Dialog open={!!openDetail} onOpenChange={(v) => { if (!v) setOpenDetail(null); }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Tray photo</DialogTitle></DialogHeader>
          {openDetail && <DetailContent row={openDetail}
            onTagClick={() => setOpenWizard(true)}
            onArchive={() => { archive(openDetail.id); setOpenDetail(null); }} />}
        </DialogContent>
      </Dialog>

      {/* Tag wizard */}
      {openDetail && (
        <TagWizard open={openWizard} onOpenChange={setOpenWizard}
          tray={openDetail} projectId={activeId}
          onTagged={() => { load(); setOpenDetail(null); }} />
      )}

      {/* Bucket F Phase 5 -- in-app AI help. Same panel renders on the
          AI Usage settings tab; importing keeps copy consistent. */}
      {helpOpen && <AiHelpPanel onClose={() => setHelpOpen(false)} />}
    </div>
  );
}

function TrayCard({ row, onClick }) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    let cancelled = false; let blob = null;
    api.get(`/photo-tray/${row.id}/file`, { responseType: "blob" })
      .then((r) => { if (cancelled) return; blob = URL.createObjectURL(r.data); setSrc(blob); })
      .catch(() => {});
    return () => { cancelled = true; if (blob) URL.revokeObjectURL(blob); };
  }, [row.id]);
  const status = STATUS_LABELS[row.status] || STATUS_LABELS.untagged;
  return (
    <div data-testid={`tray-card-${row.id}`} onClick={onClick}
      className="relative aspect-square bg-slate-100 rounded overflow-hidden border border-slate-200 cursor-pointer hover:border-blue-400 hover:shadow-sm transition">
      {src ? <img src={src} alt="" className="w-full h-full object-cover" />
        : <div className="w-full h-full grid place-items-center text-slate-300"><FileImage className="h-8 w-8" /></div>}
      <span className={`absolute top-1 left-1 text-[9px] px-1.5 py-0.5 rounded ${status.tone}`}>
        {status.label}
      </span>
      {row.exif_taken_at && (
        <span className="absolute bottom-1 left-1 text-[9px] px-1.5 py-0.5 rounded bg-black/60 text-white inline-flex items-center gap-1">
          <Clock className="h-2.5 w-2.5" /> {String(row.exif_taken_at).slice(0, 10)}
        </span>
      )}
      {row.exif_gps_lat != null && (
        <span className="absolute bottom-1 right-1 text-[9px] px-1 py-0.5 rounded bg-black/60 text-white">
          <MapPin className="h-2.5 w-2.5 inline" />
        </span>
      )}
      {/* Bucket F Phase 3 -- AI status badge in top-right of card. */}
      {row.ai_analysis_status && row.ai_analysis_status !== "skipped" && (
        <span data-testid={`tray-card-ai-${row.id}`}
          title={`AI: ${row.ai_analysis_status}${
            row.ai_confidence_score
              ? ` (${Math.round(row.ai_confidence_score * 100)}%)` : ""
          }`}
          className={`absolute top-1 right-1 text-[9px] px-1.5 py-0.5 rounded inline-flex items-center gap-0.5 ${
            row.ai_analysis_status === "complete"
              ? "bg-blue-600 text-white"
              : row.ai_analysis_status === "failed"
                ? "bg-rose-600 text-white"
                : row.ai_analysis_status === "budget_paused"
                  ? "bg-amber-500 text-white"
                  : row.ai_analysis_status === "rejected_by_user"
                    ? "bg-slate-400 text-white"
                    : "bg-sky-500 text-white"
          }`}>
          <Sparkles className="h-2.5 w-2.5" />
          {row.ai_analysis_status === "complete" && row.ai_confidence_score
            ? `${Math.round(row.ai_confidence_score * 100)}%`
            : row.ai_analysis_status === "complete"
              ? "AI"
              : row.ai_analysis_status.charAt(0).toUpperCase()}
        </span>
      )}
    </div>
  );
}

function DetailContent({ row, onTagClick, onArchive }) {
  const [src, setSrc] = useState(null);
  const [tags, setTags] = useState([]);
  // Bucket F Phase 3 -- first-run tooltip state. Read once on mount;
  // dismiss writes to localStorage so the tooltip stays gone for this
  // browser. Per-user (not per-tenant) since localStorage is browser-local.
  const [showFirstRun, setShowFirstRun] = useState(() => {
    try { return localStorage.getItem(AI_FIRSTRUN_KEY) !== "1"; }
    catch { return false; }
  });
  const dismissFirstRun = () => {
    try { localStorage.setItem(AI_FIRSTRUN_KEY, "1"); } catch { /* ignore */ }
    setShowFirstRun(false);
  };
  useEffect(() => {
    let cancelled = false; let blob = null;
    api.get(`/photo-tray/${row.id}/file`, { responseType: "blob" })
      .then((r) => { if (cancelled) return; blob = URL.createObjectURL(r.data); setSrc(blob); })
      .catch(() => {});
    api.get(`/photo-tray/${row.id}/tags`).then((r) => setTags(r.data || [])).catch(() => setTags([]));
    return () => { cancelled = true; if (blob) URL.revokeObjectURL(blob); };
  }, [row.id]);

  return (
    <div className="space-y-3 text-sm">
      {/* Bucket F Phase 3 -- first-run AI tooltip. Dismissable. */}
      {showFirstRun && (
        <div data-testid="ai-firstrun-tooltip"
          className="p-2 rounded border border-sky-200 bg-sky-50 text-xs flex items-start gap-2">
          <Sparkles className="h-3.5 w-3.5 text-sky-700 mt-0.5 shrink-0" />
          <div className="flex-1 text-sky-900">
            <strong>New: AI photo analysis.</strong> Every upload now runs through
            Claude Vision to suggest a class, caption, and event type. You'll
            see the suggestion below — accept, override, or reject as needed.
            All overrides are audit-logged.
          </div>
          <button onClick={dismissFirstRun}
            data-testid="ai-firstrun-dismiss"
            className="text-sky-700 hover:text-sky-900 shrink-0"
            title="Dismiss">
            <XIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      <div className="bg-slate-100 rounded overflow-hidden">
        {src ? <img src={src} alt="" className="w-full max-h-[400px] object-contain bg-black" />
          : <div className="h-64 grid place-items-center text-slate-400">Loading…</div>}
      </div>
      {/* Bucket F Phase 3 -- AI analysis section. */}
      <AiAnalysisCard trayId={row.id} />
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div><span className="text-slate-500 uppercase text-[10px]">Source:</span> {SOURCE_LABELS[row.capture_source] || row.capture_source}</div>
        <div><span className="text-slate-500 uppercase text-[10px]">Status:</span> {STATUS_LABELS[row.status]?.label}</div>
        <div><span className="text-slate-500 uppercase text-[10px]">Uploaded:</span> {new Date(row.uploaded_at).toLocaleString()}</div>
        {row.exif_taken_at && <div><span className="text-slate-500 uppercase text-[10px]">EXIF taken:</span> {new Date(row.exif_taken_at).toLocaleString()}</div>}
        <div><span className="text-slate-500 uppercase text-[10px]">Size:</span> {row.width}×{row.height} · {Math.round((row.file_size_bytes || 0) / 1024)} KB</div>
        {row.phash && <div><span className="text-slate-500 uppercase text-[10px]">pHash:</span> <span className="font-mono">{row.phash}</span></div>}
        {row.sha256 && <div className="col-span-2"><span className="text-slate-500 uppercase text-[10px]">SHA-256:</span> <span className="font-mono text-[10px]">{row.sha256}</span></div>}
      </div>

      {tags.length > 0 && (
        <div>
          <div className="text-xs font-medium text-slate-700 mb-1">Tagged to ({tags.length}):</div>
          <ul className="space-y-1 text-xs text-slate-600">
            {tags.map((t) => (
              <li key={t.id} className="flex items-center gap-2">
                <Check className="h-3 w-3 text-emerald-600" />
                <span className="font-mono text-[10px]">{(t.daily_report_id || "").slice(0, 8)}</span>
                <span>{t.photo_class}</span>
                {t.evidentiary_flag && <span className="text-amber-700">[evidentiary]</span>}
                {t.tag_caption && <span className="italic">"{t.tag_caption}"</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex justify-between gap-2 pt-3 border-t border-slate-100">
        <button data-testid="tray-detail-archive" onClick={onArchive}
          className="h-9 px-3 rounded bg-slate-200 text-slate-700 text-sm hover:bg-slate-300 inline-flex items-center gap-1">
          <Archive className="h-3.5 w-3.5" /> Archive
        </button>
        <button data-testid="tray-detail-tag" onClick={onTagClick}
          className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1">
          <Check className="h-3.5 w-3.5" /> Tag to event
        </button>
      </div>
    </div>
  );
}
