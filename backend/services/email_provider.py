"""Bug #47 (2026-05-14) -- Email provider abstraction.

Why an abstraction layer instead of calling SendGrid directly:
ICE is using SendGrid as an interim provider while waiting for AWS
GovCloud account 673787498362 clearance. Post-clearance the platform
migrates to AWS SES for federal-compliance. By routing all email
through this single abstraction, swapping providers is one env-var
change (EMAIL_PROVIDER) plus filling in the SESProvider stub at the
bottom of this file. Callers (invite_email.py, future password-reset
flows, etc.) stay untouched.

Three concrete providers:
  - SendGridProvider:   uses the `sendgrid` package + SENDGRID_API_KEY
  - SESProvider:        NotImplementedError stub for post-GovCloud cutover
  - DisabledEmailProvider: no-op + log line, used in tests/local dev
                            and as the safe fallback when keys are missing

The base class' `send()` returns a dict like
  {"sent": bool, "provider_message_id": Optional[str], "error": Optional[str]}
so call sites get a uniform shape and never see provider-specific
exceptions. send() catches everything internally; raising is left to
the caller's discretion (most invite flows want to log+continue, not
fail the request).
"""
from __future__ import annotations
import logging
import os
from typing import Optional

log = logging.getLogger("email_provider")


class EmailProvider:
    """Abstract base. Subclasses implement send(); the factory picks one."""

    name: str = "abstract"

    def send(
        self,
        to_email: str,
        to_name: Optional[str],
        subject: str,
        html: str,
        text: str,
    ) -> dict:
        raise NotImplementedError


class DisabledEmailProvider(EmailProvider):
    """No-op provider. Logs a structured line so the operator can see
    what WOULD have been sent (minus the body, which would be noisy).
    Returns sent=False with no error -- caller treats this as a
    soft-skip, not a failure.

    Used:
      - tests (no SENDGRID_API_KEY set)
      - local dev (EMAIL_PROVIDER=disabled)
      - safe fallback when sendgrid lib not installed or key missing
    """

    name = "disabled"

    def send(self, to_email, to_name, subject, html, text):
        log.info(
            "email_skipped: provider=disabled to=%s name=%s subject=%r",
            to_email, to_name, subject,
        )
        return {"sent": False, "provider_message_id": None, "error": None}


class SendGridProvider(EmailProvider):
    """SendGrid v3 API wrapper. Uses the `sendgrid` package which is
    pinned in requirements.txt. API key + sender identity read from
    env at __init__ time; we keep one shared client instance for the
    process lifetime (SendGridAPIClient is cheap to construct, but
    no need to repeat it per send).
    """

    name = "sendgrid"

    def __init__(self):
        # Lazy import so the rest of the codebase loads cleanly even
        # if sendgrid isn't installed (e.g. test environments that
        # haven't pip-installed yet).
        try:
            from sendgrid import SendGridAPIClient  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "sendgrid package not installed. Run "
                "`pip install sendgrid` or set EMAIL_PROVIDER=disabled "
                "in env. Original: %s" % exc,
            )
        self._api_key = os.environ.get("SENDGRID_API_KEY") or ""
        self._from_email = os.environ.get("SENDGRID_FROM_EMAIL") or ""
        self._from_name = (
            os.environ.get("SENDGRID_FROM_NAME") or "ICE Schedule Ops"
        )
        if not self._api_key:
            log.warning(
                "SENDGRID_API_KEY not set -- SendGridProvider will fail "
                "every send. Set EMAIL_PROVIDER=disabled to silence.",
            )
        if not self._from_email:
            log.warning(
                "SENDGRID_FROM_EMAIL not set -- SendGridProvider will fail "
                "every send. Verify a sender in the SendGrid dashboard.",
            )

    def send(self, to_email, to_name, subject, html, text):
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, From, Subject, HtmlContent, PlainTextContent

        if not self._api_key or not self._from_email:
            err = "sendgrid not configured (missing API key or from-email)"
            log.warning("email_send_failed: %s to=%s", err, to_email)
            return {"sent": False, "provider_message_id": None, "error": err}

        try:
            mail = Mail(
                from_email=From(self._from_email, self._from_name),
                to_emails=To(to_email, to_name) if to_name else To(to_email),
                subject=Subject(subject),
                plain_text_content=PlainTextContent(text),
                html_content=HtmlContent(html),
            )
            client = SendGridAPIClient(self._api_key)
            resp = client.send(mail)
            # SendGrid returns 2xx on accept; message_id is in the
            # X-Message-Id header on the response.
            status_code = getattr(resp, "status_code", None)
            headers = getattr(resp, "headers", {}) or {}
            msg_id = (
                headers.get("X-Message-Id")
                or headers.get("x-message-id")
                or None
            )
            if status_code and 200 <= int(status_code) < 300:
                log.info(
                    "email_sent: provider=sendgrid to=%s msg_id=%s status=%s",
                    to_email, msg_id, status_code,
                )
                return {
                    "sent": True,
                    "provider_message_id": msg_id,
                    "error": None,
                }
            err = f"sendgrid non-2xx status={status_code}"
            log.warning("email_send_failed: %s to=%s", err, to_email)
            return {"sent": False, "provider_message_id": None, "error": err}
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning(
                "email_send_failed: provider=sendgrid to=%s err=%s",
                to_email, err,
            )
            return {"sent": False, "provider_message_id": None, "error": err}


class SESProvider(EmailProvider):
    """Post-AWS-GovCloud-clearance provider. Not yet implemented.

    Migration path (when AWS GovCloud account 673787498362 access lands):
      1. Install boto3 (already in requirements.txt for storage).
      2. Implement send() using ses_v2 client:
           client = boto3.client(
               "sesv2",
               region_name=os.environ.get("AWS_SES_REGION", "us-gov-west-1"),
           )
           resp = client.send_email(FromEmailAddress=..., Destination=...,
                                     Content={"Simple": {Subject, Body}})
      3. Map the SES MessageId to provider_message_id.
      4. Set EMAIL_PROVIDER=ses in production env.
      5. Decommission SendGrid (keep this file's SendGridProvider for
         disaster-recovery rollback for ~30 days, then remove).

    Until then, attempting to use this provider raises so misconfig is
    loud rather than silently delivering no email.
    """

    name = "ses"

    def __init__(self):
        raise NotImplementedError(
            "SESProvider deferred until AWS GovCloud account 673787498362 "
            "clearance lands. Use EMAIL_PROVIDER=sendgrid (default) until "
            "then. See services/email_provider.py docstring for the "
            "migration steps.",
        )

    def send(self, to_email, to_name, subject, html, text):  # pragma: no cover
        raise NotImplementedError


# -------------------------------------------------------------------
# Factory
# -------------------------------------------------------------------

_PROVIDER_SINGLETON: Optional[EmailProvider] = None


def get_email_provider() -> EmailProvider:
    """Return the configured email provider. Caches a single instance
    per process. Env var EMAIL_PROVIDER selects:
      - "sendgrid" (default) -> SendGridProvider, falls back to
        DisabledEmailProvider if the sendgrid package isn't installed
      - "ses"                -> SESProvider (raises NotImplementedError
        on construction; caller falls back to disabled)
      - "disabled"           -> DisabledEmailProvider (no-op + log)
      - anything else        -> DisabledEmailProvider with a warning
    """
    global _PROVIDER_SINGLETON
    if _PROVIDER_SINGLETON is not None:
        return _PROVIDER_SINGLETON

    choice = (os.environ.get("EMAIL_PROVIDER") or "sendgrid").lower()
    if choice == "disabled":
        _PROVIDER_SINGLETON = DisabledEmailProvider()
    elif choice == "sendgrid":
        try:
            _PROVIDER_SINGLETON = SendGridProvider()
        except RuntimeError as exc:
            log.warning(
                "EMAIL_PROVIDER=sendgrid but provider could not "
                "initialize (%s). Falling back to disabled.", exc,
            )
            _PROVIDER_SINGLETON = DisabledEmailProvider()
    elif choice == "ses":
        try:
            _PROVIDER_SINGLETON = SESProvider()
        except NotImplementedError as exc:
            log.warning(
                "EMAIL_PROVIDER=ses but SES provider is a deferred "
                "stub (%s). Falling back to disabled.", exc,
            )
            _PROVIDER_SINGLETON = DisabledEmailProvider()
    else:
        log.warning(
            "EMAIL_PROVIDER=%r is unknown. Falling back to disabled.",
            choice,
        )
        _PROVIDER_SINGLETON = DisabledEmailProvider()
    return _PROVIDER_SINGLETON


def _reset_provider_for_tests() -> None:
    """Test helper. Resets the singleton so a test can flip env vars
    and observe a different provider on the next call.
    """
    global _PROVIDER_SINGLETON
    _PROVIDER_SINGLETON = None
