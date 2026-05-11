import { useEffect, useState } from "react";
import api from "../lib/api";
import { ShieldCheck, Bot, FileText, History, ShoppingBasket, FileBarChart, Settings as SettingsIcon, MapPin } from "lucide-react";
import { useProjects } from "../lib/projects";

// Bucket H Phase 5 (2026-05-08) -- audit op filter dropdown options.
// Grouped for legibility; "(all)" sentinel left as empty string so the
// query-param wire format stays clean. New RBAC + resource-leveling
// ops are listed in the resource_leveling group per spec.
const AUDIT_OP_GROUPS = [
  {
    label: "Common",
    ops: ["INSERT", "UPDATE", "DELETE"],
  },
  {
    label: "Daily reports",
    ops: ["SUPPLEMENT_FILED", "EVENT_EDITED_POST_SUBMIT",
           "EDIT_REQUEST_FILED", "EDIT_REQUEST_APPROVED",
           "EDIT_REQUEST_DENIED", "NO_WORK_DAY_SUBMITTED",
           "DR_AS_PARENT_ORPHAN_DROP"],
  },
  {
    label: "Photos",
    ops: ["PHOTO_TAG_CREATED", "PHOTO_CLASS_CHANGE",
           "PHOTO_TRAY_ARCHIVE"],
  },
  {
    label: "QC + cascade",
    ops: ["QC_REWORK_DELAY_CREATED", "QC_REWORK_OUTCOME_RECORDED",
           "QC_REWORK_CASCADE_FAILED", "CASCADE_RECOMPUTE_BATCH",
           "GATING_ENTITY_DELETE"],
  },
  {
    label: "RBAC (project membership)",
    ops: ["PROJECT_MEMBER_ADDED", "PROJECT_MEMBER_REMOVED",
           "PROJECT_MEMBER_ROLE_CHANGED", "PROJECT_MEMBER_BACKFILL"],
  },
  {
    label: "Resource leveling",
    ops: ["CREW_CAPACITY_OVERRIDE", "RESOURCE_LEVELING_RECOMPUTED",
           "RESOURCE_LEVELING_RECOMPUTE_FAILED", "CREW_REALLOCATED",
           "RESOURCE_LEVELING_BACKFILL",
           "CAPACITY_ACTUALIZED_FROM_PRESENCE"],
  },
  {
    label: "Auth + admin",
    ops: ["ADMIN_PASSWORD_RESET", "OPERATOR_PASSWORD_RESET",
           "LINKAGE_FIELD_MIGRATED", "RESET_BOUNDARY"],
  },
];

// AuditLog page — append-only, with hash-chain integrity check button.
export function AuditLog() {
  const [rows, setRows] = useState([]);
  const [verify, setVerify] = useState(null);
  // Phase 5 -- op + table filters with query-param wiring.
  const [opFilter, setOpFilter] = useState("");
  const [tableFilter, setTableFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    try {
      const params = { limit: 200 };
      if (opFilter) params.op = opFilter;
      if (tableFilter) params.table_name = tableFilter;
      const { data } = await api.get("/audit-log", { params });
      setRows(Array.isArray(data) ? data : []);
    } catch { setRows([]); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [opFilter, tableFilter]);
  const check = async () => {
    const { data } = await api.get("/audit-log/verify");
    setVerify(data);
  };
  return (
    <div data-testid="audit-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <History className="h-5 w-5" />
          <h1 className="text-2xl font-semibold">Audit log</h1>
        </div>
        <button data-testid="verify-chain" onClick={check} className="h-9 px-3 rounded bg-primary text-primary-foreground text-sm">
          Verify hash chain
        </button>
      </div>
      {verify && (
        <div data-testid="verify-result" className={`p-3 border rounded text-sm ${verify.valid ? "bg-emerald-50 text-emerald-800 border-emerald-200" : "bg-red-50 text-red-800 border-red-200"}`}>
          {verify.valid ? `✓ Chain valid. Verified ${verify.entries_checked} entries.` : `✗ Chain broken at ${verify.broken_at}`}
        </div>
      )}
      {/* Phase 5 -- filter row. */}
      <div className="flex flex-wrap items-end gap-2 p-2 bg-slate-50 border border-slate-200 rounded text-sm">
        <div>
          <label className="text-[10px] uppercase text-slate-500 block">Op</label>
          <select data-testid="audit-filter-op" value={opFilter}
            onChange={(e) => setOpFilter(e.target.value)}
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs">
            <option value="">— all ops —</option>
            {AUDIT_OP_GROUPS.map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.ops.map((op) => <option key={op} value={op}>{op}</option>)}
              </optgroup>
            ))}
          </select>
        </div>
        <div>
          <label className="text-[10px] uppercase text-slate-500 block">Table name</label>
          <input data-testid="audit-filter-table" value={tableFilter}
            onChange={(e) => setTableFilter(e.target.value)}
            placeholder="e.g. crew_capacity"
            className="h-8 px-2 rounded border border-slate-300 text-xs w-48" />
        </div>
        {(opFilter || tableFilter) && (
          <button onClick={() => { setOpFilter(""); setTableFilter(""); }}
            data-testid="audit-filter-clear"
            className="h-8 px-3 rounded bg-slate-200 text-slate-700 text-xs hover:bg-slate-300">
            Clear
          </button>
        )}
        <div className="ml-auto text-xs text-slate-500">
          {loading ? "Loading…" : `${rows.length} entr${rows.length === 1 ? "y" : "ies"}`}
        </div>
      </div>
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead><tr><th>When</th><th>Op</th><th>Table</th><th>Record</th><th>Actor</th><th>Hash</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="font-mono text-xs">{new Date(r.created_at).toLocaleString()}</td>
                <td><span className={`pill ${r.op === "INSERT" ? "pill-green" : r.op === "UPDATE" ? "pill-blue" : "pill-red"}`}>{r.op}</span></td>
                <td className="font-mono text-xs">{r.table_name}</td>
                <td className="font-mono text-xs truncate max-w-[180px]">{r.record_id}</td>
                <td className="font-mono text-xs truncate max-w-[140px]">{r.actor_id || "—"}</td>
                <td className="font-mono text-[10px] text-muted-foreground truncate max-w-[160px]">{r.hash?.slice(0, 24)}…</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan="6" className="text-center py-6 text-sm text-muted-foreground">No audit entries.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function AiAgentsPage() {
  return (
    <div data-testid="agents-page" className="space-y-4">
      <div className="flex items-center gap-2"><Bot className="h-5 w-5" /><h1 className="text-2xl font-semibold">AI agents</h1></div>
      <p className="text-sm text-muted-foreground">
        Multi-step agent workflows. SUBSTITUTION: master prompt §6 specified LangGraph; on Emergent we
        use Anthropic via emergentintegrations.LlmChat with explicit graph-node functions.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {[
          { id: "copilot", label: "Copilot", desc: "General-purpose Q&A over project context. Open via FAB.", status: "live" },
          { id: "recovery", label: "Recovery engine", desc: "Schedule-slip recovery suggestions + apply. Visit /recovery.", status: "live" },
          { id: "rfi_drafter", label: "RFI drafter", desc: "Draft formal RFI responses citing spec sections.", status: "live" },
          { id: "ingest_schedule", label: "Schedule ingest", desc: "Single source of truth shared with synchronous import.", status: "live" },
          { id: "ingest_spec", label: "Spec ingestion", desc: "MasterFormat detection + section extraction + auto-submittals.", status: "live" },
          { id: "ingest_drawing", label: "Drawing ingest", desc: "OCR sheet metadata, auto-create drawings.", status: "stub" },
          { id: "voice_annotation", label: "Voice annotation", desc: "Whisper transcribe + Claude structured-field extractor.", status: "live" },
          { id: "work_item_assistant", label: "Work item assistant", desc: "Analyze field events → propose work items.", status: "live" },
          { id: "change_order_assistant", label: "Change order assistant", desc: "RFI + scope → CO breakdown via cost-rate library.", status: "live" },
          { id: "polygon_suggester", label: "Polygon suggester", desc: "Claude vision suggests room polygons.", status: "stub" },
        ].map((a) => (
          <div key={a.id} data-testid={`agent-card-${a.id}`} className="bg-card border border-border rounded-lg p-4">
            <div className="flex items-center justify-between">
              <div className="font-medium">{a.label}</div>
              <span className={`pill ${a.status === "live" ? "pill-green" : "pill-amber"}`}>{a.status}</span>
            </div>
            <div className="text-sm text-muted-foreground mt-1">{a.desc}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Compliance() {
  return (
    <div data-testid="compliance-page" className="space-y-4">
      <div className="flex items-center gap-2"><ShieldCheck className="h-5 w-5" /><h1 className="text-2xl font-semibold">Compliance</h1></div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {[
          { code: "AC-2", label: "Account management", evidence: "tenant_users + JWT", state: "Implemented" },
          { code: "AU-2", label: "Audit events", evidence: "audit_log + hash chain", state: "Implemented" },
          { code: "AU-9", label: "Audit log integrity", evidence: "SHA-256 chain, append-only", state: "Implemented" },
          { code: "IA-2", label: "Identification & auth", evidence: "JWT HS256 + bcrypt", state: "Implemented" },
          { code: "SC-13", label: "Cryptographic protection", evidence: "bcrypt rounds=12, JWT HS256", state: "Implemented" },
          { code: "SC-28", label: "Data at rest", evidence: "MongoDB instance encryption (deploy-level)", state: "Doc" },
          { code: "AC-3", label: "Access enforcement", evidence: "Tenant filter on every query", state: "Implemented" },
          { code: "AT-2", label: "Security awareness", evidence: "Tenant-scoped onboarding", state: "Doc" },
          { code: "IR-4", label: "Incident handling", evidence: "Field-issues + safety events", state: "Implemented" },
        ].map((c) => (
          <div key={c.code} className="bg-card border border-border rounded-lg p-4">
            <div className="flex items-center justify-between">
              <div className="font-mono text-xs text-muted-foreground">NIST 800-171 / {c.code}</div>
              <span className={`pill ${c.state === "Implemented" ? "pill-green" : "pill-amber"}`}>{c.state}</span>
            </div>
            <div className="font-medium mt-1">{c.label}</div>
            <div className="text-sm text-muted-foreground mt-1">{c.evidence}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Marketplace() {
  return (
    <div data-testid="marketplace-page" className="space-y-4">
      <div className="flex items-center gap-2"><ShoppingBasket className="h-5 w-5" /><h1 className="text-2xl font-semibold">Marketplace</h1></div>
      <div className="bg-card border border-border rounded-lg p-6 text-sm text-muted-foreground">
        Cross-tenant postings (private / network / union hall / public visibility) per master prompt §19.
        Schema and API stubbed; activation requires tenant network onboarding.
      </div>
    </div>
  );
}

export function Settings() {
  const [tenant, setTenant] = useState(null);
  useEffect(() => { api.get("/tenants/current").then(({ data }) => setTenant(data)); }, []);
  return (
    <div data-testid="settings-page" className="space-y-4">
      <div className="flex items-center gap-2"><SettingsIcon className="h-5 w-5" /><h1 className="text-2xl font-semibold">Settings</h1></div>
      {tenant && (
        <div className="bg-card border border-border rounded-lg p-4 space-y-2 text-sm">
          <div><span className="text-muted-foreground">Legal name:</span> <b>{tenant.legal_name}</b></div>
          <div><span className="text-muted-foreground">Display name:</span> {tenant.display_name}</div>
          <div><span className="text-muted-foreground">Slug:</span> <code>{tenant.slug}</code></div>
          <div><span className="text-muted-foreground">Tenant type:</span> {tenant.type}</div>
          <div><span className="text-muted-foreground">CMMC level:</span> {tenant.cmmc_level}</div>
          <div><span className="text-muted-foreground">Data region:</span> {tenant.data_region || "us_east_commercial"}</div>
          <div><span className="text-muted-foreground">Work item label:</span> {tenant.work_item_label}</div>
          <div><span className="text-muted-foreground">Status:</span> <span className="pill pill-green">{tenant.status}</span></div>
        </div>
      )}
    </div>
  );
}

export function WeeklyReports() {
  const { activeId, projects } = useProjects();
  const [week, setWeek] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - d.getDay()); // Sunday
    return d.toISOString().slice(0, 10);
  });
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/weekly-rollup?week_starting=${week}`).then(({ data }) => setData(data));
  }, [activeId, week]);

  const downloadPdf = async () => {
    const res = await api.get(`/projects/${activeId}/weekly-rollup/pdf?week_starting=${week}`, { responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url; a.download = `weekly_${week}.pdf`; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div data-testid="weekly-reports-page" className="space-y-4">
      <div className="flex items-center gap-2"><FileBarChart className="h-5 w-5" /><h1 className="text-2xl font-semibold">Weekly rollup</h1></div>
      <p className="text-sm text-muted-foreground">{projects.find(p => p.id === activeId)?.name}</p>
      <div className="flex items-center gap-2">
        <label className="field-label mb-0">Week starting</label>
        <input type="date" value={week} onChange={(e) => setWeek(e.target.value)} className="h-8 px-2 rounded border border-input" />
        <button data-testid="weekly-pdf" onClick={downloadPdf} className="ml-2 h-8 px-3 rounded bg-blue-600 text-white text-xs">Download PDF</button>
      </div>
      {data && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat label="Reports" value={data.daily_report_count} />
          <Stat label="PPC" value={`${data.ppc}%`} />
          <Stat label="Hours" value={data.total_hours_worked} />
          <Stat label="Delay min" value={data.delay_minutes} />
          <Stat label="Weather min" value={data.weather_impact_minutes} />
          <Stat label="Activities completed" value={`${data.activities_completed} / ${data.total_planned_activities}`} />
        </div>
      )}
      {data?.hours_by_trade && (
        <div className="bg-card border border-border rounded-lg p-4">
          <div className="font-medium text-sm mb-2">Hours by trade</div>
          <div className="space-y-1 text-sm">
            {Object.entries(data.hours_by_trade).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between">
                <div>{k}</div>
                <div className="font-mono">{v.toFixed(1)} hrs</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

const Stat = ({ label, value }) => (
  <div className="bg-card border border-border rounded-lg p-4">
    <div className="text-xs text-muted-foreground">{label}</div>
    <div className="text-2xl font-semibold tabular-nums mt-1">{value}</div>
  </div>
);
