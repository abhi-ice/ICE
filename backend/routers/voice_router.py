"""Voice annotation router."""
import os
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Response, Request
from typing import Optional
from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel
from auth import require_tenant
from services.voice_pipeline import process_voice_annotation
from services.storage import get_object

router = APIRouter(tags=["voice"])


@router.post("/voice/annotate")
async def voice_annotate(
    file: UploadFile = File(...),
    kind: str = Form("work_item"),
    linked_entity_type: Optional[str] = Form(None),
    linked_entity_id: Optional[str] = Form(None),
    ctx: dict = Depends(require_tenant),
):
    if kind not in ("delay", "safety_event", "qc_issue", "work_item", "rfi"):
        raise HTTPException(400, f"Unknown kind: {kind}")
    audio_bytes = await file.read()
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio file > 25MB; Whisper max")
    if not audio_bytes:
        raise HTTPException(400, "Empty audio file")
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
        data, ct = get_object(row["audio_url"])
    except Exception as e:
        raise HTTPException(500, f"Storage error: {e}")
    return Response(content=data, media_type=ct)
