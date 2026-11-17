// Who may change daily reports (2026-11-16).
//
// Mirrors backend/auth.py role_can_write_field_events: admin, project
// manager (pm), project executive (px), superintendent (super),
// professional engineer (pe) and foreman may change daily reports and
// everything filed on them. Viewer, submittal technician and scheduling
// engineer can open and read reports but not change them; the server
// refuses their saves with 403 and code "role_read_only". The screens use
// this to hide or disable the edit controls up front.
//
// Report ownership (mirrors backend/services/edit_permissions.py
// can_change_draft / ensure_can_delete_report): a draft is changed only by
// the person who started it (prepared_by, or created_by on older reports),
// an admin or a project manager. A draft with no author recorded is open to
// every field writer. A submitted or approved report follows the locked-
// report rules (edit reason, 24-hour window) and is deleted only by an admin
// or project manager.
//
// The role is read from the copy of the user saved on this device. lib/api.js
// refreshes that copy from /auth/me on load, on focus, on reconnect and when
// a save is refused with "role_read_only", and fires USER_CHANGED_EVENT, so
// a role change shows without signing out (useDailyReportPermissions).
import { useEffect, useState } from "react";
import { getUser, USER_CHANGED_EVENT } from "./auth";

const DR_WRITER_ROLES = new Set(["admin", "pm", "px", "super", "pe", "foreman"]);
const PRIVILEGED_ROLES = new Set(["admin", "pm"]);
const LOCKED_STATUSES = new Set(["submitted", "approved"]);

export const DR_VIEW_ONLY_TEXT = "View only — your role can't change daily reports.";

/** Banner text for a draft someone else started. */
export function notAuthorText(name) {
  const who = name ? `Started by ${name}` : "Started by someone else";
  return `${who} — only they, an admin or a project manager can change it.`;
}

function roleOf(user) {
  return String(user?.role || "").trim().toLowerCase();
}

function currentUser() {
  try { return getUser(); } catch { return null; }
}

export function canWriteDailyReports(role) {
  return DR_WRITER_ROLES.has(String(role || "").trim().toLowerCase());
}

/** True when the signed-in user may only read daily reports. */
export function isDailyReportViewOnly(user = currentUser()) {
  return !canWriteDailyReports(roleOf(user));
}

/** Who started the report (prepared_by, or created_by on older reports). */
export function reportAuthorIds(report) {
  return [report?.prepared_by, report?.created_by].filter(Boolean);
}

/** May the user change this report while it is a draft? Always true for a
 *  submitted or approved report and for a draft with no author recorded. */
export function canChangeDraft(report, user = currentUser()) {
  if (!report || LOCKED_STATUSES.has(report.status)) return true;
  if (PRIVILEGED_ROLES.has(roleOf(user))) return true;
  const authors = reportAuthorIds(report);
  if (!authors.length) return true;
  return !!user?.id && authors.includes(user.id);
}

/** May the user delete this report? Submitted or approved: admin or project
 *  manager only. Draft: also its author (or anyone who may write, when no
 *  author is recorded). */
export function canDeleteReport(report, user = currentUser()) {
  if (!canWriteDailyReports(roleOf(user))) return false;
  if (report && LOCKED_STATUSES.has(report.status)) return PRIVILEGED_ROLES.has(roleOf(user));
  return canChangeDraft(report, user);
}

/** The signed-in user, re-read whenever lib/api.js refreshes it. */
export function useCurrentUser() {
  const [user, setUser] = useState(currentUser);
  useEffect(() => {
    const onChange = () => setUser(currentUser());
    try { window.addEventListener(USER_CHANGED_EVENT, onChange); } catch { /* non-window env */ }
    return () => { try { window.removeEventListener(USER_CHANGED_EVENT, onChange); } catch { /* ignore */ } };
  }, []);
  return user;
}

/**
 * What the signed-in user may do on a report screen.
 *  viewOnly    - every edit control hidden or disabled (read-only role, or a
 *                draft someone else started).
 *  roleViewOnly- the read-only role case alone.
 *  notAuthor   - the "someone else's draft" case alone.
 *  bannerText  - plain text for the view-only banner (null when editable).
 *  canDelete   - whether to show Delete report.
 * `authorName` is the starter's name when the page knows it.
 */
export function useDailyReportPermissions(report, authorName = null) {
  const user = useCurrentUser();
  const roleViewOnly = isDailyReportViewOnly(user);
  const notAuthor = !roleViewOnly && !!report && !canChangeDraft(report, user);
  let bannerText = null;
  if (roleViewOnly) bannerText = DR_VIEW_ONLY_TEXT;
  else if (notAuthor) bannerText = notAuthorText(authorName || report?.prepared_by_name);
  return {
    user,
    roleViewOnly,
    notAuthor,
    viewOnly: roleViewOnly || notAuthor,
    bannerText,
    canDelete: !!report && canDeleteReport(report, user),
  };
}
