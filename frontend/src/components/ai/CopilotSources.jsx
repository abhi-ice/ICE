import { Fragment } from "react";
import { BookOpen, ChevronRight } from "lucide-react";

// Search everything (phase 2): the assistant's reply can carry a
// "citations" list — the records it found with the search tool and quoted
// as [S1], [S2] ... in its answer. This file renders:
//   * renderTextWithCites — the answer text with each [S1] marker turned into
//     a link to that record (only for numbers that are really in the list);
//   * CopilotSources      — a compact "Sources" list under the answer.
// Each citation's url is an in-app path such as "/daily-reports?openId=...".
// When a reply has no citations nothing here renders, so the panel looks
// exactly as it did before.

// Single tags only ([S1]); the server already splits "[S1, S3]" into "[S1] [S3]".
const CITE_PATTERN = /\[(S\d+)\]/g;

// Only in-app paths are followed; anything else is shown as plain text.
export function isAppPath(url) {
  return typeof url === "string" && url.startsWith("/") && !url.startsWith("//");
}

function citeMap(citations) {
  const map = new Map();
  for (const c of citations || []) {
    if (c && c.cite && !map.has(c.cite)) map.set(c.cite, c);
  }
  return map;
}

function describe(c) {
  return [c.label, c.project_name, c.date].filter(Boolean).join(" · ");
}

// Turns "Concrete was poured [S1]." into text + a clickable [S1].
export function renderTextWithCites(text, citations, onOpen) {
  if (!text || !citations || citations.length === 0) return text;
  const map = citeMap(citations);
  const out = [];
  let last = 0;
  let match;
  CITE_PATTERN.lastIndex = 0;
  while ((match = CITE_PATTERN.exec(text)) !== null) {
    const c = map.get(match[1]);
    if (!c || !isAppPath(c.url)) continue;
    if (match.index > last) out.push(text.slice(last, match.index));
    out.push(
      <button
        key={`cite-${match.index}`}
        type="button"
        onClick={() => onOpen(c)}
        title={describe(c)}
        data-testid={`copilot-inline-cite-${c.cite}`}
        className="inline px-0.5 rounded font-semibold text-[color:var(--brand-primary)] hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--brand-primary)]"
      >
        [{c.cite}]
      </button>,
    );
    last = match.index + match[0].length;
  }
  if (out.length === 0) return text;
  if (last < text.length) out.push(text.slice(last));
  return out.map((part, i) => <Fragment key={i}>{part}</Fragment>);
}

export default function CopilotSources({ citations, onOpen, testid }) {
  if (!Array.isArray(citations) || citations.length === 0) return null;
  return (
    <div
      data-testid={testid}
      className="mt-1 w-full max-w-[88%] min-w-0 rounded-lg border border-[color:var(--v4-border)] px-2 py-1.5"
    >
      <div className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-[color:var(--v4-fg-secondary)]">
        <BookOpen className="h-3 w-3" aria-hidden /> Sources
      </div>
      <ul className="mt-1 space-y-0.5">
        {citations.map((c, idx) => {
          const meta = [c.project_name, c.date].filter(Boolean).join(" · ");
          const linked = isAppPath(c.url);
          const body = (
            <>
              <span className="shrink-0 font-semibold text-[color:var(--brand-primary)]">[{c.cite}]</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[color:var(--v4-fg-primary)]">
                  {c.label || "Record"}
                  {meta && <span className="text-[color:var(--v4-fg-secondary)]"> · {meta}</span>}
                </span>
                {c.excerpt && (
                  <span className="block truncate text-[10.5px] text-[color:var(--v4-fg-secondary)]">
                    {c.excerpt}
                  </span>
                )}
              </span>
              {linked && <ChevronRight className="h-3 w-3 shrink-0 text-[color:var(--v4-fg-secondary)]" aria-hidden />}
            </>
          );
          const rowCls = "flex w-full min-w-0 items-start gap-1.5 rounded px-1 py-1 text-left text-[12px] leading-snug max-[480px]:min-h-[44px] max-[480px]:items-center";
          return (
            <li key={`${c.cite}-${c.source_id || idx}`} className="min-w-0">
              {linked ? (
                <button
                  type="button"
                  onClick={() => onOpen(c)}
                  title={c.excerpt || describe(c)}
                  data-testid={`copilot-source-${c.cite}`}
                  className={`${rowCls} hover:bg-[rgba(16,185,129,0.08)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--brand-primary)]`}
                >
                  {body}
                </button>
              ) : (
                <div title={c.excerpt || describe(c)} className={rowCls}>{body}</div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
