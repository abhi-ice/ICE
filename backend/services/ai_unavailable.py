"""One plain answer for "the AI can't run right now" (2026-11-16).

When no AI key is set (ANTHROPIC_API_KEY, or EMERGENT_LLM_KEY as a
fallback; see services/llm_keys.py), every AI feature answers with a
clear message and saves nothing. Before this, some features made up
results (photo analysis showed fake "1 safety · 1 QC" findings) or
showed the raw key error.

The answer is 422, not 503: production sits behind Cloudflare, which
swaps the body of any 5xx reply for its own page, so a 503 message would
never reach the user. The body is the usual {"detail": text} that every
page already shows, plus "code": "ai_unavailable" so the phone's offline
queue keeps a queued save (for example a voice clip) for a later retry
instead of dropping it like a real rejection.
"""
from __future__ import annotations

from fastapi import HTTPException

from services.llm_keys import has_llm_key

AI_UNAVAILABLE_CODE = "ai_unavailable"

PHOTO_AI_UNAVAILABLE_MESSAGE = (
    "AI analysis isn't available right now. Your photos are saved; "
    "try again later."
)
COPILOT_UNAVAILABLE_MESSAGE = (
    "The AI assistant isn't available right now. Try again later."
)
VOICE_EXTRACT_UNAVAILABLE_MESSAGE = (
    "AI isn't available right now, so the voice memo couldn't be turned "
    "into report entries. Nothing on the report was changed; try again later."
)
CLIENT_TEMPLATE_UNAVAILABLE_MESSAGE = (
    "The client-format export uses AI, which isn't available right now. "
    "Try again later."
)
VOICE_NOTE_EXTRACT_UNAVAILABLE_MESSAGE = (
    "AI isn't available right now, so no fields were pulled from this "
    "recording. The recording and transcript are saved."
)

DELAY_REPORT_UNAVAILABLE_MESSAGE = (
    "AI isn't available right now, so the delay report was not written. "
    "Any earlier report is kept; try again later."
)
RFI_DRAFT_UNAVAILABLE_MESSAGE = (
    "The AI RFI drafter isn't available right now. Nothing was saved; try again later."
)
RECOVERY_UNAVAILABLE_MESSAGE = (
    "AI recovery suggestions aren't available right now. Nothing was saved; "
    "try again later."
)
MITIGATION_UNAVAILABLE_MESSAGE = (
    "AI mitigation scenarios aren't available right now. The current "
    "scenarios are kept; try again later."
)
SCHEDULE_NARRATIVE_UNAVAILABLE_MESSAGE = (
    "The AI schedule-change summary isn't available right now. Try again later."
)


class AiUnavailableError(HTTPException):
    """422 with code "ai_unavailable" (see the module note)."""

    code = AI_UNAVAILABLE_CODE

    def __init__(self, message: str = PHOTO_AI_UNAVAILABLE_MESSAGE):
        super().__init__(422, message)


async def ai_unavailable_handler(request, exc: AiUnavailableError):
    """Exception handler (registered in server.py)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


def ensure_ai_available(message: str) -> None:
    """Raise AiUnavailableError(message) when no AI key is set."""
    if not has_llm_key():
        raise AiUnavailableError(message)


__all__ = [
    "AI_UNAVAILABLE_CODE",
    "PHOTO_AI_UNAVAILABLE_MESSAGE",
    "COPILOT_UNAVAILABLE_MESSAGE",
    "VOICE_EXTRACT_UNAVAILABLE_MESSAGE",
    "CLIENT_TEMPLATE_UNAVAILABLE_MESSAGE",
    "VOICE_NOTE_EXTRACT_UNAVAILABLE_MESSAGE",
    "DELAY_REPORT_UNAVAILABLE_MESSAGE",
    "RFI_DRAFT_UNAVAILABLE_MESSAGE",
    "RECOVERY_UNAVAILABLE_MESSAGE",
    "MITIGATION_UNAVAILABLE_MESSAGE",
    "SCHEDULE_NARRATIVE_UNAVAILABLE_MESSAGE",
    "AiUnavailableError",
    "ai_unavailable_handler",
    "ensure_ai_available",
]
