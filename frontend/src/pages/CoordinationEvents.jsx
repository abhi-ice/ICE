// Bucket C3b Phase 1 — sidebar Coordination page. Filterable list of
// coordination events, plus the "+ New" dialog with the full form.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, MessagesSquare, Phone, Mail, FileText, Users,
         ArrowDownLeft, ArrowUpRight, Filter } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import CoordinationEventForm from "../components/forms/CoordinationEventForm";
import { useUrlState } from "../lib/useUrlState";
import { useNavigate } from "react-router-dom";
import { navigateToTodaysDrForCreate } from "../lib/todaysDr";

const TOPIC_LABELS = {
  tenant_access: "Tenant access",
  building_coordination: "Building coordination",
  a_e_clarification: "A/E clarification",
  owner_direction: "Owner direction",
  sub_coordination: "Sub coordination",
  regulatory: "Regulatory",
  safety: "Safety",
  quality: "Quality",
  scope: "Scope",
  payment: "Payment",
  other: "Other",
};

const CHANNEL_ICONS = {
  email: Mail,
  phone: Phone,
  in_person: Users,
  formal_letter: FileText,
  text_message: MessagesSquare,
  meeting: Users,
  system_notification: MessagesSquare,
};

export default function CoordinationEvents() {
  const { activeId } = useProjects();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  // Bucket D Fix H -- filters mirrored to URL.
  const [filters, setFilters] = useUrlState({ topic: "", channel: "", direction: "", date_from: "", date_to: "" });

  const params = useMemo(() => {
    const p = {};
    if (filters.topic) p.topic = filters.topic;
    if (filters.channel) p.channel = filters.channel;
    if (filters.direction) p.direction = filters.direction;
    if (filters.date_from) p.date_from = filters.date_from;
    if (filters.date_to) p.date_to = filters.date_to;
    return p;
  }, [filters]);

  const refresh = async () => {
    if (!activeId) return;
    setLoading(true);
    try {
      const { data } = await api.get(`/projects/${activeId}/coordination-events`, { params });
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    } finally { setLoading(false); }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId, params]);

  if (!activeId) {
    return <div className="text-sm text-slate-500">Select a project from the top bar to view coordination events.</div>;
  }

  return (
    <div data-testid="coordination-events-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Coordination log</h1>
          <p className="text-sm text-slate-500">Every email, call, meeting, and letter that affects schedule or scope. TIA-ready.</p>
        </div>
        <CoordinationNewButton projectId={activeId}
          fallbackOpen={() => setOpen(true)} />
      </div>

      {/* Filter bar */}
      <div className="surface p-3">
        <div className="flex items-center gap-2 mb-2">
          <Filter className="h-3.5 w-3.5 text-slate-500" />
          <div className="text-xs text-slate-500">Filters</div>
          {(filters.topic || filters.channel || filters.direction || filters.date_from || filters.date_to) && (
            <button onClick={() => setFilters({ topic: "", channel: "", direction: "", date_from: "", date_to: "" })}
              className="ml-auto text-xs text-blue-600 hover:underline">Clear</button>
          )}
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
          <select data-testid="coord-filter-topic" value={filters.topic}
            onChange={(e) => setFilters({ ...filters, topic: e.target.value })}
            className="h-8 px-2 border border-slate-200 rounded text-xs max-md:min-h-[44px]">
            <option value="">Topic — all</option>
            {Object.entries(TOPIC_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <select data-testid="coord-filter-channel" value={filters.channel}
            onChange={(e) => setFilters({ ...filters, channel: e.target.value })}
            className="h-8 px-2 border border-slate-200 rounded text-xs max-md:min-h-[44px]">
            <option value="">Channel — all</option>
            {Object.keys(CHANNEL_ICONS).map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
          </select>
          {/* max-md:col-span-2 -- Direction spans the full width on phones so
              the From/To date pair lands together on its own row (Topic|Channel,
              Direction, From|To) instead of a split range + a lone date cell.
              Desktop keeps the 5-across row (md:grid-cols-5). */}
          <select data-testid="coord-filter-direction" value={filters.direction}
            onChange={(e) => setFilters({ ...filters, direction: e.target.value })}
            className="h-8 px-2 border border-slate-200 rounded text-xs max-md:min-h-[44px] max-md:col-span-2">
            <option value="">Direction — all</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
            <option value="internal">Internal</option>
          </select>
          <input data-testid="coord-filter-from" type="date" value={filters.date_from}
            onChange={(e) => setFilters({ ...filters, date_from: e.target.value })}
            placeholder="From"
            className="h-8 px-2 border border-slate-200 rounded text-xs max-md:min-h-[44px]" />
          <input data-testid="coord-filter-to" type="date" value={filters.date_to}
            onChange={(e) => setFilters({ ...filters, date_to: e.target.value })}
            placeholder="To"
            className="h-8 px-2 border border-slate-200 rounded text-xs max-md:min-h-[44px]" />
        </div>
      </div>

      {/* List */}
      {/* crud-list-table-wrap: on phones each event collapses to a compact
          card (Summary is the headline; When/Channel/Party/Topic/Outcome flow
          as meta; the bare direction icon + optional Trade tag hide to cut
          clutter). Inert >=768px -> desktop table stays byte-identical. */}
      <div className="surface overflow-hidden">
        <div className="crud-list-table-wrap">
        <table className="dense-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Channel</th>
              <th>Dir</th>
              <th>Other party</th>
              <th>Topic</th>
              {/* Bug #32 fix (2026-05-13) -- optional trade tag column;
                  renders "—" when unset. */}
              <th>Trade</th>
              <th>Summary</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const Icon = CHANNEL_ICONS[r.channel] || MessagesSquare;
              const Dir = r.direction === "inbound" ? ArrowDownLeft
                : r.direction === "outbound" ? ArrowUpRight : Users;
              const outClass = r.outcome === "agreement" ? "pill-green"
                : r.outcome === "disagreement" || r.outcome === "escalation_required" ? "pill-red"
                : r.outcome === "pending" ? "pill-amber" : "pill-slate";
              return (
                <tr key={r.id} data-testid={`coord-row-${r.id}`}>
                  <td className="font-mono text-xs whitespace-nowrap" data-label="When">{(r.occurred_at || "").slice(0, 16).replace("T", " ")}</td>
                  <td className="text-xs" data-label="Channel">
                    <span className="inline-flex items-center gap-1">
                      <Icon className="h-3 w-3 text-slate-500" /> {r.channel.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="col-hide-m"><Dir className="h-3.5 w-3.5 text-slate-500" /></td>
                  <td className="text-xs" data-label="Party">
                    <div>{r.other_party_name}</div>
                    {r.other_party_org && <div className="text-xs text-slate-400">{r.other_party_org}</div>}
                  </td>
                  <td className="text-xs" data-label="Topic">{TOPIC_LABELS[r.topic] || r.topic}</td>
                  {/* Bug #32 fix -- optional trade tag, "—" when unset. */}
                  <td className="text-xs col-hide-m">{r.trade_code ? r.trade_code.replace(/_/g, " ") : "—"}</td>
                  <td className="text-sm max-w-[420px] col-primary" data-label="Summary">
                    <Link to={`/coordination/${r.id}`} className="text-blue-700 hover:underline">
                      {r.summary}
                    </Link>
                    {r.voice_extracted && (
                      <span className="ml-2 text-xs px-1 rounded bg-amber-100 text-amber-700">voice draft</span>
                    )}
                  </td>
                  <td data-label="Outcome"><span className={`pill ${outClass} capitalize text-xs`}>{r.outcome.replace(/_/g, " ")}</span></td>
                </tr>
              );
            })}
            {!loading && !rows.length && (
              <tr><td colSpan="8" className="text-center py-8 text-sm text-slate-500">No coordination events match the current filters.</td></tr>
            )}
            {loading && rows.length === 0 && (
              <tr><td colSpan="8" className="text-center py-8 text-sm text-slate-400">Loading…</td></tr>
            )}
          </tbody>
        </table>
        </div>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Log a coordination event</DialogTitle></DialogHeader>
          <CoordinationEventForm
            projectId={activeId}
            onSaved={() => { setOpen(false); refresh(); toast.success("Coordination event logged"); }}
            onCancel={() => setOpen(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}

// Bucket D Fix G [4/4] -- + New button routes to today's DR with the
// quick-log dialog opened (DR detail's createEvent=coordination handler
// flips setCoordOpen=true). If today's DR is locked, we fall back to
// opening the standalone form dialog on this page so the user can
// still log the event project-wide.
function CoordinationNewButton({ projectId, fallbackOpen }) {
  const navigate = useNavigate();
  const [routing, setRouting] = useState(false);
  const click = async () => {
    if (routing) return;
    if (!projectId) { fallbackOpen(); return; }
    setRouting(true);
    try {
      const { url, locked } = await navigateToTodaysDrForCreate(projectId, "coordination");
      if (locked) {
        toast.info("Today's DR is locked. Opening project-wide log here.");
        fallbackOpen();
      } else {
        navigate(url);
      }
    } catch (e) {
      // If today's DR can't be resolved (no project, network error),
      // fall back to the standalone dialog rather than blocking the user.
      fallbackOpen();
    } finally { setRouting(false); }
  };
  return (
    <button data-testid="coord-new" onClick={click} disabled={routing}
      className="sbtn sbtn-md sbtn-primary">
      {routing ? "Opening today's DR…" : "New on today's DR"}
    </button>
  );
}
