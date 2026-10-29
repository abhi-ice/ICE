// pt.37 (2026-05-27) — themed modal that backs `confirmAction()` calls.
// Mounted once at AppLayout. Listens on a module-level singleton; every
// confirmAction({...}) call opens THIS dialog with the provided options
// and resolves with the user's choice.
//
// Design follows Option C in design-mockup/delete-confirmation-options.html:
// - Red circular icon (Trash2 for delete, AlertTriangle for non-delete
//   destructive operations) when danger=true (default)
// - Title + optional message block
// - Optional "type to confirm" field (requireType prop) for Tier-3
//   irreversible operations (project delete, tenant close, hard-delete
//   of submitted records)
// - Cancel + Confirm buttons in the footer; Confirm uses the
//   destructive button variant when danger=true

import { useEffect, useRef, useState } from "react";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogDescription } from "./ui/dialog";
import { Button } from "./ui/button";
import { AlertTriangle, Trash2 } from "lucide-react";
import { setConfirmActionHandler } from "../lib/confirmAction";

export default function ConfirmActionDialog() {
  const [opts, setOpts] = useState(null);
  const [typed, setTyped] = useState("");
  const resolveRef = useRef(null);

  useEffect(() => {
    setConfirmActionHandler((o) =>
      new Promise((resolve) => {
        // If a stale dialog is somehow still open (shouldn't happen, but
        // belt-and-suspenders), resolve the prior promise as cancelled
        // before opening the new one so no resolver is orphaned.
        if (resolveRef.current) {
          try { resolveRef.current(false); } catch {}
        }
        resolveRef.current = resolve;
        setTyped("");
        setOpts(o);
      })
    );
    return () => setConfirmActionHandler(null);
  }, []);

  const close = (result) => {
    if (resolveRef.current) {
      const r = resolveRef.current;
      resolveRef.current = null;
      r(result);
    }
    setOpts(null);
    setTyped("");
  };

  if (!opts) return null;

  const isDanger = opts.danger !== false;     // default destructive
  const requireType = opts.requireType || null;
  // requireReason: {label, min} — Confirm resolves with the typed reason.
  const requireReason = opts.requireReason || null;
  const reasonMin = requireReason?.min || 10;
  const confirmEnabled = requireReason
    ? typed.trim().length >= reasonMin
    : (!requireType || typed.trim() === requireType);
  const confirmValue = requireReason ? typed.trim() : true;
  const IconEl = opts.icon === "trash" ? Trash2 : AlertTriangle;

  return (
    <Dialog open={!!opts} onOpenChange={(o) => !o && close(false)}>
      <DialogContent
        data-testid="confirm-action-dialog"
        className="max-w-md"
        onEscapeKeyDown={() => close(false)}
      >
        <DialogHeader>
          <div className="flex items-start gap-3 pr-6">
            {isDanger && (
              <div className="h-9 w-9 rounded-full bg-rose-100 dark:bg-rose-900/30 grid place-items-center shrink-0">
                <IconEl className="h-4 w-4 text-rose-600 dark:text-rose-400" strokeWidth={2} />
              </div>
            )}
            <div className="flex-1 min-w-0 text-left">
              <DialogTitle>{opts.title || "Are you sure?"}</DialogTitle>
              {opts.message && (
                <DialogDescription className="mt-1.5 leading-relaxed">
                  {opts.message}
                </DialogDescription>
              )}
            </div>
          </div>
        </DialogHeader>

        {requireReason && (
          <div>
            <label className="block text-xs text-muted-foreground mb-1.5">
              {requireReason.label || "Reason (saved to the audit trail)"}
            </label>
            <textarea
              autoFocus
              data-testid="confirm-action-reason-input"
              rows={3}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              className="w-full px-2.5 py-2 rounded border border-input bg-background text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder={requireReason.placeholder || "Why is this being deleted?"}
            />
            <div className="text-[11px] text-muted-foreground mt-1" data-testid="confirm-action-reason-hint">
              At least {reasonMin} characters ({typed.trim().length}/{reasonMin})
            </div>
          </div>
        )}

        {requireType && (
          <div>
            <label className="block text-xs text-muted-foreground mb-1.5">
              Type{" "}
              <code className="px-1.5 py-0.5 rounded bg-muted text-rose-700 dark:text-rose-300 font-mono text-[11px]">
                {requireType}
              </code>{" "}
              to confirm
            </label>
            <input
              autoFocus
              data-testid="confirm-action-type-input"
              type="text"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && confirmEnabled) close(confirmValue);
              }}
              className="w-full h-9 px-2.5 rounded border border-input bg-background text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder={requireType}
            />
          </div>
        )}

        <DialogFooter>
          <Button
            data-testid="confirm-action-cancel"
            variant="outline"
            onClick={() => close(false)}
          >
            {opts.cancelText || "Cancel"}
          </Button>
          <Button
            data-testid="confirm-action-confirm"
            variant={isDanger ? "destructive" : "default"}
            onClick={() => close(confirmValue)}
            disabled={!confirmEnabled}
          >
            {opts.confirmText || (isDanger ? "Delete" : "Confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
