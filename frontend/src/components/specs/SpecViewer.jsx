// SpecViewer (pt.76) — read-only, scrollable in-app viewer for an uploaded
// spec PDF (up to ~900 pages). Cloned down from DrawingViewer but stripped of
// Konva markups / pins / sidebar: just react-pdf page render + page nav + zoom.
// Theme-aware (light + dark). Fetches the original PDF from GET /specs/:id/pdf.
import { useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { ChevronLeft, ChevronRight, ZoomIn, ZoomOut, X, Loader2 } from "lucide-react";
import api, { API } from "../../lib/api";
import { getToken, getActiveTenantId } from "../../lib/auth";

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

export default function SpecViewer({ spec, onClose }) {
  const [numPages, setNumPages] = useState(spec?.pdf_page_count || 0);
  const [page, setPage] = useState(1);
  const [scale, setScale] = useState(1.1);
  const [pageInput, setPageInput] = useState("1");
  const [err, setErr] = useState(null);
  const [blobUrl, setBlobUrl] = useState(null); // populated only on fallback
  const blobRef = useRef(null);

  // pt.77: prefer URL mode so pdf.js streams pages via HTTP range requests
  // (the backend serves 206 + an LRU blob cache). httpHeaders carry the JWT +
  // tenant so the authenticated endpoint accepts pdf.js's own fetches.
  const urlFile = useMemo(() => ({
    url: `${API}/specs/${spec.id}/pdf`,
    httpHeaders: {
      Authorization: `Bearer ${getToken() || ""}`,
      "X-Tenant-ID": getActiveTenantId() || "",
    },
    withCredentials: false,
  }), [spec?.id]);

  useEffect(() => {
    setErr(null); setBlobUrl(null); setPage(1);
    if (blobRef.current) { URL.revokeObjectURL(blobRef.current); blobRef.current = null; }
    return () => { if (blobRef.current) { URL.revokeObjectURL(blobRef.current); blobRef.current = null; } };
  }, [spec?.id]);

  // On any load error, fall back to downloading the full blob (the prior
  // behavior) so the viewer is never worse than before range streaming.
  const onDocError = async () => {
    if (blobUrl) { setErr("Failed to render the spec PDF."); return; }
    try {
      const res = await api.get(`/specs/${spec.id}/pdf`, { responseType: "blob" });
      const u = URL.createObjectURL(res.data);
      blobRef.current = u;
      setBlobUrl(u);
    } catch (e) {
      setErr(e.response?.status === 404
        ? "No PDF has been uploaded for this spec yet."
        : "Failed to load the spec PDF.");
    }
  };

  const fileProp = blobUrl || urlFile;

  // Clamp in the handler (rather than disabling buttons) so the controls are
  // never rendered in an invisible disabled state in either theme.
  const goto = (n) => {
    const max = numPages || n;
    const p = Math.min(Math.max(1, n), max);
    setPage(p);
    setPageInput(String(p));
  };

  useEffect(() => { setPageInput(String(page)); }, [page]);

  return (
    <div data-testid="spec-viewer" className="fixed inset-0 z-[60] flex"
      style={{ background: "rgba(0,0,0,.7)" }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="m-auto rounded-lg shadow-2xl flex flex-col w-[min(95vw,1100px)] h-[92vh]"
        style={{ background: "var(--v4-bg-surface)", border: "1px solid var(--v4-border)" }}>
        {/* header */}
        <div className="h-12 flex items-center gap-2 px-3 flex-wrap"
          style={{ borderBottom: "1px solid var(--v4-border)" }}>
          <span className="text-sm font-semibold truncate max-w-[40%]" style={{ color: "var(--v4-fg-primary)" }}>
            {spec?.name}
          </span>
          {spec?.revision_label && (
            <span className="text-xs" style={{ color: "var(--v4-fg-muted)" }}>· {spec.revision_label}</span>
          )}
          <div className="ml-auto flex items-center gap-1">
            <button onClick={() => setScale((s) => Math.max(0.5, +(s - 0.1).toFixed(2)))}
              className="sviewer-btn" title="Zoom out"><ZoomOut className="h-4 w-4" /></button>
            <span className="text-xs w-10 text-center tabular-nums" style={{ color: "var(--v4-fg-muted)" }}>
              {Math.round(scale * 100)}%
            </span>
            <button onClick={() => setScale((s) => Math.min(3, +(s + 0.1).toFixed(2)))}
              className="sviewer-btn" title="Zoom in"><ZoomIn className="h-4 w-4" /></button>
            <span className="mx-1" style={{ color: "var(--v4-border)" }}>|</span>
            <button onClick={() => goto(page - 1)} className="sviewer-btn" title="Previous page">
              <ChevronLeft className="h-4 w-4" />
            </button>
            <input
              data-testid="spec-viewer-page"
              value={pageInput}
              onChange={(e) => setPageInput(e.target.value.replace(/[^0-9]/g, ""))}
              onKeyDown={(e) => { if (e.key === "Enter") goto(parseInt(pageInput || "1", 10)); }}
              onBlur={() => goto(parseInt(pageInput || "1", 10))}
              className="w-10 h-7 text-center text-xs rounded dr-mp-input"
            />
            <span className="text-xs tabular-nums" style={{ color: "var(--v4-fg-faint)" }}>/ {numPages || "?"}</span>
            <button onClick={() => goto(page + 1)} className="sviewer-btn" title="Next page">
              <ChevronRight className="h-4 w-4" />
            </button>
            <button onClick={onClose} data-testid="spec-viewer-close" className="sviewer-btn ml-1" title="Close">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* body */}
        <div className="flex-1 overflow-auto grid place-items-start justify-center p-4 scrollbar-soft"
          style={{ background: "var(--v4-bg-surface-2)" }}>
          {err ? (
            <div className="text-sm mt-12" style={{ color: "var(--v4-fg-muted)" }}>{err}</div>
          ) : (
            <Document
              file={fileProp}
              onLoadSuccess={({ numPages: n }) => setNumPages(n)}
              onLoadError={onDocError}
              loading={<div className="flex items-center gap-2 text-sm mt-12" style={{ color: "var(--v4-fg-muted)" }}>
                <Loader2 className="h-4 w-4 animate-spin" /> Loading PDF…</div>}
            >
              <Page
                pageNumber={page}
                scale={scale}
                renderTextLayer={false}
                renderAnnotationLayer={false}
                loading={<div className="text-xs mt-8" style={{ color: "var(--v4-fg-faint)" }}>Rendering page…</div>}
              />
            </Document>
          )}
        </div>
      </div>
    </div>
  );
}
