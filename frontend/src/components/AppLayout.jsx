import { useState } from "react";
import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import CopilotPanel from "./ai/CopilotPanel";
import VoiceCaptureButton from "./voice/VoiceCaptureButton";
import { Bot } from "lucide-react";

export default function AppLayout() {
  const [copilotOpen, setCopilotOpen] = useState(false);
  return (
    <div className="flex min-h-screen page-shell">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar />
        <main className="flex-1 p-6 overflow-x-hidden">
          <Outlet />
        </main>
      </div>

      <button
        data-testid="copilot-fab"
        onClick={() => setCopilotOpen(true)}
        className="fixed bottom-5 right-5 h-12 w-12 rounded-full bg-[#ef233c] text-white grid place-items-center shadow-[0_8px_28px_-6px_rgba(239,35,60,0.6)] hover:scale-105 hover:bg-[#b91c2c] transition z-20"
        title="AI Copilot"
      >
        <Bot className="h-5 w-5" strokeWidth={1.6} />
      </button>

      <VoiceCaptureButton />

      <CopilotPanel open={copilotOpen} onClose={() => setCopilotOpen(false)} />
    </div>
  );
}
