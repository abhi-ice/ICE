"""Password-reset email composer + sender (2026-02-XX, Abhi).

Mirrors `services/invite_email.py` -- separate from email_provider.py
so ICE-specific copy / branding never bleeds into the generic provider
abstraction. The provider abstraction already anticipates this exact
use case (see services/email_provider.py docstring).

PRIVACY: the reset_token IS the security token. Treat it like a
password:
  - never log it in plain text -- mask as `token=***LAST4`
  - the email itself sends the full token embedded in the link; that's
    inherent to the reset UX
  - tokens are SHA-256 hashed at rest (see auth_router.forgot_password)
    so a DB dump cannot be used to forge resets

Email tone: short, one CTA, expiry duration spelled out, plus a fallback
copy-paste URL for clients that strip auto-links. No PII besides the
recipient email so the body is safe to ship through SendGrid's audit
trail.
"""
from __future__ import annotations
import html
import logging
from typing import Optional

from services.email_provider import get_email_provider

log = logging.getLogger("password_reset_email")


def _mask_token(token: str) -> str:
    if not token:
        return "token=***"
    if len(token) <= 4:
        return "token=***" + token
    return f"token=***{token[-4:]}"


def _build_html_body(*, recipient_email: str, reset_link: str,
                     expires_minutes: int) -> str:
    """ICE-branded inline HTML. No external CSS / images so the email
    renders cleanly across Gmail / Outlook / Apple Mail. Brand emerald
    matches the in-app accent.
    """
    safe_link = html.escape(reset_link, quote=True)
    safe_email = html.escape(recipient_email)
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f6fa;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <div style="max-width:560px;margin:24px auto;background:#ffffff;border-radius:12px;border:1px solid #e2e8f0;padding:32px 28px;">
    <div style="font-size:13px;letter-spacing:0.18em;text-transform:uppercase;color:#10b981;font-weight:600;margin-bottom:8px;">
      SiteOps · Construction OS
    </div>
    <h1 style="font-size:22px;margin:0 0 12px;color:#0f172a;">Reset your password</h1>
    <p style="font-size:15px;line-height:1.6;color:#334155;margin:0 0 18px;">
      We received a request to reset the password for <strong>{safe_email}</strong>.
      Click the button below to choose a new one.
    </p>
    <p style="margin:24px 0;">
      <a href="{safe_link}"
         style="display:inline-block;background:#10b981;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;font-size:15px;">
        Reset password
      </a>
    </p>
    <p style="font-size:13px;line-height:1.6;color:#64748b;margin:0 0 14px;">
      This link expires in <strong>{expires_minutes} minutes</strong>. If you
      didn't request this reset, you can safely ignore this email — your
      password won't change.
    </p>
    <p style="font-size:12px;line-height:1.6;color:#94a3b8;margin:14px 0 0;">
      Button not working? Paste this link into your browser:<br>
      <span style="word-break:break-all;color:#475569;">{safe_link}</span>
    </p>
  </div>
  <div style="text-align:center;font-size:11px;color:#94a3b8;padding:0 12px 18px;">
    Sent by SiteOps · This is an automated message, please do not reply.
  </div>
</body></html>"""


def _build_text_body(*, recipient_email: str, reset_link: str,
                     expires_minutes: int) -> str:
    return (
        f"SiteOps password reset\n"
        f"\n"
        f"We received a request to reset the password for {recipient_email}.\n"
        f"Open the link below to choose a new password:\n"
        f"\n"
        f"{reset_link}\n"
        f"\n"
        f"This link expires in {expires_minutes} minutes. If you didn't\n"
        f"request this reset, you can safely ignore this email — your\n"
        f"password won't change.\n"
    )


def send_password_reset_email(
    *,
    recipient_email: str,
    recipient_name: Optional[str],
    reset_token: str,
    reset_link: str,
    expires_minutes: int,
) -> dict:
    """Public entry point. Composes + sends the reset email.

    Returns the email_provider contract:
      {"sent": bool, "provider_message_id": Optional[str], "error": Optional[str]}

    Never raises. Caller (auth_router.forgot_password) inspects the
    return value and stamps the audit op accordingly. An email
    failure MUST NOT change the API response: the endpoint always
    returns 200 to prevent account enumeration -- whether a user
    exists is invisible to the caller.
    """
    subject = "Reset your SiteOps password"
    html_body = _build_html_body(
        recipient_email=recipient_email,
        reset_link=reset_link,
        expires_minutes=expires_minutes,
    )
    text_body = _build_text_body(
        recipient_email=recipient_email,
        reset_link=reset_link,
        expires_minutes=expires_minutes,
    )

    log.info(
        "password_reset_email_attempt: to=%s %s expires_min=%d",
        recipient_email, _mask_token(reset_token), expires_minutes,
    )

    provider = get_email_provider()
    try:
        result = provider.send(
            to_email=recipient_email,
            to_name=recipient_name,
            subject=subject,
            html=html_body,
            text=text_body,
        )
    except Exception as exc:
        # Defense in depth -- providers catch internally but if a
        # subclass forgets we still honor the contract.
        log.warning(
            "password_reset_email_unhandled_exception: to=%s err=%s",
            recipient_email, exc,
        )
        return {
            "sent": False,
            "provider_message_id": None,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    return {
        "sent": bool(result.get("sent")),
        "provider_message_id": result.get("provider_message_id"),
        "error": result.get("error"),
    }
