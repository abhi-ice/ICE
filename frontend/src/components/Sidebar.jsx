import { NavLink } from "react-router-dom";
import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { SIDEBAR_SECTIONS, APP_BRAND } from "../lib/sidebar";
import api from "../lib/api";

const COLLAPSE_KEY = "ice.sidebar.collapsed";

export default function Sidebar() {
  const [badges, setBadges] = useState({ approvals: 0 });
  const [collapsed, setCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}"); }
    catch { return {}; }
  });

  useEffect(() => {
    const load = async () => {
      try {
        const { data } = await api.get("/approvals/pending-count");
        setBadges((b) => ({ ...b, approvals: data.count || 0 }));
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
                          <span data-testid={`sidebar-badge-${item.badgeKey}`} className="ml-auto inline-flex items-center justify-center text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-500 text-slate-900 min-w-[18px]">
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
