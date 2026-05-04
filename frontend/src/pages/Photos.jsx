// Photos page — upload via Emergent Object Storage + masonry grid + viewer.
import { useEffect, useState } from "react";
import api, { API } from "../lib/api";
import { useProjects } from "../lib/projects";
import { Camera, Upload, X } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";
import { getToken } from "../lib/auth";

export default function Photos() {
  const { activeId, projects } = useProjects();
  const [photos, setPhotos] = useState([]);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null);
  const [locations, setLocations] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [linkLoc, setLinkLoc] = useState("");
  const [linkAct, setLinkAct] = useState("");
  const [caption, setCaption] = useState("");
  const project = projects.find((p) => p.id === activeId);

  const load = () => activeId && api.get(`/projects/${activeId}/photos`).then(({ data }) => setPhotos(data));
  useEffect(() => {
    if (!activeId) return;
    load();
    api.get(`/projects/${activeId}/locations`).then(({ data }) => setLocations(data));
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data.filter((t) => t.status !== "deleted")));
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [activeId]);

  const upload = async (file) => {
    if (!file || !activeId) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("project_id", activeId);
      fd.append("file", file);
      if (linkLoc) fd.append("location_id", linkLoc);
      if (linkAct) fd.append("schedule_activity_id", linkAct);
      if (caption) fd.append("caption", caption);
      await api.post("/photos/upload", fd);
      toast.success("Photo uploaded");
      setCaption(""); setLinkLoc(""); setLinkAct("");
      load();
    } catch (e) {
      toast.error("Upload failed: " + (e.response?.data?.detail || e.message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="photos-page" className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Photos</h1>
        <p className="text-sm text-slate-500">{project?.name}</p>
      </div>

      <div className="surface p-4 grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="md:col-span-2">
          <label className="field-label">Capture or upload photo *</label>
          <input
            data-testid="photo-file"
            type="file"
            accept="image/*"
            capture="environment"
            disabled={busy}
            onChange={(e) => e.target.files[0] && upload(e.target.files[0])}
            className="block text-sm w-full h-9"
          />
          <div className="text-[11px] text-slate-400 mt-1">On mobile this opens the rear camera. Stored in Emergent Object Storage under <code>photos/&#123;tenant_id&#125;/</code>.</div>
        </div>
        <div>
          <label className="field-label">Caption (optional)</label>
          <input data-testid="photo-caption" value={caption} onChange={(e) => setCaption(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200 text-sm" />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="field-label">Location</label>
            <select data-testid="photo-location" value={linkLoc} onChange={(e) => setLinkLoc(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-xs">
              <option value="">—</option>
              {locations.map((l) => <option key={l.id} value={l.id}>{l.code} {l.name}</option>)}
            </select>
          </div>
          <div>
            <label className="field-label">Activity</label>
            <select data-testid="photo-activity" value={linkAct} onChange={(e) => setLinkAct(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-xs">
              <option value="">—</option>
              {tasks.map((t) => <option key={t.id} value={t.id}>{t.activity_code}</option>)}
            </select>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
        {photos.map((p) => (
          <button
            key={p.id}
            data-testid={`photo-tile-${p.id}`}
            onClick={() => setOpen(p)}
            className="surface aspect-square overflow-hidden relative group hover:ring-2 hover:ring-blue-500 transition"
          >
            <PhotoImage photoId={p.id} />
            <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-slate-900/80 to-transparent text-white text-[11px] p-2 truncate">
              {p.caption || new Date(p.captured_at).toLocaleDateString()}
            </div>
          </button>
        ))}
        {!photos.length && <div className="col-span-full text-sm text-slate-500 py-8 text-center">No photos yet. Capture one above.</div>}
      </div>

      {open && (
        <div data-testid="photo-modal" className="fixed inset-0 z-40 bg-slate-900/80 grid place-items-center p-4">
          <div className="bg-white rounded-md max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col">
            <div className="h-12 px-4 border-b border-slate-200 flex items-center gap-2">
              <Camera className="h-4 w-4 text-blue-600" />
              <div className="text-sm font-medium truncate">{open.caption || open.id.slice(0, 8)}</div>
              <button onClick={() => setOpen(null)} className="ml-auto h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><X className="h-4 w-4" /></button>
            </div>
            <div className="flex-1 grid place-items-center bg-slate-50 overflow-auto">
              <PhotoImage photoId={open.id} large />
            </div>
            <div className="px-4 py-3 border-t border-slate-200 text-xs text-slate-600 grid grid-cols-3 gap-3">
              <div><span className="text-slate-400">Captured:</span> {new Date(open.captured_at).toLocaleString()}</div>
              <div><span className="text-slate-400">Location:</span> {locations.find((l) => l.id === open.location_id)?.name || "—"}</div>
              <div><span className="text-slate-400">Activity:</span> {tasks.find((t) => t.id === open.schedule_activity_id)?.activity_code || "—"}</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function PhotoImage({ photoId, large = false }) {
  // We hit the streaming endpoint with the bearer token via fetch + blob URL,
  // because <img src> can't carry auth headers.
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let urlObj = null;
    (async () => {
      try {
        const res = await fetch(`${API}/photos/${photoId}/file`, {
          headers: { Authorization: `Bearer ${getToken()}` },
          credentials: "include",
        });
        if (res.ok) {
          urlObj = URL.createObjectURL(await res.blob());
          setUrl(urlObj);
        }
      } catch {}
    })();
    return () => { if (urlObj) URL.revokeObjectURL(urlObj); };
  }, [photoId]);
  if (!url) return <div className="w-full h-full bg-slate-200 animate-pulse" />;
  return <img src={url} alt="" className={large ? "max-h-[80vh] object-contain" : "w-full h-full object-cover"} />;
}
