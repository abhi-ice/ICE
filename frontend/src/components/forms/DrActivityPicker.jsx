// Bucket D Task 1 (post-Phase 5) -- shared multi-select picker for
// daily_report_activities rows. Used by every inline event-create
// form on the daily report page (SafetyPanel, RfisPanel, Deliveries
// Panel, etc.) and by the standalone create dialogs (DelayForm,
// CoordinationEventForm) so the same picker UX appears everywhere
// the linked_dr_activity_ids contract applies.
//
// Two source modes:
//   * grid prop given -> use as-is (no fetch).
//   * dailyReportId prop given without grid -> fetch
//     /daily-reports/{id}/grid on mount and use the result.
//
// Searchable: typing in the filter input narrows by activity_code or
// description case-insensitively. Selected rows render as removable
// chips above the picker.
import { useEffect, useMemo, useState } from "react";
import { X, Search } from "lucide-react";
import api from "../../lib/api";

export default function DrActivityPicker({
  grid, dailyReportId, value, onChange,
  required = true, label, testidPrefix = "dr-activity-picker",
  compact = false,
}) {
  const [fetched, setFetched] = useState(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    if (grid) { setFetched(null); return; }
    if (!dailyReportId) { setFetched(null); return; }
    let cancelled = false;
    api.get(`/daily-reports/${dailyReportId}/grid`)
      .then(({ data }) => { if (!cancelled) setFetched(Array.isArray(data) ? data : []); })
      .catch(() => { if (!cancelled) setFetched([]); });
    return () => { cancelled = true; };
  }, [grid, dailyReportId]);

  const allRows = grid ?? fetched ?? [];
  const valArr = Array.isArray(value) ? value : [];
  const byId = useMemo(() => Object.fromEntries(allRows.map((r) => [r.id, r])), [allRows]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return allRows;
    return allRows.filter((r) =>
      (r.activity_code || "").toLowerCase().includes(q)
      || (r.activity_description || "").toLowerCase().includes(q)
    );
  }, [allRows, filter]);

  const toggle = (id) => {
    if (valArr.includes(id)) onChange(valArr.filter((x) => x !== id));
    else onChange([...valArr, id]);
  };
  const remove = (id) => onChange(valArr.filter((x) => x !== id));

  const heightClass = compact ? "max-h-20" : "max-h-32";

  return (
    <div data-testid={testidPrefix} className="space-y-1">
      <div className="flex items-center justify-between">
        <label className="text-xs text-slate-500">
          {label || "Affected activities"} {required && <span className="text-rose-600">*</span>}
        </label>
        <span className="text-xs text-slate-400">{valArr.length} picked</span>
      </div>

      {valArr.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {valArr.map((id) => {
            const r = byId[id];
            return (
              <span key={id} data-testid={`${testidPrefix}-chip-${id}`}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-blue-50 border border-blue-200 text-blue-800 text-xs">
                <span className="font-mono">{r?.activity_code || id.slice(0, 6)}</span>
                <button type="button" onClick={() => remove(id)}
                  className="text-blue-500 hover:text-blue-700"
                  aria-label="remove">
                  <X className="h-2.5 w-2.5" />
                </button>
              </span>
            );
          })}
        </div>
      )}

      <div className="relative">
        <Search className="absolute left-2 top-1.5 h-3 w-3 text-slate-400" />
        <input data-testid={`${testidPrefix}-filter`} type="text" value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by code or description"
          className="w-full h-7 pl-7 pr-2 rounded border border-slate-200 text-xs" />
      </div>

      <div className={`${heightClass} overflow-auto border border-slate-200 rounded`}>
        {!allRows.length ? (
          <div className="p-2 text-xs text-slate-400 italic">
            No activities on this DR yet.
          </div>
        ) : !filtered.length ? (
          <div className="p-2 text-xs text-slate-400 italic">
            No matches for "{filter}".
          </div>
        ) : filtered.map((a) => (
          <label key={a.id}
            className="flex items-center gap-2 px-2 py-1 border-b border-slate-100 cursor-pointer hover:bg-slate-50 text-xs">
            <input type="checkbox" data-testid={`${testidPrefix}-opt-${a.activity_code || a.id}`}
              checked={valArr.includes(a.id)} onChange={() => toggle(a.id)} />
            <span className="font-mono text-blue-700">{a.activity_code}</span>
            <span className="text-slate-700 truncate">{a.activity_description}</span>
          </label>
        ))}
      </div>
    </div>
  );
}
