"""Claude chat client built on the official `anthropic` package.

Every AI feature imports from here:

    from services.anthropic_compat import LlmChat, UserMessage, ImageContent
    chat = LlmChat(api_key=..., session_id=..., system_message=...) \
              .with_model("anthropic", "claude-sonnet-4-5-20250929")
    reply: str = await chat.send_message(UserMessage(text=..., file_contents=[ImageContent(...)]))

The class names and call shape match the old `emergentintegrations.llm.chat`
module, so the call sites did not have to change beyond the import line.

Which key is used (see services/llm_keys.py):
  * ANTHROPIC_API_KEY set  -> the call goes straight to api.anthropic.com.
                              This is the normal path (Railway, and Emergent
                              today).
  * only EMERGENT_LLM_KEY  -> the call is handed to Emergent's own
                              `emergentintegrations` package, if it is
                              installed. That keeps the app working unchanged
                              if this branch is ever deployed on Emergent
                              without an Anthropic key.
  * neither                -> send_message raises a "401 ... no api key"
                              error, which callers already treat as "AI off".

One LlmChat object remembers its own conversation: a second send_message on
the same object includes the earlier question and answer, the same way the
Emergent client behaved. The copilot's tool loop relies on that. A new
LlmChat object always starts empty.

server.py used to register this module under the name
`emergentintegrations.llm.chat`; nothing needs that any more, but if it is
still done this module notices and does not hand calls back to itself.
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Any, List, Optional

import anthropic

from services.llm_keys import anthropic_api_key, emergent_llm_key

log = logging.getLogger(__name__)

# Default reply length cap. Our call sites return JSON of 1-4 KB, so 8192
# tokens is generous. Claude Sonnet allows more if a caller ever needs it.
_DEFAULT_MAX_TOKENS = 8192


def _get_api_key() -> str:
    """The owner's direct Anthropic key, read at call time so a changed
    environment variable takes effect after a restart."""
    return anthropic_api_key() or ""


_EMERGENT_MODULE_UNSET = object()
_emergent_module: Any = _EMERGENT_MODULE_UNSET


def _emergent_chat_module():
    """Emergent's real chat module, or None when it is not installed.

    Only used when ANTHROPIC_API_KEY is missing and EMERGENT_LLM_KEY is
    set. Imported lazily because it pulls in litellm, which is large.
    Returns None if the name points back at this module (the old
    server.py swap), so we never call ourselves in a loop."""
    global _emergent_module
    if _emergent_module is _EMERGENT_MODULE_UNSET:
        try:
            mod = importlib.import_module("emergentintegrations.llm.chat")
        except Exception:
            mod = None
        if mod is not None and (mod is sys.modules.get(__name__)
                                or getattr(mod, "LlmChat", None) is LlmChat):
            mod = None
        _emergent_module = mod
    return _emergent_module


class ImageContent:
    """An image attached to a message. Same shape as the Emergent class:
    ImageContent(image_base64=<base64 JPEG/PNG/GIF/WebP string>).

    media_type is worked out from the first bytes of the image when it is
    not given; it defaults to image/jpeg."""

    __slots__ = ("image_base64", "media_type")

    def __init__(self, image_base64: str, media_type: Optional[str] = None) -> None:
        self.image_base64 = image_base64
        self.media_type = media_type or self._detect_media_type(image_base64)

    @staticmethod
    def _detect_media_type(b64: str) -> str:
        # The base64 text of each format starts with a known prefix.
        if not b64:
            return "image/jpeg"
        head = b64[:16]
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
    """One user message: text plus optional images (file_contents)."""

    __slots__ = ("text", "file_contents")

    def __init__(
        self,
        text: str = "",
        file_contents: Optional[List[ImageContent]] = None,
    ) -> None:
        self.text = text or ""
        self.file_contents = list(file_contents) if file_contents else []

    def to_content_blocks(self) -> list:
        """Build the content list for the Anthropic messages API.
        Images go before the text, as Anthropic recommends for vision."""
        blocks: list = []
        for img in self.file_contents:
            if hasattr(img, "to_block"):
                blocks.append(img.to_block())
            elif getattr(img, "image_base64", None):
                blocks.append(ImageContent(img.image_base64).to_block())
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        # The API rejects an empty message, so always send at least one block.
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks


class LlmChat:
    """Claude chat client with the same calls as Emergent's LlmChat:

        LlmChat(api_key=..., session_id=..., system_message=...)
        .with_model(provider, model_name)
        await .send_message(UserMessage) -> str

    api_key is kept only for the Emergent fallback. When ANTHROPIC_API_KEY
    is set it is ignored and the Anthropic key is used. session_id is kept
    for logging; conversation memory lives on the object itself."""

    def __init__(
        self,
        api_key: str = "",
        session_id: str = "",
        system_message: str = "",
        **_legacy_kwargs: Any,      # accept and ignore any extra arguments
    ) -> None:
        self._api_key = api_key or ""
        self._session_id = session_id or ""
        self._system_message = system_message or ""
        self._provider: str = "anthropic"
        self._model: str = "claude-sonnet-4-5-20250929"
        self._max_tokens: int = _DEFAULT_MAX_TOKENS
        self._temperature: Optional[float] = None
        self._effort: Optional[str] = None
        self._no_thinking: bool = False
        # Earlier turns of this conversation, in Anthropic message format.
        self._history: list = []
        # Emergent client, created on first use of the fallback path.
        self._emergent_chat: Any = None

    # ---- settings ---------------------------------------------------
    def with_model(self, provider: str, model_name: str) -> "LlmChat":
        """Pick the model. Only Anthropic is supported for chat; any other
        provider name is logged and the model name is still sent to Claude."""
        if provider and provider.lower() != "anthropic":
            log.warning(
                "anthropic_compat.LlmChat: provider=%r is not 'anthropic'; "
                "sending model=%r to Anthropic anyway.", provider, model_name,
            )
        if provider:
            self._provider = provider
        if model_name:
            self._model = model_name
        return self

    def with_max_tokens(self, max_tokens: int) -> "LlmChat":
        if isinstance(max_tokens, int) and max_tokens > 0:
            self._max_tokens = max_tokens
        return self

    def with_effort(self, effort: str) -> "LlmChat":
        """How much the model may think before answering ("low", "medium",
        "high"). Low keeps tiny one-word checks from spending their whole
        answer budget on thinking."""
        if effort in ("low", "medium", "high"):
            self._effort = effort
        return self

    def without_thinking(self) -> "LlmChat":
        """Answer straight away with no thinking step. For tiny one-word
        checks: thinking can otherwise use the whole answer budget and leave
        the reply empty."""
        self._no_thinking = True
        return self

    def with_temperature(self, temperature: float) -> "LlmChat":
        if isinstance(temperature, (int, float)):
            self._temperature = float(temperature)
        return self

    # ---- main call --------------------------------------------------
    async def send_message(self, user_message: UserMessage) -> str:
        """Send one user message and return Claude's reply as plain text."""
        if not isinstance(user_message, UserMessage):
            # Accept a bare string, or another library's message object.
            user_message = UserMessage(
                text=str(getattr(user_message, "text", user_message) or ""),
                file_contents=getattr(user_message, "file_contents", None),
            )

        api_key = _get_api_key()
        if api_key:
            return await self._send_direct(api_key, user_message)

        fallback_key = self._api_key or emergent_llm_key() or ""
        if fallback_key and _emergent_chat_module() is not None:
            return await self._send_via_emergent(fallback_key, user_message)

        # Wording keeps "401", "unauthorized" and "api key" so the callers'
        # error sorting still files this under "no key / auth".
        raise RuntimeError(
            "Anthropic auth failed: ANTHROPIC_API_KEY is not set "
            "(401 unauthorized -- no api key configured)."
        )

    async def _send_via_emergent(self, key: str, user_message: UserMessage) -> str:
        """Hand the call to Emergent's own client (fallback only)."""
        mod = _emergent_chat_module()
        if self._emergent_chat is None:
            self._emergent_chat = mod.LlmChat(
                api_key=key,
                session_id=self._session_id or "ice",
                system_message=self._system_message,
            ).with_model(self._provider or "anthropic", self._model)
        images = [
            mod.ImageContent(image_base64=img.image_base64)
            for img in user_message.file_contents
            if getattr(img, "image_base64", None)
        ]
        if images:
            msg = mod.UserMessage(text=user_message.text, file_contents=images)
        else:
            msg = mod.UserMessage(text=user_message.text)
        reply = await self._emergent_chat.send_message(msg)
        return reply if isinstance(reply, str) else str(reply or "")

    async def _send_direct(self, api_key: str, user_message: UserMessage) -> str:
        """Call api.anthropic.com with the owner's key."""
        content_blocks = user_message.to_content_blocks()
        new_turn = {"role": "user", "content": content_blocks}

        client = anthropic.AsyncAnthropic(api_key=api_key)
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": self._history + [new_turn],
        }
        if self._system_message:
            kwargs["system"] = self._system_message
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        extra: dict = {}
        if self._effort:
            extra["output_config"] = {"effort": self._effort}
        if self._no_thinking:
            extra["thinking"] = {"type": "disabled"}
        if extra:
            kwargs["extra_body"] = extra

        # Turn Anthropic's error classes into messages that contain the
        # status codes and words the callers look for (rate limit, 429, 401,
        # unauthorized, api key, 403, 400, connection, 500-504). photo_ai.py
        # and others sort errors by those words.
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
            # Timeouts are a kind of connection error, so they land here
            # too; photo_ai.py files both under "api_connection".
            raise RuntimeError(
                f"Anthropic connection error (network/DNS/TLS): {exc}"
            ) from exc
        except anthropic.APIStatusError as exc:
            # Any other HTTP status from Anthropic.
            status = getattr(exc, "status_code", "?")
            raise RuntimeError(
                f"Anthropic HTTP {status} error: {exc}"
            ) from exc

        # The reply is a list of blocks; the text is in the "text" blocks.
        try:
            blocks = getattr(resp, "content", None) or []
            chunks: list = []
            for b in blocks:
                if getattr(b, "type", None) == "text":
                    txt = getattr(b, "text", "") or ""
                    if txt:
                        chunks.append(txt)
            reply = "".join(chunks).strip() if chunks else ""
        except Exception as exc:
            raise RuntimeError(
                f"Anthropic response text extraction failed: {exc}"
            ) from exc

        # Remember this turn so a follow-up message on the same object
        # has the context. An empty reply is not stored, because the API
        # does not accept an empty assistant message.
        if reply:
            self._history.append(new_turn)
            self._history.append({"role": "assistant", "content": reply})
        return reply


class ChatError(RuntimeError):
    """Same name as the Emergent error class, kept so old imports work."""


class FileContent:
    """Generic file attachment, kept so old imports of this name work.
    No current call site sends one to Claude."""

    __slots__ = ("data",)

    def __init__(self, data: Any = None) -> None:
        self.data = data


class FileContentWithMimeType(FileContent):
    """FileContent plus a mime_type, kept so old imports work."""

    __slots__ = ("mime_type",)

    def __init__(self, data: Any = None, mime_type: str = "") -> None:
        super().__init__(data)
        self.mime_type = mime_type or ""


class ModelResponse(dict):
    """Stand-in for litellm's ModelResponse name, kept so old imports
    work. send_message returns a plain string, not this."""


__all__ = [
    "LlmChat", "UserMessage", "ImageContent",
    "ChatError", "FileContent", "FileContentWithMimeType",
    "ModelResponse",
]
