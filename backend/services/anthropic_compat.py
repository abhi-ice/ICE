"""Anthropic Claude direct-SDK compat shim.

Replaces `emergentintegrations.llm.chat` at runtime so every existing
call site that does

    from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    chat = LlmChat(api_key=..., session_id=..., system_message=...) \
              .with_model("anthropic", "claude-sonnet-4-5-20250929")
    reply: str = await chat.send_message(UserMessage(text=..., file_contents=[ImageContent(...)]))

continues to work UNCHANGED -- but the call goes directly to
api.anthropic.com using ANTHROPIC_API_KEY (the customer's own
Anthropic key) instead of via Emergent's gateway.

The shim is installed by server.py at import time via
    sys.modules['emergentintegrations.llm.chat'] = anthropic_compat
so neither services/ nor routers/ require any source edits.

Whisper STT (emergentintegrations.llm.openai.OpenAISpeechToText) and
Stripe (emergentintegrations.payments.stripe.checkout) are NOT
intercepted -- they keep using EMERGENT_LLM_KEY.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

import anthropic

log = logging.getLogger(__name__)

# Default max_tokens to match what Emergent's gateway used (8192 is the
# generous practical default we've been hitting; Claude Sonnet supports
# up to 64K but our call sites generate JSON responses of 1-4KB).
_DEFAULT_MAX_TOKENS = 8192


def _get_api_key() -> str:
    """Pull the user's direct Anthropic API key from env at call time.

    Env-resolution at call time (not import time) so a key rotation
    via .env reload + supervisor restart picks up the new value
    without rebuilding the module."""
    return os.environ.get("ANTHROPIC_API_KEY", "")


class ImageContent:
    """Multimodal image attachment. Matches the Emergent API shape:
    ImageContent(image_base64=<base64-encoded JPEG/PNG string>).

    media_type is auto-detected from the base64 magic bytes; defaults
    to image/jpeg if undetermined."""

    __slots__ = ("image_base64", "media_type")

    def __init__(self, image_base64: str, media_type: Optional[str] = None) -> None:
        self.image_base64 = image_base64
        self.media_type = media_type or self._detect_media_type(image_base64)

    @staticmethod
    def _detect_media_type(b64: str) -> str:
        # Sniff first few bytes after base64 decode to pick PNG vs JPEG.
        # All existing call sites pass JPEG, but this stays robust.
        if not b64:
            return "image/jpeg"
        head = b64[:16]
        # PNG: iVBOR... ; JPEG: /9j/...
        if head.startswith("iVBOR"):
            return "image/png"
        if head.startswith("R0lGOD"):
            return "image/gif"
        if head.startswith("UklGR"):
            return "image/webp"
        return "image/jpeg"

    def to_block(self) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.image_base64,
            },
        }


class UserMessage:
    """One user-role message. text + optional file_contents (images)."""

    __slots__ = ("text", "file_contents")

    def __init__(
        self,
        text: str = "",
        file_contents: Optional[List[ImageContent]] = None,
    ) -> None:
        self.text = text or ""
        self.file_contents = list(file_contents) if file_contents else []

    def to_content_blocks(self) -> list:
        """Build the Anthropic messages-API content blocks array.
        Images come BEFORE the text block (per Anthropic's vision
        recommendation -- improves the model's grounding)."""
        blocks: list = []
        for img in self.file_contents:
            blocks.append(img.to_block())
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        # An empty user content is invalid; guard so empty UserMessage
        # never crashes the API call.
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks


class LlmChat:
    """Drop-in replacement for emergentintegrations.llm.chat.LlmChat.

    The original signature was:
        LlmChat(api_key=..., session_id=..., system_message=...)
        .with_model(provider, model_name)
        await .send_message(UserMessage) -> str

    The api_key arg from call sites carries EMERGENT_LLM_KEY; we IGNORE
    it (the whole point of this shim) and use ANTHROPIC_API_KEY from
    env instead. session_id is recorded but not used (Anthropic's API
    is stateless per-request, same as Emergent's gateway was)."""

    def __init__(
        self,
        api_key: str = "",          # IGNORED: see class docstring
        session_id: str = "",       # informational only
        system_message: str = "",
        **_legacy_kwargs: Any,      # swallow any extra kwargs gracefully
    ) -> None:
        self._session_id = session_id or ""
        self._system_message = system_message or ""
        self._provider: str = "anthropic"
        self._model: str = "claude-sonnet-4-5-20250929"
        self._max_tokens: int = _DEFAULT_MAX_TOKENS
        self._temperature: Optional[float] = None

    # ---- fluent configuration --------------------------------------
    def with_model(self, provider: str, model_name: str) -> "LlmChat":
        """Match Emergent's fluent API. Only the 'anthropic' provider
        is supported here -- it's the only one any call site uses for
        chat. If someone passes 'openai' we honor the request but the
        actual transport is still Anthropic (we route everything to
        Claude) -- log a warning so it's debuggable."""
        if provider and provider.lower() != "anthropic":
            log.warning(
                "anthropic_compat.LlmChat: provider=%r is not 'anthropic' "
                "but we only route Claude here; using model=%r against "
                "Anthropic anyway.", provider, model_name,
            )
        if model_name:
            self._model = model_name
        return self

    def with_max_tokens(self, max_tokens: int) -> "LlmChat":
        # Available for future call sites; current code doesn't use it.
        if isinstance(max_tokens, int) and max_tokens > 0:
            self._max_tokens = max_tokens
        return self

    def with_temperature(self, temperature: float) -> "LlmChat":
        if isinstance(temperature, (int, float)):
            self._temperature = float(temperature)
        return self

    # ---- main API call ---------------------------------------------
    async def send_message(self, user_message: UserMessage) -> str:
        """Send the user message to api.anthropic.com and return the
        assistant's reply as a plain string (matching the Emergent
        gateway return type that all call sites expect)."""
        api_key = _get_api_key()
        if not api_key:
            # Match Emergent's error vocabulary so existing string-match
            # error-classification at call sites still routes correctly.
            raise RuntimeError(
                "Anthropic auth failed: ANTHROPIC_API_KEY is not set "
                "(401 unauthorized -- no api key configured)."
            )

        # Convert UserMessage -> Anthropic messages payload.
        if not isinstance(user_message, UserMessage):
            # Be lenient: accept a bare string too.
            user_message = UserMessage(text=str(user_message))
        content_blocks = user_message.to_content_blocks()

        client = anthropic.AsyncAnthropic(api_key=api_key)
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": content_blocks}],
        }
        if self._system_message:
            kwargs["system"] = self._system_message
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        # Translate Anthropic exception classes into messages that
        # contain the status-code / keyword tokens existing callers
        # grep for (rate limit, 429, 401, unauthorized, api key, 403,
        # 400, connection, 500/502/503/504). This keeps the
        # error-classification logic in photo_ai.py and others intact.
        try:
            resp = await client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise RuntimeError(
                f"Anthropic 401 unauthorized (invalid api key): {exc}"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise RuntimeError(
                f"Anthropic 403 permission denied: {exc}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise RuntimeError(
                f"Anthropic 429 rate limit hit: {exc}"
            ) from exc
        except anthropic.BadRequestError as exc:
            raise RuntimeError(
                f"Anthropic 400 bad request: {exc}"
            ) from exc
        except anthropic.NotFoundError as exc:
            raise RuntimeError(
                f"Anthropic 404 not found (likely bad model name "
                f"'{self._model}'): {exc}"
            ) from exc
        except anthropic.InternalServerError as exc:
            raise RuntimeError(
                f"Anthropic 500 server error: {exc}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise RuntimeError(
                f"Anthropic connection error (network/DNS/TLS): {exc}"
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise RuntimeError(
                f"Anthropic API timeout: {exc}"
            ) from exc
        except anthropic.APIStatusError as exc:
            # Catch-all for any other HTTP status from Anthropic.
            status = getattr(exc, "status_code", "?")
            raise RuntimeError(
                f"Anthropic HTTP {status} error: {exc}"
            ) from exc

        # Extract reply text. Anthropic returns Message with content
        # as a list of blocks; the assistant's text is in TextBlock
        # entries (.type == 'text', .text holds the string).
        try:
            blocks = getattr(resp, "content", None) or []
            chunks: list = []
            for b in blocks:
                btype = getattr(b, "type", None)
                if btype == "text":
                    txt = getattr(b, "text", "") or ""
                    if txt:
                        chunks.append(txt)
            return "".join(chunks).strip() if chunks else ""
        except Exception as exc:
            raise RuntimeError(
                f"Anthropic response text extraction failed: {exc}"
            ) from exc


class ChatError(RuntimeError):
    """Replacement for emergentintegrations.llm.chat.ChatError.

    Whisper STT (emergentintegrations.llm.openai) does
    `from ..chat import ChatError` so this symbol must exist for the
    Whisper module to import. Whisper itself does not catch ChatError
    in any code path we use, so a plain RuntimeError subclass is fine.
    """


class FileContent:
    """Generic file attachment. Emergent's chat module exports this;
    no current call site in our app uses it for Claude. Whisper's
    `from ..chat import FileContent...` import succeeds because we
    export the symbol. Body is intentionally minimal."""

    __slots__ = ("data",)

    def __init__(self, data: Any = None) -> None:
        self.data = data


class FileContentWithMimeType(FileContent):
    """As FileContent above, plus a mime_type field. Re-exported for
    import-compat with the Whisper module's wildcard chain."""

    __slots__ = ("mime_type",)

    def __init__(self, data: Any = None, mime_type: str = "") -> None:
        super().__init__(data)
        self.mime_type = mime_type or ""


# Placeholder so any code that references the original litellm
# ModelResponse symbol still resolves. We do not return one from
# send_message (we return str) -- this is purely for import-compat.
class ModelResponse(dict):
    """Stand-in for litellm.ModelResponse re-export. Behaves like a
    dict; not used by any current call site of the shim."""


# Public surface that mirrors emergentintegrations.llm.chat. Includes
# import-compat aliases (ChatError, FileContent, FileContentWithMimeType,
# ModelResponse) needed by emergentintegrations.llm.openai (Whisper STT),
# which does `from ..chat import LlmChat, ChatError, UserMessage,
# ImageContent, FileContentWithMimeType` at module load.
__all__ = [
    "LlmChat", "UserMessage", "ImageContent",
    "ChatError", "FileContent", "FileContentWithMimeType",
    "ModelResponse",
]
