import { useEffect, useMemo, useState } from "react";
import { Search, AlertTriangle, Plus, Loader2 } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "../ui/dialog";
import api, { extractApiError } from "../../lib/api";
import { toast } from "../ui/sonner";
import { sectionLabel } from "../../lib/sectionLabel";
import { formatDateDisplay } from "../../lib/dateUtil";

// The list arrives ordered by the server: activities scheduled now (or late)
// first, then the next two weeks, then the rest -- each by start date.
const GROUPS = [
  ["now", "Scheduled now"],
  ["soon", "Starting in the next 2 weeks"],
  ["later", "Later in the schedule"],
];

// "Add activity" picker shared by the desktop and phone Daily Report views.
// Field work only; if the activity has an open delay the super must resolve
// it (optional comment) before it is added.
export default function ActivityPickerDialog({ open, onOpenChange, reportId, reportDate, locked, lockedReason, onAdded }) {
  const [items, setItems] = useState(null);
  const [q, setQ] = useState("");
  const [busyId, setBusyId] = useState(null);
  const [blocked, setBlocked] = useState(null);
  const [showLater, setShowLater] = useState(false);

  useEffect(() => {
    if (!open) return;
    setItems(null); setQ(""); setBlocked(null); setShowLater(false);
    api.get(`/daily-reports/${reportId}/activity-picker`)
      .then(({ data }) => setItems(data))
      .catch((e) => { setItems([]); toast.error(extractApiError(e, "Couldn't load activities")); });
  }, [open, reportId]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = items || [];
    if (!needle) return list;
    return list.filter((t) => [t.name, t.description, t.activity_code, ...(t.section_path || [])]
      .filter(Boolean).join(" ").toLowerCase().includes(needle));
  }, [items, q]);
  // Later activities stay folded until asked for (or matched by a search),
  // so the super starts from what is scheduled now.
  const searching = q.trim().length > 0;
  const groups = GROUPS.map(([key, title]) => ({
    key, title, rows: shown.filter((t) => (t.group || "later") === key),
  })).filter((g) => g.rows.length);
  const laterCount = (shown.filter((t) => (t.group || "later") === "later")).length;

  const renderItem = (t) => {
    const label = sectionLabel(t.section_path);
    return (
      <button type="button" key={t.id} data-testid={`activity-picker-item-${t.id}`} onClick={() => add(t)} disabled={!!busyId}
        className="w-full overflow-hidden text-left rounded-lg border border-border px-3 py-2.5 hover:border-emerald-500/60 hover:bg-emerald-500/5 transition-colors flex items-start gap-3 disabled:opacity-60">
        <div className="flex-1 min-w-0 [overflow-wrap:anywhere]">
          <div className="text-sm font-medium break-words">{t.name}</div>
          {label && !(t.display_name || "").includes(label) && (
            <div className="text-xs text-emerald-400/90 break-words" data-testid={`activity-picker-section-${t.id}`}>{label}</div>
          )}
          <div className="text-xs opacity-60">
            {t.activity_code}
            {t.planned_start ? ` · ${formatDateDisplay(t.projected_start_date || t.planned_start)} → ${formatDateDisplay(t.projected_finish_date || t.planned_finish)}` : ""}
            {t.pct_complete ? ` · ${Math.round(t.pct_complete)}% done` : ""}
          </div>
        </div>
        {t.late && (
          <span className="shrink-0 text-[11px] px-2 py-0.5 rounded-full bg-red-500/15 text-red-400" data-testid={`activity-picker-late-${t.id}`}>Late</span>
        )}
        {t.open_delay_count > 0 && (
          <span className="shrink-0 text-[11px] px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-400 inline-flex items-center gap-1" data-testid={`activity-picker-delay-badge-${t.id}`}>
            <AlertTriangle className="h-3 w-3" /> Open delay</span>
        )}
        {busyId === t.id ? <Loader2 className="h-4 w-4 animate-spin shrink-0 mt-1" /> : <Plus className="h-4 w-4 shrink-0 mt-1 text-emerald-400" />}
      </button>
    );
  };

  const add = async (task) => {
    if (locked && (lockedReason || "").trim().length < 10) {
      toast.error("Enter the edit reason at the top first (at least 10 characters)");
      return;
    }
    setBusyId(task.id);
    try {
      const { data } = await api.post(`/daily-reports/${reportId}/grid`, {
        schedule_activity_id: task.id, ...(locked ? { add_reason: lockedReason.trim() } : {}),
      });
      toast.success(`Added ${task.name}`);
      onOpenChange(false);
      onAdded?.(data);
    } catch (e) {
      const d = e?.response?.data?.detail;
      if (e?.response?.status === 409 && d?.code === "open_delay") setBlocked({ task, delays: d.delays, note: "" });
      else toast.error(extractApiError(e, "Couldn't add activity"));
    } finally {
      setBusyId(null);
    }
  };

  const resolveAndAdd = async () => {
    setBusyId("resolve");
    try {
      for (const d of blocked.delays) {
        await api.patch(`/delays/${d.id}`, {
          status: "resolved", resolved_date: reportDate,
          ...(blocked.note.trim() ? { resolved_note: blocked.note.trim() } : {}),
        });
      }
      toast.success("Delay resolved");
      const task = blocked.task;
      setBlocked(null);
      await add(task);
    } catch (e) {
      toast.error(extractApiError(e, "Couldn't resolve the delay"));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl max-h-[88vh] flex flex-col" data-testid="activity-picker-dialog">
        <DialogHeader>
          <DialogTitle>{blocked ? "Open delay on this activity" : "Add activity"}</DialogTitle>
          <DialogDescription>
            {blocked ? blocked.task.name : "Pick the construction activity being worked today."}
          </DialogDescription>
        </DialogHeader>
        {blocked ? (
          <div className="space-y-3" data-testid="activity-picker-delay-panel">
            {blocked.delays.map((d) => (
              <div key={d.id} className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm" data-testid={`activity-picker-delay-${d.id}`}>
                <div className="flex items-center gap-2 font-medium"><AlertTriangle className="h-4 w-4 text-amber-400" />
                  {d.impact_days} impact day{d.impact_days === 1 ? "" : "s"} · logged {String(d.logged_date || "").slice(0, 10)}</div>
                {d.description && <div className="mt-1 opacity-80">{d.description}</div>}
              </div>
            ))}
            <p className="text-sm opacity-80">Work is happening on it again. Mark the delay resolved and add the activity?</p>
            <textarea data-testid="activity-picker-resolve-note" rows={2} value={blocked.note}
              onChange={(e) => setBlocked({ ...blocked, note: e.target.value })}
              placeholder="Comment (optional) — e.g. trench pumped out, crew back on site"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring" />
            <div className="flex justify-end gap-2">
              <button type="button" data-testid="activity-picker-resolve-cancel" onClick={() => setBlocked(null)}
                className="h-9 px-4 rounded-md border border-input text-sm hover:bg-muted transition-colors">Back</button>
              <button type="button" data-testid="activity-picker-resolve-confirm" onClick={resolveAndAdd} disabled={!!busyId}
                className="h-9 px-4 rounded-md bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-500 disabled:opacity-50 transition-colors inline-flex items-center gap-2">
                {busyId === "resolve" && <Loader2 className="h-4 w-4 animate-spin" />} Resolve &amp; add</button>
            </div>
          </div>
        ) : (
          <>
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 opacity-50" />
              <input autoFocus data-testid="activity-picker-search" value={q} onChange={(e) => setQ(e.target.value)}
                placeholder="Search activity, area or code…"
                className="w-full h-11 pl-9 pr-3 rounded-md border border-input bg-background text-[16px] md:text-sm focus:outline-none focus:ring-2 focus:ring-ring" />
            </div>
            <div className="flex-1 overflow-y-auto -mx-1 px-1 mt-1 space-y-1.5" data-testid="activity-picker-list">
              {items === null && <div className="py-8 text-center text-sm opacity-70"><Loader2 className="h-5 w-5 animate-spin inline" /></div>}
              {items !== null && shown.length === 0 && (
                <div className="py-8 text-center text-sm opacity-70" data-testid="activity-picker-empty">
                  {items.length ? "No match. Try another word." : "Every open field activity is already on this report."}</div>
              )}
              {groups.map((g) => (
                <div key={g.key} data-testid={`activity-picker-group-${g.key}`}>
                  <div className="sticky top-0 z-[1] bg-background/95 backdrop-blur px-1 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wide opacity-70">
                    {g.title} <span className="opacity-60">({g.rows.length})</span>
                  </div>
                  {g.key === "later" && !searching && !showLater ? (
                    <button type="button" data-testid="activity-picker-show-later" onClick={() => setShowLater(true)}
                      className="w-full h-10 rounded-lg border border-dashed border-border text-sm opacity-80 hover:opacity-100 hover:bg-muted transition-colors">
                      Show {laterCount} later activit{laterCount === 1 ? "y" : "ies"}</button>
                  ) : (
                    <div className="space-y-1.5">{g.rows.map(renderItem)}</div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
