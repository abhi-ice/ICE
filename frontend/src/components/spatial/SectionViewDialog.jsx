// Section view dialog — pick 2 points on plan-view, fetch section payload
// from /api/spatial/section, render vertical slice as SVG.
// Master prompt §22 (spatial path 2 — section views).
import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { Stage, Layer, Line, Circle } from "react-konva";
import { X, Scissors } from "lucide-react";
import api from "../../lib/api";

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

export default function SectionViewDialog({ projectId, planRevisionId, onClose }) {
  const [pdfUrl, setPdfUrl] = useState(null);
  const [pts, setPts] = useState([]);
  const [size, setSize] = useState({ w: 800, h: 1000 });
  const [section, setSection] = useState(null);

  useEffect(() => {
    let urlObj = null;
    if (planRevisionId) {
      api.get(`/drawing-revisions/${planRevisionId}/pdf`, { responseType: "blob" }).then((res) => {
        urlObj = URL.createObjectURL(res.data);
        setPdfUrl(urlObj);
      });
    }
    return () => { if (urlObj) URL.revokeObjectURL(urlObj); };
  }, [planRevisionId]);

  const onClick = (e) => {
    const pos = e.target.getStage().getPointerPosition();
    setPts((p) => p.length >= 2 ? [[pos.x, pos.y]] : [...p, [pos.x, pos.y]]);
    setSection(null);
  };

  const compute = async () => {
    if (pts.length !== 2) return;
    const { data } = await api.post("/spatial/section", {
      project_id: projectId,
      cut_line: pts,
    });
    setSection(data);
  };

  return (
    <div data-testid="section-dialog" className="fixed inset-0 z-50 bg-slate-900/70 grid place-items-center">
      <div className="bg-white rounded-md w-[min(95vw,1400px)] h-[88vh] flex flex-col">
        <div className="h-12 border-b border-slate-200 flex items-center gap-2 px-3">
          <Scissors className="h-4 w-4 text-blue-600" />
          <div className="font-medium">Section view</div>
          <div className="text-xs text-slate-500 ml-2">
            {pts.length === 0 && "Click 2 points on the plan to define a cut-line"}
            {pts.length === 1 && "Pick the second point…"}
            {pts.length === 2 && !section && "Ready — press Compute"}
            {section && `${section.floors.length} floors sliced`}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button data-testid="section-clear" onClick={() => { setPts([]); setSection(null); }} className="h-8 px-3 rounded border border-slate-200 text-xs">Clear</button>
            <button data-testid="section-compute" onClick={compute} disabled={pts.length !== 2} className="h-8 px-3 rounded bg-blue-600 text-white text-xs disabled:opacity-50">Compute slice</button>
            <button onClick={onClose} className="h-8 w-8 grid place-items-center rounded hover:bg-slate-100"><X className="h-4 w-4" /></button>
          </div>
        </div>
        <div className="flex-1 grid grid-cols-2 overflow-hidden">
          <div className="overflow-auto bg-slate-100 grid place-items-start justify-center p-4 scrollbar-soft">
            {pdfUrl ? (
              <div className="relative">
                <Document file={pdfUrl}>
                  <Page pageNumber={1} onRenderSuccess={({ width, height }) => setSize({ w: width, h: height })} renderTextLayer={false} renderAnnotationLayer={false} />
                </Document>
                <div className="absolute inset-0">
                  <Stage width={size.w} height={size.h} onClick={onClick}>
                    <Layer>
                      {pts.length === 2 && (
                        <Line points={[pts[0][0], pts[0][1], pts[1][0], pts[1][1]]} stroke="#dc2626" strokeWidth={2} dash={[6, 4]} />
                      )}
                      {pts.map(([x, y], i) => (
                        <Circle key={i} x={x} y={y} radius={5} fill="#dc2626" stroke="white" strokeWidth={1.5} />
                      ))}
                    </Layer>
                  </Stage>
                </div>
              </div>
            ) : (
              <div className="text-sm text-slate-500 mt-12">Loading plan PDF…</div>
            )}
          </div>
          <div className="border-l border-slate-200 p-4 overflow-y-auto bg-slate-50 scrollbar-soft">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">Vertical slice</div>
            {section ? <SectionSvg section={section} /> : (
              <div className="text-sm text-slate-500">No slice computed yet.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function SectionSvg({ section }) {
  const PADDING = 20;
  const W = 600;
  // Cut line length in plan coords
  const [[x1, z1], [x2, z2]] = section.cut_line;
  const lineLen = Math.hypot(x2 - x1, z2 - z1);
  const totalH = section.floors.reduce((sum, f) => Math.max(sum, (f.elevation_z_ft || 0) + (f.floor_height_ft || 12)), 0);
  const PX_PER_UNIT_X = (W - 2 * PADDING) / Math.max(1, lineLen);
  const PX_PER_UNIT_Y = 4;
  const H = Math.max(200, totalH * PX_PER_UNIT_Y + 2 * PADDING);

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} className="bg-white border border-slate-200 rounded">
      <defs>
        <pattern id="grid" width="40" height="20" patternUnits="userSpaceOnUse">
          <path d="M 40 0 L 0 0 0 20" fill="none" stroke="#e2e8f0" strokeWidth="0.5" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill="url(#grid)" />

      {section.floors.map((f) => {
        const floorY = H - PADDING - (f.elevation_z_ft + f.floor_height_ft) * PX_PER_UNIT_Y;
        const floorH = f.floor_height_ft * PX_PER_UNIT_Y;
        return (
          <g key={f.floor_id}>
            <rect x={PADDING} y={floorY} width={W - 2 * PADDING} height={floorH}
                  fill="#dbeafe" stroke="#1e3a8a" strokeWidth="0.5" opacity={0.4} />
            <text x={PADDING + 4} y={floorY + 12} fontSize="9" fill="#1e3a8a">
              Floor {f.floor_number} {f.floor_name}
            </text>
            {/* Each room intersection becomes a vertical post */}
            {f.intersections.map((it, i) => {
              const tx = PADDING + Math.max(0, Math.min(1, it.t)) * (W - 2 * PADDING);
              return (
                <g key={i}>
                  <line x1={tx} y1={floorY} x2={tx} y2={floorY + floorH} stroke="#1e40af" strokeWidth="1" />
                  <circle cx={tx} cy={floorY} r="2" fill="#1e40af" />
                </g>
              );
            })}
          </g>
        );
      })}

      {/* Ground line */}
      <line x1={PADDING} y1={H - PADDING} x2={W - PADDING} y2={H - PADDING} stroke="#0f172a" strokeWidth="1.2" />
      <text x={PADDING} y={H - PADDING + 14} fontSize="9" fill="#475569">Plan-cut length: {lineLen.toFixed(0)}px</text>
    </svg>
  );
}
