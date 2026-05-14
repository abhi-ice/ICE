"""Bug #47 (2026-05-14) -- Invite email composer + sender.

One concern per file: this module composes the invite email content
(HTML + plain text) and calls into services/email_provider.py to
deliver it. Splitting the composer from the provider keeps the
provider abstraction clean (no ICE-specific copy bleeding into
SendGridProvider) and lets future password-reset / mention-
notification flows share email_provider.py without inheriting
invite-specific code.

PRIVACY: the invite_code IS the security token. Treat it like a
password:
  - never log it in plain text
  - mask in any log lines as `code=***LAST4`
  - the email itself sends the full code embedded in the link; that's
    inherent to the invite UX
"""
from __future__ import annotations
import html
import logging
from typing import Optional

from services.email_provider import get_email_provider

log = logging.getLogger("invite_email")


# Human-readable labels for the invite role. Keeps the email body
# from saying "submittal_tech" verbatim -- spells out the actual job.
_ROLE_LABELS = {
    "admin": "Administrator",
    "pm": "Project Manager",
    "super": "Superintendent",
    "submittal_tech": "Submittal Coordinator",
    "qa_qc_manager": "QA/QC Manager",
    "safety_manager": "Safety Manager",
    "field_engineer": "Field Engineer",
    "viewer": "Viewer (read-only)",
}


def _mask_code(code: str) -> str:
    """Privacy helper for log lines. Reveals only the last 4 chars."""
    if not code:
        return "code=***"
    if len(code) <= 4:
        return "code=***" + code
    return f"code=***{code[-4:]}"


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get((role or "").lower(), role or "Team Member")


def _build_html_body(
    *, tenant_name: str, invitee_email: str, role_label: str,
    invite_link: str,
) -> str:
    """ICE-branded inline HTML. No external CSS / images so the email
    renders cleanly across Gmail / Outlook / Apple Mail. Brand color
    matches Palette B Warm Concrete navy (#1a2845) from CLAUDE.md.
    """
    safe_tenant = html.escape(tenant_name or "your team")
    safe_role = html.escape(role_label)
    safe_link = html.escape(invite_link)
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f5f1ea;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a2845;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f5f1ea;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="560" cellspacing="0" cellpadding="0" border="0" style="max-width:560px;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e6dfd1;">
  <tr>
    <td style="background:#1a2845;padding:20px 28px;color:#ffffff;font-size:18px;font-weight:600;letter-spacing:0.3px;">
      ICE Schedule Ops
    </td>
  </tr>
  <tr>
    <td style="padding:28px;font-size:15px;line-height:1.55;">
      <p style="margin:0 0 14px;">Hello,</p>
      <p style="margin:0 0 14px;">
        You've been invited to join <strong>{safe_tenant}</strong> on
        ICE Schedule Ops as <strong>{safe_role}</strong>.
      </p>
      <p style="margin:0 0 18px;color:#54607a;">
        ICE Schedule Ops is the federal construction operations
        platform used by our project teams for daily reports, RFIs,
        submittals, schedule tracking, and TIA-ready audit logs.
      </p>
      <p style="margin:0 0 22px;text-align:center;">
        <a href="{safe_link}"
           style="display:inline-block;background:#d97757;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;">
          Accept Invite
        </a>
      </p>
      <p style="margin:0 0 8px;color:#54607a;font-size:13px;">
        Or paste this link into your browser:
      </p>
      <p style="margin:0 0 18px;color:#1a2845;font-size:13px;word-break:break-all;">
        <a href="{safe_link}" style="color:#1a2845;">{safe_link}</a>
      </p>
      <p style="margin:0;color:#9aa5b5;font-size:12px;">
        If you didn't expect this invite, you can safely ignore this
        email -- it will expire on its own.
      </p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>
"""


def _build_text_body(
    *, tenant_name: str, invitee_email: str, role_label: str,
    invite_link: str,
) -> str:
    return (
        "Hello,\n\n"
        f"You've been invited to join {tenant_name or 'your team'} on "
        f"ICE Schedule Ops as {role_label}.\n\n"
        "ICE Schedule Ops is the federal construction operations "
        "platform used by our project teams for daily reports, RFIs, "
        "submittals, schedule tracking, and TIA-ready audit logs.\n\n"
        f"Accept the invite: {invite_link}\n\n"
        "If you didn't expect this invite, you can safely ignore this "
        "email -- it will expire on its own.\n\n"
        "-- ICE Schedule Ops\n"
    )


def send_invite_email(
    *,
    tenant_name: str,
    invitee_email: str,
    role: str,
    invite_code: str,
    invite_link: str,
    invitee_name: Optional[str] = None,
) -> dict:
    """Public entry point. Composes + sends the invite email.

    Returns a dict matching the email_provider contract:
      {"sent": bool, "provider_message_id": Optional[str], "error": Optional[str]}

    Never raises. Caller (auth_router.create_invite) inspects the
    return value and stamps the response + audit op accordingly. An
    email failure MUST NOT block invite creation -- the invite_code
    is already persisted and the admin can fall back to copy-paste.
    """
    role_label = _role_label(role)
    subject = "You've been invited to ICE Schedule Ops"
    html_body = _build_html_body(
        tenant_name=tenant_name,
        invitee_email=invitee_email,
        role_label=role_label,
        invite_link=invite_link,
    )
    text_body = _build_text_body(
        tenant_name=tenant_name,
        invitee_email=invitee_email,
        role_label=role_label,
        invite_link=invite_link,
    )

    masked = _mask_code(invite_code)
    log.info(
        "invite_email_attempt: tenant=%r to=%s role=%s %s",
        tenant_name, invitee_email, role, masked,
    )

    provider = get_email_provider()
    try:
        result = provider.send(
            to_email=invitee_email,
            to_name=invitee_name,
            subject=subject,
            html=html_body,
            text=text_body,
        )
    except Exception as exc:
        # Defense in depth: the provider's send() already catches
        # everything internally, but if a subclass forgets, we still
        # return the contract shape and don't bubble up.
        log.warning(
            "invite_email_unhandled_exception: to=%s err=%s",
            invitee_email, exc,
        )
        return {
            "sent": False,
            "provider_message_id": None,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    # Ensure the shape is what we promise even if a custom provider
    # returns a partial dict.
    return {
        "sent": bool(result.get("sent")),
        "provider_message_id": result.get("provider_message_id"),
        "error": result.get("error"),
    }
