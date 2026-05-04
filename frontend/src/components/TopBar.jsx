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

export default function TopBar() {
  const [user] = useState(getUser());
  const [tenant] = useState(getTenant());
  const { projects, activeId, setActiveId } = useProjects();
  const [tenants, setTenants] = useState([]);

  useEffect(() => {
    api.get("/auth/tenants").then(({ data }) => setTenants(data)).catch(() => {});
  }, []);

  return (
    <header
      data-testid="app-topbar"
      className="h-14 border-b border-border bg-card flex items-center gap-3 px-4 sticky top-0 z-10"
    >
      <div className="flex items-center gap-2">
        <div className="text-xs text-muted-foreground">Tenant</div>
        <Select value={tenant?.id} onValueChange={() => {}}>
          <SelectTrigger className="h-8 w-[180px]" data-testid="tenant-switcher">
            <SelectValue placeholder="Select tenant" />
          </SelectTrigger>
          <SelectContent>
            {tenants.map((t) => (
              <SelectItem key={t.id} value={t.id}>{t.display_name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex items-center gap-2">
        <div className="text-xs text-muted-foreground">Project</div>
        <Select value={activeId || ""} onValueChange={(v) => setActiveId(v)}>
          <SelectTrigger className="h-8 w-[300px]" data-testid="project-switcher">
            <SelectValue placeholder="Select project" />
          </SelectTrigger>
          <SelectContent>
            {projects.map((p) => (
              <SelectItem key={p.id} value={p.id} data-testid={`project-option-${p.id}`}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="ml-auto flex items-center gap-3">
        <GlobalSearch />

        <NotificationsBell />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              data-testid="topbar-user-menu"
              className="flex items-center gap-2 h-8 px-2 rounded hover:bg-muted"
            >
              <div className="h-7 w-7 rounded-full bg-primary text-primary-foreground grid place-items-center text-xs font-semibold">
                {(user?.full_name || "U").split(" ").map((s) => s[0]).join("").slice(0, 2)}
              </div>
              <div className="text-left hidden md:block">
                <div className="text-xs font-medium">{user?.full_name}</div>
                <div className="text-[10px] text-muted-foreground">{user?.role}</div>
              </div>
              <ChevronDown className="h-3 w-3" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel>{user?.email}</DropdownMenuLabel>
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
