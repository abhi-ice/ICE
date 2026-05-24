import { NavLink } from "react-router-dom";
import { useEffect, useLayoutEffect, useState } from "react";
import { ChevronDown, ChevronRight, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { SIDEBAR_SECTIONS, APP_BRAND } from "../lib/sidebar";
import api from "../lib/api";
import SiteOpsLogo from "./SiteOpsLogo";

const SECTION_COLLAPSE_KEY = "ice.sidebar.collapsed";
// 2026-05-19 -- whole-sidebar collapse state. Default = collapsed
// (true) so first-time users land in compact-rail mode that gives the
// data table the maximum horizontal space. Persisted across sessions
// via localStorage so a super who expands it once stays expanded.
const RAIL_COLLAPSE_KEY = "ice.sidebar.rail.collapsed";

export default function Sidebar() {
  const [badges, setBadges] = useState({
    approvals: 0, rfis: 0, submittals: 0, work_items: 0,
    aging: 0, edit_requests: 0, suggestions: 0,
    suggestions_more: false,
  });
  const [collapsed, setCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SECTION_COLLAPSE_KEY) || "{}"); }
    catch { return {}; }
  });
  // Compact "icon rail" state -- defaults to TRUE for new users so the
  // Tasks page (and every other dense-table view) gets maximum width
  // without the super having to learn about a sidebar toggle first.
  const [rail, setRail] = useState(() => {
    try {
      const raw = localStorage.getItem(RAIL_COLLAPSE_KEY);
      if (raw === null) return true;   // first visit -> collapsed
      return JSON.parse(raw);
    } catch { return true; }
  });

  useEffect(() => {
    const load = async () => {
      try {
        // Addition 4 -- include edit_requests/pending-count alongside
        // the existing approvals + aging fetches. Endpoint returns the
        // user's pending count (admin/PM see all, others see own).
        //
        // Bucket G Phase 3 + 4 (2026-05-11) -- pending Jigsaw
        // suggestions for the current user. Phase 4 bumps the fetch
        // limit from 50 -> 500 (the server's hard cap) so a single
        // schedule import dropping 50-100 trade suggestions counts
        // accurately. When the server still has more rows beyond
        // that page (next_cursor non-null), the sidebar renders
        // "N+" instead of just N. Fail-soft on any error so the
        // sidebar stays alive even if the suggestions backend hiccups.
        const [{ data: pa }, { data: aging }, editReq, sugg] = await Promise.all([
          api.get("/approvals/pending-count"),
          api.get("/aging-summary"),
          api.get("/edit-requests/pending-count").catch(() => ({ data: { pending: 0 } })),
          api.get("/users/current/pending-suggestions?limit=500")
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
        const suggHasMore = Boolean(sugg?.data?.next_cursor);
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
          suggestions_more: suggHasMore,
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
    localStorage.setItem(SECTION_COLLAPSE_KEY, JSON.stringify(next));
  };
  const toggleRail = () => {
    const next = !rail;
    setRail(next);
    localStorage.setItem(RAIL_COLLAPSE_KEY, JSON.stringify(next));
  };

  // Keep a CSS variable in sync with the current sidebar width so the
  // fixed-position sidebar can offset the main column without relying
  // on flexbox math. 56px in rail mode, 256px expanded.
  useLayoutEffect(() => {
    document.documentElement.style.setProperty("--sidebar-w", rail ? "56px" : "256px");
  }, [rail]);

  const Brand = APP_BRAND.icon;
  return (
    <aside
      data-testid="app-sidebar"
      data-collapsed={rail ? "true" : "false"}
      className={`${rail ? "w-14" : "w-64"} app-sidebar fixed left-0 top-0 bottom-0 h-screen overflow-y-auto overflow-x-hidden scrollbar-soft transition-[width] duration-150 ease-out z-30`}
    >
      <div className={`${rail ? "px-2" : "px-4"} py-4 border-b border-white/10 flex items-center ${rail ? "justify-center" : "gap-2.5"}`}>
        <button
          data-testid="sidebar-rail-toggle"
          onClick={toggleRail}
          title={rail ? "Expand sidebar" : "Collapse sidebar"}
          className="h-9 w-9 grid place-items-center rounded-md hover:bg-white/5 shrink-0 transition-colors"
        >
          <SiteOpsLogo className="h-7 w-7" />
        </button>
        {!rail && (
          <>
            <div className="min-w-0 flex-1">
              <div className="font-semibold text-sm tracking-tight text-white leading-tight truncate">{APP_BRAND.name}</div>
              <div className="text-[10px] text-slate-500 leading-tight font-mono uppercase tracking-wider">Construction OS</div>
            </div>
            <button
              data-testid="sidebar-rail-collapse"
              onClick={toggleRail}
              title="Collapse sidebar"
              className="h-7 w-7 grid place-items-center rounded text-slate-400 hover:text-white hover:bg-white/5 shrink-0"
            >
              <PanelLeftClose className="h-4 w-4" strokeWidth={1.6} />
            </button>
          </>
        )}
      </div>
      {rail && (
        /* Tiny "expand" affordance just under the brand button -- so
           a user who doesn't realise the brand icon toggles the rail
           has a second, more obvious entry point. */
        <div className="px-2 pt-2">
          <button
            data-testid="sidebar-rail-expand"
            onClick={toggleRail}
            title="Expand sidebar"
            className="h-7 w-full grid place-items-center rounded text-slate-400 hover:text-white hover:bg-slate-800"
          >
            <PanelLeftOpen className="h-4 w-4" strokeWidth={1.6} />
          </button>
        </div>
      )}

      <nav className="pb-6">
        {SIDEBAR_SECTIONS.map((section) => {
          const isCollapsed = !!collapsed[section.title];
          // In rail mode every section is rendered fully (we can't show
          // section titles anyway -- not enough horizontal room) and
          // the user-selected per-section collapse is ignored.
          const showItems = rail ? true : !isCollapsed;
          return (
            <div key={section.title}>
              {!rail && (
                <button
                  onClick={() => toggle(section.title)}
                  data-testid={`sidebar-section-${section.title.replace(/\s+/g, '-').toLowerCase()}`}
                  className="w-full flex items-center gap-1 px-4 pt-5 pb-1 text-left text-slate-500 hover:text-slate-300 transition-colors"
                >
                  {isCollapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                  <span className="text-xs font-semibold uppercase tracking-[0.13em]">{section.title}</span>
                </button>
              )}
              {rail && (
                /* Hairline group separator in rail mode -- gives the
                   eye a visual break between OVERVIEW / DAILY OPS /
                   PLANNING etc. without consuming horizontal space. */
                <div className="mx-2 mt-3 mb-1 border-t border-white/10" />
              )}
              {showItems && (
                <div className={`space-y-0.5 ${rail ? "px-2" : "pl-6"}`}>
                  {section.items.map((item) => {
                    const Icon = item.icon;
                    const badge = item.badgeKey ? badges[item.badgeKey] : 0;
                    const badgeMore = item.badgeKey === "approvals"
                      ? badges.suggestions_more
                      : false;
                    if (rail) {
                      // Rail mode -- icon-only NavLink, native title
                      // tooltip on hover, badge collapsed to a small
                      // count chip in the bottom-right corner.
                      return (
                        <NavLink
                          key={item.path}
                          to={item.path}
                          end={item.path === "/"}
                          data-testid={item.testid}
                          title={item.label + (badge > 0 ? ` (${badge}${badgeMore ? "+" : ""} pending)` : "")}
                          className={({ isActive }) =>
                            `relative h-10 grid place-items-center rounded-md mx-0 text-slate-300 hover:bg-slate-800 hover:text-white transition-colors ${isActive ? "bg-[#10b981] text-white hover:bg-[#10b981]" : ""}`
                          }
                        >
                          <Icon className="h-4 w-4" strokeWidth={1.6} />
                          {badge > 0 && (
                            <span
                              data-testid={`sidebar-${item.badgeKey}-badge`}
                              className="absolute top-0.5 right-0.5 inline-flex items-center justify-center text-[10px] font-semibold leading-none px-1 py-0.5 rounded bg-amber-500 text-slate-900 min-w-[16px]"
                              title={badgeMore
                                ? `${badge}+ pending (more available beyond first page)`
                                : `${badge} pending`}
                            >
                              {badge > 99 ? "99" : badge}{badgeMore ? "+" : ""}
                            </span>
                          )}
                        </NavLink>
                      );
                    }
                    return (
                      <NavLink
                        key={item.path}
                        to={item.path}
                        end={item.path === "/"}
                        data-testid={item.testid}
                        className={({ isActive }) =>
                          `sidebar-link ${isActive ? "active" : ""}${
                            item.nested ? " sidebar-link-nested" : ""
                          }`
                        }
                      >
                        <Icon className="h-4 w-4 shrink-0" strokeWidth={1.6} />
                        <span className="flex-1 truncate">{item.label}</span>
                        {badge > 0 && (
                          <span
                            data-testid={`sidebar-${item.badgeKey}-badge`}
                            className="ml-auto inline-flex items-center justify-center text-xs font-semibold px-1.5 py-0.5 rounded bg-amber-500 text-slate-900 min-w-[18px]"
                            title={badgeMore
                              ? `${badge}+ pending (more available beyond first page)`
                              : `${badge} pending`}
                          >
                            {badge}{badgeMore ? "+" : ""}
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

      {!rail && (
        <div className="px-4 py-3 mt-auto border-t border-white/10 text-xs text-slate-500">
          v0.3 Â· Iter 3 Â· {new Date().getFullYear()}
        </div>
      )}
    </aside>
  );
}
