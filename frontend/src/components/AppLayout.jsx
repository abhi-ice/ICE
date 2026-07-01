import { useState, useEffect } from "react";
import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import CopilotPanel from "./ai/CopilotPanel";
import ConfirmActionDialog from "./ConfirmActionDialog";
import { Bot } from "lucide-react";

// AppLayout (2026-05-24 rev 3) — fixed-sidebar shell.
// Sidebar is position:fixed on the left so it never scrolls with the
// page. The right-hand content column gets a left margin equal to the
// sidebar width so it doesn't slide under it. The Copilot FAB sits in
// the bottom-right. The Voice mic FAB was removed per request — voice
// annotation lives on the per-feature pages that need it (DR, RFI etc.)
// rather than as a global floater.
export default function AppLayout() {
  const [copilotOpen, setCopilotOpen] = useState(false);
  // P1 mobile drawer (phones only). navOpen is the source of truth; an effect
  // mirrors it to a `mobile-nav-open` body class that drives the CSS (the
  // off-canvas transform + backdrop + body scroll-lock), and ESC closes it.
  // Desktop never sets navOpen (the hamburger is md:hidden), so the body class
  // is never added there and desktop is unaffected.
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => {
    document.body.classList.toggle("mobile-nav-open", navOpen);
    return () => document.body.classList.remove("mobile-nav-open");
  }, [navOpen]);
  // Lock background scroll while the full-screen Copilot overlay is open on
  // phones (the CSS rule is gated to <=480px, so this is a no-op elsewhere).
  useEffect(() => {
    document.body.classList.toggle("copilot-open", copilotOpen);
    return () => document.body.classList.remove("copilot-open");
  }, [copilotOpen]);
  useEffect(() => {
    if (!navOpen) return undefined;
    const onKey = (e) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);
  return (
    <div className="min-h-screen page-shell">
      <Sidebar onNavigate={() => setNavOpen(false)} />
      {/* P1 (2026-06-29): mobile drawer backdrop. CSS shows it ONLY on phones
          when body.mobile-nav-open is set; tap it to close the drawer. Inert
          (display:none) on tablet/desktop. data-skip-page-stacking keeps the
          .page-shell relative-positioning cascade from demoting its fixed
          position (same guard the Copilot panel uses). */}
      <div
        className="mobile-nav-backdrop"
        data-skip-page-stacking
        aria-hidden="true"
        onClick={() => setNavOpen(false)}
      />
      {/* main column offsets for the fixed sidebar.
          ml-14  = 56px = collapsed rail width
          peer-data-[collapsed=false]:ml-64 would be ideal but Sidebar
          isn't a peer here; instead the sidebar itself manages a CSS
          variable on :root for its current width, and we read that
          here via the `app-main` class (see index.css). */}
      <div className="app-main flex flex-col min-w-0">
        <TopBar navOpen={navOpen} onToggleNav={() => setNavOpen((v) => !v)} />
        <main className="flex-1 p-6 max-md:p-3 overflow-x-hidden">
          <Outlet />
        </main>
      </div>

      <button
        data-testid="copilot-fab"
        onClick={() => setCopilotOpen(true)}
        /* V4: a CSS rule in index.css lifts the FAB up when
           body.has-sticky-submit is set (DR detail page). */
        className="copilot-fab fixed bottom-5 right-5 h-12 w-12 rounded-full bg-[#10b981] text-white grid place-items-center shadow-[0_8px_28px_-6px_rgba(16,185,129,0.55)] hover:scale-105 hover:bg-[#059669] transition z-20"
        title="AI Copilot"
      >
        <Bot className="h-5 w-5" strokeWidth={1.6} />
      </button>

      <CopilotPanel open={copilotOpen} onClose={() => setCopilotOpen(false)} />

      {/* pt.37: themed replacement for window.confirm(). Mounted once
          at the layout root; opens whenever code calls confirmAction()
          from lib/confirmAction.js. See ConfirmActionDialog.jsx for
          the implementation and Option C in
          design-mockup/delete-confirmation-options.html for the design
          source. */}
      <ConfirmActionDialog />
    </div>
  );
}
