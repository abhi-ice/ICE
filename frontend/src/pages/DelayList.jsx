// Sidebar Delays page — drop-in replacement for the generic CrudList. Uses
// the shared DelayForm for create/edit and shows status + linked-task counts
// + days open in the table. Each row is clickable to /delays/:id (detail).
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus, Pencil, Trash2 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";
import GalleryActivityChips from "../components/photos/GalleryActivityChips";
import { useUrlState } from "../lib/useUrlState";
import { navigateToTodaysDrForCreate } from "../lib/todaysDr";
// CPM Part G (2026-05-15) -- delay.date rendered MM/DD/YYYY via chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";
import PageHelp from "../components/PageHelp";

export default function DelayList() {
  // ALL hooks must come before any conditional return per the Rules
  // of Hooks. Earlier code put `useNavigate` + the routing/deleting
  // useStates after the `if (!activeId) return` guard, which broke
  // the bundler with "React Hook called conditionally" errors and
  // wedged the entire app behind a compiled-with-problems overlay.
  const { activeId } = useProjects();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  // Bucket D Phase 3 + Fix H -- gallery filter mirrored to URL.
  const [urlFilters, setUrlFilters] = useUrlState({ affected_activity_id: "" });
  const affectedActivityId = urlFilters.affected_activity_id;
  const setAffectedActivityId = (v) => setUrlFilters({ affected_activity_id: v || "" });
  // Bucket D Fix G [2/4] -- "+ Add" routes to today's DR's delay
  // creation (or supplement flow on locked DR). useNavigate +
  // `routing` state must be declared at the top of the component,
  // never after the `if (!activeId)` early return below.
  const navigate = useNavigate();
  const [routing, setRouting] = useState(false);
  // Bucket D Fix E -- per-row delete guard (Set tracks which rows are mid-delete).
  const [deleting, setDeleting] = useState(new Set());

  const refresh = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      const params = {};
      if (affectedActivityId) params.affected_activity_id = affectedActivityId;
      const { data } = await api.get(`/projects/${activeId}/delays`, { params });
      setRows(Array.isArray(data) ? data : []);
    } catch (e) { toast.error("Load failed"); }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId, affectedActivityId]);

  if (!activeId) {
    return <div className="text-sm text-slate-500">Select a project from the top bar to view Delays.</div>;
  }

  // The legacy startCreate inline-modal path stays for the row-edit
  // Pencil button so editing existing delays still works inline.
  const startCreate = async () => {
    if (routing) return;
    if (!activeId) return toast.error("Pick a project first");
    setRouting(true);
    try {
      const { url, locked, dr } = await navigateToTodaysDrForCreate(activeId, "delay");
      if (locked) {
        toast.info(`Today's DR is locked. Use Supplements panel on the parent DR to add a delay.`);
        navigate(`/daily-reports?openId=${dr.id}`);
      } else {
        navigate(url);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Couldn't resolve today's DR");
    } finally { setRouting(false); }
  };
  const startEdit = (r) => { setEditing(r); setOpen(true); };
  const remove = async (r) => {
    if (deleting.has(r.id)) return;
    if (!window.confirm(`Delete this delay? This is permanent and the audit log will record it.`)) return;
    setDeleting((prev) => new Set(prev).add(r.id));
    try {
      await api.delete(`/delays/${r.id}`);
      toast.success("Delay deleted");
      refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    } finally {
      setDeleting((prev) => { const n = new Set(prev); n.delete(r.id); return n; });
    }
  };

  return (
    <div data-testid="delays-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight inline-flex items-baseline">
            Delays
            {/* pt.36: PageHelp houses the longer explanation about
                per-task impact + reminder cards on the DR; previously
                lived in two stacked bricks (slate + amber) above the
                table. Subtitle keeps the short A-style summary. */}
            <PageHelp title="Delays">
              Each delay can link to multiple schedule tasks and capture per-task projected
              impact in days. Open delays whose tasks are scheduled today appear as reminder
              cards on the daily report.
              <br/><br/>
              Click any row's date or activity chip to jump to the parent daily report.
            </PageHelp>
          </h1>
          <p className="text-sm text-slate-500">Logged schedule delays with linked tasks, float impact, and resolution status.</p>
        </div>
        <Button data-testid="delays-add" onClick={startCreate} disabled={routing}
          className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 gap-1">
          <Plus className="h-4 w-4" /> {routing ? "Opening today's DR…" : "New on today's DR"}
        </Button>
      </div>

      {/* pt.36: slate brick removed -- long explanation moved to
          PageHelp (?) on the title. Yellow gallery brick collapsed
          to a neutral filter row below since the "Gallery view"
          description is redundant once the title carries the help. */}

      {/* Bucket D Phase 3 -- gallery filter. */}
      <div className="rounded border border-slate-200 bg-slate-50 p-2 text-xs text-slate-600 flex items-center gap-2">
        <label className="uppercase tracking-wide text-xs">Filter by activity id</label>
        <input data-testid="delays-affected-activity-filter"
          value={affectedActivityId}
          onChange={(e) => setAffectedActivityId(e.target.value)}
          placeholder="paste a daily_report_activity.id"
          className="h-7 px-2 rounded border border-slate-300 bg-white text-xs flex-1" />
        {affectedActivityId && (
          <button onClick={() => setAffectedActivityId("")}
            className="h-7 px-2 rounded bg-slate-200 text-slate-700 text-xs hover:bg-slate-300">
            Clear
          </button>
        )}
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Description</th>
              <th>Linked tasks</th>
              <th>Affected activities</th>
              <th>Days open</th>
              <th className="w-20 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const sev = r.severity === "critical" || r.severity === "major" ? "pill-red" : "pill-slate";
              const stat = r.status === "resolved" ? "pill-green" : r.status === "in_progress" ? "pill-amber" : "pill-slate";
              return (
                <tr key={r.id} data-testid={`delay-row-${r.id}`}>
                  <td className="font-mono text-xs">
                    {r.daily_report_id ? (
                      <Link to={`/daily-reports?openId=${r.daily_report_id}&openEvent=${r.id}&openEventType=delay`} className="text-blue-700 hover:underline">
                        {formatDateDisplay(r.date)}
                      </Link>
                    ) : formatDateDisplay(r.date)}
                  </td>
                  <td><span className={`pill ${sev}`}>{r.severity}</span></td>
                  <td><span className={`pill ${stat}`}>{(r.status || "open").replace(/_/g, " ")}</span></td>
                  <td className="text-sm max-w-[420px] truncate">
                    <Link to={`/delays/${r.id}`} className="text-blue-600 hover:underline">
                      {(r.description || "").slice(0, 120) || "—"}
                    </Link>
                  </td>
                  <td className="text-xs">{(r.linked_task_ids || []).length}</td>
                  <td><GalleryActivityChips projectId={activeId} activityIds={r.linked_task_ids || []} /></td>
                  <td className="text-xs">{r.duration_days ?? "—"}</td>
                  <td className="text-right">
                    <button data-testid={`delays-edit-${r.id}`} onClick={() => startEdit(r)}
                      className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100 text-slate-500"
                      title="Edit">
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button data-testid={`delays-delete-${r.id}`} onClick={() => remove(r)}
                      disabled={deleting.has(r.id)}
                      className="h-7 w-7 grid place-items-center rounded hover:bg-red-50 text-red-500 disabled:opacity-50"
                      title="Delete">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              );
            })}
            {!rows.length && !loading && (
              <tr><td colSpan="7" className="text-center py-8 text-sm text-slate-500">No delays logged for this project yet.</td></tr>
            )}
            {loading && rows.length === 0 && (
              <tr><td colSpan="7" className="text-center py-8 text-sm text-slate-400">Loading…</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>{editing ? "Edit delay" : "Log a delay"}</DialogTitle></DialogHeader>
          <DelayForm
            projectId={activeId}
            initial={editing}
            onSaved={() => { setOpen(false); setEditing(null); refresh(); }}
            onCancel={() => { setOpen(false); setEditing(null); }}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}
