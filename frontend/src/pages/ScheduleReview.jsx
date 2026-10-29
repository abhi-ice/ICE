import { useCallback, useEffect, useState } from "react";
import { Wand2, Check, X, Loader2, RefreshCw, GitBranch } from "lucide-react";
import api, { extractApiError } from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";

const predText = (preds) => (preds || []).map((p) => (typeof p === "string" ? p
  : `${p.code} ${p.type || "FS"}${p.lag_days ? (p.lag_days > 0 ? `+${p.lag_days}` : p.lag_days) : ""}`)).join(", ") || "none";

function NameRow({ s, value, onChange, onReject }) {
  return (
    <tr className="border-b border-border/60" data-testid={`name-row-${s.id}`}>
      <td className="py-2 pr-3 text-xs opacity-70 whitespace-nowrap">{s.activity_code}</td>
      <td className="py-2 pr-3 text-sm opacity-80">{s.current_name}</td>
      <td className="py-2 pr-3">
        <input data-testid={`name-input-${s.id}`} value={value} onChange={(e) => onChange(e.target.value)}
          className="w-full h-9 px-2.5 rounded-md border border-input bg-background text-sm focus:outline-none focus:ring-2 focus:ring-ring" />
      </td>
      <td className="py-2 pr-3 text-[11px] opacity-60">{s.source === "ai" ? "AI" : s.source === "dates" ? "Dates" : "Headings"}</td>
      <td className="py-2 text-right">
        <button type="button" data-testid={`name-reject-${s.id}`} onClick={onReject} title="Reject"
          className="h-8 w-8 inline-flex items-center justify-center rounded-md hover:bg-red-500/10 text-red-400"><X className="h-4 w-4" /></button>
      </td>
    </tr>
  );
}

const areaOf = (name) => ((name || "").includes(" › ") ? name.split(" › ")[0].trim() : "Other");

function AreaGroup({ area, list, edits, setEdits, onReject, onApprove, busy }) {
  const [areaText, setAreaText] = useState(area);
  const [open, setOpen] = useState(true);
  const renameArea = () => {
    const next = areaText.trim();
    if (!next || next === area || area === "Other") return;
    setEdits((e) => {
      const out = { ...e };
      list.forEach((s) => {
        const cur = e[s.id] ?? s.suggested_name;
        out[s.id] = cur.startsWith(`${area} › `) ? `${next} › ${cur.slice(area.length + 3)}` : cur;
      });
      return out;
    });
  };
  const slug = area.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return (
    <div className="rounded-xl border border-border" data-testid={`names-area-${slug}`}>
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 border-b border-border/60">
        <button type="button" onClick={() => setOpen(!open)} className="text-xs opacity-70 w-5" data-testid={`names-area-toggle-${slug}`}>{open ? "▾" : "▸"}</button>
        {area === "Other" ? <span className="font-medium">Other</span> : (
          <input data-testid={`names-area-input-${slug}`} value={areaText} onChange={(e) => setAreaText(e.target.value)} onBlur={renameArea}
            title="Rename this area for every activity in it"
            className="h-9 px-2.5 rounded-md border border-input bg-background text-sm font-medium min-w-[200px] focus:outline-none focus:ring-2 focus:ring-ring" />
        )}
        <span className="text-xs opacity-60">{list.length} activit{list.length === 1 ? "y" : "ies"}</span>
        <button type="button" data-testid={`names-area-approve-${slug}`} onClick={onApprove} disabled={busy}
          className="ml-auto h-9 px-3 rounded-md bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-500 disabled:opacity-50 inline-flex items-center gap-1.5 transition-colors">
          <Check className="h-3.5 w-3.5" /> Approve this area</button>
      </div>
      {open && (
        <div className="overflow-x-auto px-4">
          <table className="w-full text-left">
            <thead><tr className="text-[11px] uppercase tracking-wide opacity-60">
              <th className="py-2 pr-3">Code</th><th className="py-2 pr-3">Current name</th><th className="py-2 pr-3 w-[45%]">Suggested name</th><th className="py-2 pr-3">Source</th><th /></tr></thead>
            <tbody>{list.map((s) => (
              <NameRow key={s.id} s={s} value={edits[s.id] ?? s.suggested_name}
                onChange={(v) => setEdits((e) => ({ ...e, [s.id]: v }))} onReject={() => onReject(s.id)} />))}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function DigestTab({ projectId }) {
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.get(`/projects/${projectId}/schedule-digest/preview`).then(({ data }) => setD(data))
      .catch((e) => { setD({ error: true }); toast.error(extractApiError(e, "Couldn't load digest")); });
  }, [projectId]);
  const sendNow = async () => {
    setBusy(true);
    try {
      const { data } = await api.post(`/projects/${projectId}/schedule-digest/send-now`);
      if (data.sent.length) toast.success(`Digest sent to ${data.sent.length} people`);
      if (data.failed.length) toast.error(`Email failed for ${data.failed.length} — check the email provider key`);
      if (!data.sent.length && !data.failed.length) toast.error("No admins or project managers to send to");
    } catch (e) { toast.error(extractApiError(e, "Send failed")); } finally { setBusy(false); }
  };
  if (!d) return <Loader2 className="h-5 w-5 animate-spin" />;
  if (d.error) return null;
  const Sec = ({ title, items, render, empty, tid }) => (
    <div data-testid={tid}><div className="text-sm font-semibold mb-1.5">{title} ({items.length})</div>
      {items.length ? <ul className="space-y-1 text-sm">{items.map((x, i) => <li key={i} className="rounded-md border border-border/60 px-3 py-1.5">{render(x)}</li>)}</ul>
        : <div className="text-sm opacity-60">{empty}</div>}</div>
  );
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm opacity-75 flex-1 min-w-[260px]">Emailed every Friday at 7 AM site time to: <b>{d.recipients.join(", ") || "no admins / PMs yet"}</b></p>
        <button type="button" data-testid="digest-send-now" onClick={sendNow} disabled={busy}
          className="h-10 px-4 rounded-md border border-emerald-500/50 text-emerald-400 text-sm font-medium hover:bg-emerald-500/10 disabled:opacity-50 inline-flex items-center gap-2">
          {busy && <Loader2 className="h-4 w-4 animate-spin" />} Send it now</button>
      </div>
      <Sec tid="digest-logic" title="Logic changes waiting" items={d.logic_changes} empty="None"
        render={(x) => <><b>{x.activity}</b> · {x.kind === "out_of_order" ? "work out of order" : "unplanned work"}</>} />
      <Sec tid="digest-delays" title="Open delays" items={d.open_delays} empty="None"
        render={(x) => <><b>{x.activities.join(", ") || "Delay"}</b> · {x.impact_days} impact day(s) · since {x.since}</>} />
      <Sec tid="digest-slipped" title="Slipped activities" items={d.slipped} empty="Nothing has slipped"
        render={(x) => <><b>{x.activity}</b> · {x.slip_days} working day(s) late · {x.planned_finish} → {x.projected_finish}{x.critical ? " · critical" : ""}</>} />
    </div>
  );
}

function NamesTab({ projectId }) {
  const [rows, setRows] = useState(null);
  const [edits, setEdits] = useState({});
  const [job, setJob] = useState({ status: "idle" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const [{ data }, { data: j }] = await Promise.all([
      api.get(`/projects/${projectId}/activity-names`), api.get(`/projects/${projectId}/activity-names/job`)]);
    setRows(data); setJob(j); setEdits({});
  }, [projectId]);
  useEffect(() => { load().catch((e) => toast.error(extractApiError(e, "Couldn't load"))); }, [load]);
  useEffect(() => {
    if (job.status !== "running") return undefined;
    const t = setInterval(() => load().catch(() => {}), 4000);
    return () => clearInterval(t);
  }, [job.status, load]);

  const generate = async () => {
    try { await api.post(`/projects/${projectId}/activity-names/generate`); setJob({ status: "running" }); toast.success("AI is naming the activities…"); }
    catch (e) { toast.error(extractApiError(e, "Couldn't start")); }
  };
  const groups = Object.entries((rows || []).reduce((acc, r) => {
    const a = areaOf(r.suggested_name); (acc[a] = acc[a] || []).push(r); return acc;
  }, {})).sort(([a], [b]) => (a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b)));
  const approve = async (list) => {
    setBusy(true);
    try {
      const items = list.map((s) => ({ id: s.id, name: (edits[s.id] ?? s.suggested_name) }));
      const { data } = await api.post("/activity-name-suggestions/approve", { items });
      toast.success(`${data.approved} names approved`); load();
    } catch (e) { toast.error(extractApiError(e, "Approve failed")); } finally { setBusy(false); }
  };
  const reject = async (id) => {
    await api.post(`/activity-name-suggestions/${id}/reject`);
    setRows((r) => r.filter((x) => x.id !== id));
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm opacity-75 flex-1 min-w-[260px]">AI reads your schedule's headings and order, then suggests a clear name for each field activity, so duplicates like "Electrical" are easy to tell apart. Edit any name, then approve.</p>
        <button type="button" data-testid="names-generate" onClick={generate} disabled={job.status === "running"}
          className="h-10 px-4 rounded-md border border-emerald-500/50 text-emerald-400 text-sm font-medium hover:bg-emerald-500/10 disabled:opacity-50 inline-flex items-center gap-2 transition-colors">
          {job.status === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {job.status === "running" ? "Naming…" : "Suggest names"}</button>
        <button type="button" data-testid="names-approve-all" onClick={() => approve(rows)} disabled={busy || !rows?.length}
          className="h-10 px-4 rounded-md bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-500 disabled:opacity-50 inline-flex items-center gap-2 transition-colors">
          <Check className="h-4 w-4" /> Approve {rows?.length || 0}</button>
      </div>
      {job.status === "failed" && <div className="text-sm text-red-400" data-testid="names-job-error">Naming failed: {job.error}</div>}
      {rows === null ? <Loader2 className="h-5 w-5 animate-spin" /> : rows.length === 0 ? (
        <div className="text-sm opacity-70 py-10 text-center" data-testid="names-empty">No suggestions waiting. Click “Suggest names” to have AI go through the schedule.</div>
      ) : (
        <div className="space-y-4" data-testid="names-groups">
          {groups.map(([area, list]) => (
            <AreaGroup key={area} area={area} list={list} edits={edits} setEdits={setEdits}
              onReject={reject} onApprove={() => approve(list)} busy={busy} />
          ))}
        </div>
      )}
    </div>
  );
}

function LogicCard({ p, onDecided }) {
  const [text, setText] = useState(predText(p.suggested_predecessors));
  const [busy, setBusy] = useState(false);
  const parse = () => text.split(",").map((x) => x.trim()).filter(Boolean).filter((x) => x !== "none").map((x) => {
    const m = x.match(/^(\S+)\s*(FS|SS|FF|SF)?\s*([+-]\d+)?$/i);
    if (!m) throw new Error(`Can't read "${x}" — use e.g. D100 SS+2`);
    return { code: m[1], type: (m[2] || "FS").toUpperCase(), lag_days: parseInt(m[3] || "0", 10) };
  });
  const decide = async (approve) => {
    setBusy(true);
    try {
      if (approve) await api.post(`/logic-proposals/${p.id}/approve`, { predecessors: parse() });
      else await api.post(`/logic-proposals/${p.id}/reject`, {});
      toast.success(approve ? "Logic change applied — schedule recalculated" : "Proposal rejected");
      onDecided();
    } catch (e) { toast.error(e.message?.startsWith("Can't") ? e.message : extractApiError(e, "Failed")); } finally { setBusy(false); }
  };
  return (
    <div className="rounded-xl border border-border p-4 space-y-2" data-testid={`logic-card-${p.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <GitBranch className="h-4 w-4 text-emerald-400" />
        <span className="font-medium">{p.activity_name}</span>
        <span className="text-xs opacity-60">{p.activity_code}</span>
        <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-400">{p.kind === "out_of_order" ? "Work out of order" : "Unplanned work"}</span>
        <span className="text-[11px] opacity-60 ml-auto">{p.ai_status === "pending" ? "AI drafting…" : p.ai_status === "done" ? "AI drafted" : "Rule-based draft"}</span>
      </div>
      <p className="text-sm opacity-80">{p.rationale}</p>
      <div className="text-xs opacity-70">Current predecessors: <b>{predText(p.current_predecessors)}</b></div>
      <label className="block text-xs opacity-70">New predecessors (edit if needed, e.g. <code>D100 SS+2, D090 FS</code>)</label>
      <input data-testid={`logic-input-${p.id}`} value={text} onChange={(e) => setText(e.target.value)}
        className="w-full h-9 px-2.5 rounded-md border border-input bg-background text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring" />
      <div className="flex justify-end gap-2 pt-1">
        <button type="button" data-testid={`logic-reject-${p.id}`} disabled={busy} onClick={() => decide(false)}
          className="h-9 px-4 rounded-md border border-input text-sm hover:bg-muted transition-colors">Reject</button>
        <button type="button" data-testid={`logic-approve-${p.id}`} disabled={busy} onClick={() => decide(true)}
          className="h-9 px-4 rounded-md bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-500 disabled:opacity-50 inline-flex items-center gap-2">
          {busy && <Loader2 className="h-4 w-4 animate-spin" />} Approve &amp; apply</button>
      </div>
    </div>
  );
}

function LogicTab({ projectId }) {
  const [rows, setRows] = useState(null);
  const load = useCallback(() => api.get(`/projects/${projectId}/logic-proposals`).then(({ data }) => setRows(data))
    .catch((e) => { setRows([]); toast.error(extractApiError(e, "Couldn't load")); }), [projectId]);
  useEffect(() => { load(); }, [load]);
  if (rows === null) return <Loader2 className="h-5 w-5 animate-spin" />;
  if (!rows.length) return <div className="text-sm opacity-70 py-10 text-center" data-testid="logic-empty">No logic changes waiting. They appear here when daily reports show work happening out of order or unplanned work.</div>;
  return <div className="space-y-3">{rows.map((p) => <LogicCard key={p.id} p={p} onDecided={load} />)}</div>;
}

export default function ScheduleReview() {
  const { activeId, projects } = useProjects();
  const project = (projects || []).find((p) => p.id === activeId);
  return (
    <div className="max-w-6xl space-y-6" data-testid="schedule-review-page">
      <div>
        <h1 className="text-3xl font-semibold flex items-center gap-3"><Wand2 className="h-7 w-7 text-emerald-400" /> Schedule review</h1>
        <p className="text-sm opacity-70 mt-1">{project ? project.name : "Pick a project"} · AI suggestions wait here for your approval. Nothing changes until you approve.</p>
      </div>
      {!activeId ? <div className="text-sm opacity-70">Select a project first.</div> : (
        <Tabs defaultValue="logic">
          <TabsList>
            <TabsTrigger value="logic" data-testid="tab-logic">Logic changes</TabsTrigger>
            <TabsTrigger value="names" data-testid="tab-names">Activity names</TabsTrigger>
            <TabsTrigger value="digest" data-testid="tab-digest">Weekly digest</TabsTrigger>
          </TabsList>
          <TabsContent value="logic" className="mt-4"><LogicTab projectId={activeId} /></TabsContent>
          <TabsContent value="names" className="mt-4"><NamesTab projectId={activeId} /></TabsContent>
          <TabsContent value="digest" className="mt-4"><DigestTab projectId={activeId} /></TabsContent>
        </Tabs>
      )}
    </div>
  );
}
