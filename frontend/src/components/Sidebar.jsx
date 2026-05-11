import { NavLink } from "react-router-dom";
import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { SIDEBAR_SECTIONS, APP_BRAND } from "../lib/sidebar";
import api from "../lib/api";

const COLLAPSE_KEY = "ice.sidebar.collapsed";

export default function Sidebar() {
  const [badges, setBadges] = useState({
    approvals: 0, rfis: 0, submittals: 0, work_items: 0,
    aging: 0, edit_requests: 0, suggestions: 0,
  });
  const [collapsed, setCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}"); }
    catch { return {}; }
  });

  useEffect(() => {
    const load = async () => {
      try {
        // Addition 4 -- include edit_requests/pending-count alongside
        // the existing approvals + aging fetches. Endpoint returns the
        // user's pending count (admin/PM see all, others see own).
        //
        // Bucket G Phase 3 (2026-05-11) -- pending Jigsaw suggestions
        // for the current user. Hits the same endpoint the Review
        // Queue uses; we ask for one page (limit=50) so the badge
        // shows current-page count + an "N+" hint when the cursor is
        // non-null. Fail-soft on any error so the sidebar stays alive
        // even if the suggestions backend is down.
        const [{ data: pa }, { data: aging }, editReq, sugg] = await Promise.all([
          api.get("/approvals/pending-count"),
          api.get("/aging-summary"),
          api.get("/edit-requests/pending-count").catch(() => ({ data: { pending: 0 } })),
          api.get("/users/current/pending-suggestions?limit=50")
              .catch(() => ({ data: { rows: [], next_cursor: null } })),
        ]);
        // Followup-fix 2 (2026-05-07): backend now returns working-day
        // aware counts under _wd suffix. Read those first; fall back to
        // legacy _d suffix until the next deploy where the backend
        // drops the legacy keys.
        const rfis = aging.rfis_over_5wd ?? aging.rfis_over_5d ?? 0;
        const submittals = aging.submittals_over_14wd ?? aging.submittals_over_14d ?? 0;
        const wis = aging.work_items_over_7wd ?? aging.work_items_over_7d ?? 0;
        const apvs = aging.approvals_over_3wd ?? aging.approvals_over_3d ?? 0;
        const total = rfis + submittals + wis + apvs;
        const suggRows = sugg?.data?.rows || [];
        // Roll the pending-suggestions count into the approvals badge
        // (Sid: no new sidebar -- the Pending suggestions tab lives
        // inside Approvals, so the badge should reflect both). The
        // dedicated "suggestions" key is also exposed for any future
        // standalone badge call site.
        const approvalsCount = (pa.count || 0) + suggRows.length;
        setBadges({
          approvals: approvalsCount,
          rfis, submittals, work_items: wis,
          aging: total,
          edit_requests: (editReq?.data?.pending) || 0,
          suggestions: suggRows.length,
        });
      } catch {}
    };
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, []);

  const toggle = (title) => {
    const next = { ...collapsed, [title]: !collapsed[title] };
    setCollapsed(next);
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify(next));
  };

  const Brand = APP_BRAND.icon;
  return (
    <aside data-testid="app-sidebar" className="w-64 shrink-0 app-sidebar h-screen sticky top-0 overflow-y-auto scrollbar-soft">
      <div className="px-5 py-4 border-b border-slate-800 flex items-center gap-2.5">
        <div className="h-8 w-8 grid place-items-center rounded bg-blue-600">
          <Brand className="h-4 w-4 text-white" strokeWidth={1.6} />
        </div>
        <div>
          <div className="font-semibold text-[13px] tracking-tight text-white leading-tight">{APP_BRAND.name}</div>
          <div className="text-[10px] text-slate-500 leading-tight">Federal construction OS</div>
        </div>
      </div>

      <nav className="pb-6">
        {SIDEBAR_SECTIONS.map((section) => {
          const isCollapsed = !!collapsed[section.title];
          return (
            <div key={section.title}>
              <button
                onClick={() => toggle(section.title)}
                data-testid={`sidebar-section-${section.title.replace(/\s+/g, '-').toLowerCase()}`}
                className="w-full flex items-center gap-1 px-4 pt-5 pb-1 text-left text-slate-500 hover:text-slate-300 transition-colors"
              >
                {isCollapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                <span className="text-[10px] font-semibold uppercase tracking-[0.13em]">{section.title}</span>
              </button>
              {!isCollapsed && (
                <div className="space-y-0.5">
                  {section.items.map((item) => {
                    const Icon = item.icon;
                    const badge = item.badgeKey ? badges[item.badgeKey] : 0;
                    return (
                      <NavLink
                        key={item.path}
                        to={item.path}
                        end={item.path === "/"}
                        data-testid={item.testid}
                        className={({ isActive }) => `sidebar-link ${isActive ? "active" : ""}`}
                      >
                        <Icon className="h-4 w-4 shrink-0" strokeWidth={1.6} />
                        <span className="flex-1 truncate">{item.label}</span>
                        {badge > 0 && (
                          <span
                            data-testid={`sidebar-${item.badgeKey}-badge`}
                            className="ml-auto inline-flex items-center justify-center text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-500 text-slate-900 min-w-[18px]"
                          >
                            {badge}
                          </span>
                        )}
                      </NavLink>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <div className="px-4 py-3 mt-auto border-t border-slate-800 text-[10px] text-slate-500">
        v0.3 · Iter 3 · {new Date().getFullYear()}
      </div>
    </aside>
  );
}
