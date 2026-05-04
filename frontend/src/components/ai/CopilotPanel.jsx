import { useEffect, useRef, useState } from "react";
import { Bot, Send, X } from "lucide-react";
import { useProjects } from "../../lib/projects";
import api from "../../lib/api";

export default function CopilotPanel({ open, onClose }) {
  const { activeId } = useProjects();
  const [sessionId] = useState(`copilot-${Date.now()}`);
  const [thread, setThread] = useState([
    { role: "ai", text: "I'm the ICE Copilot. Ask me about lookahead, open RFIs, or schedule recovery." },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread]);

  const send = async () => {
    if (!input.trim() || busy) return;
    const msg = input.trim();
    setThread((t) => [...t, { role: "user", text: msg }]);
    setInput("");
    setBusy(true);
    try {
      const { data } = await api.post("/ai/copilot", {
        project_id: activeId,
        message: msg,
        session_id: sessionId,
      });
      setThread((t) => [...t, { role: "ai", text: data.response || "(no response)" }]);
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
