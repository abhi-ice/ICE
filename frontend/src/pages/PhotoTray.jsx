// Bucket D Phase 2 -- Photo tray page. Staging area for photos before
// they're tagged to specific DR events. Grid of tray photos with
// status filter; click a photo to open the tagging wizard.
import { useEffect, useState, useCallback } from "react";
import { Camera, Upload, Filter, Archive, Check, MapPin, Clock, FileImage } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import TagWizard from "../components/photos/TagWizard";
import { useUrlState } from "../lib/useUrlState";

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
        <label data-testid="photo-tray-upload" className="block">
          <span className="h-9 px-3 rounded bg-blue-600 text-white text-sm inline-flex items-center gap-1 hover:bg-blue-700 cursor-pointer">
            <Upload className="h-3.5 w-3.5" /> {uploading ? "Uploading…" : "Upload photo"}
          </span>
          <input type="file" accept="image/*" onChange={onUpload} className="hidden" />
        </label>
      </header>

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
    </div>
  );
}

function DetailContent({ row, onTagClick, onArchive }) {
  const [src, setSrc] = useState(null);
  const [tags, setTags] = useState([]);
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
      <div className="bg-slate-100 rounded overflow-hidden">
        {src ? <img src={src} alt="" className="w-full max-h-[400px] object-contain bg-black" />
          : <div className="h-64 grid place-items-center text-slate-400">Loading…</div>}
      </div>
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
