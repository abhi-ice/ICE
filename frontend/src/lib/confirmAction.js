// pt.37 (2026-05-27) — themed replacement for window.confirm().
//
// 19 call sites across 14 files used the native browser confirm dialog,
// which doesn't respect light/dark theme, blocks the main thread, has
// the browser's own header ("...says"), and offers no Undo. This module
// + the matching <ConfirmActionDialog /> mounted in AppLayout deliver
// a themed Radix Dialog that drops in as a near-direct replacement.
//
// Old:
//   if (!window.confirm("Delete vendor?")) return;
//   await api.delete(...);
//
// New:
//   const ok = await confirmAction({ title: "Delete vendor?",
//                                    message: '"GKM" cannot be undone.' });
//   if (!ok) return;
//   await api.delete(...);
//
// For high-stakes irreversible cascades, pass requireType so the user
// must type a sentinel string (e.g. project name) before Confirm
// enables — mirrors GitHub repo delete / AWS console:
//
//   const ok = await confirmAction({
//     title: 'Delete project "MSU"?',
//     message: 'Removes 247 daily reports, 12 delays, 89 photos. Cannot be undone.',
//     requireType: 'MSU',
//     confirmText: 'Delete project',
//   });
//
// For low-stakes cheap deletes (manpower row, tray photo) prefer
// `deleteWithUndo` (toast + Undo) — see lib/deleteWithUndo.js.
//
// Implementation: module-level singleton handler. The Provider sets it
// on mount; calling confirmAction before the Provider mounts falls back
// to window.confirm() so nothing silently breaks.

let handler = null;

/** @internal — called by <ConfirmActionDialog /> on mount. */
export function setConfirmActionHandler(fn) {
  handler = fn;
}

/**
 * Open a themed confirm dialog. Returns a Promise<boolean> — true on
 * Confirm, false on Cancel / Esc / backdrop click.
 *
 * @param {object} opts
 * @param {string} [opts.title]        Header, defaults to "Are you sure?"
 * @param {string} [opts.message]      Body sentence under the title
 * @param {string} [opts.confirmText]  Confirm button label, defaults "Confirm"
 * @param {string} [opts.cancelText]   Cancel button label, defaults "Cancel"
 * @param {boolean} [opts.danger=true] Style as destructive (red icon + red button)
 * @param {string} [opts.requireType]  If set, user must type this exact string to enable Confirm
 * @param {string} [opts.icon]         "trash" for delete-style icon, otherwise warn-triangle
 * @returns {Promise<boolean>}
 */
export function confirmAction(opts) {
  if (!handler) {
    // Fallback so the call site doesn't silently hang if the provider
    // isn't mounted yet. The themed dialog is the design goal but the
    // semantics (returns boolean) must hold no matter what.
    // eslint-disable-next-line no-console
    console.warn("[confirmAction] handler not mounted; falling back to window.confirm");
    return Promise.resolve(window.confirm(opts?.message || opts?.title || "Confirm?"));
  }
  return handler(opts || {});
}
