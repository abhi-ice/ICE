/** Schedule Proposals — list + detail review page.
 *
 * Federal-contract baseline-change workflow:
 *   1. Anyone uploads a schedule (Imports page) → creates a proposal
 *   2. Claude AI reviews the diff (risk_level, summary, flags)
 *   3. PM/Admin reviews this page → Approve (commits to tasks) or Reject
 *   4. Audit log captures every transition
 */
import { useEffect, useState, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { getUser } from "../lib/auth";
import { toast } from "../components/ui/sonner";
import {
  GitCompare, ChevronLeft, ShieldCheck, ShieldAlert, ShieldQuestion,
  CheckCircle2, XCircle, Sparkles, Plus, Minus, ArrowRight, Clock, RefreshCw,
  ListFilter, MessageCircle, Send,
} from "lucide-react";
// CPM Part G (2026-05-15) -- planned_start / planned_finish in the
// activity diff table rendered MM/DD/YYYY via chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";

const STATUS_PILL = {
  pending_review: "pill-blue",
  approved: "pill-green",
  rejected: "pill-red",
};

export default function ScheduleProposals() {
  const { activeId, projects } = useProjects();
  const [searchParams] = useSearchParams();
  const focusId = searchParams.get("id");
  const [proposals, setProposals] = useState([]);
  const [openId, setOpenId] = useState(focusId);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    if (!activeId) { setLoading(false); return; }
    setLoading(true);
    try {
      const { data } = await api.get(`/projects/${activeId}/schedule-proposals`);
      setProposals(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load proposals");
    } finally { setLoading(false); }
  }, [activeId]);
  useEffect(() => { reload(); }, [reload]);

  if (openId) {
    return <ProposalDetail id={openId} onBack={() => { setOpenId(null); reload(); }} />;
  }

  const proj = projects.find((p) => p.id === activeId);
  return (
    <div data-testid="schedule-proposals-page" className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <GitCompare className="h-5 w-5" /> Schedule changes
        </h1>
        <p className="text-sm text-muted-foreground">
          Every uploaded schedule is reviewed before its activities commit. The AI flags risk; a PM or admin approves.
          {proj && <> Active project: <strong>{proj.name}</strong>.</>}
        </p>
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table w-full">
          <thead>
            <tr>
              <th>Uploaded</th><th>File</th><th>Status</th><th>AI risk</th>
              <th>+</th><th>−</th><th>Δ dates</th><th>Δ duration</th><th></th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan="9" className="text-center py-6 text-sm text-muted-foreground">Loading…</td></tr>
            ) : !proposals.length ? (
              <tr><td colSpan="9" className="text-center py-6 text-sm text-muted-foreground">
                No schedule proposals yet. Upload a schedule on the Imports page to start.
              </td></tr>
            ) : proposals.map((p) => {
              const s = p.diff?.summary || {};
              const risk = p.ai_review?.risk_level || "—";
              return (
                <tr key={p.id} data-testid={`proposal-row-${p.id}`}>
                  <td className="text-xs">{new Date(p.uploaded_at).toLocaleString()}</td>
                  <td className="font-mono text-xs truncate max-w-[260px]">{p.filename}</td>
                  <td><span className={`pill ${STATUS_PILL[p.status] || "pill-slate"}`} data-testid={`proposal-status-${p.id}`}>{p.status.replace("_", " ")}</span></td>
                  <td><RiskPill risk={risk} /></td>
                  <td className="tabular-nums text-emerald-600">{s.added_count || 0}</td>
                  <td className="tabular-nums text-red-600">{s.dropped_count || 0}</td>
                  <td className="tabular-nums">{s.shifted_count || 0}{s.max_shift_days ? ` (max ${s.max_shift_days}d)` : ""}</td>
                  <td className="tabular-nums">{s.duration_changed_count || 0}</td>
                  <td><button data-testid={`open-proposal-${p.id}`} onClick={() => setOpenId(p.id)} className="text-sm text-primary hover:underline">Open</button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ProposalDetail({ id, onBack }) {
  const me = getUser();
  const navigate = useNavigate();
  const [doc, setDoc] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState("");

  const load = useCallback(async () => {
    try {
      const { data } = await api.get(`/schedule-proposals/${id}`);
      setDoc(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load proposal");
    }
  }, [id]);
  useEffect(() => { load(); }, [load]);

  if (!doc) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;

  const isReviewer = ["admin", "pm"].includes((me?.role || "").toLowerCase());
  const pending = doc.status === "pending_review";

  const approve = async () => {
    setBusy(true);
    try {
      const { data } = await api.post(`/schedule-proposals/${id}/approve`, { notes });
      const a = data.applied || {};
      toast.success(`Approved · created ${a.created || 0} · updated ${a.updated || 0} · dropped ${a.deleted || 0}`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Approve failed");
    } finally { setBusy(false); }
  };
  const reject = async () => {
    if (!notes.trim()) { toast.error("Add review notes explaining why."); return; }
    setBusy(true);
    try {
      await api.post(`/schedule-proposals/${id}/reject`, { notes });
      toast.success("Proposal rejected");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Reject failed");
    } finally { setBusy(false); }
  };
  const refreshAi = async () => {
    setBusy(true);
    try {
      const { data } = await api.post(`/schedule-proposals/${id}/refresh-ai-review`);
      setDoc((d) => ({ ...d, ai_review: data.ai_review }));
      toast.success("AI review refreshed");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Refresh failed");
    } finally { setBusy(false); }
  };

  const s = doc.diff?.summary || {};
  const ai = doc.ai_review || {};

  return (
    <div data-testid="proposal-detail" className="space-y-5">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="text-sm text-blue-600 hover:underline flex items-center gap-1">
          <ChevronLeft className="h-4 w-4" /> Back
        </button>
        <h1 className="text-xl font-semibold flex items-center gap-2 truncate">
          <GitCompare className="h-5 w-5" /> {doc.filename}
        </h1>
        <span className={`pill ${STATUS_PILL[doc.status] || "pill-slate"}`} data-testid="detail-status">
          {doc.status.replace("_", " ")}
        </span>
        <span className="text-xs text-slate-500 ml-auto">Uploaded {new Date(doc.uploaded_at).toLocaleString()}</span>
      </div>

      {/* AI REVIEW */}
      <div data-testid="ai-review-card" className="surface p-4">
        <div className="flex items-center gap-2 mb-2">
          <Sparkles className="h-4 w-4 text-violet-600" />
          <div className="font-medium text-sm">Claude review</div>
          <RiskPill risk={ai.risk_level || "—"} />
          {ai.recommendation && (
            <span className="text-xs text-slate-500 ml-2">
              recommendation: <strong>{ai.recommendation.replace(/_/g, " ")}</strong>
            </span>
          )}
          {ai.model && <span className="text-[10px] text-slate-400 ml-auto">{ai.model}</span>}
        </div>
        <div className="text-sm text-slate-700">{ai.summary || "No AI review available yet."}</div>
        {!!(ai.flags && ai.flags.length) && (
          <ul className="mt-3 space-y-1 text-xs text-slate-600 list-disc pl-5">
            {ai.flags.map((f, i) => <li key={i} data-testid={`ai-flag-${i}`}>{f}</li>)}
          </ul>
        )}
        {ai.justification_required && (
          <div className="mt-3 px-2 py-1.5 bg-amber-50 border border-amber-200 rounded text-xs text-amber-800">
            High-risk change — justification is required before approval.
          </div>
        )}
        <button data-testid="refresh-ai" onClick={refreshAi} disabled={busy}
          className="mt-3 h-7 px-2 rounded border border-slate-200 text-xs hover:bg-slate-50 flex items-center gap-1">
          <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} /> Re-run review
        </button>
      </div>

      {/* SUMMARY KPIs */}
      <div className="grid grid-cols-2 sm:grid-cols-6 gap-2">
        <Stat label="Current schedule" value={s.current_count || 0} />
        <Stat label="Incoming file" value={s.incoming_count || 0} />
        <Stat label="Added" value={s.added_count || 0} tone="emerald" />
        <Stat label="Dropped" value={s.dropped_count || 0} tone="red" />
        <Stat label="Date shifts >3d" value={s.shifted_count || 0} sub={s.max_shift_days ? `max ${s.max_shift_days}d` : ""} />
        <Stat label="Duration changes" value={s.duration_changed_count || 0} />
      </div>

      {/* PARSED ROWS (with summary toggles) — ONLY while pending */}
      {pending && doc.parsed_rows && doc.parsed_rows.length > 0 && (
        <ParsedRowsPanel doc={doc} reload={load} />
      )}

      {/* CHAT — let the user ask Claude to fix mis-categorised rows */}
      {pending && (
        <ChatPanel proposalId={id} onMutated={load} />
      )}

      {/* DIFF SECTIONS */}
      <DiffSection icon={Plus} tone="emerald" title="Added" rows={doc.diff?.added || []} testid="added"
        cols={[
          ["activity_code", "Code"], ["description", "Description"],
          ["planned_start", "Start"], ["planned_finish", "Finish"],
          ["duration_days", "Days"], ["trade_code", "Trade"],
        ]} />
      <DiffSection icon={Minus} tone="red" title="Dropped" rows={doc.diff?.dropped || []} testid="dropped"
        cols={[
          ["activity_code", "Code"], ["description", "Description"],
          ["planned_start", "Start"], ["planned_finish", "Finish"],
          ["pct_complete", "% complete"],
        ]} />
      <DiffSection icon={ArrowRight} tone="amber" title="Date shifts > 3 days" rows={doc.diff?.date_shifts || []} testid="shifts"
        cols={[
          ["activity_code", "Code"], ["description", "Description"],
          ["old_start", "Old start"], ["new_start", "New start"],
          ["old_finish", "Old finish"], ["new_finish", "New finish"],
          ["biggest_shift_days", "Shift (d)"],
        ]} />
      <DiffSection icon={Clock} tone="slate" title="Duration changes" rows={doc.diff?.duration_changes || []} testid="duration"
        cols={[
          ["activity_code", "Code"], ["description", "Description"],
          ["old_duration_days", "Old"], ["new_duration_days", "New"],
          ["delta_days", "Δ"],
        ]} />
      {!!(doc.diff?.predecessor_changes?.length) && (
        <DiffSection icon={GitCompare} tone="slate" title="Predecessor changes"
          rows={doc.diff.predecessor_changes} testid="preds"
          cols={[
            ["activity_code", "Code"], ["description", "Description"],
            ["old_predecessors", "Old preds"], ["new_predecessors", "New preds"],
          ]}
          render={{
            old_predecessors: (v) => (v || []).join(", ") || "—",
            new_predecessors: (v) => (v || []).join(", ") || "—",
          }} />
      )}

      {/* DECISION */}
      {pending && isReviewer && (
        <div className="surface p-4 space-y-3" data-testid="decision-card">
          <div className="font-medium text-sm flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-emerald-600" /> Decision
          </div>
          <textarea data-testid="decision-notes" placeholder="Review notes (required to reject; optional to approve)" value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="w-full h-20 px-3 py-2 rounded border border-slate-200 text-sm" />
          <div className="flex gap-2 justify-end">
            <button data-testid="reject-btn" onClick={reject} disabled={busy}
              className="h-9 px-4 rounded bg-red-600 text-white text-sm hover:bg-red-700 disabled:opacity-50 flex items-center gap-1">
              <XCircle className="h-4 w-4" /> Reject
            </button>
            <button data-testid="approve-btn" onClick={approve} disabled={busy}
              className="h-9 px-4 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4" /> Approve & apply
            </button>
          </div>
        </div>
      )}

      {pending && !isReviewer && (
        <div className="surface p-4 text-sm text-slate-600 flex items-center gap-2" data-testid="awaiting-pm">
          <ShieldQuestion className="h-4 w-4 text-blue-600" /> Awaiting PM/Admin approval. You don&apos;t have permission to approve.
        </div>
      )}

      {!pending && (
        <div className="surface p-4 text-sm" data-testid="decision-recap">
          <div className="font-medium mb-1 flex items-center gap-2">
            {doc.status === "approved" ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <XCircle className="h-4 w-4 text-red-600" />}
            {doc.status === "approved" ? "Approved" : "Rejected"} on {doc.reviewed_at ? new Date(doc.reviewed_at).toLocaleString() : "—"}
          </div>
          {doc.review_notes && <div className="text-slate-600">Notes: {doc.review_notes}</div>}
          {doc.applied_result && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
              <Stat label="Created" value={doc.applied_result.created || 0} tone="emerald" />
              <Stat label="Updated" value={doc.applied_result.updated || 0} />
              <Stat label="Dropped (soft-deleted)" value={doc.applied_result.deleted || 0} tone="red" />
              <Stat label="Skipped" value={(doc.applied_result.failed || []).length} tone={(doc.applied_result.failed || []).length ? "amber" : "slate"} />
            </div>
          )}
          {!!(doc.applied_result?.failed?.length) && (
            <details className="text-xs mt-2">
              <summary className="cursor-pointer text-amber-700">{doc.applied_result.failed.length} skipped row(s)</summary>
              <ul className="mt-2 space-y-1 list-disc pl-5">
                {doc.applied_result.failed.slice(0, 25).map((f, i) => <li key={i}><span className="font-mono">{f.row || "?"}</span>: {f.error}</li>)}
              </ul>
            </details>
          )}
          {doc.status === "approved" && (
            <button onClick={() => navigate("/tasks")} className="mt-3 text-sm text-blue-600 hover:underline">
              Go to Tasks →
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function RiskPill({ risk }) {
  const Icon = risk === "high" ? ShieldAlert : risk === "medium" ? ShieldQuestion : ShieldCheck;
  const klass = risk === "high" ? "pill-red" : risk === "medium" ? "pill-amber" : risk === "low" ? "pill-green" : "pill-slate";
  return <span className={`pill ${klass} inline-flex items-center gap-1`} data-testid={`risk-${risk}`}><Icon className="h-3 w-3" /> {risk}</span>;
}

function Stat({ label, value, sub, tone = "slate" }) {
  const klass = tone === "emerald" ? "text-emerald-700"
    : tone === "red" ? "text-red-700"
    : tone === "amber" ? "text-amber-700"
    : "text-slate-700";
  return (
    <div className="bg-slate-50 border border-slate-200 rounded p-2" data-testid={`kpi-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}>
      <div className="text-[10px] uppercase text-slate-500 tracking-wide">{label}</div>
      <div className={`font-semibold tabular-nums ${klass}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}

function DiffSection({ icon: Icon, tone, title, rows, cols, testid, render = {} }) {
  if (!rows.length) return null;
  const toneClass = tone === "emerald" ? "text-emerald-700"
    : tone === "red" ? "text-red-700"
    : tone === "amber" ? "text-amber-700"
    : "text-slate-700";
  return (
    <details className="surface p-0 overflow-hidden" data-testid={`section-${testid}`} open={rows.length <= 6}>
      <summary className="cursor-pointer p-3 flex items-center gap-2 hover:bg-slate-50">
        <Icon className={`h-4 w-4 ${toneClass}`} />
        <div className="font-medium text-sm">{title}</div>
        <span className="text-xs text-slate-500">({rows.length})</span>
      </summary>
      <div className="overflow-x-auto">
        <table className="dense-table w-full">
          <thead>
            <tr>{cols.map(([k, label]) => <th key={k}>{label}</th>)}</tr>
          </thead>
          <tbody>
            {rows.slice(0, 200).map((r, i) => (
              <tr key={r.activity_code || i}>
                {cols.map(([k]) => (
                  <td key={k} className="text-xs">
                    {render[k] ? render[k](r[k]) : (r[k] === null || r[k] === undefined || r[k] === "" ? "—" : String(r[k]))}
                  </td>
                ))}
              </tr>
            ))}
            {rows.length > 200 && <tr><td colSpan={cols.length} className="text-center text-xs text-slate-500 py-2">… {rows.length - 200} more not shown</td></tr>}
          </tbody>
        </table>
      </div>
    </details>
  );
}


// ------------------------------------------------------------
// Parsed-rows panel — checkbox per row to mark/unmark heading.
// ------------------------------------------------------------
function ParsedRowsPanel({ doc, reload }) {
  const rows = doc.parsed_rows || [];
  const excludedCount = rows.filter((r) => r.excluded_as_summary).length;
  const [busyCode, setBusyCode] = useState(null);

  const toggle = async (code, excluded) => {
    setBusyCode(code);
    try {
      await api.post(`/schedule-proposals/${doc.id}/toggle-rows`, {
        activity_codes: [code],
        excluded,
      });
      await reload();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Toggle failed");
    } finally { setBusyCode(null); }
  };

  return (
    <details data-testid="parsed-rows-panel" className="surface p-0 overflow-hidden" open>
      <summary className="cursor-pointer p-3 flex items-center gap-2 hover:bg-slate-50">
        <ListFilter className="h-4 w-4 text-slate-600" />
        <div className="font-medium text-sm">Parsed activities</div>
        <span className="text-xs text-slate-500">
          ({rows.length - excludedCount} task(s) · {excludedCount} hidden as heading)
        </span>
      </summary>
      <div className="px-2 pb-3 text-xs text-slate-500">
        Uncheck rows that are summary headings — they will be removed from the diff above and won&apos;t be applied. The auto-detected headings are already unchecked. You can also use the chat below to ask Claude to fix mistakes.
      </div>
      <div className="overflow-x-auto max-h-96 overflow-y-auto border-t border-slate-100">
        <table className="dense-table w-full">
          <thead className="sticky top-0 bg-white">
            <tr>
              <th className="w-12">Task?</th>
              <th>Code</th>
              <th>Description</th>
              <th>Start</th>
              <th>Finish</th>
              <th>Days</th>
              <th>Reason if hidden</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.activity_code}
                data-testid={`parsed-row-${r.activity_code}`}
                className={r.excluded_as_summary ? "bg-amber-50/50 text-slate-500" : ""}>
                <td className="text-center">
                  <input data-testid={`row-checkbox-${r.activity_code}`}
                    type="checkbox"
                    checked={!r.excluded_as_summary}
                    disabled={busyCode === r.activity_code}
                    onChange={(e) => toggle(r.activity_code, !e.target.checked)} />
                </td>
                <td className="font-mono text-xs">{r.activity_code}</td>
                <td className="text-xs truncate max-w-[280px]">{r.description}</td>
                <td className="text-xs font-mono">{formatDateDisplay(r.planned_start)}</td>
                <td className="text-xs font-mono">{formatDateDisplay(r.planned_finish)}</td>
                <td className="tabular-nums text-xs">{r.duration_days ?? "—"}</td>
                <td className="text-xs text-amber-700 italic">{r.summary_reason || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// ------------------------------------------------------------
// Chat panel — one Claude turn per message. Saves credits.
// ------------------------------------------------------------
function ChatPanel({ proposalId, onMutated }) {
  const [thread, setThread] = useState([
    { role: "ai", text: "Hi — I am here to help clean up the parsed schedule. Tell me which rows are headings (e.g., 'remove row 1.21 it is a heading') or ask any question about the diff. Each message uses one Claude credit." },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    if (!input.trim() || busy) return;
    const msg = input.trim();
    setThread((t) => [...t, { role: "user", text: msg }]);
    setInput("");
    setBusy(true);
    try {
      const { data } = await api.post(`/schedule-proposals/${proposalId}/chat`, { message: msg });
      const tool = (data.exclude?.length || data.include?.length)
        ? `Action: ${data.exclude?.length ? `excluded ${data.exclude.length}` : ""}${data.include?.length ? ` · included ${data.include.length}` : ""}`
        : null;
      setThread((t) => [...t, { role: "ai", text: data.reply || "(no reply)", tool }]);
      if (data.mutated) await onMutated();
    } catch (e) {
      setThread((t) => [...t, { role: "ai", text: "Chat error: " + (e.response?.data?.detail || e.message) }]);
    } finally { setBusy(false); }
  };

  return (
    <div data-testid="chat-panel" className="surface p-4">
      <div className="flex items-center gap-2 mb-3">
        <MessageCircle className="h-4 w-4 text-blue-600" />
        <div className="font-medium text-sm">Chat with Claude about this proposal</div>
        <span className="text-[10px] text-slate-400 ml-auto">1 message = 1 LLM call</span>
      </div>
      <div className="space-y-2 max-h-72 overflow-y-auto pr-1 mb-3">
        {thread.map((m, i) => (
          <div key={i} data-testid={`chat-msg-${i}`}
            className={`text-sm ${m.role === "user" ? "ml-12 bg-blue-50 border-blue-100" : "mr-12 bg-slate-50 border-slate-100"} border rounded-lg px-3 py-2`}>
            <div>{m.text}</div>
            {m.tool && <div className="mt-1 text-[10px] text-emerald-700 font-mono">{m.tool}</div>}
          </div>
        ))}
        {busy && <div className="text-xs text-slate-400 ml-2 animate-pulse">Claude is thinking…</div>}
      </div>
      <div className="flex gap-2">
        <input data-testid="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") send(); }}
          placeholder='e.g. "1.21 is a heading, hide it"'
          disabled={busy}
          className="flex-1 h-9 px-3 rounded border border-slate-200 text-sm" />
        <button data-testid="chat-send" onClick={send} disabled={busy || !input.trim()}
          className="h-9 px-3 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1">
          <Send className="h-3.5 w-3.5" /> Send
        </button>
      </div>
    </div>
  );
}
