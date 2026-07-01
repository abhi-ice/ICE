// Photos page — gallery of every photo attached to this project's daily
// reports. pt.69 (2026-05-28): stripped the per-photo tagging / AI-class /
// caption / confidence UI. Photos now simply attach to a daily report on
// upload (no class/caption/event-tag step), so the page is a clean gallery
// with one filter — pick a daily report — plus the evidence-pack export.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api, { API } from "../lib/api";
import { useProjects } from "../lib/projects";
import { useUrlState } from "../lib/useUrlState";
import { navigateToTodaysDrForCreate } from "../lib/todaysDr";
import { Camera, AlertTriangle, FileImage, Upload, HelpCircle, Download, Loader2 } from "lucide-react";
import { toast } from "../components/ui/sonner";
import { getToken } from "../lib/auth";
import { formatDateDisplay } from "../lib/dateUtil";
import { AiHelpPanel } from "./SystemPages";
import PageHelp from "../components/PageHelp";

export default function Photos() {
  const { activeId, projects } = useProjects();
  const [photos, setPhotos] = useState([]);
  const [loading, setLoading] = useState(false);
  // pt.69: single filter — which daily report. Mirrored to the URL.
  const [filters, setFilters] = useUrlState({ dr: "" });
  const project = projects.find((p) => p.id === activeId);
  const [helpOpen, setHelpOpen] = useState(false);

  // DR id -> report_date, so the "Daily report" filter reads as dates.
  const [drDates, setDrDates] = useState({});
  useEffect(() => {
    if (!activeId) { setDrDates({}); return; }
    api.get(`/projects/${activeId}/daily-reports`)
      .then(({ data }) => {
        const m = {};
        (Array.isArray(data) ? data : []).forEach((d) => { m[d.id] = d.report_date; });
        setDrDates(m);
      })
      .catch(() => {});
  }, [activeId]);

  const drLabel = (id) => (id && drDates[id]) ? formatDateDisplay(drDates[id]) : (id ? id.slice(0, 8) : "—");

  const load = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      const r = await api.get(`/projects/${activeId}/photos`);
      setPhotos(Array.isArray(r.data) ? r.data : []);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load photos");
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [activeId]);

  // DRs that actually have photos, newest first — the filter options.
  const drOptions = useMemo(() => {
    const ids = [...new Set(photos.map((p) => p.daily_report_id).filter(Boolean))];
    return ids
      .map((id) => ({ id, label: drLabel(id), date: drDates[id] || "" }))
      .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [photos, drDates]);

  const displayed = filters.dr ? photos.filter((p) => p.daily_report_id === filters.dr) : photos;

  if (!activeId) return <div className="text-sm text-slate-500">Pick a project to see photos.</div>;

  return (
    <div data-testid="photos-page">
      <header className="mb-4 flex items-center justify-between max-md:flex-col max-md:items-start max-md:gap-2">
        <div>
          <div className="flex items-center gap-2">
            <Camera className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800 inline-flex items-baseline">
              Photos
              <PageHelp title="Photos gallery">
                Every photo here is attached to a daily report. Upload photos from a
                daily report (they attach automatically), or from the{" "}
                <Link to="/photo-tray" className="text-emerald-700 underline">Photo tray</Link>.
                Pin photos on a daily report to run an AI Safety / QC review.
              </PageHelp>
            </h1>
          </div>
          <p className="text-sm text-slate-500 page-desc">{project?.name} · gallery view across all daily reports</p>
        </div>
        <div className="flex max-md:flex-wrap items-center gap-2">
          <button data-testid="photos-ai-help"
            onClick={() => setHelpOpen(true)} title="How does Photo AI work?"
            className="sbtn sbtn-md sbtn-secondary">
            <HelpCircle /> AI help
          </button>
          <PhotoEvidenceExport projectId={activeId} />
          <PhotoNewButton projectId={activeId} />
          <Link to="/photo-tray"
            data-testid="photos-go-to-tray"
            className="sbtn sbtn-md sbtn-secondary">
            <Upload /> Photo tray
          </Link>
        </div>
      </header>

      {/* pt.69: one filter — pick a daily report. The old class / tagged-to /
          affected-activity / evidentiary filters + AI-usage stats card were
          removed along with per-photo tagging. */}
      <div className="bg-white border border-slate-200 rounded p-3 mb-4 flex items-end gap-3 flex-wrap">
        <div>
          <label className="text-xs text-slate-500">Daily report</label>
          <select data-testid="photos-filter-dr"
            value={filters.dr} onChange={(e) => setFilters({ ...filters, dr: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white min-w-[180px]">
            <option value="">All daily reports</option>
            {drOptions.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
        </div>
        <div className="text-xs text-slate-400 mb-2">{displayed.length} photo{displayed.length !== 1 ? "s" : ""}</div>
      </div>

      {loading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : !displayed.length ? (
        <div className="text-sm text-slate-500 border border-dashed border-slate-300 rounded p-8 text-center">
          No photos yet. Upload them from a daily report or the Photo tray.
        </div>
      ) : (
        <div className="grid grid-cols-3 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 gap-2">
          {displayed.map((p) => <PhotoCard key={p.id} photo={p} drLabel={drLabel} />)}
        </div>
      )}

      {helpOpen && <AiHelpPanel onClose={() => setHelpOpen(false)} />}
    </div>
  );
}

function PhotoCard({ photo, drLabel }) {
  return (
    <Link to={`/daily-reports?openId=${photo.daily_report_id}&openEvent=${photo.id}&openEventType=photo`}
      data-testid={`photos-card-${photo.id}`}
      className="block bg-white border border-slate-200 rounded overflow-hidden hover:border-blue-400 hover:shadow-sm transition">
      <div className="relative aspect-square bg-slate-100">
        <PhotoImage photoId={photo.id} />
        {photo.timestamp_mismatch_flag && (
          <span title="Device/server timestamp mismatch" className="absolute bottom-1 right-1 bg-amber-500 text-white rounded p-0.5">
            <AlertTriangle className="h-2.5 w-2.5" />
          </span>
        )}
      </div>
      <div className="p-2 space-y-1 text-xs">
        <div className="text-slate-500">{drLabel(photo.daily_report_id)}</div>
        {photo.is_supplement && (
          <div data-testid={`photo-gallery-supp-${photo.id}`}
            className="text-xs inline-block px-1.5 py-0.5 rounded bg-amber-200 text-amber-900 font-mono max-md:truncate max-md:max-w-full">
            SUPP→{photo.parent_daily_report_date || photo.parent_daily_report_id?.slice(0, 8) || "?"}
          </div>
        )}
      </div>
    </Link>
  );
}

// + New button that routes to today's DR with the Photos panel in view.
function PhotoNewButton({ projectId }) {
  const navigate = useNavigate();
  const [routing, setRouting] = useState(false);
  const click = async () => {
    if (routing || !projectId) return;
    setRouting(true);
    try {
      const { url, locked, dr } = await navigateToTodaysDrForCreate(projectId, "photo");
      if (locked) {
        toast.info(`Today's DR is locked. Upload photos via the Photo tray instead.`);
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
      {routing ? "Opening today's DR…" : "New on today's DR"}
    </button>
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

// Photo evidence pack export (PDF / DOCX / ZIP). Project-scoped.
function PhotoEvidenceExport({ projectId }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const downloadFmt = async (fmt) => {
    if (!projectId) { toast.error("Pick a project first"); return; }
    setBusy(true);
    setOpen(false);
    try {
      const res = await api.get(
        `/projects/${projectId}/photo-evidence-pack/export`,
        { params: { format: fmt }, responseType: "blob" },
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
