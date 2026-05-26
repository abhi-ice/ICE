import { useEffect, useRef, useState } from "react";
import {
  Bot, Send, X, CheckCircle2, FileSpreadsheet, ClipboardCheck,
  ShieldAlert, Sparkles, MessageSquare, ListChecks, BookOpen, AlertCircle,
} from "lucide-react";
import { useProjects } from "../../lib/projects";
import { subscribeDailyReportId, emitDescriptionInsert } from "../../lib/copilotContext";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

// v4 pt.25 (2026-05-26) — Frosted-glass redesign + AI rename.
// Design picked from /design-mockup/copilot-panel-designs.html Option C:
// translucent panel with backdrop blur + brand-emerald radial wash, semi-
// transparent AI bubbles so the page peeks through, gradient-emerald user
// bubbles. AI renamed from generic "Copilot" → "Mason" (Federal
// construction term for the on-site coordinator who knows everything).
// Backend integration unchanged from pt.23 — same /ai/copilot POST, same
// state shape, every testid preserved.
const CAPABILITIES = [
  { icon: ListChecks,      label: "Schedule",       hint: "Add tasks, check dependencies, recompute CPM" },
  { icon: MessageSquare,   label: "RFIs",           hint: "Draft, send, link to tasks, track aging" },
  { icon: FileSpreadsheet, label: "Daily Reports",  hint: "Record presence, mark progress, create unplanned" },
  { icon: BookOpen,        label: "Knowledge Base", hint: "Cite lessons from past projects" },
];

const SAMPLE_PROMPTS = [
  "Add task install conference room AV, duration 4 days",
  "Record presence electrical 4 workers 8 hours each",
  "Draft RFI about the wall vent conflict on level 2",
  "What's blocking my critical path this week?",
];

export default function CopilotPanel({ open, onClose }) {
  const { activeId } = useProjects();
  const [sessionId] = useState(`copilot-${Date.now()}`);
  const [activeDrId, setActiveDrId] = useState(null);
  const [thread, setThread] = useState([
    {
      role: "ai",
      text: "Hi — I'm Mason. Think of me as the craftsman on your project: I can read your schedule, RFIs, and knowledge base, and I can build for you — draft RFIs, log daily reports, file work items, write KB lessons, prep submittals. With a Daily Report open I can also record presence, mark activity progress, and create unplanned activities for PM review.",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread]);

  useEffect(() => subscribeDailyReportId(setActiveDrId), []);

  // Auto-grow the composer textarea as the user types
  useEffect(() => {
    const ta = inputRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(140, ta.scrollHeight) + "px";
  }, [input]);

  const send = async (overrideText) => {
    const msg = (overrideText ?? input).trim();
    if (!msg || busy) return;
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
      setThread((t) => [...t, { role: "ai", text: "Mason error: " + (e.response?.data?.detail || e.message) }]);
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;
  const isFresh = thread.length === 1 && thread[0]?.role === "ai";

  return (
    <div data-testid="copilot-panel" data-skip-page-stacking className="copilot-panel fixed">
      {/* ---- HEADER ---- */}
      <header className="copilot-panel-header">
        <div className="copilot-panel-icon" aria-hidden>
          <Bot className="h-4 w-4" strokeWidth={2} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="copilot-panel-title">Mason</div>
          <div className="copilot-panel-tagline">
            <Sparkles className="h-2.5 w-2.5" />
            <span>Your AI craftsman · drafts · reports · schedule · KB</span>
          </div>
        </div>
        {activeDrId && (
          <span data-testid="copilot-dr-scope" className="copilot-scope-pill" title="Active Daily Report in scope">
            <FileSpreadsheet className="h-3 w-3" /> DR scope
          </span>
        )}
        <button
          data-testid="copilot-close"
          onClick={onClose}
          className="copilot-panel-close"
          aria-label="Close Mason"
          title="Close"
        >
          <X className="h-4 w-4" />
        </button>
      </header>

      {/* ---- BODY ---- */}
      <div className="copilot-panel-body">
        {isFresh ? (
          <div className="copilot-greeting" data-testid="copilot-greeting">
            <div className="copilot-greeting-bubble">{thread[0].text}</div>
            <div className="copilot-cap-label">What Mason can do</div>
            <div className="copilot-cap-grid">
              {CAPABILITIES.map((c) => (
                <div key={c.label} className="copilot-cap-chip" title={c.hint}>
                  <c.icon className="h-3.5 w-3.5" /> {c.label}
                </div>
              ))}
            </div>
            <div className="copilot-cap-label">Try one of these</div>
            <div className="copilot-prompts">
              {SAMPLE_PROMPTS.map((p) => (
                <button
                  key={p}
                  type="button"
                  data-testid={`copilot-sample-${p.slice(0, 12).replace(/\W/g, "")}`}
                  className="copilot-prompt-btn"
                  onClick={() => send(p)}
                  disabled={busy}
                >
                  <Sparkles className="h-3 w-3" /> {p}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            {thread.map((m, i) => (
              <div key={i} className={`copilot-msg copilot-msg--${m.role}`}>
                {m.role === "ai" && (
                  <div className="copilot-msg-badge">
                    <Bot className="h-2.5 w-2.5" /> Mason
                  </div>
                )}
                <div className="copilot-msg-bubble">{m.text}</div>
                {m.tools && m.tools.length > 0 && (
                  <div data-testid={`copilot-tools-${i}`} className="copilot-tools">
                    {m.tools.map((tt, j) => {
                      const isErr = tt.startsWith("error");
                      return (
                        <span key={j} className={`copilot-tool ${isErr ? "copilot-tool--err" : "copilot-tool--ok"}`}>
                          {isErr ? <AlertCircle className="h-2.5 w-2.5" /> : <CheckCircle2 className="h-2.5 w-2.5" />}
                          <span className="copilot-tool-text">{tt}</span>
                        </span>
                      );
                    })}
                  </div>
                )}
                {m.role === "ai" && activeDrId && i > 0 && m.text && (
                  <div className="copilot-send-to">
                    <button
                      data-testid={`copilot-send-qc-${i}`}
                      onClick={() => { emitDescriptionInsert("qc", m.text); toast.success("Sent to QC description"); }}
                      className="copilot-send-to-btn copilot-send-to-btn--qc"
                    >
                      <ClipboardCheck className="h-3 w-3" /> Send to QC
                    </button>
                    <button
                      data-testid={`copilot-send-safety-${i}`}
                      onClick={() => { emitDescriptionInsert("safety", m.text); toast.success("Sent to Safety description"); }}
                      className="copilot-send-to-btn copilot-send-to-btn--safety"
                    >
                      <ShieldAlert className="h-3 w-3" /> Send to Safety
                    </button>
                  </div>
                )}
              </div>
            ))}
          </>
        )}
        {busy && (
          <div className="copilot-thinking" data-testid="copilot-thinking">
            <div className="copilot-msg-badge">
              <Bot className="h-2.5 w-2.5" /> Mason
            </div>
            <div className="copilot-thinking-dots">
              <span /> <span /> <span />
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* ---- INPUT ---- */}
      <div className="copilot-panel-input">
        <div className="copilot-input-wrap">
          <textarea
            ref={inputRef}
            data-testid="copilot-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={busy ? "Mason is thinking…" : "Ask Mason…"}
            className="copilot-input"
            rows={1}
            disabled={busy}
          />
          <button
            data-testid="copilot-send"
            onClick={() => send()}
            disabled={busy || !input.trim()}
            className="copilot-send-btn"
            aria-label="Send"
            title="Send (Enter)"
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="copilot-input-hint">
          <span><kbd>Enter</kbd> send</span>
          <span className="copilot-input-hint-sep">·</span>
          <span><kbd>Shift</kbd>+<kbd>Enter</kbd> newline</span>
          {activeDrId && (
            <>
              <span className="copilot-input-hint-sep">·</span>
              <span className="copilot-input-hint-scope">DR context attached</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
