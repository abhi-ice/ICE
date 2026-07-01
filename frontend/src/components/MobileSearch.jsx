// Mobile search (2026-06-30). The desktop GlobalSearch is `hidden md:block`,
// so phones had no search entry point. This adds a `md:hidden` magnifier in
// the TopBar that opens a full-screen search sheet reusing the same /search
// API + result→route navigation. EVERYTHING here is md:hidden, so desktop
// (>=768px) renders nothing from this component and stays byte-identical.
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Search, X } from "lucide-react";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";

export default function MobileSearch() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const nav = useNavigate();

  // Debounced query (same contract as GlobalSearch: >=2 chars, 200ms).
  useEffect(() => {
    if (!open) return undefined;
    if (!q.trim() || q.length < 2) { setResults([]); return undefined; }
    const id = setTimeout(async () => {
      try {
        const { data } = await api.get(`/search?q=${encodeURIComponent(q)}`);
        setResults(data.results || []);
      } catch { /* network/empty -> leave results as-is */ }
    }, 200);
    return () => clearTimeout(id);
  }, [q, open]);

  // ESC closes the sheet.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const close = () => { setOpen(false); setQ(""); setResults([]); };
  const go = (r) => { close(); nav(r.route); };

  return (
    <>
      {/* trigger -- phones only, 44px tap target, matches the hamburger style */}
      <button
        type="button"
        data-testid="mobile-search-toggle"
        aria-label="Search"
        onClick={() => setOpen(true)}
        className="md:hidden h-11 w-11 grid place-items-center rounded-md text-white/80 hover:bg-white/10 shrink-0"
      >
        <Search className="h-5 w-5" strokeWidth={1.8} />
      </button>

      {/* full-screen sheet -- phones only. PORTALED to <body> so it escapes
          the TopBar's sticky-z-10 stacking context; otherwise its z-50 is
          trapped below the Copilot FAB (z-20, a direct child of the layout
          root) and the FAB floats over the open search sheet. */}
      {open && createPortal((
        <div data-testid="mobile-search-sheet" className="md:hidden fixed inset-0 z-50 bg-background flex flex-col">
          <div className="flex items-center gap-2 p-3 border-b border-border">
            <Search className="h-5 w-5 text-slate-400 shrink-0" strokeWidth={2} />
            <input
              autoFocus
              data-testid="mobile-search-input"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search projects, RFIs, activities…"
              className="flex-1 min-w-0 h-11 text-base bg-transparent outline-none"
            />
            <button
              type="button"
              data-testid="mobile-search-close"
              aria-label="Close search"
              onClick={close}
              className="h-11 w-11 grid place-items-center rounded-md text-slate-500 hover:bg-slate-100 shrink-0"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto overscroll-contain">
            {q.trim().length >= 2 && results.length === 0 && (
              <div className="p-6 text-center text-sm text-slate-400">No matches.</div>
            )}
            {results.map((r, i) => (
              <button
                key={`${r.type}-${r.id}-${i}`}
                data-testid={`mobile-search-result-${r.type}-${r.id}`}
                onClick={() => go(r)}
                className="w-full text-left px-4 py-3 text-sm hover:bg-slate-50 flex items-center gap-2 border-b border-slate-100 min-h-[44px]"
              >
                <span className="pill pill-slate text-xs capitalize shrink-0">{r.type.replace("_", " ")}</span>
                <span className="truncate">{r.label}</span>
              </button>
            ))}
          </div>
        </div>
      ), document.body)}
    </>
  );
}
