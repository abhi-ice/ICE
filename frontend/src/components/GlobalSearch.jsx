// Global search — debounced /api/search results dropdown.
import { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";

export default function GlobalSearch() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const nav = useNavigate();

  useEffect(() => {
    if (!q.trim() || q.length < 2) { setResults([]); return; }
    const id = setTimeout(async () => {
      try {
        const { data } = await api.get(`/search?q=${encodeURIComponent(q)}`);
        setResults(data.results || []);
        setOpen(true);
      } catch {}
    }, 200);
    return () => clearTimeout(id);
  }, [q]);

  useEffect(() => {
    const onClickAway = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickAway);
    return () => document.removeEventListener("mousedown", onClickAway);
  }, []);

  const go = (r) => {
    setOpen(false); setQ("");
    nav(r.route);
  };

  return (
    <div ref={ref} className="relative w-72 hidden md:block">
      <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
      <input
        data-testid="topbar-search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        placeholder="Search projects, RFIs, activities..."
        className="pl-7 pr-3 h-8 w-full rounded border border-input bg-background text-sm"
      />
      {open && results.length > 0 && (
        <div data-testid="topbar-search-results" className="absolute top-9 left-0 right-0 bg-white border border-slate-200 rounded shadow-lg z-30 max-h-80 overflow-y-auto">
          {results.map((r, i) => (
            <button
              key={`${r.type}-${r.id}-${i}`}
              data-testid={`search-result-${r.type}-${r.id}`}
              onClick={() => go(r)}
              className="w-full text-left px-3 py-2 text-sm hover:bg-slate-50 flex items-center gap-2 border-b border-slate-50 last:border-b-0"
            >
              <span className="pill pill-slate text-[10px] capitalize">{r.type.replace("_", " ")}</span>
              <span className="truncate">{r.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
