// pt.37 (2026-05-27) — toast-with-Undo helper for low-stakes deletes.
//
// Replaces window.confirm() for "Tier 1" destructive operations where:
// - the deleted item is cheap to re-create (manpower row, tray photo,
//   draft DR, queued voice memo)
// - the user does this often enough that a confirmation modal is friction
// - we want the operation to feel instant
//
// Pattern matches Gmail / Slack / Linear: click trash → toast appears
// with an Undo button → if the user clicks Undo within 5s, the backend
// call is never fired. If 5s elapses with no Undo, the commit fires.
//
// USAGE (simple, no optimistic UI):
//   import { deleteWithUndo } from "../lib/deleteWithUndo";
//
//   const remove = (rid) => {
//     deleteWithUndo({
//       label: "Manpower row deleted",
//       onCommit: async () => {
//         await api.delete(`/daily-presence/${rid}`);
//         reload();
//       },
//     });
//   };
//
// USAGE (with optimistic hide for instant-feeling UX):
//   const remove = (rid) => {
//     const row = rows.find(r => r.id === rid);
//     deleteWithUndo({
//       label: "Photo archived",
//       onCommit: async () => { await api.delete(`/photo-tray/${rid}`); },
//       onOptimisticHide: () => setRows(prev => prev.filter(r => r.id !== rid)),
//       onRestore:        () => setRows(prev => [...prev, row]),
//     });
//   };
//
// NOTE on backend semantics: this is *deferred commit*, not soft-delete.
// The backend DELETE fires after 5s of no Undo. If the user reloads
// the page during the undo window, the optimistic hide is lost on
// browsers that don't keep the timer alive — but the commit will still
// fire as long as the tab stays alive. For a true soft-delete + undo
// pattern across reloads we'd need backend support (deleted_at column
// + nightly cleanup). Deferred is the right MVP.

import { toast } from "../components/ui/sonner";

const DEFAULT_DURATION_MS = 5000;

export function deleteWithUndo({
  label = "Deleted",
  onCommit,
  onOptimisticHide,
  onRestore,
  duration = DEFAULT_DURATION_MS,
}) {
  if (typeof onCommit !== "function") {
    // eslint-disable-next-line no-console
    console.warn("[deleteWithUndo] onCommit is required");
    return;
  }

  // 1) Hide the item immediately if the caller wants the instant feel.
  if (typeof onOptimisticHide === "function") {
    try { onOptimisticHide(); }
    catch (e) { /* swallow — keep going */ } // eslint-disable-line no-unused-vars
  }

  // 2) Defer the actual commit by `duration` ms. The user can cancel
  //    via the Undo button on the toast.
  let cancelled = false;
  const timer = setTimeout(async () => {
    if (cancelled) return;
    try {
      await onCommit();
    } catch (e) {
      // Commit failed — restore the optimistic hide if the caller
      // provided a restore handler, and surface the error.
      // eslint-disable-next-line no-console
      console.error("[deleteWithUndo] commit failed:", e);
      if (typeof onRestore === "function") {
        try { onRestore(); } catch {}
      }
      toast.error(
        e?.response?.data?.detail
        || e?.message
        || `Failed: ${label.toLowerCase()}`
      );
    }
  }, duration);

  // 3) Show the toast with an Undo action that cancels the timer +
  //    restores the optimistic UI.
  toast(label, {
    duration,
    action: {
      label: "Undo",
      onClick: () => {
        cancelled = true;
        clearTimeout(timer);
        if (typeof onRestore === "function") {
          try { onRestore(); } catch {}
        }
      },
    },
  });
}
