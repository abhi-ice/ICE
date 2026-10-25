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
# 2026-07-06 (prod-deploy-fix) -- emergentintegrations.llm.openai and
# emergentintegrations.llm.chat both pull in litellm, which adds ~160MB
# to boot RSS via 900+ transitive imports. voice_router imports this
# service at module load, so lazy the SDK to endpoint-time to keep the
# cold pod under any reasonable k8s memory limit.
# from emergentintegrations.llm.openai import OpenAISpeechToText   # moved inside transcribe_audio
# from emergentintegrations.llm.chat import LlmChat, UserMessage    # moved inside call sites
from database import get_db
from models import gen_id, utc_now
from services.storage import put_object, aput_object, make_path, record_file

WHISPER_MODEL = "whisper-1"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"


def _llm_key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "")


async def transcribe_audio(audio_bytes: bytes, ext: str = "webm", language: str | None = "en") -> dict:
    """Whisper-1 transcription. Returns {text, language, duration?}."""
    from emergentintegrations.llm.openai import OpenAISpeechToText
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
        "event_type (near_miss|first_aid|recordable|lost_time|fatality|other), "
        "location_hint (string|null), activity_hint (string|null). No prose, no code fences."
    ),
    "qc_issue": (
        "You extract QC-issue fields from a construction inspector's voice memo. "
        "Return ONLY a JSON object with keys: description, severity (minor|moderate|major|critical), "
        "location_hint (string|null), activity_hint (string|null). "
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
    # Bucket C3b Phase 2 — coordination event extractor. Triggered when
    # the user picks kind=coordination_event in the voice UI, OR when
    # the auto-detector below classifies a free-form memo as coordination
    # language. Output mirrors CoordinationEvent fields; downstream code
    # creates a draft event with voice_extracted=True for user review.
    "coordination_event": (
        "You extract coordination-event fields from a construction PM's voice memo about a "
        "phone call, email, meeting, or letter. Return ONLY a JSON object with keys: "
        "channel (email|phone|in_person|formal_letter|text_message|meeting|system_notification), "
        "direction (inbound|outbound|internal), "
        "other_party_name (string), other_party_org (string|null), other_party_role (string|null), "
        "topic (tenant_access|building_coordination|a_e_clarification|owner_direction|sub_coordination|"
        "regulatory|safety|quality|scope|payment|other), "
        "topic_other_description (string|null when topic=other), "
        "summary (one-line, required), detail_text (string|null longer narrative), "
        "outcome (agreement|disagreement|pending|informational|escalation_required), "
        "outcome_notes (string|null). "
        "If a field can't be confidently inferred, use null. "
        "Pick the closest-matching enum even when phrasing varies. No prose, no code fences."
    ),
}


# Heuristic auto-detector — when kind is unspecified or generic, scan
# the transcript for coordination language so the platform can offer a
# draft event without forcing the user to choose kind=coordination_event
# upfront. Cheap keyword check; AI extraction confirms specifics.
_COORDINATION_TRIGGERS = (
    "phone call", "email from", "email to", "spoke with", "spoke to",
    "talked to", "talked with", "meeting with", "letter from", "letter to",
    "tenant rep", "tenant representative", "building manager", "owner direction",
    "a/e", "architect said", "engineer said", "owner said", "got off the phone",
)


def transcript_looks_like_coordination(transcript: str) -> bool:
    if not transcript:
        return False
    low = transcript.lower()
    return any(t in low for t in _COORDINATION_TRIGGERS)


async def extract_fields(transcript: str, kind: str) -> dict:
    sys = EXTRACT_SYSTEMS.get(kind, EXTRACT_SYSTEMS["work_item"])
    from emergentintegrations.llm.chat import LlmChat, UserMessage
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
        await aput_object(storage_path, audio_bytes, "audio/webm")
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

    # Bucket C3b Phase 2 — coordination-event side extraction. Two paths:
    # (1) user explicitly picked kind=coordination_event — `structured`
    #     above already has the event payload; persist directly.
    # (2) user picked some other kind but the transcript reads like
    #     coordination language — run the coordination extractor in
    #     addition and persist a draft event for review. Skipped when
    #     the kind already IS coordination_event (covered by (1)).
    coordination_event_id = None
    coordination_extracted = None
    project_id_for_event = None
    # Only auto-create when we know which project to attach to. The
    # voice annotation may be linked to a daily_report (preferred) or
    # to an entity that itself has project_id; we look those up.
    if linked_entity_type == "daily_report" and linked_entity_id:
        dr = await db.daily_reports.find_one(
            {"tenant_id": tenant_id, "id": linked_entity_id},
            {"_id": 0, "project_id": 1},
        )
        if dr:
            project_id_for_event = dr.get("project_id")

    if transcript and project_id_for_event:
        coord_payload = None
        if kind == "coordination_event" and structured and "_error" not in structured:
            coord_payload = structured
        elif transcript_looks_like_coordination(transcript) and kind != "coordination_event":
            try:
                coord_payload = await extract_fields(transcript, "coordination_event")
            except Exception:
                coord_payload = None

        if coord_payload and "_error" not in coord_payload and coord_payload.get("summary"):
            try:
                ev_doc = _draft_coordination_event_from_payload(
                    payload=coord_payload, tenant_id=tenant_id, user_id=user_id,
                    project_id=project_id_for_event,
                    linked_daily_report_id=(linked_entity_id if linked_entity_type == "daily_report" else None),
                )
                await db.coordination_events.insert_one(ev_doc.copy())
                # Best-effort audit; voice service runs outside a request
                # context so we pass a synthesized request_id.
                try:
                    from audit import write_audit
                    await write_audit(
                        tenant_id=tenant_id, table_name="coordination_events",
                        op="COORDINATION_EVENT_CREATE", record_id=ev_doc["id"],
                        before=None, after=ev_doc,
                        actor_id=user_id, request_id=ev_doc["id"],
                    )
                except Exception:
                    pass
                coordination_event_id = ev_doc["id"]
                coordination_extracted = ev_doc
            except Exception:
                # Don't fail the whole voice flow if coordination persist
                # blows up — the transcript + structured payload are
                # already in the row below for a manual fallback.
                coordination_event_id = None

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
        # Phase 2 — surface the auto-created coordination draft so the
        # voice annotation result UI can show "Coordination event detected"
        # without a second round-trip.
        "coordination_event_id": coordination_event_id,
        "coordination_extracted": coordination_extracted,
        "created_at": utc_now().isoformat(),
    }
    await db.voice_annotations.insert_one(row.copy())
    return row


def _draft_coordination_event_from_payload(
    *, payload: dict, tenant_id: str, user_id: str, project_id: str,
    linked_daily_report_id: Optional[str],
) -> dict:
    """Bucket C3b Phase 2 — build a coordination_events doc from a voice-
    extracted payload. Sane defaults where the AI couldn't infer (e.g.
    occurred_at = now since the memo was just spoken). Marked
    voice_extracted=True so the UI shows it as a draft."""
    now = utc_now()
    other_name = (payload.get("other_party_name") or "").strip() or "(unidentified party)"
    topic = (payload.get("topic") or "other").lower()
    valid_topics = {"tenant_access", "building_coordination", "a_e_clarification",
                     "owner_direction", "sub_coordination", "regulatory", "safety",
                     "quality", "scope", "payment", "other"}
    if topic not in valid_topics:
        topic = "other"
    channel = (payload.get("channel") or "phone").lower()
    valid_channels = {"email", "phone", "in_person", "formal_letter",
                       "text_message", "meeting", "system_notification"}
    if channel not in valid_channels:
        channel = "phone"
    direction = (payload.get("direction") or "inbound").lower()
    if direction not in {"inbound", "outbound", "internal"}:
        direction = "inbound"
    outcome = (payload.get("outcome") or "informational").lower()
    if outcome not in {"agreement", "disagreement", "pending", "informational", "escalation_required"}:
        outcome = "informational"
    return {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "occurred_at": now.isoformat(),
        "channel": channel,
        "direction": direction,
        "our_party_user_id": user_id,
        "other_party_name": other_name[:200],
        "other_party_org": (payload.get("other_party_org") or None),
        "other_party_role": (payload.get("other_party_role") or None),
        "other_party_email": None,
        "other_party_phone": None,
        "topic": topic,
        "topic_other_description": (payload.get("topic_other_description") or None) if topic == "other" else None,
        "summary": (payload.get("summary") or "")[:500],
        "detail_text": (payload.get("detail_text") or None),
        "outcome": outcome,
        "outcome_notes": (payload.get("outcome_notes") or None),
        "evidence_attachment_ids": [],
        "linked_rfi_ids": [],
        "linked_field_issue_ids": [],
        "linked_submittal_ids": [],
        "linked_bulletin_ids": [],
        "linked_activity_ids": [],
        "linked_gating_entity_ids": [],
        "linked_daily_report_id": linked_daily_report_id,
        "voice_extracted": True,
        "created_at": now.isoformat(),
        "created_by_user_id": user_id,
        "updated_at": now.isoformat(),
        "deleted_at": None,
        "deleted_by_user_id": None,
    }
