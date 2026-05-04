import { useEffect, useState } from "react";
import api from "../lib/api";
import { ShieldCheck, Bot, FileText, History, ShoppingBasket, FileBarChart, Settings as SettingsIcon, MapPin } from "lucide-react";
import { useProjects } from "../lib/projects";

// AuditLog page — append-only, with hash-chain integrity check button.
export function AuditLog() {
  const [rows, setRows] = useState([]);
  const [verify, setVerify] = useState(null);
  useEffect(() => { api.get("/audit-log?limit=200").then(({ data }) => setRows(data)); }, []);
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
          { id: "recovery", label: "Recovery engine", desc: "Schedule-slip recovery suggestions. Visit /recovery.", status: "live" },
          { id: "rfi_drafter", label: "RFI drafter", desc: "Draft formal RFI responses citing spec sections.", status: "live" },
          { id: "ingest_schedule", label: "Schedule ingest", desc: "Single source of truth shared with synchronous import.", status: "live" },
          { id: "ingest_spec", label: "Spec ingestion", desc: "MasterFormat detection + section extraction.", status: "stub" },
          { id: "ingest_drawing", label: "Drawing ingest", desc: "OCR sheet metadata, auto-create drawings.", status: "stub" },
          { id: "voice_annotation", label: "Voice annotation", desc: "Transcribe + extract structured fields.", status: "stub" },
          { id: "work_item_assistant", label: "Work item assistant", desc: "Analyze field events → propose work items.", status: "stub" },
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
  return (
    <div data-testid="weekly-reports-page" className="space-y-4">
      <div className="flex items-center gap-2"><FileBarChart className="h-5 w-5" /><h1 className="text-2xl font-semibold">Weekly rollup</h1></div>
      <p className="text-sm text-muted-foreground">{projects.find(p => p.id === activeId)?.name}</p>
      <div className="flex items-center gap-2">
        <label className="field-label mb-0">Week starting</label>
        <input type="date" value={week} onChange={(e) => setWeek(e.target.value)} className="h-8 px-2 rounded border border-input" />
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
