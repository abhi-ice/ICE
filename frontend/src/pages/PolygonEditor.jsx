// Polygon Editor (Spatial Path 1)
// Pick a floor → pick a plan revision → click PDF to add vertices → save to
// location.polygon_geojson via PATCH /api/locations/:id/polygon.
import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { Stage, Layer, Line, Circle } from "react-konva";
import { Save, Trash2, Undo2, MapPin, ChevronLeft, ChevronRight, Wand2 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

export default function PolygonEditor() {
  const { activeId, projects } = useProjects();
  const [floors, setFloors] = useState([]);
  const [floorId, setFloorId] = useState("");
  const [locations, setLocations] = useState([]);
  const [drawings, setDrawings] = useState([]);
  const [revisions, setRevisions] = useState([]);
  const [revisionId, setRevisionId] = useState("");
  const [pdfUrl, setPdfUrl] = useState(null);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(1);
  const [size, setSize] = useState({ w: 800, h: 1000 });
  const [selectedLoc, setSelectedLoc] = useState("");
  const [points, setPoints] = useState([]); // [[x,y], ...]
  const project = projects.find((p) => p.id === activeId);

  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/floors`).then(({ data }) => setFloors(data));
    api.get(`/projects/${activeId}/drawings`).then(({ data }) => setDrawings(data));
  }, [activeId]);

  useEffect(() => {
    if (!floorId) { setLocations([]); return; }
    api.get(`/projects/${activeId}/locations?floor_id=${floorId}`).then(({ data }) => setLocations(data));
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [floorId]);

  // Drawings → fetch their current revisions
  useEffect(() => {
    if (!drawings.length) return;
    const ids = drawings.filter((d) => d.current_revision_id).map((d) => d.current_revision_id);
    if (!ids.length) { setRevisions([]); return; }
    Promise.all(ids.map((id) => api.get(`/drawing-revisions/${id}`).then((r) => ({ ...r.data, drawing_id: drawings.find((d) => d.current_revision_id === id)?.id })).catch(() => null)))
      .then((all) => setRevisions(all.filter(Boolean)));
  }, [drawings]);

  useEffect(() => {
    let urlObj = null;
    if (revisionId) {
      api.get(`/drawing-revisions/${revisionId}/pdf`, { responseType: "blob" }).then((res) => {
        urlObj = URL.createObjectURL(res.data);
        setPdfUrl(urlObj);
      });
    } else { setPdfUrl(null); }
    return () => { if (urlObj) URL.revokeObjectURL(urlObj); };
  }, [revisionId]);

  // When a location is picked, load its existing polygon (if any) into the editor
  useEffect(() => {
    if (!selectedLoc) { setPoints([]); return; }
    const loc = locations.find((l) => l.id === selectedLoc);
    const ring = loc?.polygon_geojson?.coordinates?.[0];
    if (ring && ring.length > 1) {
      // Drop the closing point (we close on save)
      setPoints(ring.slice(0, -1));
    } else {
      setPoints([]);
    }
  }, [selectedLoc, locations]);

  const onStageClick = (e) => {
    if (!selectedLoc) {
      toast.info("Pick a location first");
      return;
    }
    const pos = e.target.getStage().getPointerPosition();
    setPoints((p) => [...p, [Math.round(pos.x), Math.round(pos.y)]]);
  };

  const undo = () => setPoints((p) => p.slice(0, -1));
  const clear = () => setPoints([]);

  const save = async () => {
    if (!selectedLoc || points.length < 3) {
      toast.error("Need at least 3 vertices");
      return;
    }
    const ring = [...points, points[0]]; // close the ring
    try {
      await api.patch(`/locations/${selectedLoc}/polygon`, {
        polygon_geojson: { type: "Polygon", coordinates: [ring] },
      });
      toast.success("Polygon saved");
      // refresh locations
      api.get(`/projects/${activeId}/locations?floor_id=${floorId}`).then(({ data }) => setLocations(data));
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    }
  };

  const flatPoints = points.flatMap(([x, y]) => [x, y]);
  const isClosed = points.length >= 3;

  return (
    <div data-testid="polygon-editor-page" className="space-y-3">
      <div>
        <div className="flex items-center gap-2">
          <MapPin className="h-5 w-5 text-blue-600" />
          <h1 className="text-2xl font-semibold tracking-tight">Polygon Editor — Spatial Path 1</h1>
        </div>
        <p className="text-sm text-slate-500 page-desc">{project?.name} · click on the plan PDF to add vertices, then Save.</p>
      </div>

      <div className="surface p-3 grid grid-cols-1 md:grid-cols-4 gap-3">
        <div>
          <label className="field-label">Floor</label>
          <select data-testid="pe-floor" value={floorId} onChange={(e) => { setFloorId(e.target.value); setSelectedLoc(""); }} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
            <option value="">— pick floor —</option>
            {floors.map((f) => <option key={f.id} value={f.id}>Floor {f.floor_number} — {f.floor_name}</option>)}
          </select>
        </div>
        <div>
          <label className="field-label">Plan revision</label>
          <select data-testid="pe-revision" value={revisionId} onChange={(e) => setRevisionId(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
            <option value="">— pick a drawing revision —</option>
            {revisions.map((r) => {
              const dr = drawings.find((d) => d.id === r.drawing_id);
              return <option key={r.id} value={r.id}>{dr?.sheet_number} {dr?.title?.slice(0, 30)} ({r.revision_label})</option>;
            })}
          </select>
        </div>
        <div>
          <label className="field-label">Location to edit</label>
          <select data-testid="pe-location" value={selectedLoc} onChange={(e) => setSelectedLoc(e.target.value)} disabled={!floorId} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
            <option value="">— pick a room —</option>
            {locations.map((l) => <option key={l.id} value={l.id}>{l.code} {l.name}{l.polygon_geojson ? " ✓" : ""}</option>)}
          </select>
        </div>
        <div className="flex items-end gap-2">
          <Button data-testid="pe-undo" onClick={undo} variant="outline" disabled={!points.length} className="gap-1"><Undo2 className="h-4 w-4" /> Undo</Button>
          <Button data-testid="pe-clear" onClick={clear} variant="outline" disabled={!points.length} className="gap-1"><Trash2 className="h-4 w-4" /> Clear</Button>
          <Button data-testid="pe-suggest" onClick={async () => {
            if (!revisionId || !selectedLoc) { toast.error("Pick revision + location first"); return; }
            toast.info("Asking Claude vision to identify the room polygon…");
            try {
              const { data } = await api.post("/spatial/suggest-polygon", {
                drawing_revision_id: revisionId, page, location_id: selectedLoc,
              });
              if (data.points && data.points.length >= 3) {
                // Drop closing duplicate if Claude returned one
                const pts = data.points;
                const last = pts[pts.length - 1];
                const first = pts[0];
                const cleaned = (last[0] === first[0] && last[1] === first[1]) ? pts.slice(0, -1) : pts;
                setPoints(cleaned);
                toast.success(`Got ${cleaned.length} points — review & save`);
              } else { toast.error("Could not parse a polygon"); }
            } catch (e) { toast.error(e.response?.data?.detail || "Suggestion failed"); }
          }} variant="outline" disabled={!revisionId || !selectedLoc} className="gap-1">
            <Wand2 className="h-4 w-4" /> AI suggest 1
          </Button>
          <Button data-testid="pe-detect-all" onClick={async () => {
            if (!revisionId || !floorId) { toast.error("Pick floor + drawing first"); return; }
            toast.info("Claude is reading the plan to identify every room…");
            try {
              const { data } = await api.post("/spatial/auto-detect-rooms", {
                drawing_revision_id: revisionId, floor_id: floorId, page, create_locations: true,
              });
              toast.success(`Detected ${data.detected_count} rooms — created ${data.created_count} new locations`);
              api.get(`/projects/${activeId}/locations?floor_id=${floorId}`).then(({ data }) => setLocations(data));
            } catch (e) { toast.error(e.response?.data?.detail || "Auto-detect failed"); }
          }} disabled={!revisionId || !floorId} className="bg-violet-600 hover:bg-violet-700 gap-1">
            <Wand2 className="h-4 w-4" /> AI detect all rooms
          </Button>
          <Button data-testid="pe-save" onClick={save} disabled={!isClosed || !selectedLoc} className="bg-blue-600 hover:bg-blue-700 gap-1"><Save className="h-4 w-4" /> Save</Button>
        </div>
      </div>

      <div className="surface relative overflow-auto bg-slate-100 p-3 scrollbar-soft" style={{ minHeight: 600 }}>
        {pdfUrl ? (
          <div className="inline-block relative">
            <Document file={pdfUrl} onLoadSuccess={({ numPages }) => setPageCount(numPages)}>
              <Page
                pageNumber={page}
                onRenderSuccess={({ width, height }) => setSize({ w: width, h: height })}
                renderTextLayer={false}
                renderAnnotationLayer={false}
              />
            </Document>
            <div className="absolute inset-0">
              <Stage width={size.w} height={size.h} onClick={onStageClick} onTap={onStageClick} style={{ cursor: selectedLoc ? "crosshair" : "default" }}>
                <Layer>
                  {flatPoints.length >= 4 && (
                    <Line
                      points={isClosed ? [...flatPoints, flatPoints[0], flatPoints[1]] : flatPoints}
                      stroke="#2563eb"
                      strokeWidth={2}
                      closed={isClosed}
                      fill="rgba(37, 99, 235, 0.15)"
                    />
                  )}
                  {points.map(([x, y], i) => (
                    <Circle key={i} x={x} y={y} radius={5} fill="#1d4ed8" stroke="white" strokeWidth={2} />
                  ))}
                </Layer>
              </Stage>
            </div>
            {pageCount > 1 && (
              <div className="absolute bottom-3 left-1/2 -translate-x-1/2 bg-white rounded shadow border border-slate-200 px-2 py-1 flex items-center gap-2 text-xs">
                <button onClick={() => setPage((p) => Math.max(1, p - 1))} className="h-6 w-6 grid place-items-center rounded hover:bg-slate-100"><ChevronLeft className="h-3.5 w-3.5" /></button>
                <span>page {page} / {pageCount}</span>
                <button onClick={() => setPage((p) => Math.min(pageCount, p + 1))} className="h-6 w-6 grid place-items-center rounded hover:bg-slate-100"><ChevronRight className="h-3.5 w-3.5" /></button>
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-slate-500 p-12 text-center">
            Pick a floor and plan revision above to begin.<br />
            Need to upload a PDF? Visit <a href="/drawings" className="text-blue-600 hover:underline">Drawings</a> first.
          </div>
        )}
      </div>

      <div className="text-xs text-slate-500">
        Tip: vertex coords are stored in raw PDF pixel space. The 2.5D viewer uses the same coordinates to extrude rooms — so changes here flow through to <a href="/spatial-3d" className="text-blue-600 hover:underline">Spatial 3D</a>.
      </div>
    </div>
  );
}
