import { useState } from "react";
import { ChevronDown, LogOut, User2, Trash2, Sun, Moon, Building2 } from "lucide-react";
import { getUser, getTenant, clearAuth } from "../lib/auth";
import { toast } from "./ui/sonner";
import { useProjects } from "../lib/projects";
import { useTheme } from "../lib/theme";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "./ui/select";
import api from "../lib/api";
import NotificationsBell from "./NotificationsBell";
import GlobalSearch from "./GlobalSearch";

// TopBar (2026-05-24 rev 3) — 56px height, larger search field + avatar.
// Profile dropdown given a proper structured header card with the
// avatar at top-left, name + email + role pill in a clean stack.
export default function TopBar() {
  const [user] = useState(getUser());
  const [tenant] = useState(getTenant());
  const { projects, activeId, setActiveId } = useProjects();
  const { theme, toggle: toggleTheme } = useTheme();

  const initials = (user?.full_name || "U")
    .split(" ")
    .map((s) => s[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <header
      data-testid="app-topbar"
      // pt.29 — match v4 mockup exactly: 52px tall (was 56), 14px
      // horizontal padding, 10px gap, sticky.
      className="v4-topbar h-[52px] flex items-center gap-2.5 px-3.5 sticky top-0 z-10"
    >
      {/* pt.59 (2026-05-28): the tenant "switcher" was a no-op dropdown
          (one user = one tenant; switching was never wired). Replaced with
          a static org label — new orgs are created via /signup, not here. */}
      <div data-testid="tenant-label" title={tenant?.display_name || ""}
        className="v4-topbar-tenant flex items-center gap-1.5 h-8 px-2 text-xs font-medium"
        style={{ color: "rgba(255,255,255,0.82)" }}>
        <Building2 className="h-3.5 w-3.5" style={{ opacity: 0.65 }} />
        <span className="truncate max-w-[150px]">{tenant?.display_name || "—"}</span>
      </div>

      <Select value={activeId || ""} onValueChange={(v) => setActiveId(v)}>
        <SelectTrigger className="v4-topbar-select h-8 w-[230px] text-xs" data-testid="project-switcher">
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

      {/* pt.34 (2026-05-26): center the search between the project
          picker and the right-cluster ("All changes saved" + theme +
          bell + avatar). Previously it sat glued to the project
          dropdown with a wide ml-auto void before the right side. Two
          flex-1 spacers — one before, one after — float the 280px
          search in the middle of whatever horizontal space is left,
          which scales nicely as the window resizes. (Pattern matches
          GitHub / Notion top-bar search placement.) */}
      <div className="flex-1" aria-hidden="true" />
      <GlobalSearch />
      <div className="flex-1" aria-hidden="true" />

      <div className="flex items-center gap-2.5">
        {/* v4: subtle save-state indicator (placeholder until wired to
            DR autosave state — for now indicates the app is healthy). */}
        <div className="v4-save-state hidden lg:inline-flex" title="Your work is auto-saved">
          <span className="dot"></span>
          <span>All changes saved</span>
        </div>

        {/* v4: light/dark theme toggle */}
        <button
          data-testid="theme-toggle"
          onClick={toggleTheme}
          className="v4-theme-toggle"
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
        >
          {theme === "dark"
            ? <Sun className="h-3.5 w-3.5" strokeWidth={2} />
            : <Moon className="h-3.5 w-3.5" strokeWidth={2} />}
          <span className="hidden md:inline">{theme === "dark" ? "Light" : "Dark"}</span>
        </button>

        <NotificationsBell />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              data-testid="topbar-user-menu"
              className="v4-topbar-avatar-btn flex items-center gap-1.5 h-9 pl-1 pr-2 rounded-md transition-colors"
              title={user?.full_name}
            >
              <div className="h-[30px] w-[30px] rounded-full bg-[#10b981]/15 text-[#34D399] border border-[#10b981]/30 grid place-items-center text-[10.5px] font-semibold tracking-wide">
                {initials}
              </div>
              <ChevronDown className="h-3 w-3" strokeWidth={2} style={{ color: "rgba(255,255,255,0.55)" }} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-72 p-0">
            {/* Profile card header */}
            <div className="px-4 pt-4 pb-3 flex items-start gap-3">
              <div className="h-10 w-10 rounded-full bg-[#10b981]/15 text-[#34D399] border border-[#10b981]/30 grid place-items-center text-sm font-semibold tracking-wide flex-shrink-0">
                {initials}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold text-foreground truncate">{user?.full_name}</div>
                <div className="text-xs text-muted-foreground truncate mt-0.5">{user?.email}</div>
                {user?.role && (
                  <span className="inline-flex items-center mt-2 px-2 py-0.5 rounded-full text-[10px] font-medium bg-[#10b981]/15 text-[#34D399] border border-[#10b981]/30">
                    {user.role}
                  </span>
                )}
              </div>
            </div>
            <DropdownMenuSeparator className="my-0" />
            <div className="p-1">
              <DropdownMenuItem className="text-sm py-2"><User2 className="h-4 w-4 mr-2.5 opacity-70" /> Profile</DropdownMenuItem>
              <DropdownMenuItem
                data-testid="logout-button"
                className="text-sm py-2"
                onClick={() => { clearAuth(); window.location.href = "/login"; }}
              >
                <LogOut className="h-4 w-4 mr-2.5 opacity-70" /> Sign out
              </DropdownMenuItem>
              {(user?.role || "").toLowerCase() === "admin" && (
                <>
                  <DropdownMenuSeparator className="my-0" />
                  <DropdownMenuItem
                    data-testid="close-account-button"
                    className="text-sm py-2 text-rose-500 focus:text-rose-400"
                    onClick={async () => {
                      const tenantName = tenant?.display_name || "this organization";
                      const ack = window.prompt(
                        `This will PERMANENTLY close "${tenantName}" and wipe every project, daily report, photo, RFI, delay, team member, audit row, and the tenant itself.\n\nYour email becomes free to sign up a brand-new organization afterwards.\n\nType DELETE to confirm:`,
                        "",
                      );
                      if (ack == null) return;
                      if ((ack || "").trim().toUpperCase() !== "DELETE") {
                        toast.error("Confirmation did not match — close cancelled.");
                        return;
                      }
                      try {
                        await api.post("/auth/close-account");
                        toast.success("Account closed. Goodbye!");
                        clearAuth();
                        window.location.href = "/signup";
                      } catch (e) {
                        toast.error(e.response?.data?.detail || "Close failed");
                      }
                    }}
                  >
                    <Trash2 className="h-4 w-4 mr-2.5 opacity-70" /> Close account
                  </DropdownMenuItem>
                </>
              )}
            </div>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
