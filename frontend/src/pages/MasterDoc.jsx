// Project master doc — project-type gating.
// Construction type drives different AI agent context. Master prompt §22.
import { useEffect, useState } from "react";
import { Building2, Save, Wand2 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";

const CONSTRUCTION_TYPES = [
  { id: "new_construction", label: "New construction", desc: "Greenfield/groundbreak. AI sequences foundation→shell→MEP→finishes." },
  { id: "renovation", label: "Renovation", desc: "Occupied building. AI emphasizes phasing, dust barriers, after-hours noisy work." },
  { id: "tenant_improvement", label: "Tenant improvement", desc: "Base building operational. Coordinate with landlord; lobby/elevator restrictions." },
  { id: "infrastructure", label: "Infrastructure", desc: "Civil/heavy work. Permits dominate; weather impact severe; ROW/utility-locate hard constraints." },
];

const DELIVERY = ["design_bid_build", "design_build", "cmar", "ipd", "task_order"];
const SECURITY = ["public", "fouo", "secret", "ts"];
const SKETCH_REVIEW = ["over_the_shoulder", "formal_submittal", "rfi_based"];

export default function MasterDoc() {
  const { activeId, projects } = useProjects();
  const [doc, setDoc] = useState(null);
  const [busy, setBusy] = useState(false);
  const project = projects.find((p) => p.id === activeId);

  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/master-doc`).then(({ data }) => setDoc({ ...data, project_id: activeId }));
  }, [activeId]);

  const save = async () => {
    setBusy(true);
    try {
      await api.put(`/projects/${activeId}/master-doc`, doc);
      toast.success("Master doc saved — AI agents will use this on next call");
    } catch (e) {
      toast.error(e.response?.data?.detail?.[0]?.msg || "Save failed");
    } finally { setBusy(false); }
  };

  if (!doc) return <div className="text-sm text-slate-500">Loading…</div>;

  const update = (k, v) => setDoc({ ...doc, [k]: v });
  const toggleConstraint = (s) => {
    const list = doc.site_constraints || [];
    const next = list.includes(s) ? list.filter((x) => x !== s) : [...list, s];
    update("site_constraints", next);
  };
  const COMMON_CONSTRAINTS = [
    "Limited loading dock", "Single-stair egress only",
    "After-hours noisy work only", "Live data center on lower floor",
    "Single-elevator building", "ADA-only access during business hours",
    "Existing roof — no overload", "Hospital ICU adjacency",
  ];

  return (
    <div data-testid="master-doc-page" className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between max-md:flex-col max-md:items-start max-md:gap-2">
        <div>
          <div className="flex items-center gap-2">
            <Building2 className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">Project master doc</h1>
          </div>
          <p className="text-sm text-slate-500 page-desc">{project?.name} — drives project-type gating for every AI agent.</p>
        </div>
        {/* pt.40: page-header primary -> sbtn-md (32px). */}
        <button data-testid="md-save" onClick={save} disabled={busy} className="sbtn sbtn-md sbtn-primary">
          <Save /> {busy ? "Saving…" : "Save"}
        </button>
      </div>

      <div className="surface p-4 space-y-3">
        <div className="text-xs font-semibold text-slate-500 tracking-wider">Construction type</div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {CONSTRUCTION_TYPES.map((t) => (
            <button
              key={t.id}
              data-testid={`md-type-${t.id}`}
              onClick={() => update("construction_type", t.id)}
              className={`allow-wrap text-left p-3 rounded-md border transition ${doc.construction_type === t.id ? "border-[var(--brand-primary)] bg-[rgba(16,185,129,0.06)] shadow-[0_0_0_3px_rgba(16,185,129,0.10)]" : "border-[var(--v4-border)] hover:border-[var(--v4-border-strong)]"}`}
              style={{ backgroundColor: doc.construction_type === t.id ? "rgba(16,185,129,0.06)" : "var(--v4-bg-surface)" }}
            >
              <div className="font-medium text-sm flex items-center gap-2" style={{ color: "var(--v4-fg-primary)" }}>
                {doc.construction_type === t.id && <span className="h-2 w-2 rounded-full" style={{ background: "var(--brand-primary)" }} />}
                {t.label}
              </div>
              <div className="text-xs mt-1 leading-snug" style={{ color: "var(--v4-fg-secondary)" }}>{t.desc}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="surface p-4 space-y-3">
        <div className="text-xs font-semibold text-slate-500 tracking-wider">Delivery & sketch review</div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div>
            <label className="field-label">Delivery method</label>
            <select data-testid="md-delivery" value={doc.delivery_method} onChange={(e) => update("delivery_method", e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
              {DELIVERY.map((d) => <option key={d} value={d}>{d.replace(/_/g, " ")}</option>)}
            </select>
          </div>
          <div>
            <label className="field-label">Sketch review</label>
            <select data-testid="md-sketch" value={doc.sketch_review_method} onChange={(e) => update("sketch_review_method", e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
              {SKETCH_REVIEW.map((d) => <option key={d} value={d}>{d.replace(/_/g, " ")}</option>)}
            </select>
          </div>
          <div>
            <label className="field-label">Govt security level</label>
            <select data-testid="md-security" value={doc.govt_security_level} onChange={(e) => update("govt_security_level", e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
              {SECURITY.map((d) => <option key={d} value={d}>{d.toUpperCase()}</option>)}
            </select>
          </div>
        </div>
      </div>

      <div className="surface p-4 space-y-3">
        <div className="text-xs font-semibold text-slate-500 tracking-wider">Site conditions</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2">
          {[
            { k: "occupancy_during_work", label: "Building occupied during work" },
            { k: "historic_designation", label: "Historic designation" },
            { k: "abatement_required", label: "Abatement required" },
            { k: "asbestos_known", label: "Asbestos known" },
            { k: "lead_paint_known", label: "Lead paint known" },
            { k: "swing_shifts_allowed", label: "Swing shifts allowed" },
          ].map((f) => (
            <label key={f.k} className="flex items-center gap-2 text-sm">
              <input data-testid={`md-${f.k}`} type="checkbox" checked={!!doc[f.k]} onChange={(e) => update(f.k, e.target.checked)} />
              {f.label}
            </label>
          ))}
        </div>
      </div>

      <div className="surface p-4 space-y-3">
        <div className="text-xs font-semibold text-slate-500 tracking-wider">Site constraints (chips)</div>
        <div className="flex flex-wrap gap-2">
          {COMMON_CONSTRAINTS.map((c) => {
            const on = (doc.site_constraints || []).includes(c);
            return (
              <button
                key={c}
                data-testid={`md-constraint-${c.slice(0, 12).replace(/\W/g, "")}`}
                onClick={() => toggleConstraint(c)}
                className={`px-3 h-7 max-md:min-h-[44px] rounded-full text-xs border ${on ? "bg-blue-600 text-white border-blue-600" : "bg-white border-slate-200 hover:border-slate-300"}`}
              >
                {c}
              </button>
            );
          })}
        </div>
        <div className="text-xs text-slate-500">Custom constraints:</div>
        <textarea data-testid="md-notes" value={doc.notes || ""} onChange={(e) => update("notes", e.target.value)} className="w-full h-20 p-2 rounded border border-slate-200 text-sm" placeholder="e.g. coordinate roof crane with adjacent fire-station ladder access…" />
      </div>

      <div className="bg-amber-50 border border-amber-200 rounded p-3 text-xs flex items-start gap-2">
        <Wand2 className="h-4 w-4 text-amber-600 mt-0.5" />
        <div>
          <b>How AI agents use this:</b> on every recovery / RFI-drafter / copilot call, this doc is prepended as context. Renovation projects get phasing-aware suggestions; new-construction projects get foundation-out sequencing; occupied buildings get noise/dust constraints folded into recommendations.
        </div>
      </div>
    </div>
  );
}
