// Procurement aging dashboard — RFIs, submittals, work items, approvals
// past their SLA. Master prompt §15 + §23 (smart sidebar badges).
import { useEffect, useState } from "react";
import { Clock, AlertTriangle, MessagesSquare, FileSignature, Wrench, BadgeCheck } from "lucide-react";
import api from "../lib/api";
import { Link } from "react-router-dom";

// Followup-fix 2 (2026-05-07): summaryKeys list new (_wd) names first so
// the page reads working-day-aware counts on the latest backend, with
// legacy (_d) fallback until the deploy fully rolls.
const SECTIONS = [
  { key: "rfis",       icon: MessagesSquare, label: "RFIs > 5 working days",        route: "/rfis",        colId: "rfi_number",       colTitle: "subject", summaryKeys: ["rfis_over_5wd", "rfis_over_5d"] },
  { key: "submittals", icon: FileSignature,  label: "Submittals > 14 working days", route: "/submittals",  colId: "submittal_number", colTitle: "title",   summaryKeys: ["submittals_over_14wd", "submittals_over_14d"] },
  { key: "work_items", icon: Wrench,         label: "Field issues > 7 working days", route: "/work-items", colId: "id",               colTitle: "title",   summaryKeys: ["work_items_over_7wd", "work_items_over_7d"] },
  { key: "approvals",  icon: BadgeCheck,     label: "Approvals > 3 days",            route: "/approvals",   colId: "id",               colTitle: "subject", summaryKeys: ["approvals_over_3wd", "approvals_over_3d"] },
];

export default function Aging() {
  const [summary, setSummary] = useState(null);
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    api.get("/aging-summary").then(({ data }) => setSummary(data));
    api.get("/aging-detail").then(({ data }) => setDetail(data));
  }, []);

  return (
    <div data-testid="aging-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <Clock className="h-5 w-5 text-amber-500" />
        <h1 className="text-2xl font-semibold tracking-tight">Procurement aging</h1>
      </div>
      <p className="text-sm text-slate-500">Items past their SLA. Click a row to drill into the source list.</p>

      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {SECTIONS.map((s) => {
            const Icon = s.icon;
            // Read first defined value across summaryKeys (new _wd then legacy _d).
            const v = s.summaryKeys.map((k) => summary[k]).find((x) => x !== undefined) ?? 0;
            const overdue = v > 0;
            return (
              <Link
                key={s.key}
                to={s.route}
                data-testid={`aging-card-${s.key}`}
                className={`surface p-4 hover:shadow-md transition border ${overdue ? "border-amber-200 bg-amber-50/40" : ""}`}
              >
                <div className="flex items-center gap-2 text-slate-500 text-xs font-semibold tracking-wider">
                  <Icon className={`h-4 w-4 ${overdue ? "text-amber-600" : ""}`} />
                  {s.label}
                </div>
                <div className={`text-3xl font-semibold mt-2 tabular-nums ${overdue ? "text-amber-700" : ""}`}>{v}</div>
              </Link>
            );
          })}
        </div>
      )}

      {detail && (
        <div className="space-y-4">
          {SECTIONS.map((s) => {
            const list = detail[s.key] || [];
            if (!list.length) return null;
            return (
              <div key={s.key} data-testid={`aging-detail-${s.key}`} className="surface overflow-hidden">
                <div className="px-4 py-3 border-b border-slate-200 flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-amber-500" />
                  <div className="font-medium">{s.label}</div>
                  <span className="ml-auto text-xs text-slate-500">{list.length} overdue</span>
                </div>
                <table className="dense-table">
                  <thead><tr><th>Ref</th><th>Title</th><th>Status</th><th>Days open</th></tr></thead>
                  <tbody>
                    {list.map((it) => (
                      <tr key={it.id} data-testid={`aging-row-${s.key}-${it.id}`}>
                        <td className="font-mono text-xs">{it[s.colId] || it.id?.slice(0, 8)}</td>
                        <td className="truncate max-w-[440px]">{it[s.colTitle] || "—"}</td>
                        <td><span className="pill pill-amber capitalize">{it.status}</span></td>
                        <td className="font-mono text-xs">
                          <span className={it.days_open > 30 ? "text-red-600 font-semibold" : "text-amber-700"}>
                            {it.days_open}d
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })}
          {!Object.values(detail).some((v) => v.length) && (
            <div className="surface p-12 text-center text-sm text-slate-500">
              <BadgeCheck className="h-8 w-8 text-emerald-500 mx-auto mb-2" />
              All clear — nothing past its SLA.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
