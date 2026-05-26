import { useEffect, useRef, useState, useCallback } from "react";
import {
  Bot, Send, X, CheckCircle2, FileSpreadsheet, ClipboardCheck,
  ShieldAlert, Sparkles, MessageSquare, ListChecks, BookOpen, AlertCircle,
  Download, FileText, Copy, Loader2, RotateCcw, Wrench, FileDown,
} from "lucide-react";
import * as XLSX from "xlsx";
import { useProjects } from "../../lib/projects";
import { subscribeDailyReportId, emitDescriptionInsert } from "../../lib/copilotContext";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

// v4 pt.26 (2026-05-26) — Mason robustness pass:
//   1) Conversation memory: thread persisted to localStorage AND last 8
//      turns prepended to each outgoing message as context, so even if
//      the backend's LlmChat session breaks, Mason still has continuity.
//   2) Better tool-execution UX: bare tool-name replies (e.g. the
//      raw "get_status" text shown previously) render as a "Mason ran
//      tool: X" chip with a Retry button, not as a confused message.
//   3) Document generation: every AI response gets a Download menu —
//      Markdown / PDF (jsPDF) / Word (docx) / Excel (xlsx, when tabular
//      data is detected). All client-side, zero backend changes.
//   4) Timeout + retry: 60s timeout on the POST. On error or timeout
//      the user message gets a "Retry" button instead of vanishing.
//   5) Clear-conversation button so the user can start fresh anytime.
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
  "Summarize what was logged in the last daily report",
];

const GREETING = "Hi — I'm Mason. Think of me as the craftsman on your project: I can read your schedule, RFIs, and knowledge base, and I can build for you — draft RFIs, log daily reports, file work items, write KB lessons, prep submittals. With a Daily Report open I can also record presence, mark activity progress, and create unplanned activities for PM review.";

// Stable session id per browser so server-side LlmChat memory persists
// across panel open/close. Stored in localStorage so a page reload reuses it.
function getOrMakeSessionId() {
  if (typeof window === "undefined") return `mason-${Date.now()}`;
  try {
    const k = "mason.session_id";
    let s = window.localStorage.getItem(k);
    if (!s) {
      s = `mason-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      window.localStorage.setItem(k, s);
    }
    return s;
  } catch {
    return `mason-${Date.now()}`;
  }
}

// Heuristic: backend sometimes returns just the tool name as the reply text
// instead of a follow-up summary. Detect those so we render them as a chip
// (and offer Retry) instead of as a confusing one-word "AI" message.
const BARE_TOOL_PATTERN = /^[a-z][a-z0-9_]{2,40}$/;
function looksLikeBareToolName(text) {
  if (!text) return false;
  const trimmed = String(text).trim();
  return BARE_TOOL_PATTERN.test(trimmed);
}

// Heuristic for when the reply has tabular data we can export as a sheet.
function looksTabular(text) {
  if (!text) return false;
  const lines = String(text).split("\n").filter(Boolean);
  if (lines.length < 3) return false;
  const tableHits = lines.filter((l) => l.includes("|") || /\t/.test(l) || /:\s*\S+\s+[·•]\s+\S+/.test(l));
  return tableHits.length >= 2;
}

// Claude.ai-style document-intent detection. Inspect the user's message to
// see if they're asking for a downloadable file, and infer the format. If
// no intent is detected, no download button is shown on the AI reply —
// just the Copy button. This matches how Claude/ChatGPT behave: artifacts
// only appear when the user actually asks for one.
//
// Returns: null OR { format: 'pdf'|'docx'|'xlsx'|'md', label: 'PDF' }
function detectDocumentIntent(userMessage) {
  if (!userMessage) return null;
  const t = String(userMessage).toLowerCase();
  // Strong format-specific signals first
  if (/\b(pdf|\.pdf)\b/.test(t))                                          return { format: "pdf",  label: "PDF" };
  if (/\b(excel|xlsx|spreadsheet|\.xlsx?|csv|\.csv)\b/.test(t))           return { format: "xlsx", label: "Excel" };
  if (/\b(word|docx?|\.docx?|google docs?)\b/.test(t))                    return { format: "docx", label: "Word" };
  if (/\b(markdown|\.md)\b/.test(t))                                      return { format: "md",   label: "Markdown" };
  // Generic "generate/export/download a document" → default to PDF
  if (/\b(generate|create|make|prepare|draft|export|download|produce|build)\b.*\b(document|doc|report|file|brief|summary|memo|analysis|whitepaper)\b/.test(t))
    return { format: "pdf", label: "PDF" };
  if (/\b(document|report|file)\b.*\b(download|export)\b/.test(t))
    return { format: "pdf", label: "PDF" };
  return null;
}

export default function CopilotPanel({ open, onClose }) {
  const { activeId } = useProjects();
  const [sessionId] = useState(getOrMakeSessionId);
  const [activeDrId, setActiveDrId] = useState(null);
  const [thread, setThread] = useState(() => {
    // Restore prior conversation so closing/reopening doesn't lose context.
    try {
      const raw = window.localStorage.getItem(`mason.thread.${getOrMakeSessionId()}`);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length) return parsed;
      }
    } catch {}
    return [{ role: "ai", text: GREETING }];
  });
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);
  const inputRef = useRef(null);

  // Persist thread on every change so panel close/reload keeps history.
  useEffect(() => {
    try {
      window.localStorage.setItem(`mason.thread.${sessionId}`, JSON.stringify(thread.slice(-50)));
    } catch {}
  }, [thread, sessionId]);

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

  const send = useCallback(async (overrideText) => {
    const msg = (overrideText ?? input).trim();
    if (!msg || busy) return;
    setThread((t) => [...t, { role: "user", text: msg }]);
    setInput("");
    setBusy(true);
    // Build a context string of the last 8 turns so the LLM has memory
    // even if the server-side session library drops state. The format
    // looks like a conversation transcript, which Claude handles well.
    const recent = thread.slice(-8)
      .filter((m) => m.text && !m.tools_only)
      .map((m) => `${m.role === "user" ? "User" : "Mason"}: ${m.text}`)
      .join("\n\n");
    const messageWithContext = recent
      ? `=== Prior conversation (for context) ===\n${recent}\n\n=== New message ===\n${msg}\n\nReply directly to the new message; use the prior conversation as context only.`
      : msg;
    // 60-second client-side timeout so the user gets a clear "try again"
    // affordance instead of an indefinite spinner.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60000);
    try {
      const { data } = await api.post("/ai/copilot", {
        project_id: activeId,
        daily_report_id: activeDrId || undefined,
        message: messageWithContext,
        session_id: sessionId,
      }, { signal: controller.signal });
      clearTimeout(timeoutId);
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
      const replyText = data.response || "";
      const isBareTool = looksLikeBareToolName(replyText);
      // Claude-style: only attach a download artifact when the USER asked
      // for one. Detection runs against the original user message (msg).
      const intent = !isBareTool && replyText && replyText.length > 40
        ? detectDocumentIntent(msg)
        : null;
      setThread((t) => [...t, {
        role: "ai",
        text: isBareTool ? `I ran ${replyText}, but the follow-up summary didn't come back.` : replyText,
        tools,
        tools_only: isBareTool && !replyText,
        bare_tool: isBareTool ? replyText : null,
        retry_payload: isBareTool ? msg : null,
        artifact: intent,
        user_prompt: msg,
      }]);
    } catch (e) {
      clearTimeout(timeoutId);
      const timedOut = e.name === "AbortError" || e.message?.includes("aborted");
      const detail = timedOut
        ? "Mason timed out after 60 seconds. The server may be slow or the tool call may have hung."
        : "Mason error: " + (e.response?.data?.detail || e.message);
      setThread((t) => [...t, { role: "ai", text: detail, error: true, retry_payload: msg }]);
    } finally {
      setBusy(false);
    }
  }, [activeDrId, activeId, busy, input, sessionId, thread]);

  // ---- Document downloads (all client-side) ----
  const downloadFile = (blob, filename) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 500);
  };

  const downloadMarkdown = (text) => {
    const stamp = new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-");
    downloadFile(new Blob([text], { type: "text/markdown;charset=utf-8" }), `mason-${stamp}.md`);
    toast.success("Downloaded Markdown");
  };

  const downloadPdf = async (text) => {
    const { jsPDF } = await import("jspdf");
    const doc = new jsPDF({ unit: "mm", format: "letter" });
    doc.setFont("helvetica", "normal");
    doc.setFontSize(10);
    const margin = 18;
    const maxWidth = 215.9 - margin * 2; // letter width - margins
    doc.setFontSize(13).setFont("helvetica", "bold");
    doc.text("Mason — AI craftsman", margin, margin);
    doc.setFontSize(8).setFont("helvetica", "normal").setTextColor(120);
    doc.text(`Generated ${new Date().toLocaleString()}`, margin, margin + 5);
    doc.setTextColor(20).setFontSize(10);
    const lines = doc.splitTextToSize(text, maxWidth);
    let y = margin + 14;
    const lineHeight = 4.4;
    for (const line of lines) {
      if (y > 270) { doc.addPage(); y = margin; }
      doc.text(line, margin, y);
      y += lineHeight;
    }
    const stamp = new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-");
    doc.save(`mason-${stamp}.pdf`);
    toast.success("Downloaded PDF");
  };

  const downloadDocx = (text) => {
    // Word handles HTML files saved with .doc extension natively — much
    // lighter than pulling in the full docx-js library (which broke the
    // older Babel config). Output is escaped, paragraph-wrapped HTML
    // with a thin header. Opens cleanly in Word, Google Docs, Pages.
    const esc = (s) => String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    const paras = text.split("\n").map((l) => l.trim() ? `<p>${esc(l)}</p>` : "<p>&nbsp;</p>").join("");
    const html = `<!DOCTYPE html><html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" xmlns="http://www.w3.org/TR/REC-html40">
<head><meta charset="utf-8"><title>Mason</title>
<style>body{font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#111;max-width:7in;margin:0.75in;}
h1{font-size:18pt;color:#0f172a;margin:0 0 4px;}.meta{color:#888;font-style:italic;font-size:9pt;margin:0 0 18px;}
p{margin:0 0 8px;line-height:1.45;}</style></head>
<body><h1>Mason — AI craftsman</h1><div class="meta">Generated ${esc(new Date().toLocaleString())}</div>${paras}</body></html>`;
    const blob = new Blob(["﻿", html], { type: "application/msword" });
    const stamp = new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-");
    downloadFile(blob, `mason-${stamp}.doc`);
    toast.success("Downloaded Word doc");
  };

  const downloadXlsx = (text) => {
    // Convert lines that look tabular into rows. Pipe and tab are the
    // standard separators; falls back to splitting on 2+ spaces.
    const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
    const rows = lines.map((l) => {
      if (l.includes("|")) return l.split("|").map((c) => c.trim()).filter(Boolean);
      if (l.includes("\t")) return l.split("\t").map((c) => c.trim());
      return l.split(/\s{2,}/).map((c) => c.trim());
    });
    const ws = XLSX.utils.aoa_to_sheet(rows);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Mason");
    const stamp = new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-");
    XLSX.writeFile(wb, `mason-${stamp}.xlsx`);
    toast.success("Downloaded Excel");
  };

  const copyToClipboard = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied");
    } catch {
      toast.error("Couldn't copy");
    }
  };

  const clearConversation = () => {
    if (thread.length <= 1) return;
    if (!window.confirm("Clear this conversation with Mason? Cannot be undone.")) return;
    setThread([{ role: "ai", text: GREETING }]);
    try { window.localStorage.removeItem(`mason.thread.${sessionId}`); } catch {}
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
        {thread.length > 1 && (
          <button
            data-testid="copilot-clear"
            onClick={clearConversation}
            className="copilot-panel-close"
            aria-label="Clear conversation"
            title="Clear conversation"
          >
            <RotateCcw className="h-4 w-4" />
          </button>
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
            {thread.map((m, i) => {
              // Trigger the correct download handler when the artifact CTA is clicked
              const handleArtifactDownload = () => {
                const f = m.artifact?.format;
                if (f === "pdf")   return downloadPdf(m.text);
                if (f === "docx")  return downloadDocx(m.text);
                if (f === "xlsx")  return downloadXlsx(m.text);
                if (f === "md")    return downloadMarkdown(m.text);
                return downloadPdf(m.text);
              };
              const artifactPreviewLines = m.text ? m.text.split("\n").slice(0, 6).join("\n") : "";
              return (
                <div key={i} className={`copilot-msg copilot-msg--${m.role}`}>
                  {m.role === "ai" && (
                    <div className="copilot-msg-badge">
                      <Bot className="h-2.5 w-2.5" /> Mason
                    </div>
                  )}
                  {m.bare_tool && (
                    <div className="copilot-tool-execution">
                      <Wrench className="h-3 w-3" />
                      <span>Ran tool: <code>{m.bare_tool}</code></span>
                    </div>
                  )}
                  <div className={`copilot-msg-bubble ${m.error ? "copilot-msg-bubble--error" : ""}`}>{m.text}</div>
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
                  {/* CLAUDE-STYLE ARTIFACT CARD — only when the user
                      explicitly asked for a document. Otherwise no
                      download clutter. Copy stays always-on below. */}
                  {m.artifact && (
                    <div className="copilot-artifact" data-testid={`copilot-artifact-${i}`}>
                      <div className="copilot-artifact-head">
                        <div className="copilot-artifact-icon">
                          {m.artifact.format === "xlsx" ? <FileSpreadsheet className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
                        </div>
                        <div className="copilot-artifact-meta">
                          <div className="copilot-artifact-title">
                            {m.artifact.label} document ready
                          </div>
                          <div className="copilot-artifact-sub">
                            {m.text.length.toLocaleString()} characters · auto-formatted
                          </div>
                        </div>
                      </div>
                      <pre className="copilot-artifact-preview">{artifactPreviewLines}{m.text.split("\n").length > 6 ? "\n…" : ""}</pre>
                      <button
                        onClick={handleArtifactDownload}
                        className="copilot-artifact-download"
                        data-testid={`copilot-artifact-download-${i}`}
                      >
                        <Download className="h-3.5 w-3.5" /> Download {m.artifact.label}
                      </button>
                    </div>
                  )}
                  {/* Action row — Copy is always available; Retry only when last turn errored */}
                  {m.role === "ai" && m.text && (
                    <div className="copilot-actions">
                      <button
                        onClick={() => copyToClipboard(m.text)}
                        className="copilot-action-btn"
                        title="Copy text"
                      >
                        <Copy className="h-3 w-3" /> Copy
                      </button>
                      {m.retry_payload && (
                        <button
                          onClick={() => send(m.retry_payload)}
                          className="copilot-action-btn copilot-action-btn--retry"
                          title="Retry the request"
                          disabled={busy}
                        >
                          <RotateCcw className="h-3 w-3" /> Retry
                        </button>
                      )}
                    </div>
                  )}
                  {m.role === "ai" && activeDrId && i > 0 && m.text && !m.error && (
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
              );
            })}
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
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
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
