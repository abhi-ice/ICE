import { useState } from "react";
import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import CopilotPanel from "./ai/CopilotPanel";
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
  return (
    <div className="min-h-screen page-shell">
      <Sidebar />
      {/* main column offsets for the fixed sidebar.
          ml-14  = 56px = collapsed rail width
          peer-data-[collapsed=false]:ml-64 would be ideal but Sidebar
          isn't a peer here; instead the sidebar itself manages a CSS
          variable on :root for its current width, and we read that
          here via the `app-main` class (see index.css). */}
      <div className="app-main flex flex-col min-w-0">
        <TopBar />
        <main className="flex-1 p-6 overflow-x-hidden">
          <Outlet />
        </main>
      </div>

      <button
        data-testid="copilot-fab"
        onClick={() => setCopilotOpen(true)}
        className="fixed bottom-5 right-5 h-12 w-12 rounded-full bg-[#10b981] text-white grid place-items-center shadow-[0_8px_28px_-6px_rgba(16,185,129,0.55)] hover:scale-105 hover:bg-[#059669] transition z-20"
        title="AI Copilot"
      >
        <Bot className="h-5 w-5" strokeWidth={1.6} />
      </button>

      <CopilotPanel open={copilotOpen} onClose={() => setCopilotOpen(false)} />
    </div>
  );
}
