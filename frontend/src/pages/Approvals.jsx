import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
import {
  BadgeCheck, Sparkles, Filter, Check, X, ChevronRight, Loader2,
  ChevronLeft, RefreshCw, ChevronsRight,
} from "lucide-react";

// Bucket G Phase 3 (2026-05-11) -- Review Queue tab.
// Per Sid: no new sidebar entries. Pending-suggestions tab lives
// inside the existing Approvals page since both are "review the
// AI/system proposals and approve/reject" flows.
export default function Approvals() {
  const [tab, setTab] = useState("approvals");
  return (
    <div data-testid="approvals-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <BadgeCheck className="h-5 w-5" />
        <h1 className="text-2xl font-semibold">Approvals & AI suggestions</h1>
      </div>

      <div className="flex border-b border-slate-200 text-sm">
        <ApprovalsTab
          tab="approvals" active={tab} onClick={setTab}
          label="Approvals" Icon={BadgeCheck}
          testid="approvals-tab-approvals"
        />
        <ApprovalsTab
          tab="suggestions" active={tab} onClick={setTab}
          label="Pending suggestions" Icon={Sparkles}
          testid="approvals-tab-suggestions"
        />
      </div>

      {tab === "approvals" && <ApprovalsList />}
      {tab === "suggestions" && <PendingSuggestionsQueue />}
    </div>
  );
}

function ApprovalsTab({ tab, active, onClick, label, Icon, testid }) {
  const isActive = tab === active;
  return (
    <button
      data-testid={testid}
      onClick={() => onClick(tab)}
      className={`px-3 py-2 -mb-px border-b-2 inline-flex items-center gap-1.5 ${
        isActive
          ? "border-blue-600 text-blue-900 font-medium"
          : "border-transparent text-slate-600 hover:text-slate-900"
      }`}
    >
      <Icon className="h-3.5 w-3.5" /> {label}
    </button>
  );
}


// =====================================================================
// Existing approvals list -- preserved verbatim.
// =====================================================================
function ApprovalsList() {
  const [items, setItems] = useState([]);
  const [filter, setFilter] = useState("pending");

  const load = () => api.get(`/approvals?status_filter=${filter}`).then(({ data }) => setItems(data));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [filter]);

  const decide = async (id, decision) => {
    await api.post(`/approvals/${id}/decide`, { decision, notes: null });
    toast.success(`Marked ${decision}`);
    load();
  };

  return (
    <div className="space-y-4" data-testid="approvals-list">
      <div className="flex items-center justify-end">
        <div className="flex gap-2">
          {["pending", "approved", "rejected"].map((s) => (
            <button key={s} data-testid={`approvals-filter-${s}`} onClick={() => setFilter(s)}
              className={`h-8 px-3 rounded border text-sm capitalize ${filter === s ? "bg-primary text-primary-foreground border-primary" : "border-border"}`}>
              {s}
            </button>
          ))}
        </div>
      </div>
      <div className="space-y-3">
        {items.map((a) => (
          <div key={a.id} data-testid={`approval-card-${a.id}`} className="bg-card border border-border rounded-lg p-4">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-mono">{a.id.slice(0, 8)}</span>
                  <span>•</span>
                  <span className="capitalize">{a.kind.replace(/_/g, " ")}</span>
                  <span>•</span>
                  <span>{a.subject_entity_type}: {a.subject_entity_id}</span>
                </div>
                <pre className="mt-2 text-xs bg-muted/50 p-2 rounded overflow-x-auto">
                  {JSON.stringify(a.proposed_payload, null, 2)}
                </pre>
                {a.notes && <div className="mt-2 text-sm">{a.notes}</div>}
                <div className="mt-2 text-xs text-muted-foreground">
                  Proposed at {new Date(a.proposed_at).toLocaleString()}
                </div>
              </div>
              <div className="flex flex-col gap-2 shrink-0">
                <span className={`pill ${a.status === "pending" ? "pill-amber" : a.status === "approved" ? "pill-green" : "pill-red"}`}>{a.status}</span>
                {a.status === "pending" && (
                  <>
                    <button data-testid={`approve-${a.id}`} onClick={() => decide(a.id, "approved")} className="h-8 px-3 rounded bg-emerald-600 text-white text-sm">Approve</button>
                    <button data-testid={`reject-${a.id}`} onClick={() => decide(a.id, "rejected")} className="h-8 px-3 rounded bg-red-600 text-white text-sm">Reject</button>
                  </>
                )}
              </div>
            </div>
          </div>
        ))}
        {!items.length && <div className="text-sm text-muted-foreground">No {filter} approvals.</div>}
      </div>
    </div>
  );
}


// =====================================================================
// Pending suggestions Review Queue.
// =====================================================================
const ENTITY_LABELS = {
  daily_report: "Daily report",
  daily_report_activity: "DR activity",
  delay: "Delay",
  rfi: "RFI",
  qc_issue: "QC issue",
  safety_event: "Safety event",
  coordination_event: "Coordination event",
  delivery: "Delivery",
  photo_tag: "Photo tag",
  submittal: "Submittal",
  schedule_activity: "Schedule task",
};

const SOURCE_LABELS = {
  ai_copilot: "AI Copilot",
  ai_photo_vision: "Photo AI",
  ai_rfi_drafter: "RFI Drafter",
  ai_qc_analyzer: "QC Analyzer",
  ai_safety_analyzer: "Safety Analyzer",
  ai_schedule_analyzer: "Schedule Analyzer",
  system_inference: "System",
  calibration_rag: "Calibration RAG",
};

const SORT_OPTIONS = [
  { value: "created_at",       label: "Newest first" },
  { value: "confidence_score", label: "Confidence (high→low)" },
  { value: "entity_type",      label: "Entity type (A→Z)" },
];

// Entity -> URL deep link for the navigate-to-entity affordance.
// Routes here mirror the existing routes in App.jsx; unknown types
// just open the entity's collection page.
const ENTITY_ROUTES = {
  delay:               (id) => `/delays?openId=${id}`,
  rfi:                 (id) => `/rfis?openId=${id}`,
  qc_issue:            (id) => `/qc-issues?openId=${id}`,
  safety_event:        (id) => `/safety-events?openId=${id}`,
  coordination_event:  (id) => `/coordination?openId=${id}`,
  delivery:            (id) => `/deliveries?openId=${id}`,
  photo_tag:           (id) => `/photos?openId=${id}`,
  schedule_activity:   (id) => `/tasks?openId=${id}`,
  submittal:           (id) => `/submittals?openId=${id}`,
  daily_report:        (id) => `/daily-reports?openId=${id}`,
  daily_report_activity:
    (id) => `/daily-reports?openActivity=${id}`,
};

function PendingSuggestionsQueue() {
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [counts, setCounts] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [pageCursors, setPageCursors] = useState([null]);
  const [pageIdx, setPageIdx] = useState(0);
  // Filters
  const [entityFilter, setEntityFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [sortBy, setSortBy] = useState("created_at");
  const [selected, setSelected] = useState(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkReason, setBulkReason] = useState("");
  const [bulkRejectOpen, setBulkRejectOpen] = useState(false);

  const load = async (nextCursor = null) => {
    setLoading(true); setError(null);
    try {
      const params = {
        limit: 50,
        sort_by: sortBy,
      };
      if (entityFilter) params.entity_type = entityFilter;
      if (nextCursor) params.cursor = nextCursor;
      const { data } = await api.get("/users/current/pending-suggestions", { params });
      let r = data?.rows || [];
      // Client-side source filter (server doesn't filter by source).
      if (sourceFilter) r = r.filter((x) => x.suggested_by === sourceFilter);
      setRows(r);
      setCounts(data?.counts_by_entity_type || {});
      setCursor(data?.next_cursor || null);
      setSelected(new Set());
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "Failed to load review queue");
      setRows([]);
    } finally { setLoading(false); }
  };

  useEffect(() => {
    // Reset paging when filters/sort change.
    setPageIdx(0);
    setPageCursors([null]);
    load(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityFilter, sourceFilter, sortBy]);

  const goNext = () => {
    if (!cursor) return;
    const nextCursors = pageCursors.slice(0, pageIdx + 1);
    nextCursors.push(cursor);
    setPageCursors(nextCursors);
    setPageIdx(pageIdx + 1);
    load(cursor);
  };
  const goPrev = () => {
    if (pageIdx === 0) return;
    const prevCursor = pageCursors[pageIdx - 1];
    setPageIdx(pageIdx - 1);
    load(prevCursor);
  };

  const toggleSel = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };
  const toggleAll = () => {
    if (selected.size === rows.length) setSelected(new Set());
    else setSelected(new Set(rows.map((r) => r.id)));
  };

  const bulkConfirm = async () => {
    if (selected.size === 0) return;
    if (selected.size > 50) {
      toast.error("Max 50 suggestions per bulk operation");
      return;
    }
    setBulkBusy(true);
    try {
      const { data } = await api.post("/field-suggestions/bulk-confirm", {
        suggestion_ids: Array.from(selected),
      });
      const ok = data?.success_count || 0;
      const fail = data?.failure_count || 0;
      if (ok > 0) toast.success(`Confirmed ${ok} suggestion(s)`);
      if (fail > 0) toast.info(`${fail} skipped (race / role / not-found)`);
      await load(pageCursors[pageIdx]);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Bulk confirm failed");
    } finally { setBulkBusy(false); }
  };

  const bulkReject = async () => {
    if (selected.size === 0) return;
    if (selected.size > 50) {
      toast.error("Max 50 suggestions per bulk operation");
      return;
    }
    if (!bulkReason || bulkReason.trim().length < 10) {
      toast.error("Reject reason must be at least 10 characters");
      return;
    }
    setBulkBusy(true);
    try {
      const { data } = await api.post("/field-suggestions/bulk-reject", {
        suggestion_ids: Array.from(selected),
        reason: bulkReason.trim(),
      });
      const ok = data?.success_count || 0;
      const fail = data?.failure_count || 0;
      if (ok > 0) toast.success(`Rejected ${ok} suggestion(s)`);
      if (fail > 0) toast.info(`${fail} skipped (race / role / not-found)`);
      setBulkRejectOpen(false);
      setBulkReason("");
      await load(pageCursors[pageIdx]);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Bulk reject failed");
    } finally { setBulkBusy(false); }
  };

  const navigateToEntity = (row) => {
    const fn = ENTITY_ROUTES[row.entity_type];
    if (fn) navigate(fn(row.entity_id));
    else toast.info(`No deep link for ${row.entity_type} yet`);
  };

  const totalCount = useMemo(
    () => Object.values(counts).reduce((a, b) => a + b, 0),
    [counts],
  );

  return (
    <div className="space-y-3" data-testid="suggestions-queue">
      {/* Filters */}
      <div className="flex flex-wrap items-end gap-2 p-2 bg-slate-50 border border-slate-200 rounded text-sm">
        <Filter className="h-4 w-4 text-slate-500 mb-1.5" />
        <div>
          <label className="text-[10px] uppercase text-slate-500 block">Entity type</label>
          <select
            data-testid="suggestions-filter-entity"
            value={entityFilter}
            onChange={(e) => setEntityFilter(e.target.value)}
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs"
          >
            <option value="">(any)</option>
            {Object.entries(ENTITY_LABELS).map(([k, label]) => (
              <option key={k} value={k}>{label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-[10px] uppercase text-slate-500 block">AI source</label>
          <select
            data-testid="suggestions-filter-source"
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs"
          >
            <option value="">(any)</option>
            {Object.entries(SOURCE_LABELS).map(([k, label]) => (
              <option key={k} value={k}>{label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-[10px] uppercase text-slate-500 block">Sort by</label>
          <select
            data-testid="suggestions-sort"
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <button
          data-testid="suggestions-refresh"
          onClick={() => load(pageCursors[pageIdx])}
          disabled={loading}
          className="h-8 px-2 rounded border border-slate-300 bg-white text-xs inline-flex items-center gap-1 hover:bg-slate-50 ml-auto"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* Bulk-action bar (sticky on mobile via flex wrap) */}
      {rows.length > 0 && (
        <div
          data-testid="suggestions-bulk-bar"
          className="flex flex-wrap items-center gap-2 p-2 bg-blue-50 border border-blue-200 rounded text-xs"
        >
          <label className="inline-flex items-center gap-1">
            <input
              type="checkbox"
              data-testid="suggestions-select-all"
              checked={selected.size === rows.length && rows.length > 0}
              onChange={toggleAll}
            />
            <span>{selected.size} / {rows.length} selected on this page</span>
          </label>
          <button
            data-testid="suggestions-bulk-confirm"
            disabled={selected.size === 0 || bulkBusy}
            onClick={bulkConfirm}
            className="h-7 px-2 rounded bg-emerald-600 text-white text-xs inline-flex items-center gap-1 hover:bg-emerald-700 disabled:opacity-50"
          >
            {bulkBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
            Bulk Confirm
          </button>
          <button
            data-testid="suggestions-bulk-reject-open"
            disabled={selected.size === 0 || bulkBusy}
            onClick={() => setBulkRejectOpen(true)}
            className="h-7 px-2 rounded border border-rose-300 bg-white text-rose-700 text-xs inline-flex items-center gap-1 hover:bg-rose-50 disabled:opacity-50"
          >
            <X className="h-3 w-3" /> Bulk Reject
          </button>
          {selected.size > 50 && (
            <span className="text-rose-700 ml-1">Max 50 per call</span>
          )}
        </div>
      )}

      {/* Bulk reject inline dialog */}
      {bulkRejectOpen && (
        <div
          data-testid="suggestions-bulk-reject-dialog"
          className="rounded p-2 text-xs border border-rose-300 bg-rose-50 space-y-1"
        >
          <label className="block text-[10px] uppercase text-rose-800">
            Reason (10+ chars, applies to all {selected.size})
          </label>
          <textarea
            data-testid="suggestions-bulk-reason"
            value={bulkReason}
            onChange={(e) => setBulkReason(e.target.value)}
            rows={2}
            placeholder="Explain why all selected suggestions are wrong…"
            className="w-full p-1.5 text-xs rounded border border-rose-300 bg-white"
          />
          <div className="flex gap-1">
            <button
              data-testid="suggestions-bulk-reject-submit"
              disabled={bulkBusy}
              onClick={bulkReject}
              className="h-7 px-2 rounded bg-rose-600 text-white text-xs hover:bg-rose-700 disabled:opacity-50"
            >
              Submit reject
            </button>
            <button
              disabled={bulkBusy}
              onClick={() => { setBulkRejectOpen(false); setBulkReason(""); }}
              className="h-7 px-2 rounded border border-slate-300 bg-white text-xs"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Loading / empty / error states */}
      {loading && rows.length === 0 && (
        <div
          data-testid="suggestions-loading"
          className="space-y-2"
        >
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 bg-slate-100 rounded animate-pulse" />
          ))}
        </div>
      )}

      {error && !loading && (
        <div
          data-testid="suggestions-error"
          className="p-3 border border-rose-300 bg-rose-50 rounded text-sm text-rose-900"
        >
          <div className="font-medium mb-1">Couldn't load the review queue.</div>
          <div className="text-xs mb-2">{error}</div>
          <button
            data-testid="suggestions-retry"
            onClick={() => load(pageCursors[pageIdx])}
            className="h-7 px-2 rounded bg-rose-600 text-white text-xs"
          >
            Retry
          </button>
        </div>
      )}

      {!loading && !error && rows.length === 0 && (
        <div
          data-testid="suggestions-empty"
          className="p-6 text-center border border-dashed border-slate-300 rounded text-sm text-slate-500"
        >
          <Sparkles className="h-5 w-5 mx-auto mb-1 text-slate-400" />
          <div className="font-medium">No pending suggestions for you.</div>
          <div className="text-xs mt-1">
            AI suggestions land here when one of your projects has a producer
            that fires (Photo AI on tag, RFI drafter, schedule import…).
          </div>
        </div>
      )}

      {/* Rows */}
      {!loading && !error && rows.length > 0 && (
        <div className="space-y-2">
          {rows.map((s) => (
            <SuggestionRow
              key={s.id}
              row={s}
              selected={selected.has(s.id)}
              onToggle={() => toggleSel(s.id)}
              onNavigate={() => navigateToEntity(s)}
              onChanged={() => load(pageCursors[pageIdx])}
            />
          ))}
        </div>
      )}

      {/* Pagination */}
      {(rows.length > 0 || pageIdx > 0) && (
        <div
          data-testid="suggestions-pagination"
          className="flex items-center justify-between p-2 border-t border-slate-200 text-xs"
        >
          <div className="text-slate-600">
            Page {pageIdx + 1}
            {totalCount > 0 && (
              <span className="ml-2 text-slate-500">
                ({totalCount} on this page)
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <button
              data-testid="suggestions-page-prev"
              disabled={pageIdx === 0 || loading}
              onClick={goPrev}
              className="h-7 px-2 rounded border border-slate-300 bg-white text-xs inline-flex items-center gap-1 hover:bg-slate-50 disabled:opacity-50"
            >
              <ChevronLeft className="h-3 w-3" /> Prev
            </button>
            <button
              data-testid="suggestions-page-next"
              disabled={!cursor || loading}
              onClick={goNext}
              className="h-7 px-2 rounded border border-slate-300 bg-white text-xs inline-flex items-center gap-1 hover:bg-slate-50 disabled:opacity-50"
            >
              Next <ChevronsRight className="h-3 w-3" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SuggestionRow({ row, selected, onToggle, onNavigate, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");
  const entityLabel = ENTITY_LABELS[row.entity_type] || row.entity_type;
  const sourceLabel = SOURCE_LABELS[row.suggested_by] || row.suggested_by;
  const confidence = row.confidence_score == null
    ? null
    : Math.round(row.confidence_score * 100);

  const accept = async () => {
    setBusy(true);
    try {
      await api.post(`/field-suggestions/${row.id}/confirm`);
      toast.success("Confirmed");
      onChanged?.();
    } catch (e) {
      const detail = e.response?.data?.detail;
      const msg = typeof detail === "string" ? detail : (detail?.reason || "Confirm failed");
      toast.error(msg);
    } finally { setBusy(false); }
  };
  const reject = async () => {
    if (!reason || reason.trim().length < 10) {
      toast.error("Reject reason must be at least 10 characters");
      return;
    }
    setBusy(true);
    try {
      await api.post(`/field-suggestions/${row.id}/reject`, { reason: reason.trim() });
      toast.success("Rejected");
      setRejectOpen(false); setReason("");
      onChanged?.();
    } catch (e) {
      const detail = e.response?.data?.detail;
      const msg = typeof detail === "string" ? detail : (detail?.reason || "Reject failed");
      toast.error(msg);
    } finally { setBusy(false); }
  };

  // Render suggested_value compactly. JSON-stringify objects so the
  // user sees the shape; primitives + dates render verbatim.
  const renderValue = (v) => {
    if (v == null) return <em className="text-slate-500">—</em>;
    if (Array.isArray(v)) return v.map((x) => String(x)).join(", ");
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  };

  return (
    <div
      data-testid={`suggestion-row-${row.id}`}
      className="bg-white border border-slate-200 rounded p-2"
    >
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          data-testid={`suggestion-select-${row.id}`}
          checked={selected}
          onChange={onToggle}
          className="mt-1 shrink-0"
        />
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase">
            <span className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-900 border border-blue-200">
              {entityLabel}
            </span>
            <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200 inline-flex items-center gap-0.5">
              <Sparkles className="h-2.5 w-2.5" /> {sourceLabel}
            </span>
            {confidence != null && (
              <span className="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-800 border border-emerald-200">
                {confidence}%
              </span>
            )}
            <span className="text-slate-500 normal-case">
              {row.field_path}
            </span>
          </div>
          <div className="text-sm text-slate-800 mt-1 break-words"
            data-testid={`suggestion-value-${row.id}`}>
            {renderValue(row.suggested_value)}
          </div>
          {row.source_context && (
            <div className="text-[10px] text-slate-500 mt-0.5 italic truncate"
              title={row.source_context}>
              {row.source_context}
            </div>
          )}
          <div className="text-[10px] text-slate-400 mt-0.5">
            {new Date(row.created_at).toLocaleString()}
          </div>
        </div>
        <div className="flex flex-col gap-1 shrink-0">
          <button
            data-testid={`suggestion-accept-${row.id}`}
            onClick={accept} disabled={busy}
            className="h-7 px-2 rounded bg-emerald-600 text-white text-xs inline-flex items-center gap-1 hover:bg-emerald-700 disabled:opacity-50"
          >
            <Check className="h-3 w-3" /> Accept
          </button>
          <button
            data-testid={`suggestion-reject-${row.id}`}
            onClick={() => setRejectOpen(true)} disabled={busy}
            className="h-7 px-2 rounded border border-rose-300 bg-white text-rose-700 text-xs inline-flex items-center gap-1 hover:bg-rose-50 disabled:opacity-50"
          >
            <X className="h-3 w-3" /> Reject
          </button>
          <button
            data-testid={`suggestion-navigate-${row.id}`}
            onClick={onNavigate}
            className="h-7 px-2 rounded border border-slate-300 bg-white text-slate-700 text-xs inline-flex items-center gap-1 hover:bg-slate-50"
            title="Open the entity"
          >
            <ChevronRight className="h-3 w-3" /> Open
          </button>
        </div>
      </div>
      {rejectOpen && (
        <div className="mt-2 rounded p-2 text-xs border border-rose-300 bg-rose-50 space-y-1"
          data-testid={`suggestion-reject-dialog-${row.id}`}>
          <label className="block text-[10px] uppercase text-rose-800">
            Reason (10+ chars)
          </label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            placeholder="Why is this suggestion wrong?…"
            className="w-full p-1.5 text-xs rounded border border-rose-300 bg-white"
          />
          <div className="flex gap-1">
            <button
              onClick={reject} disabled={busy}
              className="h-7 px-2 rounded bg-rose-600 text-white text-xs hover:bg-rose-700 disabled:opacity-50"
            >
              Submit
            </button>
            <button
              disabled={busy}
              onClick={() => { setRejectOpen(false); setReason(""); }}
              className="h-7 px-2 rounded border border-slate-300 bg-white text-xs"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
