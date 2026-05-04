// DrawingViewer with PERSISTED markups + pin-to-entity polymorphic linking.
// react-pdf multi-page + Konva overlay. Markups POST to /drawing-markups
// on each shape; loaded via GET /drawing-revisions/:id/markups on mount.
import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { Stage, Layer, Line, Rect, Circle, Text } from "react-konva";
import { Pencil, Square, MapPin, Trash2, ChevronLeft, ChevronRight, ZoomIn, ZoomOut, Link2 } from "lucide-react";
import api from "../../lib/api";
import { useProjects } from "../../lib/projects";
import { toast } from "../ui/sonner";

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

const TOOLS = [
  { id: "pen", icon: Pencil, label: "Pen" },
  { id: "rect", icon: Square, label: "Rect" },
  { id: "pin", icon: MapPin, label: "Pin" },
];

export default function DrawingViewer({ revisionId, onClose }) {
  const { activeId } = useProjects();
  const [meta, setMeta] = useState(null);
  const [page, setPage] = useState(1);
  const [scale, setScale] = useState(1.0);
  const [pdfUrl, setPdfUrl] = useState(null);
  const [tool, setTool] = useState("pen");
  const [markups, setMarkups] = useState([]); // persisted
  const [pinDialog, setPinDialog] = useState(null); // {x,y}
  const [tasks, setTasks] = useState([]);
  const [rfis, setRfis] = useState([]);
  const [workItems, setWorkItems] = useState([]);
  const drawing = useRef(null);
  const drawingShape = useRef(null);
  const [size, setSize] = useState({ w: 800, h: 1000 });

  const reload = async () => {
    const { data } = await api.get(`/drawing-revisions/${revisionId}/markups`, { params: { page_number: page } });
    setMarkups(data);
  };

  useEffect(() => {
    let urlObj = null;
    (async () => {
      const { data } = await api.get(`/drawing-revisions/${revisionId}`);
      setMeta(data);
      const res = await api.get(`/drawing-revisions/${revisionId}/pdf`, { responseType: "blob" });
      urlObj = URL.createObjectURL(res.data);
      setPdfUrl(urlObj);
    })();
    return () => { if (urlObj) URL.revokeObjectURL(urlObj); };
  }, [revisionId]);

  useEffect(() => { reload(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [revisionId, page]);

  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data.filter((t) => t.status !== "deleted")));
    api.get(`/projects/${activeId}/rfis`).then(({ data }) => setRfis(data));
    api.get(`/projects/${activeId}/work-items`).then(({ data }) => setWorkItems(data));
  }, [activeId]);

  const persist = async (m) => {
    try {
      const { data } = await api.post("/drawing-markups", {
        drawing_revision_id: revisionId,
        page_number: page,
        markup_type: m.type === "rect" ? "rect" : m.type,
        geometry: m.geometry,
        style: m.style || {},
        text_content: m.text_content || null,
        pin_type: m.pin_type || null,
        linked_entity_type: m.linked_entity_type || null,
        linked_entity_id: m.linked_entity_id || null,
        visibility: "personal",
      });
      setMarkups((s) => [...s, data]);
    } catch (e) {
      toast.error("Save markup failed: " + (e.response?.data?.detail?.[0]?.msg || e.message));
    }
  };

  const remove = async (id) => {
    await api.delete(`/drawing-markups/${id}`);
    setMarkups((s) => s.filter((m) => m.id !== id));
  };

  const onMouseDown = (e) => {
    const pos = e.target.getStage().getPointerPosition();
    if (tool === "pen") {
      drawing.current = { type: "pen", points: [pos.x, pos.y] };
      drawingShape.current = { ...drawing.current, _temp: true };
      setMarkups((s) => [...s, drawingShape.current]);
    } else if (tool === "rect") {
      drawing.current = { type: "rect", x: pos.x, y: pos.y, w: 0, h: 0 };
      drawingShape.current = { ...drawing.current, _temp: true };
      setMarkups((s) => [...s, drawingShape.current]);
    } else if (tool === "pin") {
      setPinDialog({ x: pos.x, y: pos.y });
    }
  };

  const onMouseMove = (e) => {
    if (!drawing.current) return;
    const pos = e.target.getStage().getPointerPosition();
    if (drawing.current.type === "pen") {
      drawing.current.points = [...drawing.current.points, pos.x, pos.y];
    } else if (drawing.current.type === "rect") {
      drawing.current.w = pos.x - drawing.current.x;
      drawing.current.h = pos.y - drawing.current.y;
    }
    setMarkups((s) => s.map((m) => (m._temp ? { ...drawing.current, _temp: true } : m)));
  };

  const onMouseUp = async () => {
    if (!drawing.current) return;
    const finalShape = drawing.current;
    drawing.current = null;
    drawingShape.current = null;
    setMarkups((s) => s.filter((m) => !m._temp));
    const geometry = finalShape.type === "pen"
      ? { points: finalShape.points }
      : { x: finalShape.x, y: finalShape.y, w: finalShape.w, h: finalShape.h };
    await persist({ type: finalShape.type, geometry, style: { color: finalShape.type === "pen" ? "#dc2626" : "#1d4ed8" } });
  };

  const persistPin = async (label, linkType, linkId) => {
    if (!pinDialog) return;
    await persist({
      type: "pin",
      geometry: { x: pinDialog.x, y: pinDialog.y },
      text_content: label,
      pin_type: "field-issue",
      linked_entity_type: linkType,
      linked_entity_id: linkId,
      style: { color: "#f59e0b" },
    });
    setPinDialog(null);
  };

  return (
    <div data-testid="drawing-viewer" className="fixed inset-0 bg-slate-900/70 z-40 flex">
      <div className="m-auto bg-white rounded-md shadow-2xl flex flex-col w-[min(95vw,1200px)] h-[90vh]">
        <div className="h-12 border-b border-slate-200 flex items-center gap-2 px-3">
          <div className="text-sm font-medium">{meta?.revision_label || "Drawing"}</div>
          <div className="text-xs text-slate-500 ml-2">page {page} / {meta?.pdf_page_count || "?"}</div>
          <div className="ml-4 flex items-center gap-1">
            {TOOLS.map((t) => {
              const Ic = t.icon;
              return (
                <button key={t.id} onClick={() => setTool(t.id)} title={t.label}
                  data-testid={`drawing-tool-${t.id}`}
                  className={`h-7 w-7 grid place-items-center rounded ${tool === t.id ? "bg-blue-600 text-white" : "hover:bg-slate-100"}`}>
                  <Ic className="h-4 w-4" />
                </button>
              );
            })}
          </div>
          <div className="ml-2 text-[11px] text-slate-500">{markups.length} markup{markups.length !== 1 ? "s" : ""}</div>
          <div className="ml-auto flex items-center gap-1">
            <button onClick={() => setScale((s) => Math.max(0.5, s - 0.1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ZoomOut className="h-4 w-4" /></button>
            <div className="text-xs w-10 text-center">{Math.round(scale * 100)}%</div>
            <button onClick={() => setScale((s) => Math.min(3, s + 0.1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ZoomIn className="h-4 w-4" /></button>
            <button onClick={() => setPage((p) => Math.max(1, p - 1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ChevronLeft className="h-4 w-4" /></button>
            <button onClick={() => setPage((p) => Math.min(meta?.pdf_page_count || 1, p + 1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ChevronRight className="h-4 w-4" /></button>
            <button onClick={onClose} className="ml-2 h-7 px-3 rounded text-sm border border-slate-200">Close</button>
          </div>
        </div>
        <div className="flex-1 flex">
          <div className="flex-1 overflow-auto bg-slate-100 grid place-items-start justify-center p-4 scrollbar-soft">
            {pdfUrl ? (
              <div className="relative">
                <Document file={pdfUrl} onLoadError={(e) => console.warn("PDF load error", e)}>
                  <Page
                    pageNumber={page}
                    scale={scale}
                    onRenderSuccess={({ width, height }) => setSize({ w: width, h: height })}
                    renderTextLayer={false}
                    renderAnnotationLayer={false}
                  />
                </Document>
                <div className="absolute inset-0">
                  <Stage
                    width={size.w} height={size.h}
                    onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp}
                    onTouchStart={onMouseDown} onTouchMove={onMouseMove} onTouchEnd={onMouseUp}
                    style={{ pointerEvents: "auto" }}
                  >
                    <Layer>
                      {markups.map((m, i) => {
                        const t = m.markup_type || m.type;
                        const g = m.geometry || m;
                        const stroke = m.style?.color || "#dc2626";
                        if (t === "pen") return <Line key={m.id || i} points={g.points} stroke={stroke} strokeWidth={2} lineCap="round" lineJoin="round" tension={0.3} />;
                        if (t === "rect") return <Rect key={m.id || i} x={g.x} y={g.y} width={g.w} height={g.h} stroke={stroke} strokeWidth={2} dash={[8, 4]} />;
                        if (t === "pin") return (
                          <>
                            <Circle key={`c-${m.id || i}`} x={g.x} y={g.y} radius={8} fill={m.style?.color || "#f59e0b"} stroke="#92400e" />
                            <Text key={`t-${m.id || i}`} x={g.x + 12} y={g.y - 6} text={m.text_content || ""} fontSize={12} fill="#1f2937" />
                          </>
                        );
                        return null;
                      })}
                    </Layer>
                  </Stage>
                </div>
              </div>
            ) : (
              <div className="text-sm text-slate-500 mt-12">Loading PDF…</div>
            )}
          </div>

          {/* Markup list panel */}
          <div className="w-72 border-l border-slate-200 overflow-y-auto p-3 space-y-2 scrollbar-soft">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-1">Page markups</div>
            {markups.filter((m) => !m._temp).length === 0 && <div className="text-xs text-slate-400">None on this page yet.</div>}
            {markups.filter((m) => !m._temp).map((m) => (
              <div key={m.id} data-testid={`markup-${m.id}`} className="border border-slate-200 rounded p-2 text-xs">
                <div className="flex items-center gap-1">
                  <span className="pill pill-slate capitalize">{m.markup_type}</span>
                  {m.linked_entity_type && (
                    <span className="pill pill-blue inline-flex items-center gap-1">
                      <Link2 className="h-3 w-3" /> {m.linked_entity_type}
                    </span>
                  )}
                  <button onClick={() => remove(m.id)} className="ml-auto text-red-600 hover:bg-red-50 px-1 rounded"><Trash2 className="h-3 w-3" /></button>
                </div>
                {m.text_content && <div className="mt-1 text-slate-700">{m.text_content}</div>}
              </div>
            ))}
          </div>
        </div>
      </div>

      {pinDialog && (
        <PinDialog
          tasks={tasks} rfis={rfis} workItems={workItems}
          onCancel={() => setPinDialog(null)}
          onSave={persistPin}
        />
      )}
    </div>
  );
}

function PinDialog({ tasks, rfis, workItems, onCancel, onSave }) {
  const [label, setLabel] = useState("");
  const [linkType, setLinkType] = useState("");
  const [linkId, setLinkId] = useState("");

  const options = linkType === "task" ? tasks.map((t) => ({ id: t.id, label: `[${t.activity_code}] ${t.description.slice(0, 60)}` }))
    : linkType === "rfi" ? rfis.map((r) => ({ id: r.id, label: `${r.rfi_number} - ${r.subject.slice(0, 60)}` }))
    : linkType === "work_item" ? workItems.map((w) => ({ id: w.id, label: w.title.slice(0, 80) }))
    : [];

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/60 grid place-items-center" data-testid="pin-dialog">
      <div className="bg-white rounded-md p-5 w-[440px]">
        <div className="font-medium mb-3">New pin</div>
        <div className="space-y-3">
          <div>
            <label className="field-label">Pin label</label>
            <input data-testid="pin-label" value={label} onChange={(e) => setLabel(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="e.g. concealed water damage" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="field-label">Link to (optional)</label>
              <select data-testid="pin-link-type" value={linkType} onChange={(e) => { setLinkType(e.target.value); setLinkId(""); }} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                <option value="">(none)</option>
                <option value="task">Activity</option>
                <option value="rfi">RFI</option>
                <option value="work_item">Field issue</option>
              </select>
            </div>
            <div>
              <label className="field-label">Pick</label>
              <select data-testid="pin-link-id" value={linkId} onChange={(e) => setLinkId(e.target.value)} disabled={!linkType} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                <option value="">—</option>
                {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onCancel} className="h-9 px-3 rounded border border-slate-200 text-sm">Cancel</button>
          <button data-testid="pin-save" onClick={() => onSave(label, linkType || null, linkId || null)} disabled={!label} className="h-9 px-3 rounded bg-blue-600 text-white text-sm disabled:opacity-50">Save pin</button>
        </div>
      </div>
    </div>
  );
}
