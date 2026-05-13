// Bug #42 + #43 (2026-05-12) -- shared Export dropdown.
//
// Two formats by default (XLSX, PDF). Single-click download via
// blob + invisible anchor click pattern. Matches the visual language
// of the existing Bucket K Audit Extract export dropdown
// (SystemPages.jsx:AuditExtractExport).
//
// Used by:
//   - pages/Tasks.jsx (Bug #42 -- master schedule)
//   - pages/Lookahead.jsx (Bug #43 -- 2-week forward-looking)
//
// Backend writes the TASKS_EXPORTED / LOOK_AHEAD_EXPORTED audit op
// BEFORE returning the file bytes, so a successful download is a
// successful audit even if the user closes the tab mid-stream.
import { useState, useEffect, useRef } from "react";
import { Download, Loader2 } from "lucide-react";
import api from "../lib/api";
import { toast } from "./ui/sonner";

export default function ExportDropdown({
  label = "Export",
  testidPrefix = "export",
  buildUrl,                  // (fmt) => string
  filenameHint,              // (fmt) => string -- best-effort if server
                              //                    doesn't supply Content-
                              //                    Disposition filename
  formats = ["xlsx", "pdf"],
  disabled = false,
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const wrapperRef = useRef(null);

  // Close on outside click so the menu doesn't get stuck open.
  useEffect(() => {
    if (!open) return undefined;
    const onClick = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const download = async (fmt) => {
    if (busy || disabled) return;
    setBusy(true);
    setOpen(false);
    try {
      const url = buildUrl(fmt);
      const res = await api.get(url, { responseType: "blob" });
      // Parse Content-Disposition filename if the server supplied it;
      // fall back to filenameHint(fmt) otherwise.
      const cd = res.headers?.["content-disposition"]
        || res.headers?.["Content-Disposition"]
        || "";
      let filename = filenameHint ? filenameHint(fmt) : `export.${fmt}`;
      const m = cd.match(/filename="?([^";]+)"?/i);
      if (m && m[1]) filename = m[1];
      // Blob -> anchor click -> revoke URL.
      const blobUrl = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(blobUrl);
      toast.success(`${label} ${fmt.toUpperCase()} downloaded`);
    } catch (e) {
      const detail = e.response?.data?.detail
        || e.message
        || "Export failed";
      toast.error(`Export failed -- ${typeof detail === "string" ? detail : "try again"}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative" ref={wrapperRef}>
      <button
        data-testid={`${testidPrefix}-toggle`}
        onClick={() => setOpen((v) => !v)}
        disabled={busy || disabled}
        title={disabled ? "Pick a project first" : undefined}
        className="h-9 px-3 rounded border border-slate-200 bg-white text-sm inline-flex items-center gap-1 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {busy
          ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
          : <Download className="h-3.5 w-3.5" />}
        {label}
      </button>
      {open && !busy && (
        <div
          data-testid={`${testidPrefix}-menu`}
          className="absolute right-0 mt-1 w-36 bg-white border border-slate-200 rounded shadow-lg z-10"
        >
          {formats.map((fmt) => (
            <button
              key={fmt}
              data-testid={`${testidPrefix}-${fmt}`}
              onClick={() => download(fmt)}
              className="w-full text-left px-3 py-1.5 text-sm hover:bg-slate-50 first:rounded-t last:rounded-b"
            >
              Export {fmt.toUpperCase()}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
