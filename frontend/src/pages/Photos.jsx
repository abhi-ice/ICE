// Bucket D Phase 3 -- Photos page reworked as a gallery aggregation
// across all DRs in the project. Each card is a `photos` row (the
// tagging junction); filters cover date, class, evidentiary flag,
// tagged-to event type, affected activity. Click a card -> opens the
// parent DR with the relevant event highlighted.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import api, { API } from "../lib/api";
import { useProjects } from "../lib/projects";
import { Camera, Filter, Lock, AlertTriangle, FileImage, Plus, Upload } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";
import { getToken } from "../lib/auth";
import GalleryActivityChips from "../components/photos/GalleryActivityChips";
import { localTodayISO } from "../lib/dateUtil";

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
  const [filters, setFilters] = useState({
    photo_class: "", evidentiary_only: false, tagged_to: "",
    affected_activity_id: "",
  });
  const project = projects.find((p) => p.id === activeId);

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

  if (!activeId) return <div className="p-6 text-sm text-slate-500">Pick a project to see photos.</div>;

  return (
    <div className="p-6 max-w-7xl mx-auto" data-testid="photos-page">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Camera className="h-5 w-5 text-slate-700" />
            <h1 className="text-xl font-semibold text-slate-800">Photos</h1>
          </div>
          <p className="text-sm text-slate-500">{project?.name} · gallery view across all daily reports</p>
        </div>
        <Link to="/photo-tray"
          data-testid="photos-go-to-tray"
          className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 inline-flex items-center gap-1">
          <Upload className="h-3.5 w-3.5" /> Upload + tag (Photo tray)
        </Link>
      </header>

      <div className="bg-amber-50 border border-amber-200 rounded p-3 text-xs text-amber-900 mb-4">
        <strong>Gallery view.</strong> Each card is a tag connecting a photo to one event on
        a specific daily report. The same image may appear multiple times if tagged to
        multiple events. Upload + tag from the <Link to="/photo-tray" className="underline">Photo tray</Link>.
      </div>

      <div className="bg-white border border-slate-200 rounded p-3 mb-4 flex items-end gap-3 flex-wrap">
        <Filter className="h-4 w-4 text-slate-500 mb-2" />
        <div>
          <label className="text-[10px] text-slate-500 uppercase">Photo class</label>
          <select data-testid="photos-filter-class"
            value={filters.photo_class} onChange={(e) => setFilters({ ...filters, photo_class: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white">
            <option value="">(any)</option>
            {PHOTO_CLASSES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div>
          <label className="text-[10px] text-slate-500 uppercase">Tagged to</label>
          <select data-testid="photos-filter-tagged-to"
            value={filters.tagged_to} onChange={(e) => setFilters({ ...filters, tagged_to: e.target.value })}
            className="block h-8 px-2 rounded border border-slate-300 text-sm bg-white">
            {TAGGED_TO_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <div>
          <label className="text-[10px] text-slate-500 uppercase">Affected activity id</label>
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
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3">
          {photos.map((p) => <PhotoCard key={p.id} photo={p} projectId={activeId} />)}
        </div>
      )}
    </div>
  );
}

function PhotoCard({ photo, projectId }) {
  const tone = PHOTO_CLASS_TONES[photo.photo_class] || PHOTO_CLASS_TONES.progress;
  const { type: eventType } = eventLabelForRow(photo);
  return (
    <Link to={`/daily-reports?openId=${photo.daily_report_id}`}
      data-testid={`photos-card-${photo.id}`}
      className="block bg-white border border-slate-200 rounded overflow-hidden hover:border-blue-400 hover:shadow-sm transition">
      <div className="relative aspect-square bg-slate-100">
        <PhotoImage photoId={photo.id} />
        <span className={`absolute top-1 left-1 text-[9px] px-1.5 py-0.5 rounded border ${tone}`}>
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
      </div>
      <div className="p-2 space-y-1 text-xs">
        <div className="flex items-center justify-between">
          <span className="text-slate-500">{photo.daily_report_id ? (photo.daily_report_id.slice(0, 8)) : "no DR"}</span>
          <span className="text-[10px] text-slate-400 uppercase">{eventType}</span>
        </div>
        {photo.tag_caption && <div className="text-slate-700 truncate" title={photo.tag_caption}>"{photo.tag_caption}"</div>}
        {photo.daily_report_activity_id && (
          <GalleryActivityChips projectId={projectId} activityIds={[photo.daily_report_activity_id]} max={1} />
        )}
        {photo.is_supplement && (
          <div data-testid={`photo-gallery-supp-${photo.id}`}
            className="text-[9px] inline-block px-1.5 py-0.5 rounded bg-amber-200 text-amber-900 font-mono uppercase">
            SUPP→{photo.parent_daily_report_date || photo.parent_daily_report_id?.slice(0, 8) || "?"}
          </div>
        )}
      </div>
    </Link>
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
