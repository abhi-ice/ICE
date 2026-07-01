// pt.36 (2026-05-27) — small reusable "?" icon next to a page title.
// Click pops up a small popover with longer explanation.
//
// Use this when:
// - the page-header subtitle is the short summary (Option A pattern)
// - AND the page also needs a longer multi-sentence / linked explanation
//   that would otherwise eat a row of vertical space as a yellow brick.
//
// Picked over the prior amber callout bar after Sid review of the
// design-mockup/page-description-options.html — pattern matches
// Stripe / Notion / Asana help-icon-on-title style.
//
// Usage:
//   <h1 className="...">
//     Photos
//     <PageHelp title="Photos">
//       Each card is a tag connecting one photo to one event on one
//       daily report. The same image may appear multiple times if it
//       is tagged to multiple events.
//       <br/><br/>
//       Upload + tag from the <Link to="/photo-tray">Photo tray</Link>.
//     </PageHelp>
//   </h1>
import { HelpCircle } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";

export default function PageHelp({ title, children, side = "bottom" }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="page-help-trigger"
          title={title ? `Help: ${title}` : "Help"}
          aria-label={title ? `Help: ${title}` : "Help"}
          /* small, inline, low-chrome — sits next to the page title
             without competing with it. Hover state lifts to a slightly
             stronger fill so users discover it's clickable. */
          /* Desktop: tiny low-chrome inline "?" (Stripe/Notion style). Phones:
             a defined 32px bordered chip with a subtle fill + larger glyph so
             it reads as an intentional, enterprise component instead of a bare
             floating dot — and is a real tap target. All mobile-gated. */
          className="ml-1 inline-flex h-4 w-4 items-center justify-center rounded-full text-muted-foreground/70 hover:text-foreground hover:bg-muted transition-colors align-baseline max-md:h-8 max-md:w-8 max-md:border max-md:border-border max-md:bg-muted/40 max-md:text-foreground/80 max-md:[&>svg]:h-[18px] max-md:[&>svg]:w-[18px] max-md:ml-1.5 max-md:self-center"
        >
          <HelpCircle className="h-3.5 w-3.5" strokeWidth={1.8} />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={side}
        align="start"
        sideOffset={6}
        className="w-80 max-md:w-[calc(100vw-2rem)] text-xs leading-relaxed text-muted-foreground"
      >
        {title && (
          <div className="text-xs font-semibold text-foreground mb-1.5">{title}</div>
        )}
        {children}
      </PopoverContent>
    </Popover>
  );
}
