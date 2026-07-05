// DrawingViewer with PERSISTED markups + pin-to-entity polymorphic linking.
// react-pdf multi-page + Konva overlay. Markups POST to /drawing-markups
// on each shape; loaded via GET /drawing-revisions/:id/markups on mount.
// Tools: pen, rect, ellipse, arrow, line, cloud, text, pin.
import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { Stage, Layer, Line, Rect, Circle, Ellipse, Arrow, Text as KText, Path } from "react-konva";
import { Pencil, Square, Circle as CircleIcon, ArrowUpRight, Minus, MapPin, Trash2, ChevronLeft, ChevronRight, ZoomIn, ZoomOut, Link2, Type as TypeIcon, Cloud as CloudIcon, Ruler, ScanText } from "lucide-react";
import api from "../../lib/api";
import { useProjects } from "../../lib/projects";
import { toast } from "../ui/sonner";
import EntitySidePanel from "./EntitySidePanel";

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

const TOOLS = [
  { id: "pen", icon: Pencil, label: "Pen" },
  { id: "rect", icon: Square, label: "Rect" },
  { id: "ellipse", icon: CircleIcon, label: "Ellipse" },
  { id: "arrow", icon: ArrowUpRight, label: "Arrow" },
  { id: "line", icon: Minus, label: "Line" },
  { id: "cloud", icon: CloudIcon, label: "Cloud" },
  { id: "text", icon: TypeIcon, label: "Text" },
  { id: "pin", icon: MapPin, label: "Pin" },
  { id: "calibrate", icon: Ruler, label: "Calibrate" },
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
  const [textDialog, setTextDialog] = useState(null); // {x,y}
  const [calibDialog, setCalibDialog] = useState(null); // {x1,y1,x2,y2}
  const [openEntity, setOpenEntity] = useState(null); // {entity_type, entity_id}
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
    if (tool === "pen" || tool === "cloud") {
      drawing.current = { type: tool, points: [pos.x, pos.y] };
      drawingShape.current = { ...drawing.current, _temp: true };
      setMarkups((s) => [...s, drawingShape.current]);
    } else if (tool === "rect" || tool === "ellipse") {
      drawing.current = { type: tool, x: pos.x, y: pos.y, w: 0, h: 0 };
      drawingShape.current = { ...drawing.current, _temp: true };
      setMarkups((s) => [...s, drawingShape.current]);
    } else if (tool === "line" || tool === "arrow") {
      drawing.current = { type: tool, points: [pos.x, pos.y, pos.x, pos.y] };
      drawingShape.current = { ...drawing.current, _temp: true };
      setMarkups((s) => [...s, drawingShape.current]);
    } else if (tool === "calibrate") {
      // First click sets x1,y1; second click opens the calibration dialog
      if (!drawing.current?.calibStart) {
        drawing.current = { type: "calibrate", calibStart: { x: pos.x, y: pos.y } };
      } else {
        const x1 = drawing.current.calibStart.x;
        const y1 = drawing.current.calibStart.y;
        setCalibDialog({ x1, y1, x2: pos.x, y2: pos.y });
        drawing.current = null;
      }
    } else if (tool === "pin") {
      setPinDialog({ x: pos.x, y: pos.y });
    } else if (tool === "text") {
      setTextDialog({ x: pos.x, y: pos.y });
    }
  };

  const onMouseMove = (e) => {
    if (!drawing.current) return;
    const pos = e.target.getStage().getPointerPosition();
    if (drawing.current.type === "pen" || drawing.current.type === "cloud") {
      drawing.current.points = [...drawing.current.points, pos.x, pos.y];
    } else if (drawing.current.type === "rect" || drawing.current.type === "ellipse") {
      drawing.current.w = pos.x - drawing.current.x;
      drawing.current.h = pos.y - drawing.current.y;
    } else if (drawing.current.type === "line" || drawing.current.type === "arrow") {
      drawing.current.points = [drawing.current.points[0], drawing.current.points[1], pos.x, pos.y];
    } else if (drawing.current.type === "calibrate") {
      return; // calibrate mode doesn't drag
    }
    setMarkups((s) => s.map((m) => (m._temp ? { ...drawing.current, _temp: true } : m)));
  };

  const onMouseUp = async () => {
    if (!drawing.current) return;
    if (drawing.current.type === "calibrate") return;
    const finalShape = drawing.current;
    drawing.current = null;
    drawingShape.current = null;
    setMarkups((s) => s.filter((m) => !m._temp));
    let geometry, color;
    if (finalShape.type === "pen") { geometry = { points: finalShape.points }; color = "#dc2626"; }
    else if (finalShape.type === "cloud") { geometry = { points: finalShape.points }; color = "#9333ea"; }
    else if (finalShape.type === "rect") { geometry = { x: finalShape.x, y: finalShape.y, w: finalShape.w, h: finalShape.h }; color = "#1d4ed8"; }
    else if (finalShape.type === "ellipse") { geometry = { x: finalShape.x, y: finalShape.y, w: finalShape.w, h: finalShape.h }; color = "#0891b2"; }
    else if (finalShape.type === "line") { geometry = { points: finalShape.points }; color = "#0f766e"; }
    else if (finalShape.type === "arrow") { geometry = { points: finalShape.points }; color = "#b45309"; }
    else { return; }
    await persist({ type: finalShape.type, geometry, style: { color } });
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
          <div className="ml-2 text-xs text-slate-500">{markups.length} markup{markups.length !== 1 ? "s" : ""}</div>
          <button
            data-testid="drawing-ocr-btn"
            title="OCR title block"
            onClick={async () => {
              toast.info("OCR running on title block…");
              try {
                const { data } = await api.post("/drawings/ocr", { drawing_revision_id: revisionId, page });
                const summary = [
                  data.sheet_number && `sheet=${data.sheet_number}`,
                  data.title && `title=${data.title}`,
                  data.scale && `scale=${data.scale}`,
                  data.date && `date=${data.date}`,
                ].filter(Boolean).join(" · ");
                toast.success(summary || "OCR complete (no fields recognised)");
              } catch (e) { toast.error("OCR failed: " + (e.response?.data?.detail || e.message)); }
            }}
            className="ml-2 h-7 px-2 rounded text-xs border border-slate-200 inline-flex items-center gap-1 hover:bg-slate-100"
          >
            <ScanText className="h-3.5 w-3.5" /> OCR
          </button>
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
                        if (t === "cloud") return <Line key={m.id || i} points={g.points} stroke={stroke} strokeWidth={2} lineCap="round" lineJoin="round" tension={0.9} />;
                        if (t === "rect") return <Rect key={m.id || i} x={g.x} y={g.y} width={g.w} height={g.h} stroke={stroke} strokeWidth={2} dash={[8, 4]} />;
                        if (t === "ellipse") return <Ellipse key={m.id || i} x={g.x + g.w / 2} y={g.y + g.h / 2} radiusX={Math.abs(g.w / 2)} radiusY={Math.abs(g.h / 2)} stroke={stroke} strokeWidth={2} />;
                        if (t === "line") return <Line key={m.id || i} points={g.points} stroke={stroke} strokeWidth={2} />;
                        if (t === "arrow") return <Arrow key={m.id || i} points={g.points} stroke={stroke} fill={stroke} strokeWidth={2} pointerLength={10} pointerWidth={10} />;
                        if (t === "text") return (
                          <KText key={m.id || i} x={g.x} y={g.y} text={m.text_content || ""} fontSize={14} fill={stroke} />
                        );
                        if (t === "pin") return (
                          <>
                            <Circle
                              key={`c-${m.id || i}`}
                              x={g.x}
                              y={g.y}
                              radius={9}
                              fill={m.style?.color || "#f59e0b"}
                              stroke="#92400e"
                              strokeWidth={1.5}
                              onClick={() => m.linked_entity_type && m.linked_entity_id && setOpenEntity({ entity_type: m.linked_entity_type, entity_id: m.linked_entity_id })}
                              onTap={() => m.linked_entity_type && m.linked_entity_id && setOpenEntity({ entity_type: m.linked_entity_type, entity_id: m.linked_entity_id })}
                              onMouseEnter={(e) => { e.target.getStage().container().style.cursor = m.linked_entity_id ? "pointer" : "default"; }}
                              onMouseLeave={(e) => { e.target.getStage().container().style.cursor = "default"; }}
                            />
                            <KText key={`t-${m.id || i}`} x={g.x + 12} y={g.y - 6} text={m.text_content || ""} fontSize={12} fill="#1f2937" />
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
            <div className="text-xs font-semibold text-slate-500 mb-1">Page markups</div>
            {markups.filter((m) => !m._temp).length === 0 && <div className="text-xs text-slate-400">None on this page yet.</div>}
            {markups.filter((m) => !m._temp).map((m) => (
              <div key={m.id} data-testid={`markup-${m.id}`} className="border border-slate-200 rounded p-2 text-xs">
                <div className="flex items-center gap-1">
                  <span className="pill pill-slate capitalize">{m.markup_type}</span>
                  {m.linked_entity_type && (
                    <button
                      data-testid={`open-linked-${m.id}`}
                      onClick={() => setOpenEntity({ entity_type: m.linked_entity_type, entity_id: m.linked_entity_id })}
                      className="pill pill-blue inline-flex items-center gap-1 hover:underline"
                    >
                      <Link2 className="h-3 w-3" /> {m.linked_entity_type}
                    </button>
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

      {textDialog && (
        <TextDialog
          onCancel={() => setTextDialog(null)}
          onSave={async (txt) => {
            await persist({
              type: "text",
              geometry: { x: textDialog.x, y: textDialog.y },
              text_content: txt,
              style: { color: "#0f172a" },
            });
            setTextDialog(null);
          }}
        />
      )}

      {calibDialog && (
        <CalibrateDialog
          revisionId={revisionId}
          page={page}
          x1={calibDialog.x1} y1={calibDialog.y1} x2={calibDialog.x2} y2={calibDialog.y2}
          onClose={() => setCalibDialog(null)}
        />
      )}

      {openEntity && (
        <EntitySidePanel
          data-testid="entity-side-panel"
          entityType={openEntity.entity_type}
          entityId={openEntity.entity_id}
          onClose={() => setOpenEntity(null)}
        />
      )}
    </div>
  );
}

function TextDialog({ onCancel, onSave }) {
  const [txt, setTxt] = useState("");
  return (
    <div className="fixed inset-0 z-50 bg-slate-900/60 grid place-items-center" data-testid="text-dialog">
      <div className="bg-white rounded-md p-5 w-[420px]">
        <div className="font-medium mb-3">Text annotation</div>
        <input data-testid="text-input" autoFocus value={txt} onChange={(e) => setTxt(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" placeholder="e.g. RFI 14 zone" />
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onCancel} className="h-9 px-3 rounded border border-slate-200 text-sm">Cancel</button>
          <button data-testid="text-save" onClick={() => onSave(txt)} disabled={!txt.trim()} className="h-9 px-3 rounded bg-blue-600 text-white text-sm disabled:opacity-50">Save</button>
        </div>
      </div>
    </div>
  );
}

function CalibrateDialog({ revisionId, page, x1, y1, x2, y2, onClose }) {
  const [feet, setFeet] = useState("");
  const [inches, setInches] = useState("");
  const [busy, setBusy] = useState(false);
  const px = Math.hypot(x2 - x1, y2 - y1);

  const save = async () => {
    const totalFt = (parseFloat(feet) || 0) + (parseFloat(inches) || 0) / 12;
    if (!totalFt || !px) return;
    const ftPerPx = totalFt / px;
    setBusy(true);
    try {
      await api.post("/drawings-spatial-metadata", {
        drawing_revision_id: revisionId,
        page_number: page,
        drawing_type: "plan",
        scale_ft_per_px: ftPerPx,
        calibrated: true,
      });
      toast.success(`Calibrated · ${ftPerPx.toFixed(4)} ft/px`);
      onClose();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Calibration failed");
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/60 grid place-items-center" data-testid="calibrate-dialog">
      <div className="bg-white rounded-md p-5 w-[460px]">
        <div className="font-medium mb-2">Calibrate plan scale</div>
        <p className="text-xs text-slate-500 mb-3">
          You picked a 2-point segment of length <b>{px.toFixed(1)}px</b>. Enter the real-world distance.
        </p>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="field-label">Feet</label>
            <input type="number" value={feet} onChange={(e) => setFeet(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" />
          </div>
          <div>
            <label className="field-label">Inches</label>
            <input type="number" value={inches} onChange={(e) => setInches(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" />
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="h-9 px-3 rounded border border-slate-200 text-sm">Cancel</button>
          <button data-testid="calibrate-save" onClick={save} disabled={(!feet && !inches) || busy} className="h-9 px-3 rounded bg-blue-600 text-white text-sm disabled:opacity-50">
            {busy ? "Saving…" : "Save calibration"}
          </button>
        </div>
      </div>
    </div>
  );
}

function PinDialog({ tasks, rfis, workItems, onCancel, onSave }) {
  const [label, setLabel] = useState("");
  const [linkType, setLinkType] = useState("");
  const [linkId, setLinkId] = useState("");

  const options = linkType === "task" ? tasks.map((t) => ({ id: t.id, label: t.description.slice(0, 60) }))
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
                <option value="">None</option>
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
