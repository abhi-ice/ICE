import { useEffect, useRef, useState } from "react";
import { Bot, Send, X, CheckCircle2, FileSpreadsheet, ClipboardCheck, ShieldAlert } from "lucide-react";
import { useProjects } from "../../lib/projects";
import { subscribeDailyReportId, emitDescriptionInsert } from "../../lib/copilotContext";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

export default function CopilotPanel({ open, onClose }) {
  const { activeId } = useProjects();
  const [sessionId] = useState(`copilot-${Date.now()}`);
  const [activeDrId, setActiveDrId] = useState(null);
  const [thread, setThread] = useState([
    { role: "ai", text: "I am the ICE Copilot. I can read schedule + RFIs + KB and create tasks, RFIs, daily reports, work items, KB lessons, and submittals on your behalf. When you have a Daily Report open I can also record crew presence, update activity progress, and create unplanned activities for PM review. Try: add task install conference room AV, duration 4 days. Or: record presence electrical 4 workers 8 hours each." },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread]);

  // Track which DR (if any) is open so we ship daily_report_id with each turn
  useEffect(() => subscribeDailyReportId(setActiveDrId), []);

  const send = async () => {
    if (!input.trim() || busy) return;
    const msg = input.trim();
    setThread((t) => [...t, { role: "user", text: msg }]);
    setInput("");
    setBusy(true);
    try {
      const { data } = await api.post("/ai/copilot", {
        project_id: activeId,
        daily_report_id: activeDrId || undefined,
        message: msg,
        session_id: sessionId,
      });
      // Tool results are surfaced as small "actions" badges below the AI reply
      const tools = (data.usage?.tool_results || []).map((r) => {
        if (r.error) return `error · ${r.tool}: ${r.error}`;
        const result = r.result || {};
        const key = Object.keys(result)[0];
        if (!key) return `${r.tool}`;
        const v = result[key];
        if (typeof v === "object" && v !== null) {
          const nice = Object.entries(v).slice(0, 3)
            .map(([k, val]) => `${k}=${typeof val === "string" ? val.slice(0, 40) : val}`)
            .join(" · ");
          return `${r.tool} → ${nice}`;
        }
        return `${r.tool}`;
      });
      setThread((t) => [...t, { role: "ai", text: data.response || "(no response)", tools }]);
    } catch (e) {
      setThread((t) => [...t, { role: "ai", text: "Copilot error: " + (e.response?.data?.detail || e.message) }]);
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;
  return (
    <div
      data-testid="copilot-panel"
      className="fixed right-0 top-0 h-screen w-full sm:w-[420px] bg-card border-l border-border flex flex-col z-30 shadow-xl"
    >
      <div className="h-14 border-b border-border px-4 flex items-center gap-2">
        <Bot className="h-4 w-4 text-primary" />
        <div className="font-semibold text-sm">AI Copilot</div>
        {activeDrId && (
          <span data-testid="copilot-dr-scope" className="ml-2 inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
            <FileSpreadsheet className="h-3 w-3" /> Daily report scope
          </span>
        )}
        <button
          data-testid="copilot-close"
          onClick={onClose}
          className="ml-auto h-7 w-7 grid place-items-center rounded hover:bg-muted"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {thread.map((m, i) => (
          <div key={i} className={`text-sm ${m.role === "user" ? "text-right" : ""}`}>
            <div
              className={`inline-block max-w-[85%] rounded-lg px-3 py-2 whitespace-pre-wrap ${
                m.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-foreground"
              }`}
            >
              {m.text}
            </div>
            {m.tools && m.tools.length > 0 && (
              <div data-testid={`copilot-tools-${i}`} className="mt-1 flex flex-wrap gap-1">
                {m.tools.map((tt, j) => (
                  <span key={j} className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200">
                    <CheckCircle2 className="h-2.5 w-2.5" /> {tt}
                  </span>
                ))}
              </div>
            )}
            {/* Phase D — when a daily report is open, every AI reply gets two
                send-to-form buttons. They emit a context event the QC and
                Safety panels listen for to insert text into their drafts. */}
            {m.role === "ai" && activeDrId && i > 0 && m.text && (
              <div className="mt-1 flex flex-wrap gap-1">
                <button
                  data-testid={`copilot-send-qc-${i}`}
                  onClick={() => { emitDescriptionInsert("qc", m.text); toast.success("Sent to QC description"); }}
                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border border-blue-200 text-blue-700 hover:bg-blue-50"
                >
                  <ClipboardCheck className="h-3 w-3" /> Send to QC description
                </button>
                <button
                  data-testid={`copilot-send-safety-${i}`}
                  onClick={() => { emitDescriptionInsert("safety", m.text); toast.success("Sent to Safety description"); }}
                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border border-amber-200 text-amber-700 hover:bg-amber-50"
                >
                  <ShieldAlert className="h-3 w-3" /> Send to Safety description
                </button>
              </div>
            )}
          </div>
        ))}
        {busy && <div className="text-xs text-muted-foreground">Thinking…</div>}
        <div ref={endRef} />
      </div>
      <div className="p-3 border-t border-border flex gap-2">
        <input
          data-testid="copilot-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask the copilot..."
          className="flex-1 h-9 px-3 rounded border border-input bg-background text-sm"
        />
        <button
          data-testid="copilot-send"
          onClick={send}
          disabled={busy}
          className="h-9 w-9 grid place-items-center rounded bg-primary text-primary-foreground disabled:opacity-50"
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
