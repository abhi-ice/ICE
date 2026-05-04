import { NavLink } from "react-router-dom";
import { useEffect, useState } from "react";
import { SIDEBAR_SECTIONS, APP_BRAND } from "../lib/sidebar";
import api from "../lib/api";

export default function Sidebar() {
  const [badges, setBadges] = useState({ approvals: 0 });

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

  const Brand = APP_BRAND.icon;
  return (
    <aside
      data-testid="app-sidebar"
      className="w-64 shrink-0 border-r border-border bg-card h-screen sticky top-0 overflow-y-auto"
    >
      <div className="px-4 py-4 border-b border-border flex items-center gap-2">
        <Brand className="h-5 w-5 text-primary" />
        <div className="font-semibold text-sm tracking-tight">{APP_BRAND.name}</div>
      </div>

      <nav className="pb-6">
        {SIDEBAR_SECTIONS.map((section) => (
          <div key={section.title}>
            <div className="sidebar-section-title">{section.title}</div>
            <div className="px-2 space-y-0.5">
              {section.items.map((item) => {
                const Icon = item.icon;
                const badge = item.badgeKey ? badges[item.badgeKey] : 0;
                return (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.path === "/"}
                    data-testid={item.testid}
                    className={({ isActive }) =>
                      `sidebar-link ${isActive ? "active" : ""}`
                    }
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="flex-1 truncate">{item.label}</span>
                    {badge > 0 && (
                      <span
                        data-testid={`sidebar-badge-${item.badgeKey}`}
                        className="ml-auto inline-flex items-center justify-center text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-500 text-white min-w-[18px]"
                      >
                        {badge}
                      </span>
                    )}
                  </NavLink>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
    </aside>
  );
}
