import { useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import {
  ShieldCheck, Bot, FileText, History, ShoppingBasket, FileBarChart,
  Settings as SettingsIcon, MapPin, Sparkles, AlertTriangle,
  RefreshCw, Loader2, ShieldAlert, Activity, Check, X,
} from "lucide-react";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

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
  // Bucket F Phase 5 (2026-05-11) -- Photo AI op family. Surfacing
  // these in the filter dropdown lets compliance + admins audit AI
  // activity without grepping the raw audit feed.
  {
    label: "Photo AI",
    ops: [
      "PHOTO_AI_ANALYZED",
      "PHOTO_AI_ANALYSIS_FAILED",
      "PHOTO_AI_OVERRIDDEN",
      "PHOTO_AI_REJECTED",
      "PHOTO_AI_BUDGET_REACHED",
      "PHOTO_AI_BUDGET_UPDATED",
      "PHOTO_AI_CUI_BLOCKED",
      "PHOTO_AI_CUI_COMPLIANCE_REPORT_PULLED",
      "PHOTO_AI_BACKFILL",
      "PHOTO_AI_OVERRIDE_SNAPSHOT_BACKFILL",
    ],
  },
  // Bucket G Phase 5 (2026-05-11) -- Jigsaw field_suggestions op
  // family. Surfacing every lifecycle op so compliance can audit
  // "AI suggested X, human did Y" without grepping raw audit feed.
  {
    label: "Jigsaw suggestions",
    ops: [
      "FIELD_SUGGESTIONS_INITIALIZED",
      "FIELD_SUGGESTION_CREATED",
      "FIELD_SUGGESTION_CONFIRMED",
      "FIELD_SUGGESTION_REJECTED",
      "FIELD_SUGGESTION_SUPERSEDED",
      "FIELD_SUGGESTION_EXPIRED",
      "FIELD_SUGGESTIONS_SCHEDULE_TRADE_INFERENCE",
    ],
  },
  // Bucket E Phase 5 (2026-05-11) -- Submittal SLA op family. Lets
  // compliance + admins audit SLA breach + chain-of-custody events
  // without grepping the audit feed.
  {
    label: "Submittal SLA",
    ops: [
      "SUBMITTAL_SLA_INITIALIZED",
      "SUBMITTAL_SLA_BREACHED",
      "SUBMITTAL_SLA_PAUSED",
      "SUBMITTAL_SLA_RESUMED",
      "SUBMITTAL_SLA_REVIEW_EVENT_RECORDED",
      "SUBMITTAL_SLA_REVIEWER_ASSIGNED",
      "SUBMITTAL_SLA_AGING_RECOMPUTED",
    ],
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

// Bucket F Phase 5 -- Settings page now has tabs. AI Usage tab surfaces
// the cost dashboard, monthly budget controls, performance percentiles,
// and the CUI compliance report (admin only).
export function Settings() {
  const [tenant, setTenant] = useState(null);
  const [tab, setTab] = useState("general");
  useEffect(() => { api.get("/tenants/current").then(({ data }) => setTenant(data)); }, []);
  const role = (tenant?.user_role || "").toLowerCase();
  // The /tenants/current endpoint doesn't include the caller's role
  // explicitly today; we fall back to checking the AI budget endpoint
  // (admin-only). If that 200s, the user is admin.
  const [isAdmin, setIsAdmin] = useState(false);
  useEffect(() => {
    api.get("/tenants/current/ai-budget")
      .then(() => setIsAdmin(true))
      .catch(() => setIsAdmin(false));
  }, []);
  return (
    <div data-testid="settings-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <SettingsIcon className="h-5 w-5" />
        <h1 className="text-2xl font-semibold">Settings</h1>
      </div>

      <div className="flex border-b border-slate-200 text-sm">
        <SettingsTab tab="general" active={tab} onClick={setTab} label="General" />
        {isAdmin && (
          <SettingsTab tab="ai_usage" active={tab} onClick={setTab}
            label="AI Usage" icon={Sparkles} testid="settings-tab-ai-usage" />
        )}
      </div>

      {tab === "general" && tenant && (
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

      {tab === "ai_usage" && isAdmin && <AiUsageTab />}
      {tab === "ai_usage" && !isAdmin && (
        <div className="bg-amber-50 border border-amber-200 rounded p-3 text-sm text-amber-900">
          AI Usage settings are admin-only.
        </div>
      )}
    </div>
  );
}

function SettingsTab({ tab, active, onClick, label, icon: Icon, testid }) {
  const isActive = tab === active;
  return (
    <button data-testid={testid || `settings-tab-${tab}`}
      onClick={() => onClick(tab)}
      className={`px-3 py-2 -mb-px border-b-2 inline-flex items-center gap-1.5 ${
        isActive
          ? "border-blue-600 text-blue-900 font-medium"
          : "border-transparent text-slate-600 hover:text-slate-900"
      }`}>
      {Icon && <Icon className="h-3.5 w-3.5" />} {label}
    </button>
  );
}

// =====================================================================
// AI Usage tab
// =====================================================================
function AiUsageTab() {
  const [budget, setBudget] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [timings, setTimings] = useState(null);
  const [compliance, setCompliance] = useState(null);
  // Bucket G Phase 5 -- per-source Jigsaw acceptance-rate stats.
  const [sourceStats, setSourceStats] = useState(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(false);
  const [editingBudget, setEditingBudget] = useState(false);

  const loadAll = async () => {
    setLoading(true);
    try {
      const [b, d, t, ss] = await Promise.all([
        api.get("/tenants/current/ai-budget"),
        api.get("/tenants/current/photo-ai-cost-dashboard", { params: { days } }),
        api.get("/tenants/current/photo-ai-timings", { params: { days: Math.min(days, 90) } }),
        api.get("/tenants/current/suggestion-source-stats", { params: { days } })
            .catch(() => ({ data: null })),
      ]);
      setBudget(b.data);
      setDashboard(d.data);
      setTimings(t.data);
      setSourceStats(ss.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load AI usage data");
    } finally {
      setLoading(false);
    }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadAll(); }, [days]);

  const loadCompliance = async () => {
    try {
      const { data } = await api.get(
        "/tenants/current/cui-compliance-report", { params: { days: 90 } },
      );
      setCompliance(data);
      toast.success(
        data.is_clean
          ? "Compliance report: clean."
          : `Compliance report: ${data.compliance_flag_count} flag(s) need review.`,
      );
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load compliance report");
    }
  };

  // Posture banner: 100% -> rose pause banner, 80%+ -> amber warning,
  // otherwise nothing.
  const pct = budget?.pct_consumed ?? 0;
  const paused = budget && !budget.budget_ok;
  return (
    <div className="space-y-4" data-testid="settings-ai-usage">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-blue-700" />
          <h2 className="text-lg font-medium">Photo AI</h2>
          <button data-testid="ai-help-open" onClick={() => setHelpOpen(true)}
            className="ml-2 text-xs text-blue-700 hover:underline inline-flex items-center gap-1">
            <FileText className="h-3 w-3" /> How does this work?
          </button>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-[10px] uppercase text-slate-500">Window</label>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs">
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
          <button onClick={loadAll} disabled={loading}
            data-testid="ai-usage-refresh"
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs inline-flex items-center gap-1 hover:bg-slate-50">
            <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Budget posture banner */}
      {paused && (
        <div data-testid="ai-budget-paused-banner"
          className="bg-rose-50 border border-rose-300 rounded p-3 text-sm text-rose-900 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-medium">AI analyses paused.</div>
            <div className="text-xs mt-0.5">
              {budget?.reason === "tenant_ai_disabled"
                ? "AI is disabled for this tenant. Toggle it back on below."
                : "Monthly budget reached. Raise the cap below or wait for the 1st."}
              {budget?.reset_at && (
                <> Resets {new Date(budget.reset_at).toLocaleDateString()}.</>
              )}
            </div>
          </div>
        </div>
      )}
      {!paused && pct >= 80 && (
        <div data-testid="ai-budget-warning-banner"
          className="bg-amber-50 border border-amber-300 rounded p-3 text-sm text-amber-900 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-medium">Approaching monthly cap ({pct}%).</div>
            <div className="text-xs mt-0.5">
              {budget?.remaining_usd !== undefined && `$${budget.remaining_usd?.toFixed(2)} remaining of `}
              ${budget?.monthly_limit_usd?.toFixed?.(2) || budget?.monthly_limit?.toFixed?.(2) || "—"}.
              Consider raising the cap before new analyses start pausing.
            </div>
          </div>
        </div>
      )}

      {/* Budget controls */}
      <div className="bg-card border border-border rounded p-4 space-y-3">
        <div className="text-sm font-medium">Monthly budget</div>
        {budget && (
          <BudgetControls budget={budget} editing={editingBudget}
            setEditing={setEditingBudget} onSaved={loadAll} />
        )}
      </div>

      {/* Cost dashboard */}
      {dashboard && <CostDashboardCard dashboard={dashboard} />}

      {/* Performance percentiles */}
      {timings && <TimingPercentilesCard timings={timings} />}

      {/* Bucket G Phase 5 -- Suggestion Acceptance Rates card. */}
      {sourceStats && <SuggestionSourceStatsCard stats={sourceStats} />}

      {/* CUI compliance */}
      <div className="bg-card border border-border rounded p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium inline-flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-700" />
            CUI compliance report
          </div>
          <button onClick={loadCompliance} data-testid="ai-cui-report-pull"
            className="h-8 px-2 rounded border border-slate-300 bg-white text-xs hover:bg-slate-50">
            Pull report (last 90 days)
          </button>
        </div>
        {compliance ? (
          <CuiComplianceReport report={compliance} />
        ) : (
          <div className="text-xs text-slate-500">
            Generates a compliance roll-up of CUI projects, their tray AI
            activity, and PHOTO_AI_CUI_BLOCKED audit entries — for federal
            audit evidence. Pulling the report writes an audit op recording
            who looked at it and when.
          </div>
        )}
      </div>

      {helpOpen && <AiHelpPanel onClose={() => setHelpOpen(false)} />}
    </div>
  );
}

function BudgetControls({ budget, editing, setEditing, onSaved }) {
  const [val, setVal] = useState(
    Number(budget?.monthly_limit_usd ?? budget?.monthly_limit ?? 100),
  );
  const [enabled, setEnabled] = useState(Boolean(budget?.ai_enabled));
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setVal(Number(budget?.monthly_limit_usd ?? budget?.monthly_limit ?? 100));
    setEnabled(Boolean(budget?.ai_enabled));
  }, [budget]);
  const save = async () => {
    setBusy(true);
    try {
      await api.patch("/tenants/current/ai-budget", {
        ai_enabled: enabled,
        ai_monthly_budget_usd: Number(val),
      });
      toast.success("Budget updated");
      setEditing(false);
      onSaved?.();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    } finally {
      setBusy(false);
    }
  };
  const pct = Number(budget?.pct_consumed ?? 0);
  const barColor =
    pct >= 100 ? "bg-rose-500"
    : pct >= 80 ? "bg-amber-500"
    : "bg-emerald-500";
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <KpiTile label="Spend this month" value={`$${(budget?.current_spend ?? 0).toFixed(2)}`} />
        <KpiTile label="Cap" value={`$${(budget?.monthly_limit_usd ?? budget?.monthly_limit ?? 0).toFixed(2)}`} />
        <KpiTile label="Remaining" value={`$${(budget?.remaining_usd ?? 0).toFixed(2)}`} />
        <KpiTile label="% consumed" value={`${pct}%`}
          tone={pct >= 100 ? "rose" : pct >= 80 ? "amber" : "emerald"} />
      </div>
      <div className="w-full h-2 bg-slate-200 rounded-full overflow-hidden">
        <div className={`h-full ${barColor} transition-all`}
          style={{ width: `${Math.min(100, pct)}%` }}
          data-testid="ai-budget-bar" />
      </div>
      <div className="text-[10px] text-slate-500">
        Resets {budget?.reset_at ? new Date(budget.reset_at).toLocaleDateString() : "first of next month"}.
        Spend tracks the actual Anthropic input + output token cost; pHash
        reuse never adds to spend.
      </div>
      {!editing ? (
        <button data-testid="ai-budget-edit"
          onClick={() => setEditing(true)}
          className="h-8 px-3 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
          Edit
        </button>
      ) : (
        <div className="bg-slate-50 border border-slate-200 rounded p-3 space-y-2">
          <div>
            <label className="text-[10px] uppercase text-slate-500 block">Monthly cap (USD)</label>
            <input data-testid="ai-budget-input" type="number" min="0" step="10"
              value={val} onChange={(e) => setVal(e.target.value)}
              className="h-8 px-2 rounded border border-slate-300 text-sm w-32" />
          </div>
          <div>
            <label className="inline-flex items-center gap-2 text-xs">
              <input data-testid="ai-budget-enabled" type="checkbox"
                checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              AI enabled (master kill switch)
            </label>
          </div>
          <div className="flex gap-2">
            <button onClick={save} disabled={busy} data-testid="ai-budget-save"
              className="h-8 px-3 rounded bg-blue-600 text-white text-xs hover:bg-blue-700 disabled:opacity-50">
              {busy ? "Saving…" : "Save"}
            </button>
            <button onClick={() => setEditing(false)} disabled={busy}
              className="h-8 px-3 rounded border border-slate-300 bg-white text-xs hover:bg-slate-50">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function KpiTile({ label, value, tone }) {
  const toneCls =
    tone === "rose" ? "text-rose-700"
    : tone === "amber" ? "text-amber-700"
    : tone === "emerald" ? "text-emerald-700"
    : "text-slate-700";
  return (
    <div className="border border-slate-200 rounded p-2 bg-white">
      <div className="text-[10px] uppercase text-slate-500">{label}</div>
      <div className={`text-base font-semibold tabular-nums mt-0.5 ${toneCls}`}>{value}</div>
    </div>
  );
}

function CostDashboardCard({ dashboard }) {
  const days = dashboard.window_days;
  const perDay = dashboard.per_day || [];
  const maxUsd = Math.max(0.001, ...perDay.map((d) => d.usd || 0));
  return (
    <div className="bg-card border border-border rounded p-4 space-y-3">
      <div className="text-sm font-medium">Cost dashboard ({days}d)</div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <KpiTile label="Analyses" value={dashboard.analyses_complete} />
        <KpiTile label="pHash reused" value={dashboard.phash_reused_count} tone="emerald" />
        <KpiTile label="Tokens (in/out)" value={`${dashboard.input_tokens_total} / ${dashboard.output_tokens_total}`} />
        <KpiTile label="Est. cost" value={`$${(dashboard.estimated_cost_usd || 0).toFixed(2)}`} />
      </div>
      {perDay.length > 0 && (
        <div data-testid="ai-cost-per-day"
          className="border border-slate-200 rounded p-2 bg-white">
          <div className="text-[10px] uppercase text-slate-500 mb-2">Spend per day</div>
          <div className="flex items-end gap-0.5 h-20">
            {perDay.map((d) => (
              <div key={d.date} title={`${d.date}: $${d.usd.toFixed(3)} (${d.analyses} analyses)`}
                className="flex-1 bg-blue-500/70 hover:bg-blue-700 transition-colors min-w-[2px]"
                style={{ height: `${(d.usd / maxUsd) * 100}%` }} />
            ))}
          </div>
          <div className="flex justify-between text-[9px] text-slate-400 mt-1">
            <span>{perDay[0]?.date}</span>
            <span>{perDay[perDay.length - 1]?.date}</span>
          </div>
        </div>
      )}
      {dashboard.per_model?.length > 0 && (
        <div data-testid="ai-cost-per-model">
          <div className="text-[10px] uppercase text-slate-500 mb-1">By model version</div>
          <div className="space-y-1">
            {dashboard.per_model.map((m) => (
              <div key={m.model} className="flex items-center justify-between text-xs border-b border-slate-100 py-1">
                <code className="text-slate-700">{m.model}</code>
                <div className="flex items-center gap-3 text-slate-600">
                  <span>{m.analyses} analyses</span>
                  <span className="font-mono">${m.usd.toFixed(3)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {dashboard.top_cost_trays?.length > 0 && (
        <details data-testid="ai-cost-top-trays" className="text-xs">
          <summary className="cursor-pointer text-slate-700 hover:underline">
            Top {dashboard.top_cost_trays.length} most-expensive trays
          </summary>
          <ul className="mt-1 space-y-0.5">
            {dashboard.top_cost_trays.map((t) => (
              <li key={t.tray_id} className="flex justify-between border-b border-slate-100 py-1">
                <code className="text-slate-600 truncate max-w-[260px]">{t.tray_id}</code>
                <span className="font-mono">${t.usd.toFixed(4)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function TimingPercentilesCard({ timings }) {
  const fmt = (ms) => ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`;
  return (
    <div className="bg-card border border-border rounded p-4 space-y-2"
      data-testid="ai-timings-card">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium inline-flex items-center gap-1.5">
          <Activity className="h-3.5 w-3.5 text-blue-700" />
          Vision call performance ({timings.window_days}d)
        </div>
        <div className="text-[10px] text-slate-500">
          n = {timings.sample_size}
        </div>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <KpiTile label="P50" value={fmt(timings.p50_ms)} />
        <KpiTile label="P95" value={fmt(timings.p95_ms)} tone={timings.p95_ms > 15000 ? "amber" : undefined} />
        <KpiTile label="P99" value={fmt(timings.p99_ms)} tone={timings.p99_ms > 25000 ? "rose" : undefined} />
        <KpiTile label="Max" value={fmt(timings.max_ms)} />
      </div>
      {timings.by_status && Object.keys(timings.by_status).length > 0 && (
        <div className="text-[10px] uppercase text-slate-500 mt-2">By status</div>
      )}
      {timings.by_status && (
        <div className="space-y-0.5 text-xs">
          {Object.entries(timings.by_status).map(([s, v]) => (
            <div key={s} className="flex justify-between border-b border-slate-100 py-0.5">
              <span className="text-slate-700">{s}</span>
              <span className="font-mono text-slate-600">{v.n} calls · P50 {fmt(v.p50_ms)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Bucket G Phase 5 -- per-AI-source acceptance-rate roll-up. Drives
// the "is this AI producer actually useful?" feedback signal.
function SuggestionSourceStatsCard({ stats }) {
  const rows = stats?.per_source || [];
  const overall = stats?.overall || {};
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
  // Acceptance-rate tone -- mirrors the Jigsaw confidence palette so
  // operators read the visual language consistently.
  const rateTone = (pct) => {
    if (pct >= 75) return "bg-emerald-100 text-emerald-900 border-emerald-300";
    if (pct >= 50) return "bg-sky-100 text-sky-900 border-sky-300";
    if (pct >= 25) return "bg-amber-100 text-amber-900 border-amber-300";
    return "bg-rose-100 text-rose-900 border-rose-300";
  };
  return (
    <div className="bg-card border border-border rounded p-4 space-y-3"
      data-testid="ai-suggestion-source-stats">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium inline-flex items-center gap-1.5">
          <Sparkles className="h-3.5 w-3.5 text-blue-700" />
          Suggestion acceptance rates ({stats.window_days}d)
        </div>
        <div className="text-[10px] text-slate-500">
          {overall.total_created || 0} created · {overall.confirmed_count || 0} confirmed
          · {overall.rejected_count || 0} rejected
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-slate-500 italic">
          No AI suggestions in the last {stats.window_days} days.
        </div>
      ) : (
        <div className="space-y-2">
          {rows.map((r) => {
            const pct = r.acceptance_pct || 0;
            return (
              <div key={r.source}
                data-testid={`ai-source-stats-row-${r.source}`}
                className="border border-slate-200 rounded p-2 bg-white">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-medium text-slate-800">
                    {SOURCE_LABELS[r.source] || r.source}
                  </span>
                  <span className={`px-1.5 py-0.5 rounded border text-[10px] ${rateTone(pct)}`}>
                    {pct}% accepted
                  </span>
                  <span className="text-slate-500 text-[10px]">
                    ({r.confirmed_count}/{r.confirmed_count + r.rejected_count} decided)
                  </span>
                  {r.pending_count > 0 && (
                    <span className="text-[10px] text-slate-500">
                      · {r.pending_count} pending
                    </span>
                  )}
                  {r.expired_count > 0 && (
                    <span className="text-[10px] text-slate-400">
                      · {r.expired_count} expired
                    </span>
                  )}
                  {r.avg_confidence_at_acceptance > 0 && (
                    <span className="text-[10px] text-slate-500 ml-auto">
                      avg conf at accept:&nbsp;
                      {Math.round(r.avg_confidence_at_acceptance * 100)}%
                    </span>
                  )}
                </div>
                {/* Visual progress bar so trends are scannable. */}
                <div className="mt-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                  <div
                    className={
                      pct >= 75 ? "h-full bg-emerald-500" :
                      pct >= 50 ? "h-full bg-sky-500" :
                      pct >= 25 ? "h-full bg-amber-500" :
                      "h-full bg-rose-500"
                    }
                    style={{ width: `${Math.min(100, pct)}%` }}
                  />
                </div>
                {/* Top 3 rejection reasons -- the calibration feedback. */}
                {r.top_rejection_reasons?.length > 0 && (
                  <details className="mt-1.5">
                    <summary className="text-[10px] cursor-pointer text-slate-600 hover:underline">
                      Top {r.top_rejection_reasons.length} rejection reason(s)
                    </summary>
                    <ul className="mt-1 space-y-0.5">
                      {r.top_rejection_reasons.map((rr, idx) => (
                        <li key={idx} className="text-[10px] text-rose-800 border-l-2 border-rose-200 pl-2">
                          "{rr.reason}" <span className="text-slate-500">×{rr.count}</span>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function CuiComplianceReport({ report }) {
  return (
    <div className="space-y-2 text-xs" data-testid="ai-cui-report">
      <div className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium ${
        report.is_clean
          ? "bg-emerald-100 text-emerald-900 border border-emerald-300"
          : "bg-rose-100 text-rose-900 border border-rose-300"
      }`}>
        {report.is_clean
          ? <><Check className="h-3 w-3" /> Clean — no violations</>
          : <><ShieldAlert className="h-3 w-3" /> {report.compliance_flag_count} flag(s)</>}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <KpiTile label="CUI projects" value={report.cui_project_count} />
        <KpiTile label="CUI blocks enforced" value={report.cui_blocks_enforced_count} tone="emerald" />
        <KpiTile label="Non-CUI completes" value={report.non_cui_completes_in_window} />
        <KpiTile label="Generated" value={new Date(report.generated_at).toLocaleString()} />
      </div>
      {report.compliance_flags?.length > 0 && (
        <details>
          <summary className="cursor-pointer text-rose-700 hover:underline">
            Show {report.compliance_flags.length} flag(s)
          </summary>
          <ul className="mt-1 space-y-0.5">
            {report.compliance_flags.slice(0, 30).map((f) => (
              <li key={f.tray_id} className={`border-l-2 pl-2 py-0.5 ${
                f.severity === "violation" ? "border-rose-400" : "border-amber-400"
              }`}>
                <code className="text-slate-600">{f.tray_id}</code>
                {" · "}status={f.status}
                {f.reason && <> · reason={f.reason}</>}
                <span className="ml-1 text-[9px] uppercase text-slate-500">[{f.severity}]</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// In-app help panel. Surfaces from the AI Usage tab + the PhotoTray
// "?" button. Single source of truth so help copy doesn't drift.
export function AiHelpPanel({ onClose }) {
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-end md:items-center justify-center p-4"
      onClick={onClose} data-testid="ai-help-panel">
      <div className="bg-white max-w-2xl w-full rounded-lg shadow-xl max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between p-3 border-b border-slate-200 sticky top-0 bg-white">
          <div className="text-sm font-medium inline-flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-blue-700" />
            How Photo AI works
          </div>
          <button onClick={onClose}
            className="text-slate-400 hover:text-slate-700"
            data-testid="ai-help-close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-4 text-sm space-y-3 text-slate-700">
          <section>
            <div className="font-medium text-slate-900">What it does</div>
            <p>
              Every photo uploaded to the tray gets analyzed by Claude Sonnet 4.5
              Vision. The AI suggests a photo class (progress / discovery / damage /
              quality), a caption, an event type (delay / RFI / QC issue / safety
              event), a trade, and any safety concerns it spots.
            </p>
          </section>
          <section>
            <div className="font-medium text-slate-900">Privacy + CUI</div>
            <p>
              Projects flagged <code>is_cui=true</code> never have their photos sent
              to AI. The CUI check fires before any image leaves the server, and a
              <code className="px-1 mx-0.5 bg-slate-100 rounded">PHOTO_AI_CUI_BLOCKED</code>
              audit row is written for proof of enforcement. The kill switches:
              tenant <code>ai_enabled</code>, project <code>ai_enabled</code>,
              and project <code>is_cui</code>. Any one of those = no AI call.
            </p>
          </section>
          <section>
            <div className="font-medium text-slate-900">Cost</div>
            <p>
              Pricing tracks Anthropic actual: $3 per 1M input tokens, $15 per 1M output
              tokens. A typical photo costs ≈ $0.005–$0.01. pHash dedup means re-uploads
              of the same image (common on construction sites — same pour, different
              angles) reuse a prior analysis for zero cost.
            </p>
          </section>
          <section>
            <div className="font-medium text-slate-900">Budget</div>
            <p>
              Set a monthly cap; when spend hits 80% you get a warning banner; at 100%
              new analyses pause until the 1st (or until you raise the cap). pHash
              reuses never count against the cap. The budget resets on the 1st of
              every month automatically.
            </p>
          </section>
          <section>
            <div className="font-medium text-slate-900">Override / Reject</div>
            <p>
              When the AI gets it wrong, override the specific fields with a reason
              (5+ chars) or reject the whole suggestion (10+ chars). Both feed the
              <code className="px-1 mx-0.5 bg-slate-100 rounded">photo_ai_calibration</code>
              collection so future fine-tuning can learn from your corrections.
              The original AI suggestion stays on the row for evidence.
            </p>
          </section>
          <section>
            <div className="font-medium text-slate-900">Performance</div>
            <p>
              Single-photo analysis takes 3–8 seconds end-to-end. The dashboard
              tracks P50/P95/P99 so operators can spot regressions. Vision calls
              time out at 30 seconds; failed calls retry once with a 5-second
              backoff before marking the photo failed.
            </p>
          </section>

          {/* Bucket G Phase 5 (2026-05-11) -- Jigsaw Suggestions
              section. Six sub-sections per spec. */}
          <div className="border-t border-slate-200 pt-3 mt-2"
            data-testid="ai-help-jigsaw-section">
            <div className="text-sm font-semibold text-slate-900 inline-flex items-center gap-1.5 mb-2">
              <Sparkles className="h-3.5 w-3.5 text-blue-700" />
              Jigsaw Suggestions (cross-form pre-fill)
            </div>
            <section>
              <div className="font-medium text-slate-900">What is Jigsaw?</div>
              <p>
                A unified UX pattern across every form: AI producers (Photo AI,
                RFI Drafter, QC Analyzer, Safety Analyzer, Schedule Analyzer,
                Calibration RAG) drop suggested values onto specific fields.
                The fields render with a colored ring + Accept/Reject/Edit
                buttons until a human confirms.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">What the colors mean</div>
              <ul className="list-disc ml-5 mt-1 space-y-0.5">
                <li><b className="text-emerald-700">Emerald ring</b> = AI is ≥85% confident ("High confidence")</li>
                <li><b className="text-sky-700">Sky-blue ring</b> = 70–84% ("Confident")</li>
                <li><b className="text-amber-700">Amber ring</b> = 30–69% ("Review suggested")</li>
                <li><b className="text-rose-700">Rose ring</b> = &lt;30% ("Low confidence — verify")</li>
                <li>No ring = no pending suggestion, or value is human-confirmed</li>
              </ul>
            </section>
            <section>
              <div className="font-medium text-slate-900">Role permissions</div>
              <p>
                Each entity type has authorized confirmer roles: e.g. RFIs
                require <b>PM or admin</b>; safety events accept <b>super, PM,
                admin, foreman</b>. If you're not in the allowed list, the
                Accept/Reject buttons are disabled with a Lock icon — but you
                can still <b>Edit</b> (the suggestion stays pending for the
                authorized confirmer to review). Defaults are per the
                <code className="px-1 mx-0.5 bg-slate-100 rounded">FIELD_SUGGESTION_DEFAULT_ROLES</code>
                mapping; admins can override per-suggestion via the manual
                create endpoint.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">Accept / Reject / Edit</div>
              <p>
                <b>Accept</b> applies the AI value to the form and stamps a
                <code className="px-1 mx-0.5 bg-slate-100 rounded">FIELD_SUGGESTION_CONFIRMED</code>
                audit op. <b>Reject</b> (requires a 10+ character reason) feeds
                the calibration RAG corpus so future fine-tuning learns from
                "AI said X, user rejected because Y". <b>Edit</b> copies the
                value into the input and lets you tweak it before saving — the
                suggestion stays pending in the audit trail so the lineage is
                preserved.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">What expires</div>
              <p>
                Pending suggestions older than <b>30 days</b> auto-expire with
                rejection_reason <code className="px-1 mx-0.5 bg-slate-100 rounded">expired_30d_no_action</code>.
                A boot-time sweep + every-6-hour recurring task handle this;
                admins can trigger an immediate sweep via
                <code className="px-1 mx-0.5 bg-slate-100 rounded">POST /api/field-suggestions/sweep-expired</code>.
                Expired rows stay in the audit chain (for the FIELD_SUGGESTION_
                EXPIRED op) but drop out of the Review Queue.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">Where to find the Review Queue</div>
              <p>
                Cross-tenant queue lives at <b>Approvals → Pending suggestions
                tab</b>. The sidebar Approvals badge rolls in both
                approvals + pending suggestions; "N+" appears when more rows
                are available beyond the badge's fetched page. The Tasks page
                also shows a per-project banner after a schedule import drops
                AI trade suggestions in bulk.
              </p>
            </section>
          </div>

          {/* Bucket E Phase 5d (2026-05-11) -- Submittal SLA section.
              Four sub-sections covering the workflow + computed
              fields counsel + PMs need to understand. */}
          <div className="border-t border-slate-200 pt-3 mt-2"
            data-testid="ai-help-submittal-sla-section">
            <div className="text-sm font-semibold text-slate-900 inline-flex items-center gap-1.5 mb-2">
              <FileText className="h-3.5 w-3.5 text-blue-700" />
              Submittal A/E Review SLA
            </div>
            <section>
              <div className="font-medium text-slate-900">Working vs calendar days</div>
              <p>
                Default is <b>14 working days</b> per FAR 52.236-15 / GSAM.
                Working days skip weekends, 2026 federal holidays, and project
                no-work days declared on daily reports. Calendar-day mode is
                an admin override on the project (some contracts spell SLA
                as calendar). Per-submittal override available when a
                contract specifies a different SLA for a specific submittal
                type.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">SLA pills + breach detection</div>
              <ul className="list-disc ml-5 mt-1 space-y-0.5">
                <li><b className="text-emerald-700">On track</b> — more than 3 calendar days from due_date</li>
                <li><b className="text-amber-700">Approaching</b> — within 3 calendar days</li>
                <li><b className="text-rose-700">Breached</b> — past due_date</li>
                <li><b className="text-slate-700">Paused</b> — SLA clock stopped by PM/admin</li>
              </ul>
              <p className="mt-1">
                Breach detection runs every 6 hours (boot + recurring). Flipping
                to breached fires <code className="px-1 mx-0.5 bg-slate-100 rounded">SUBMITTAL_SLA_BREACHED</code>
                audit + chain event, and cascade-flags any linked task via
                gating_entities.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">must_approve_by logic</div>
              <p>
                Computed backward from the linked task's <code>planned_start</code>
                minus the <code>procurement_lead_time_days</code> on the
                submittal. When the AI projects the current SLA pace would
                miss this date, <code>urgency_flag</code> goes true and the
                register highlights the row in rose. Helps PMs spot
                procurement risk before the schedule slip locks in.
              </p>
            </section>
            <section>
              <div className="font-medium text-slate-900">Reviewer assignment + chain of custody</div>
              <p>
                PM or admin assigns a reviewer via the per-row Edit dialog;
                the assignee gets the submittal in their queue. Every chain
                hop (submitted_to_ae → ae_review_started → ae_review_completed
                → forwarded_to_sub → returned_from_sub → disposition) records
                a <code className="px-1 mx-0.5 bg-slate-100 rounded">submittal_review_events</code>
                row with calendar + working day deltas vs. the prior event.
                This drives TIA Section 9a (Submittal Review Events Timeline)
                — counsel's evidence for "A/E held this submittal N working
                days, blocking task X".
              </p>
            </section>
          </div>
        </div>
      </div>
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
