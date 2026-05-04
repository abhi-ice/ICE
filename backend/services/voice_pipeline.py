"""Voice annotation pipeline. Master prompt §11.

Two stages:
  1. Whisper transcribe (OpenAI whisper-1 via emergentintegrations) — audio bytes → transcript
  2. Claude structured-field extraction — transcript → entity-shaped JSON

Audio file persisted to Emergent object storage under voice/{tenant_id}/.
Voice annotation row saved to `voice_annotations` collection.
"""
import os
import io
import json
import re
import tempfile
from datetime import datetime, timezone
from typing import Optional
from emergentintegrations.llm.openai import OpenAISpeechToText
from emergentintegrations.llm.chat import LlmChat, UserMessage
from database import get_db
from models import gen_id, utc_now
from services.storage import put_object, make_path, record_file

WHISPER_MODEL = "whisper-1"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"


def _llm_key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "")


async def transcribe_audio(audio_bytes: bytes, ext: str = "webm", language: str | None = "en") -> dict:
    """Whisper-1 transcription. Returns {text, language, duration?}."""
    stt = OpenAISpeechToText(api_key=_llm_key())
    # The lib expects a file-like; use a NamedTemporaryFile so it has a name+ext
    suffix = f".{ext.lstrip('.').lower()}"
    if suffix not in (".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"):
        suffix = ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            resp = await stt.transcribe(
                file=f,
                model=WHISPER_MODEL,
                response_format="json",
                language=language,
            )
        text = getattr(resp, "text", None) or (resp.get("text") if isinstance(resp, dict) else None) or ""
        return {"text": text, "language": language}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


EXTRACT_SYSTEMS = {
    "delay": (
        "You extract delay-event fields from a construction superintendent's voice memo. "
        "Return ONLY a JSON object with keys: description, severity (minor|moderate|major|critical), "
        "delay_minutes (int|null), reason_category (string|null), location_hint (string|null), "
        "activity_hint (string|null). No prose, no code fences."
    ),
    "safety_event": (
        "You extract safety-event fields from a construction safety officer's voice memo. "
        "Return ONLY a JSON object with keys: description, severity (minor|moderate|major|critical), "
        "event_type (near_miss|first_aid|recordable|lost_time|fatality|other), osha_recordable (bool), "
        "location_hint (string|null), activity_hint (string|null). No prose, no code fences."
    ),
    "qc_issue": (
        "You extract QC-issue fields from a construction inspector's voice memo. "
        "Return ONLY a JSON object with keys: description, severity (minor|moderate|major|critical), "
        "spec_section_ref (string|null), location_hint (string|null), activity_hint (string|null). "
        "No prose, no code fences."
    ),
    "work_item": (
        "You extract field-issue fields from a construction PM's voice memo. "
        "Return ONLY a JSON object with keys: title (short), description, priority (low|normal|high|urgent), "
        "estimated_cost_impact (number|null), estimated_time_impact_days (number|null), "
        "location_hint (string|null), root_cause (string|null). No prose, no code fences."
    ),
    "rfi": (
        "You extract RFI fields from a construction PE's voice memo. Return ONLY a JSON object with keys: "
        "subject (short), description, priority (low|normal|high|urgent), to_party (string|null), "
        "spec_section_hint (string|null), activity_hint (string|null). No prose, no code fences."
    ),
}


async def extract_fields(transcript: str, kind: str) -> dict:
    sys = EXTRACT_SYSTEMS.get(kind, EXTRACT_SYSTEMS["work_item"])
    chat = LlmChat(
        api_key=_llm_key(),
        session_id=f"voice-extract-{gen_id()}",
        system_message=sys,
    ).with_model("anthropic", CLAUDE_MODEL)
    try:
        resp = await chat.send_message(UserMessage(text=f"Transcript:\n{transcript}"))
    except Exception as e:
        return {"_error": str(e)}
    return _parse_json_object(resp)


def _parse_json_object(text: str) -> dict:
    if not text:
        return {}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        return {"_raw": cleaned}
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception:
        return {"_raw": cleaned}


async def process_voice_annotation(
    *,
    tenant_id: str,
    user_id: str,
    audio_bytes: bytes,
    filename: str,
    kind: str,
    linked_entity_type: Optional[str] = None,
    linked_entity_id: Optional[str] = None,
) -> dict:
    """End-to-end: store audio, transcribe, extract fields, persist row."""
    db = get_db()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "webm"
    storage_path = make_path(tenant_id, "audio/voice", ext)
    try:
        put_object(storage_path, audio_bytes, "audio/webm")
        audio_url = storage_path
        await record_file(
            tenant_id=tenant_id, storage_path=storage_path,
            original_filename=filename, content_type="audio/webm",
            size=len(audio_bytes), kind="audio", actor_id=user_id,
        )
    except Exception as e:
        # if storage fails, still proceed with transcription so the user gets value
        audio_url = None
        log_err = f"storage_put_failed: {e}"
    else:
        log_err = None

    transcription = await transcribe_audio(audio_bytes, ext=ext)
    transcript = transcription.get("text", "")
    structured = await extract_fields(transcript, kind) if transcript else {}

    row = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "created_by": user_id,
        "audio_url": audio_url,
        "audio_duration_sec": None,
        "transcript": transcript,
        "transcript_confidence": None,
        "transcription_service": f"openai/{WHISPER_MODEL}",
        "structured_fields": structured,
        "kind": kind,
        "linked_entity_type": linked_entity_type,
        "linked_entity_id": linked_entity_id,
        "status": "completed" if transcript else "failed",
        "error": log_err,
        "created_at": utc_now().isoformat(),
    }
    await db.voice_annotations.insert_one(row.copy())
    return row
