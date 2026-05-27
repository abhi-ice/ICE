// pt.37 (2026-05-27) — Option C from
// design-mockup/submit-blockers.html: mini progress circle + status
// text + "show me" link that opens a popover with the unfinished
// items. Replaces the old linear bar + "X to go — A, B, C" inline
// text in DailyReports' StickySubmitBar.
//
// Design source: Notion / Asana / Linear progress circles. Gamified-
// but-grown-up: gives an immediate sense of "how close am I" via the
// arc fill, without the cluttered list of remaining items inline.
//
// USAGE:
//   <SubmitProgress
//     completeCount={4}
//     total={7}
//     missingLabels={["Events", "Photos", "Audit"]}
//   />
//
// Threshold copy:
//   100%      → "All set"          (emerald arc)
//   80-99%    → "Almost ready"     (amber arc)
//   50-79%    → "Halfway there"    (amber arc)
//   1-49%     → "Getting started"  (slate arc)
//   0%        → "Just getting started" (slate arc)

import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";

function tierFromPct(pct) {
  if (pct >= 100) return { label: "All set",            ring: "#10b981", text: "text-emerald-700 dark:text-emerald-300" };
  if (pct >= 80)  return { label: "Almost ready",       ring: "#f59e0b", text: "text-amber-700 dark:text-amber-300" };
  if (pct >= 50)  return { label: "Halfway there",      ring: "#f59e0b", text: "text-amber-700 dark:text-amber-300" };
  if (pct > 0)    return { label: "Getting started",    ring: "#94a3b8", text: "text-slate-600 dark:text-slate-300" };
  return            { label: "Just getting started",    ring: "#94a3b8", text: "text-slate-600 dark:text-slate-300" };
}

export default function SubmitProgress({ completeCount = 0, total = 0, missingLabels = [] }) {
  const pct = total > 0 ? Math.round((completeCount / total) * 100) : 0;
  const tier = tierFromPct(pct);
  const missingCount = missingLabels.length;

  // SVG arc geometry — 14px radius circle in a 32px viewbox, 3px stroke.
  // -rotate-90 puts the arc start at 12-o'clock instead of 3-o'clock.
  const r = 12;
  const c = 2 * Math.PI * r;
  const offset = c - (pct / 100) * c;

  return (
    <div className="inline-flex items-center gap-2.5 shrink-0">
      <div className="relative h-8 w-8 shrink-0" data-testid="submit-progress-circle">
        <svg viewBox="0 0 32 32" className="absolute inset-0 -rotate-90">
          <circle
            cx="16" cy="16" r={r}
            fill="none"
            stroke="var(--v4-bg-surface-2)"
            strokeWidth="3"
          />
          <circle
            cx="16" cy="16" r={r}
            fill="none"
            stroke={tier.ring}
            strokeWidth="3"
            strokeDasharray={c}
            strokeDashoffset={offset}
            strokeLinecap="round"
            style={{ transition: "stroke-dashoffset 250ms ease-out" }}
          />
        </svg>
        <div className="absolute inset-0 grid place-items-center text-[9px] font-semibold tabular-nums tracking-tight">
          {completeCount}/{total}
        </div>
      </div>
      <div className="leading-tight">
        <div className={`text-xs font-semibold ${tier.text}`} data-testid="submit-progress-status">
          {tier.label}
        </div>
        {missingCount > 0 ? (
          <div className="text-[11px] text-muted-foreground">
            {missingCount} item{missingCount === 1 ? "" : "s"} left{" — "}
            <Popover>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  data-testid="submit-progress-show-me"
                  className="font-medium text-amber-700 dark:text-amber-300 hover:underline focus:underline focus:outline-none"
                >
                  show me
                </button>
              </PopoverTrigger>
              <PopoverContent side="top" align="start" className="w-60 p-2.5">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold mb-1.5">
                  Still needed before submit
                </div>
                <ul className="space-y-1">
                  {missingLabels.map((l, i) => (
                    <li key={i} className="text-xs flex items-start gap-1.5 leading-snug">
                      <span className="mt-1 h-1.5 w-1.5 rounded-full bg-amber-400 shrink-0" />
                      <span>{l}</span>
                    </li>
                  ))}
                </ul>
              </PopoverContent>
            </Popover>
          </div>
        ) : (
          <div className="text-[11px] text-muted-foreground">Ready to submit</div>
        )}
      </div>
    </div>
  );
}
