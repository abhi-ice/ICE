// DrawingViewer — react-pdf multi-page viewer with a Konva markup overlay.
// Tools: pen, rectangle, pin. Markups are NOT persisted in this iteration —
// they're an in-memory canvas to demonstrate the UX. Real persistence
// requires the drawing_markups table router (next iteration).
import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { Stage, Layer, Line, Rect, Circle, Text } from "react-konva";
import { Pencil, Square, MapPin, Trash2, ChevronLeft, ChevronRight, ZoomIn, ZoomOut } from "lucide-react";
import api, { API } from "../../lib/api";
import { getToken } from "../../lib/auth";

// Use a CDN worker that matches the bundled pdfjs version.
pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

const TOOLS = [
  { id: "pen", icon: Pencil, label: "Pen" },
  { id: "rect", icon: Square, label: "Rect" },
  { id: "pin", icon: MapPin, label: "Pin" },
];

export default function DrawingViewer({ revisionId, onClose }) {
  const [meta, setMeta] = useState(null);
  const [page, setPage] = useState(1);
  const [scale, setScale] = useState(1.0);
  const [pdfUrl, setPdfUrl] = useState(null);
  const [tool, setTool] = useState("pen");
  const [shapes, setShapes] = useState([]); // {type,...}
  const drawing = useRef(null);
  const containerRef = useRef(null);
  const [size, setSize] = useState({ w: 800, h: 1000 });

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

  const onMouseDown = (e) => {
    const pos = e.target.getStage().getPointerPosition();
    if (tool === "pen") {
      drawing.current = { type: "pen", points: [pos.x, pos.y] };
    } else if (tool === "rect") {
      drawing.current = { type: "rect", x: pos.x, y: pos.y, w: 0, h: 0 };
    } else if (tool === "pin") {
      const note = window.prompt("Pin label:") || "";
      if (note !== null) setShapes((s) => [...s, { type: "pin", x: pos.x, y: pos.y, label: note }]);
      return;
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
    setShapes((s) => [...s.slice(0, s.length), { ...drawing.current }]);
  };
  const onMouseUp = () => {
    if (drawing.current) {
      setShapes((s) => [...s, { ...drawing.current }]);
      drawing.current = null;
    }
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
            <button title="Clear" onClick={() => setShapes([])} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><Trash2 className="h-4 w-4" /></button>
          </div>
          <div className="ml-auto flex items-center gap-1">
            <button onClick={() => setScale((s) => Math.max(0.5, s - 0.1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ZoomOut className="h-4 w-4" /></button>
            <div className="text-xs w-10 text-center">{Math.round(scale * 100)}%</div>
            <button onClick={() => setScale((s) => Math.min(3, s + 0.1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ZoomIn className="h-4 w-4" /></button>
            <button onClick={() => setPage((p) => Math.max(1, p - 1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ChevronLeft className="h-4 w-4" /></button>
            <button onClick={() => setPage((p) => Math.min(meta?.pdf_page_count || 1, p + 1))} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100"><ChevronRight className="h-4 w-4" /></button>
            <button onClick={onClose} className="ml-2 h-7 px-3 rounded text-sm border border-slate-200">Close</button>
          </div>
        </div>
        <div ref={containerRef} className="flex-1 overflow-auto bg-slate-100 grid place-items-start justify-center p-4 scrollbar-soft">
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
                    {shapes.map((s, i) => {
                      if (s.type === "pen") return <Line key={i} points={s.points} stroke="#dc2626" strokeWidth={2} lineCap="round" lineJoin="round" tension={0.3} />;
                      if (s.type === "rect") return <Rect key={i} x={s.x} y={s.y} width={s.w} height={s.h} stroke="#1d4ed8" strokeWidth={2} dash={[8, 4]} />;
                      if (s.type === "pin") return (
                        <>
                          <Circle key={`c-${i}`} x={s.x} y={s.y} radius={8} fill="#f59e0b" stroke="#92400e" />
                          <Text key={`t-${i}`} x={s.x + 12} y={s.y - 6} text={s.label} fontSize={12} fill="#1f2937" />
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
      </div>
    </div>
  );
}
