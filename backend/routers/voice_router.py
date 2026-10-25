"""Voice annotation router."""
import os
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Response, Request
from typing import Optional
from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel
from auth import require_tenant
from services.voice_pipeline import process_voice_annotation
from services.storage import get_object, put_object, aget_object, aput_object, make_path, record_file

router = APIRouter(tags=["voice"])


@router.post("/voice/annotate")
async def voice_annotate(
    file: UploadFile = File(...),
    kind: str = Form("work_item"),
    linked_entity_type: Optional[str] = Form(None),
    linked_entity_id: Optional[str] = Form(None),
    # v7 guided DR flow (2026-07-05) — optional client-measured duration so
    # the mobile voice-note list can show clip length without decoding.
    duration_sec: Optional[float] = Form(None),
    ctx: dict = Depends(require_tenant),
):
    if kind not in ("delay", "safety_event", "qc_issue", "work_item", "rfi", "voice_note"):
        raise HTTPException(400, f"Unknown kind: {kind}")
    audio_bytes = await file.read()
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio file > 25MB; Whisper max")
    if not audio_bytes:
        raise HTTPException(400, "Empty audio file")

    if kind == "voice_note":
        # v7 guided DR flow (2026-07-05) — voice-note-as-RECORD (spec D).
        # Stores the audio + a plain annotation row and returns early:
        # NO Whisper transcription, NO field extraction, NO coordination
        # auto-create. That pipeline stays the separate voice-intake
        # feature. Smallest possible addition, entirely in this router.
        db = get_db()
        filename = file.filename or "voice.webm"
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "webm"
        content_type = file.content_type or "audio/webm"
        storage_path = make_path(ctx["tenant_id"], "audio/voice", ext)
        audio_url = None
        err = None
        try:
            # Worker thread: a slow storage upload must not freeze the
            # server for everyone else (day-1 audit findings 39/42).
            await aput_object(storage_path, audio_bytes, content_type)
            audio_url = storage_path
            await record_file(
                tenant_id=ctx["tenant_id"], storage_path=storage_path,
                original_filename=filename, content_type=content_type,
                size=len(audio_bytes), kind="audio", actor_id=ctx["user"]["id"],
            )
        except Exception as e:
            err = f"storage_put_failed: {e}"
        row = {
            "id": gen_id(),
            "tenant_id": ctx["tenant_id"],
            "created_by": ctx["user"]["id"],
            "audio_url": audio_url,
            "audio_duration_sec": duration_sec,
            "transcript": None,
            "transcript_confidence": None,
            "transcription_service": None,
            "structured_fields": {},
            "kind": "voice_note",
            "linked_entity_type": linked_entity_type,
            "linked_entity_id": linked_entity_id,
            "status": "completed" if audio_url else "failed",
            "error": err,
            "coordination_event_id": None,
            "coordination_extracted": None,
            "created_at": utc_now().isoformat(),
        }
        await db.voice_annotations.insert_one(row.copy())
        return row

    result = await process_voice_annotation(
        tenant_id=ctx["tenant_id"],
        user_id=ctx["user"]["id"],
        audio_bytes=audio_bytes,
        filename=file.filename or "voice.webm",
        kind=kind,
        linked_entity_type=linked_entity_type,
        linked_entity_id=linked_entity_id,
    )
    return result


@router.get("/voice/annotations")
async def list_voice(ctx: dict = Depends(require_tenant)):
    db = get_db()
    rows = await db.voice_annotations.find(
        tenant_filter(ctx["tenant_id"]),
        {"_id": 0},
    ).sort("created_at", -1).limit(100).to_list(100)
    return rows


@router.get("/files/audio/{annotation_id}")
async def get_audio(annotation_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    row = await db.voice_annotations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": annotation_id}), {"_id": 0},
    )
    if not row or not row.get("audio_url"):
        raise HTTPException(404, "Audio not found")
    try:
        data, ct = await aget_object(row["audio_url"])
    except Exception as e:
        raise HTTPException(500, f"Storage error: {e}")
    return Response(content=data, media_type=ct)
