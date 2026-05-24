import { useEffect, useState } from "react";
import { ChevronDown, LogOut, User2 } from "lucide-react";
import { getUser, getTenant, clearAuth } from "../lib/auth";
import { useProjects } from "../lib/projects";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "./ui/select";
import api from "../lib/api";
import NotificationsBell from "./NotificationsBell";
import GlobalSearch from "./GlobalSearch";

// TopBar (2026-05-24 rev 2) — quieter, denser, closer to modern SaaS
// (Linear / Notion / Vercel) than the previous "Tenant: [...]
// Project: [...]" inline-labelled layout. Selectors are unlabelled,
// the avatar block is compact (initials + chevron, name shows on
// click), and the whole bar is 48px tall instead of 56px.
export default function TopBar() {
  const [user] = useState(getUser());
  const [tenant] = useState(getTenant());
  const { projects, activeId, setActiveId } = useProjects();
  const [tenants, setTenants] = useState([]);

  useEffect(() => {
    api.get("/auth/tenants").then(({ data }) => setTenants(data)).catch(() => {});
  }, []);

  const initials = (user?.full_name || "U")
    .split(" ")
    .map((s) => s[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <header
      data-testid="app-topbar"
      className="h-12 border-b border-border bg-card flex items-center gap-2 px-4 sticky top-0 z-10"
    >
      {/* Tenant + project selectors — unlabelled, compact */}
      <Select value={tenant?.id} onValueChange={() => {}}>
        <SelectTrigger className="h-8 w-[150px] text-xs" data-testid="tenant-switcher">
          <SelectValue placeholder="Tenant" />
        </SelectTrigger>
        <SelectContent>
          {tenants.map((t) => (
            <SelectItem key={t.id} value={t.id}>{t.display_name}</SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select value={activeId || ""} onValueChange={(v) => setActiveId(v)}>
        <SelectTrigger className="h-8 w-[240px] text-xs" data-testid="project-switcher">
          <SelectValue placeholder="Project" />
        </SelectTrigger>
        <SelectContent>
          {projects.map((p) => (
            <SelectItem key={p.id} value={p.id} data-testid={`project-option-${p.id}`}>
              {p.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <div className="ml-auto flex items-center gap-2">
        <GlobalSearch />
        <NotificationsBell />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              data-testid="topbar-user-menu"
              className="flex items-center gap-2 h-8 pl-1 pr-2 rounded-md hover:bg-white/[0.06] transition-colors"
              title={user?.full_name}
            >
              <div className="h-7 w-7 rounded-full bg-[#10b981]/15 text-[#34d399] border border-[#10b981]/30 grid place-items-center text-[11px] font-semibold tracking-wide">
                {initials}
              </div>
              <ChevronDown className="h-3 w-3 text-white/50" strokeWidth={2} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-60">
            <DropdownMenuLabel className="font-normal">
              <div className="text-sm font-medium leading-tight">{user?.full_name}</div>
              <div className="text-xs text-muted-foreground mt-0.5">{user?.email}</div>
              {user?.role && (
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground/80 mt-1.5">
                  {user.role}
                </div>
              )}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem><User2 className="h-4 w-4 mr-2" /> Profile</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              data-testid="logout-button"
              onClick={() => { clearAuth(); window.location.href = "/login"; }}
            >
              <LogOut className="h-4 w-4 mr-2" /> Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
