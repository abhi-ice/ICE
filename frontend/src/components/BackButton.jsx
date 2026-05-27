// pt.37 (2026-05-27) — Option E from
// design-mockup/back-button-options.html: pill with destination label.
// Replaces the ad-hoc "<ChevronLeft /> Back" links scattered across
// detail pages. Says where you're going back TO, not just "Back",
// so users get an extra micro-orientation cue without thinking
// (Procore / Linear pattern).
//
// USAGE:
//   <BackButton to="/delays" label="Delays" />
//
//   // Or, if the destination should be the browser history (rare):
//   <BackButton label="report list" />          // calls nav(-1)
//
//   // Or, custom click handler (e.g. close a modal):
//   <BackButton onClick={() => setOpen(false)} label="overview" />
//
// The label prop is the destination's name; the component renders
// "← Back to {label}". If no label, falls back to plain "← Back"
// (rare — most detail pages know their parent).

import { ChevronLeft } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

export default function BackButton({ to, label, onClick, className = "", testid }) {
  const nav = useNavigate();

  const baseClasses =
    "inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md border border-border bg-card text-xs text-muted-foreground hover:text-foreground hover:bg-muted hover:border-input transition-colors shrink-0";

  const content = (
    <>
      <ChevronLeft className="h-3.5 w-3.5" strokeWidth={2} />
      <span>Back{label ? ` to ${label}` : ""}</span>
    </>
  );

  if (to && !onClick) {
    return (
      <Link
        to={to}
        data-testid={testid || "back-button"}
        className={`${baseClasses} ${className}`}
      >
        {content}
      </Link>
    );
  }

  return (
    <button
      type="button"
      data-testid={testid || "back-button"}
      onClick={onClick || (() => nav(-1))}
      className={`${baseClasses} ${className}`}
    >
      {content}
    </button>
  );
}
