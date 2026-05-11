"""Bucket D Phase 2 -- Photo tray + tagging junction endpoints.

Photo tray = staging area before tagging. Every photo upload from the
mobile camera (PWA) or manual drag/drop lands here first with
status='untagged'. From the tray UI a 3-step wizard (Phase 2.3 frontend)
calls POST /photo-tags to create a `photos` row connecting one tray
entry to one event (XOR rule).

Endpoints:
  POST   /api/photo-tray                     multipart upload, returns tray entry
  GET    /api/projects/{pid}/photo-tray      list (status/source/date filters)
  GET    /api/photo-tray/{id}                detail (single tray row)
  GET    /api/photo-tray/{id}/file           stream image bytes for thumbnail/preview
  POST   /api/photo-tray/{id}/archive        flip status to archived
  POST   /api/photo-tags                     create a Photo (tag tray->event)
  GET    /api/photo-tray/{id}/tags           list Photo rows for a tray entry
  DELETE /api/photo-tags/{photo_id}          remove a single tag

Tenant-scoped via require_tenant. XOR rule on POST /photo-tags
enforced: exactly one of (daily_report_activity_id, delay_id, rfi_id,
qc_issue_id, safety_event_id, coordination_event_id, delivery_id,
drawing_pin_id) must be non-null. Tagging recomputes evidentiary_flag
per Bucket C3b Phase 4 OR semantics.
"""
from __future__ import annotations
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form, Query, Response

from database import get_db, tenant_filter
from auth import require_tenant
from audit import write_audit
from models import (PhotoTray, PhotoTagCreate, EVIDENTIARY_CLASSES,
                     gen_id, utc_now)
from services.storage import put_object, get_object, make_path, record_file


router = APIRouter(tags=["photo-tray"])


# Event-ref fields on Photo. Exactly one must be non-null per row.
_PHOTO_EVENT_REFS = (
    "daily_report_activity_id", "delay_id", "rfi_id", "qc_issue_id",
    "safety_event_id", "coordination_event_id", "delivery_id",
    "drawing_pin_id",
)

# Dedup window for pHash matches: same project + same pHash within
# this duration -> treat as same upload (return existing tray entry).
_PHASH_DEDUP_WINDOW = timedelta(hours=24)


def _extract_image_meta(body: bytes) -> dict:
    """Best-effort EXIF + pHash + dimensions. Never raises."""
    out: dict = {
        "sha256": hashlib.sha256(body).hexdigest(),
        "phash": None,
        "width": None,
        "height": None,
        "exif_taken_at": None,
        "exif_gps_lat": None,
        "exif_gps_lng": None,
    }
    try:
        from PIL import Image, ExifTags
        import imagehash
        img = Image.open(io.BytesIO(body))
        out["width"] = img.width
        out["height"] = img.height
        out["phash"] = str(imagehash.phash(img))
        try:
            exif_raw = img._getexif() or {}
        except Exception:
            exif_raw = {}
        exif: dict = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif_raw.items()}
        if "DateTimeOriginal" in exif:
            raw = str(exif["DateTimeOriginal"])
            try:
                if len(raw) >= 19 and raw[4] == ":":
                    iso = raw[:4] + "-" + raw[5:7] + "-" + raw[8:10] + "T" + raw[11:19]
                else:
                    iso = raw
                out["exif_taken_at"] = datetime.fromisoformat(iso).isoformat()
            except Exception:
                pass
        gps_raw = exif.get("GPSInfo")
        if isinstance(gps_raw, dict):
            from routers.iter6_router import _dms_to_dd
            gps = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in gps_raw.items()}
            if "GPSLatitude" in gps and "GPSLongitude" in gps:
                try:
                    out["exif_gps_lat"] = _dms_to_dd(gps["GPSLatitude"], gps.get("GPSLatitudeRef") or "N")
                    out["exif_gps_lng"] = _dms_to_dd(gps["GPSLongitude"], gps.get("GPSLongitudeRef") or "E")
                except Exception:
                    pass
    except Exception:
        pass
    return out


async def _find_dedup(tenant_id: str, project_id: str,
                       sha256: Optional[str], phash: Optional[str]) -> Optional[dict]:
    """Return an existing tray row that matches on SHA-256 (any time) or
    pHash within the dedup window. None if no match."""
    db = get_db()
    if sha256:
        existing = await db.photo_tray.find_one(
            tenant_filter(tenant_id, {"project_id": project_id, "sha256": sha256}),
            {"_id": 0},
        )
        if existing:
            return existing
    if phash:
        cutoff = (utc_now() - _PHASH_DEDUP_WINDOW).isoformat()
        existing = await db.photo_tray.find_one(
            tenant_filter(tenant_id, {
                "project_id": project_id, "phash": phash,
                "uploaded_at": {"$gte": cutoff},
            }),
            {"_id": 0},
        )
        if existing:
            return existing
    return None


# ============================================================
# Upload
# ============================================================
@router.post("/photo-tray", response_model=PhotoTray)
async def upload_to_tray(
    project_id: str = Form(...),
    capture_source: str = Form("manual_upload"),
    file: UploadFile = File(...),
    bg: BackgroundTasks = None,                # Bucket F Phase 2 -- AI auto-trigger
    ctx: dict = Depends(require_tenant),
):
    """Multipart upload -- writes bytes to storage, extracts EXIF +
    pHash, dedups against existing tray entries, returns the tray row.

    Dedup rules:
      * SHA-256 match (any time) -> return existing (true byte-equality).
      * pHash match within 24h    -> return existing (same scene
        re-uploaded; mobile cameras re-encode and break SHA but keep
        perceptual signature).

    capture_source values: mobile_camera | manual_upload | drawing_pin.
    """
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    if capture_source not in ("mobile_camera", "manual_upload", "drawing_pin"):
        capture_source = "manual_upload"

    body = await file.read()
    if len(body) > 10 * 1024 * 1024:
        raise HTTPException(413, "Photo > 10MB")
    if not body:
        raise HTTPException(400, "Empty file")

    meta = _extract_image_meta(body)
    dup = await _find_dedup(ctx["tenant_id"], project_id, meta["sha256"], meta["phash"])
    if dup:
        return PhotoTray(**dup)

    ext = (file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "heic"):
        ext = "jpg"
    storage_path = make_path(ctx["tenant_id"], "tray", ext)
    ct = file.content_type or "image/jpeg"
    try:
        put_object(storage_path, body, ct)
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    await record_file(
        tenant_id=ctx["tenant_id"], storage_path=storage_path,
        original_filename=file.filename or f"photo.{ext}",
        content_type=ct, size=len(body), kind="photo_tray",
        actor_id=ctx["user"]["id"],
    )
    now_iso = utc_now().isoformat()
    # Bucket F Phase 2 -- pre-check CUI/AI gates so we can decide
    # whether to stamp pending or skipped on the freshly created
    # tray row. The auto-trigger BackgroundTask also re-checks at
    # service entry (belt + suspenders).
    from services.photo_ai import check_cui_safety
    safety = await check_cui_safety(
        tenant_id=ctx["tenant_id"], project_id=project_id,
    )
    if safety.get("safe_to_analyze"):
        initial_ai_status = "pending"
        initial_ai_failure_reason = None
    else:
        initial_ai_status = "skipped"
        initial_ai_failure_reason = safety.get("reason")
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "uploaded_by": ctx["user"]["id"],
        "uploaded_at": now_iso,
        "capture_source": capture_source,
        "source_path": file.filename,
        "storage_key": storage_path,
        "phash": meta["phash"],
        "sha256": meta["sha256"],
        "exif_taken_at": meta["exif_taken_at"],
        "exif_gps_lat": meta["exif_gps_lat"],
        "exif_gps_lng": meta["exif_gps_lng"],
        "width": meta["width"],
        "height": meta["height"],
        "file_size_bytes": len(body),
        "status": "untagged",
        # Bucket F Phase 2 -- AI fields stamped at insert time.
        "ai_analysis_status": initial_ai_status,
        "ai_failure_reason": initial_ai_failure_reason,
        "ai_user_overridden": False,
        "ai_safety_concerns_detected": [],
        "ai_reused_from_phash": False,
    }
    await db.photo_tray.insert_one(doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photo_tray",
        op="PHOTO_TRAY_UPLOAD",
        record_id=doc["id"], before=None, after={
            "project_id": project_id, "capture_source": capture_source,
            "phash": meta["phash"], "sha256": meta["sha256"],
            "size_bytes": len(body),
            "ai_analysis_status": initial_ai_status,
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Bucket F Phase 2 -- fire AI analysis as a BackgroundTask.
    # Only when status=pending (CUI/AI-disabled paths stamped skipped
    # above; no point spawning a task that would just re-check + bail).
    if initial_ai_status == "pending" and bg is not None:
        from services.photo_ai import analyze_photo as _analyze
        async def _run_ai(tenant_id: str, tray_id: str, actor_id: str):
            try:
                await _analyze(tenant_id=tenant_id, tray_id=tray_id,
                                actor_id=actor_id)
            except Exception as exc:
                # The service catches its own errors and stamps the row;
                # this catch is the last line of defense so a runtime
                # crash here doesn't take the worker down.
                import logging
                logging.getLogger("photo_tray_router").warning(
                    "auto-trigger AI failed: %s", exc,
                )
        bg.add_task(_run_ai, ctx["tenant_id"], doc["id"], ctx["user"]["id"])
    return PhotoTray(**doc)


# ============================================================
# List + detail
# ============================================================
@router.get("/projects/{project_id}/photo-tray")
async def list_tray(
    project_id: str,
    status: Optional[str] = Query(None, description="untagged|partially_tagged|fully_tagged|archived"),
    capture_source: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="ISO date"),
    date_to: Optional[str] = Query(None, description="ISO date"),
    untagged_only: bool = Query(False),
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    f: dict = {"project_id": project_id}
    if untagged_only:
        f["status"] = "untagged"
    elif status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if wanted:
            f["status"] = {"$in": wanted}
    else:
        # Task 3 verification fix: archived rows hide by default. The
        # tray UI default view should show actionable photos only;
        # archived ones still resolvable via explicit status=archived.
        f["status"] = {"$ne": "archived"}
    if capture_source:
        f["capture_source"] = capture_source
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to + "T23:59:59Z"
        f["uploaded_at"] = rng
    docs = await db.photo_tray.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("uploaded_at", -1).to_list(length=500)
    return docs


@router.get("/photo-tray/{tray_id}")
async def get_tray_entry(tray_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}), {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Tray entry not found")
    # Also fetch the existing tags so the UI can show "already tagged to..."
    tags = await db.photos.find(
        tenant_filter(ctx["tenant_id"], {"tray_photo_id": tray_id}),
        {"_id": 0, "id": 1, "daily_report_id": 1, "tag_caption": 1,
         "tagged_at": 1, "tagged_by": 1, "photo_class": 1,
         "evidentiary_flag": 1,
         **{k: 1 for k in _PHOTO_EVENT_REFS}},
    ).to_list(length=None)
    return {"tray": doc, "tags": tags}


@router.get("/photo-tray/{tray_id}/file")
async def stream_tray_image(tray_id: str, ctx: dict = Depends(require_tenant)):
    """Stream image bytes for thumbnail/preview in the tray UI."""
    db = get_db()
    doc = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}), {"_id": 0, "storage_key": 1},
    )
    if not doc:
        raise HTTPException(404, "Tray entry not found")
    try:
        data, ct = get_object(doc["storage_key"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    return Response(content=data, media_type=ct)


# ============================================================
# Archive (status flip)
# ============================================================
@router.post("/photo-tray/{tray_id}/archive")
async def archive_tray_entry(tray_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Tray entry not found")
    if before.get("status") == "archived":
        return {"ok": True, "already_archived": True}
    await db.photo_tray.update_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}),
        {"$set": {"status": "archived"}},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photo_tray",
        op="PHOTO_TRAY_ARCHIVE",
        record_id=tray_id, before={"status": before.get("status")},
        after={"status": "archived"},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True}


# ============================================================
# Tagging (tray -> event via Photo row)
# ============================================================
def _xor_check(payload: PhotoTagCreate) -> str:
    """Returns the single non-null event-ref field name, or raises 422."""
    set_fields = [k for k in _PHOTO_EVENT_REFS if getattr(payload, k, None)]
    if len(set_fields) == 0:
        raise HTTPException(
            422,
            "Exactly one event reference required: "
            + ", ".join(_PHOTO_EVENT_REFS),
        )
    if len(set_fields) > 1:
        raise HTTPException(
            422,
            f"Only one event reference allowed; got {len(set_fields)}: "
            + ", ".join(set_fields),
        )
    return set_fields[0]


async def _validate_event_ref(db, tenant_id: str, daily_report_id: str,
                                ref_field: str, ref_id: str) -> dict:
    """Confirm the referenced event exists and (where applicable) belongs
    to the named DR. Returns the event doc for downstream copy of
    project_id and other denorm fields. Raises 422/404 as appropriate."""
    coll = {
        "daily_report_activity_id": "daily_report_activities",
        "delay_id": "delays",
        "rfi_id": "rfis",
        "qc_issue_id": "qc_issues",
        "safety_event_id": "safety_events",
        "coordination_event_id": "coordination_events",
        "delivery_id": "deliveries",
        "drawing_pin_id": "drawing_pins",
    }[ref_field]

    # daily_report_activities don't carry tenant_id; query by id +
    # daily_report_id directly. coordination_events use
    # linked_daily_report_id rather than daily_report_id.
    if ref_field == "daily_report_activity_id":
        ev = await db[coll].find_one(
            {"id": ref_id, "daily_report_id": daily_report_id},
            {"_id": 0},
        )
        if not ev:
            raise HTTPException(404, f"daily_report_activity {ref_id} not on DR {daily_report_id}")
        return ev

    ev = await db[coll].find_one(
        tenant_filter(tenant_id, {"id": ref_id}), {"_id": 0},
    )
    if not ev:
        raise HTTPException(404, f"{coll} entry {ref_id} not found")
    # Optional DR scope check for events that carry daily_report_id.
    dr_field = "linked_daily_report_id" if coll == "coordination_events" else "daily_report_id"
    ev_dr = ev.get(dr_field)
    if ev_dr and ev_dr != daily_report_id:
        # Cross-DR linkage isn't allowed except via supplements (Phase 4).
        # For Phase 2 the wizard always picks a DR; the event must share it.
        raise HTTPException(
            422,
            f"{coll} {ref_id} belongs to DR {ev_dr}, not {daily_report_id}",
        )
    return ev


@router.post("/photo-tags")
async def create_photo_tag(
    payload: PhotoTagCreate, ctx: dict = Depends(require_tenant),
):
    """Create a Photo row tying a tray entry to ONE event. XOR-enforced.
    Tray status flips untagged -> partially_tagged on first tag."""
    db = get_db()
    ref_field = _xor_check(payload)
    ref_id = getattr(payload, ref_field)

    tray = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.tray_photo_id}),
        {"_id": 0},
    )
    if not tray:
        raise HTTPException(404, "Tray entry not found")
    if tray.get("status") == "archived":
        raise HTTPException(409, "Tray entry is archived")

    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.daily_report_id}),
        {"_id": 0, "id": 1, "project_id": 1, "status": 1},
    )
    if not dr:
        raise HTTPException(404, f"daily_report {payload.daily_report_id} not found")
    if dr.get("project_id") != tray.get("project_id"):
        raise HTTPException(
            422,
            "Tray entry and daily report must belong to the same project",
        )
    if dr.get("status") == "submitted":
        raise HTTPException(
            409,
            "Daily report is locked. Use the supplement flow to tag photos against locked DRs.",
        )

    # Validate event ref exists in tenant + (where applicable) on the DR.
    ev = await _validate_event_ref(
        db, ctx["tenant_id"], payload.daily_report_id, ref_field, ref_id,
    )

    # Addition 2 (Bucket D Corrective rev) -- re-validate the event's
    # own linked_task_ids at tag time. The M2M tagging junction (Photo)
    # inherits context from the tagged event; if the event's linkage
    # was somehow broken (corrupted offline, created via copilot/voice
    # bypass before linkage was required, mutated by a future PATCH
    # that skipped validation, etc.) the photo would inherit invalid
    # context. Tag time is the right moment to catch this.
    #
    # Required-linkage events: delays / rfis / qc_issues / deliveries.
    # Optional-linkage events: safety_events / coordination_events.
    # daily_report_activity_id and drawing_pin_id tags don't carry
    # linked_task_ids of their own.
    REQUIRED_LINKAGE_REFS = {"delay_id", "rfi_id", "qc_issue_id", "delivery_id"}
    LINKAGE_OWNING_REFS = REQUIRED_LINKAGE_REFS | {
        "safety_event_id", "coordination_event_id",
    }
    if ref_field in LINKAGE_OWNING_REFS:
        ev_linkage = list(ev.get("linked_task_ids") or [])
        if ref_field in REQUIRED_LINKAGE_REFS and not ev_linkage:
            raise HTTPException(
                422,
                f"Cannot tag photo: the {ref_field.replace('_id', '')} record "
                f"{ref_id} has no linked_task_ids (required for this event "
                f"type). Edit the event to add at least one task link before "
                f"tagging photos.",
            )
        if ev_linkage:
            # Master schedule scope: tasks must belong to the same project.
            project_id = ev.get("project_id") or tray.get("project_id")
            existing_task_rows = await db.tasks.find(
                tenant_filter(ctx["tenant_id"], {
                    "id": {"$in": ev_linkage},
                    "project_id": project_id,
                }),
                {"_id": 0, "id": 1},
            ).to_list(length=None)
            existing_ids = {r["id"] for r in existing_task_rows}
            stale = [i for i in ev_linkage if i not in existing_ids]
            if stale:
                raise HTTPException(
                    422,
                    f"Cannot tag photo: the {ref_field.replace('_id', '')} "
                    f"record {ref_id} carries {len(stale)} stale "
                    f"linked_task_ids that no longer resolve on project "
                    f"{project_id}: {', '.join(stale[:3])}"
                    + (" ..." if len(stale) > 3 else "")
                    + ". Edit the event to refresh its linkage before tagging.",
                )

    # Pull schedule_activity_id from the activity ref for downstream
    # convenience (TIA generator, gallery filters).
    schedule_activity_id = (
        ev.get("schedule_activity_id")
        if ref_field == "daily_report_activity_id"
        else None
    )

    # Evidentiary flag: class in evidentiary set OR drawing_pin attached.
    evidentiary = (
        payload.photo_class in EVIDENTIARY_CLASSES
        or ref_field == "drawing_pin_id"
    )

    now_iso = utc_now().isoformat()
    photo_doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": tray["project_id"],
        "daily_report_id": payload.daily_report_id,
        # Tray + tagging-junction fields
        "tray_photo_id": tray["id"],
        "tagged_at": now_iso,
        "tagged_by": ctx["user"]["id"],
        "tag_caption": payload.tag_caption,
        # Event-ref XOR
        "daily_report_activity_id": payload.daily_report_activity_id,
        "delay_id": payload.delay_id,
        "rfi_id": payload.rfi_id,
        "qc_issue_id": payload.qc_issue_id,
        "safety_event_id": payload.safety_event_id,
        "coordination_event_id": payload.coordination_event_id,
        "delivery_id": payload.delivery_id,
        "drawing_pin_id": payload.drawing_pin_id,
        # Evidentiary discipline (carried from tray + recomputed)
        "storage_path": tray["storage_key"],  # photos.storage_path is the tray's storage_key
        "captured_at": tray.get("uploaded_at") or now_iso,
        "captured_by": tray.get("uploaded_by"),
        "phash": tray.get("phash"),
        "sha256_hash": tray.get("sha256"),
        "device_timestamp": tray.get("exif_taken_at"),
        "server_received_timestamp": tray.get("uploaded_at"),
        "timestamp_mismatch_flag": False,
        "exif_preserved": True,
        "gps_lat": tray.get("exif_gps_lat"),
        "gps_lng": tray.get("exif_gps_lng"),
        "photo_class": payload.photo_class,
        "evidentiary_flag": evidentiary,
        # Legacy/additional
        "schedule_activity_id": schedule_activity_id,
        "tags": [],
        "caption": payload.tag_caption,
        "is_supplement": False,
        "created_at": now_iso,
        # Bucket F Phase 1 -- AI lineage on the tag row.
        "created_via_ai_suggestion": bool(payload.created_via_ai_suggestion),
        "ai_suggestion_id": payload.ai_suggestion_id,
        "ai_confidence_at_tag_time": payload.ai_confidence_at_tag_time,
        # Bucket F Phase 5 (pull-forward Risk #1) -- denormalize the
        # tray's user_overridden flag at tag time so gallery + TIA
        # indicators don't drift if the tray row is later edited or
        # archived. Snapshot semantics: this represents the truth at
        # the moment the user committed the tag, even if the tray's
        # ai_user_overridden later flips. Caller can pass it
        # explicitly; otherwise we read it off the tray row.
        "user_overridden_at_tag_time": bool(
            payload.user_overridden_at_tag_time
            or tray.get("ai_user_overridden", False)
        ),
    }
    await db.photos.insert_one(photo_doc.copy())

    # Flip tray status untagged -> partially_tagged on first tag. We
    # leave the user to mark fully_tagged explicitly via /tray/{id}/done
    # (deferred -- Phase 3 may add it as part of the gallery rework).
    if tray.get("status") == "untagged":
        await db.photo_tray.update_one(
            tenant_filter(ctx["tenant_id"], {"id": tray["id"]}),
            {"$set": {"status": "partially_tagged"}},
        )

    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photos",
        op="PHOTO_TAG_CREATED",
        record_id=photo_doc["id"], before=None,
        after={
            "tray_photo_id": tray["id"],
            "daily_report_id": payload.daily_report_id,
            "ref_field": ref_field, "ref_id": ref_id,
            "photo_class": payload.photo_class,
            "evidentiary_flag": evidentiary,
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # Bucket G Phase 2 (2026-05-11) -- Photo AI producer hook.
    # When a tray with a high-confidence AI analysis gets tagged to
    # an event, drop field suggestions on the linked event so the
    # event's form lights up with the Jigsaw light-green pre-fill.
    # Best-effort; failures here never block the tag-create.
    try:
        await _create_photo_ai_field_suggestions(
            tenant_id=ctx["tenant_id"],
            project_id=tray["project_id"],
            tray=tray, ref_field=ref_field, ref_id=ref_id,
            event_doc=ev, actor_id=ctx["user"]["id"],
            request_id=ctx["request_id"],
        )
    except Exception as exc:
        # Never fail the tag insert just because the suggestion
        # producer choked. Log + carry on.
        import logging
        logging.getLogger("photo_tray_router").warning(
            "photo_ai field_suggestion producer failed: %s", exc,
        )
    return photo_doc


# =====================================================================
# Bucket G Phase 2 -- Photo AI producer hook helper. Lives here
# (instead of services/field_suggestions.py) because it needs the
# photo_tray_router's per-event-type knowledge of which field on
# which collection to suggest into. The service module stays generic.
# =====================================================================

# Per-ref_field, which destination collection + field path takes the
# AI caption. RFIs use "body", coordination events use "summary",
# delays/qc/safety/delivery use "description". daily_report_activity
# and drawing_pin tags don't get a caption suggestion (the field is
# narrative + free-form, and drawing_pin photos are pure evidence).
_REF_TO_CAPTION_TARGET: dict[str, tuple[str, str]] = {
    "delay_id":              ("delay",              "description"),
    "rfi_id":                ("rfi",                "body"),
    "qc_issue_id":           ("qc_issue",           "description"),
    "safety_event_id":       ("safety_event",       "description"),
    "coordination_event_id": ("coordination_event", "summary"),
    "delivery_id":           ("delivery",           "description"),
}

# Safety-concern keyword -> osha_classification mapping. Loose
# substring match against any concern in the tray's
# ai_safety_concerns_detected list. Defaults to "general" when
# nothing matches but at least one concern was flagged.
_OSHA_CLASS_BY_CONCERN = [
    (("fall", "height", "no harness"),               "fall"),
    (("ppe", "hard hat", "safety glasses",
       "high-vis", "hi-vis"),                         "ppe_violation"),
    (("electrical", "exposed wire", "live circuit"), "electrical"),
    (("fire", "ignition", "flammable",
       "extinguisher"),                              "fire"),
    (("trench", "excavation", "shoring"),            "excavation"),
    (("scaffold", "platform"),                       "scaffold"),
    (("rebar", "exposed steel"),                     "struck_by"),
]


# Bucket G Phase 4 (2026-05-11) -- body_part + mechanism inference.
# Same keyword vocabulary as the OSHA-class map; we just map to
# different vocabularies for the safety_event form. Defaults are
# permissive ("unspecified" / "other") so the suggestion still
# gives the PM something to confirm without forcing a hard guess.
_BODY_PART_BY_CONCERN = [
    (("fall", "height", "no harness"),               "multiple"),
    (("electrical", "exposed wire", "live circuit"), "torso/hand"),
    (("ppe", "hard hat"),                             "head"),
    (("safety glasses", "eye"),                       "eyes"),
    (("rebar", "exposed steel"),                      "torso"),
    (("scaffold", "platform"),                        "multiple"),
    (("fire", "ignition", "flammable",
       "extinguisher"),                              "torso/hand"),
]

_MECHANISM_BY_CONCERN = [
    (("fall", "height", "no harness", "scaffold"),    "fall_from_elevation"),
    (("electrical", "exposed wire",
       "live circuit"),                              "electric_shock"),
    (("fire", "ignition", "flammable",
       "extinguisher"),                              "fire_explosion"),
    (("ppe", "hard hat", "safety glasses",
       "high-vis", "hi-vis"),                         "struck_by"),
    (("trench", "excavation", "shoring"),             "caught_in_collapse"),
    (("rebar", "exposed steel"),                      "struck_by"),
]


def _infer_osha_classification(concerns: list[str]) -> str:
    for c in concerns or []:
        lc = (c or "").lower()
        for keywords, klass in _OSHA_CLASS_BY_CONCERN:
            if any(k in lc for k in keywords):
                return klass
    return "general"


def _infer_body_part(concerns: list[str]) -> str:
    """Body-part inference. Returns 'unspecified' when no keyword
    matches; the suggestion still surfaces so the PM gets a prompt
    even if the AI guess is generic."""
    for c in concerns or []:
        lc = (c or "").lower()
        for keywords, part in _BODY_PART_BY_CONCERN:
            if any(k in lc for k in keywords):
                return part
    return "unspecified"


def _infer_mechanism(concerns: list[str]) -> str:
    """Mechanism inference matching the safety_event.mechanism enum
    vocabulary."""
    for c in concerns or []:
        lc = (c or "").lower()
        for keywords, mech in _MECHANISM_BY_CONCERN:
            if any(k in lc for k in keywords):
                return mech
    return "other"


async def _create_photo_ai_field_suggestions(
    *, tenant_id: str, project_id: str, tray: dict,
    ref_field: str, ref_id: str, event_doc: dict,
    actor_id: str, request_id: str,
) -> None:
    """The producer body. Emits one or more FIELD_SUGGESTION_CREATED
    audit rows when conditions are met. No-op if tray has no AI
    complete analysis OR confidence too low OR event already has a
    non-empty value for the target field (don't overwrite human work
    with AI guesses)."""
    if tray.get("ai_analysis_status") != "complete":
        return
    confidence = tray.get("ai_confidence_score")
    if confidence is None or float(confidence) <= 0.3:
        return

    from services import field_suggestions as fs

    # ---- Caption -> event.description / body / summary ----
    caption = (tray.get("ai_suggested_caption") or "").strip()
    target = _REF_TO_CAPTION_TARGET.get(ref_field)
    if caption and target:
        entity_type, field_path = target
        # Only suggest if the field is currently empty -- a human-
        # written description always wins.
        existing_val = (event_doc.get(field_path) or "").strip() \
            if isinstance(event_doc.get(field_path), str) else event_doc.get(field_path)
        if not existing_val:
            await fs.create_suggestion(
                tenant_id=tenant_id, project_id=project_id,
                entity_type=entity_type, entity_id=ref_id,
                field_path=field_path,
                suggested_value=caption,
                suggested_by="ai_photo_vision",
                confidence_score=float(confidence),
                source_context=f"photo_tray_id={tray.get('id')}",
                actor_id=actor_id, request_id=request_id,
            )

    # ---- Safety concerns -> safety_event.osha_classification +
    # body_part + mechanism (Bucket G Phase 4 extension) ----
    if ref_field == "safety_event_id":
        concerns = list(tray.get("ai_safety_concerns_detected") or [])
        if concerns:
            # Trio of suggestions on the safety event. Each lands as its
            # own field_suggestions row so the safety form can light up
            # each input independently in the Jigsaw widget. Skip any
            # field that already carries a human-supplied value.
            trio = [
                ("osha_classification", _infer_osha_classification(concerns)),
                ("body_part",           _infer_body_part(concerns)),
                ("mechanism",           _infer_mechanism(concerns)),
            ]
            for fpath, val in trio:
                if event_doc.get(fpath):
                    continue
                await fs.create_suggestion(
                    tenant_id=tenant_id, project_id=project_id,
                    entity_type="safety_event", entity_id=ref_id,
                    field_path=fpath,
                    suggested_value=val,
                    suggested_by="ai_photo_vision",
                    confidence_score=float(confidence),
                    source_context=(
                        f"photo_tray_id={tray.get('id')}; "
                        f"concerns={','.join(concerns[:5])}"
                    ),
                    actor_id=actor_id, request_id=request_id,
                )

    # ---- Trade -> entity.trade where the entity has a trade field.
    # delays, rfis, qc_issues don't carry trade directly; only
    # coordination_events do (via trade_code). Skip for now.
    trade = (tray.get("ai_suggested_trade") or "").strip()
    if trade and ref_field == "coordination_event_id":
        if not event_doc.get("trade_code"):
            await fs.create_suggestion(
                tenant_id=tenant_id, project_id=project_id,
                entity_type="coordination_event", entity_id=ref_id,
                field_path="trade_code",
                suggested_value=trade,
                suggested_by="ai_photo_vision",
                confidence_score=float(confidence),
                source_context=f"photo_tray_id={tray.get('id')}",
                actor_id=actor_id, request_id=request_id,
            )


@router.get("/photo-tray/{tray_id}/tags")
async def list_tags_for_tray(tray_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.photos.find(
        tenant_filter(ctx["tenant_id"], {"tray_photo_id": tray_id}),
        {"_id": 0},
    ).sort("tagged_at", -1).to_list(length=None)
    return docs


@router.delete("/photo-tags/{photo_id}")
async def delete_photo_tag(photo_id: str, ctx: dict = Depends(require_tenant)):
    """Remove a single tagging-junction row. Tray entry stays; if this
    was the last tag, tray status flips back to 'untagged'."""
    db = get_db()
    photo = await db.photos.find_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0},
    )
    if not photo:
        raise HTTPException(404, "Photo tag not found")
    tray_id = photo.get("tray_photo_id")
    await db.photos.delete_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}),
    )
    if tray_id:
        remaining = await db.photos.count_documents(
            tenant_filter(ctx["tenant_id"], {"tray_photo_id": tray_id}),
        )
        if remaining == 0:
            await db.photo_tray.update_one(
                tenant_filter(ctx["tenant_id"], {"id": tray_id}),
                {"$set": {"status": "untagged"}},
            )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photos",
        op="PHOTO_TAG_REMOVED",
        record_id=photo_id, before=photo, after=None,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, "tray_photo_id": tray_id}


# =====================================================================
# Bucket F Phase 2 (2026-05-08) -- Photo AI endpoints
# =====================================================================
# 7 endpoints + RBAC permission matrix. Per spec:
#   Super:   trigger single + view/override/reject own project
#   Foreman: trigger single + view/override/reject own crew's photos
#   PM:      single + batch + view/override/reject + stats own projects
#   Admin:   all
#   Viewer:  view own projects only
# Cross-tenant URL -> 404 (leak-resistant; never 403).

from auth import has_project_access, can_manage_project_members  # noqa: E402
from models import StrictModel as _AiStrictModel  # alias to avoid name clash
from pydantic import Field as _AiField


class _AiAnalyzePayload(_AiStrictModel):
    """No payload fields -- the tray_id is in the URL. Kept as a model
    so swagger surfaces the empty body explicitly."""
    pass


class _AiBatchPayload(_AiStrictModel):
    project_id: Optional[str] = None
    limit: int = _AiField(default=10, ge=1, le=10)


class _AiRejectPayload(_AiStrictModel):
    reason: str = _AiField(..., min_length=10, max_length=500)


class _AiOverridePayload(_AiStrictModel):
    """Override one or more AI-suggested fields. All optional; pass
    only the fields the user actually changed. override_reason
    required (min 5 chars)."""
    photo_class: Optional[str] = None
    caption: Optional[str] = None
    event_type: Optional[str] = None
    trade: Optional[str] = None
    override_reason: str = _AiField(..., min_length=5, max_length=500)


# Permission helpers -- shared across the 4 mutating endpoints.
_ROLE_VIEW = {"admin", "pm", "px", "super", "foreman", "viewer", "pe"}
_ROLE_TRIGGER_SINGLE = {"admin", "pm", "px", "super", "foreman", "pe"}
_ROLE_BATCH = {"admin", "pm", "px"}
_ROLE_OVERRIDE_REJECT = {"admin", "pm", "px", "super", "foreman", "pe"}
_ROLE_STATS = {"admin", "pm", "px"}
_ROLE_BUDGET = {"admin"}


def _user_role(ctx: dict) -> str:
    return (ctx.get("user") or {}).get("role") or ""


async def _enforce_tray_access(ctx: dict, tray: dict,
                                  required_role_set: set[str]) -> None:
    """Check role tier + per-project membership for a tray-row
    mutation. Cross-tenant rows surface as 404 upstream; this fires
    on tenant-matched rows only."""
    role = _user_role(ctx)
    if role not in required_role_set:
        raise HTTPException(
            403,
            f"Role {role!r} cannot perform this action on AI suggestions",
        )
    if role == "admin":
        return
    ok = await has_project_access(
        tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
        user_role=role, project_id=tray.get("project_id"),
    )
    if not ok:
        # Leak-resistant 404.
        raise HTTPException(404, "Tray photo not found")


# (1) POST /api/photo-tray/{id}/analyze
@router.post("/photo-tray/{tray_id}/analyze")
async def analyze_single(
    tray_id: str, _payload: Optional[_AiAnalyzePayload] = None,
    ctx: dict = Depends(require_tenant),
):
    """Manually trigger / re-trigger AI analysis on one tray row.
    Useful for retrying failed rows or re-running after override."""
    db = get_db()
    tray = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        raise HTTPException(404, "Tray photo not found")
    await _enforce_tray_access(ctx, tray, _ROLE_TRIGGER_SINGLE)
    # Reset status to pending so analyze_photo's atomic flip can fire.
    await db.photo_tray.update_one(
        {"id": tray_id},
        {"$set": {"ai_analysis_status": "pending",
                   "ai_failure_reason": None}},
    )
    from services.photo_ai import analyze_photo as _analyze
    return await _analyze(
        tenant_id=ctx["tenant_id"], tray_id=tray_id,
        actor_id=ctx["user"]["id"],
    )


# (2) POST /api/photo-tray/batch-analyze (admin/PM only)
@router.post("/photo-tray/batch-analyze")
async def analyze_batch(
    payload: _AiBatchPayload, ctx: dict = Depends(require_tenant),
):
    role = _user_role(ctx)
    if role not in _ROLE_BATCH:
        raise HTTPException(
            403, f"Role {role!r} cannot run batch AI analysis (admin/pm only)",
        )
    # Per-project RBAC if project_id is specified.
    if payload.project_id and role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=payload.project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")
    from services.photo_ai import batch_analyze_pending
    result = await batch_analyze_pending(
        tenant_id=ctx["tenant_id"],
        project_id=payload.project_id, limit=payload.limit,
    )
    return result


# (3) POST /api/photo-tray/{id}/reject-ai
@router.post("/photo-tray/{tray_id}/reject-ai")
async def reject_ai(
    tray_id: str, payload: _AiRejectPayload,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    tray = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        raise HTTPException(404, "Tray photo not found")
    await _enforce_tray_access(ctx, tray, _ROLE_OVERRIDE_REJECT)
    from services.photo_ai import reject_analysis
    res = await reject_analysis(
        tenant_id=ctx["tenant_id"], tray_id=tray_id,
        reason=payload.reason, actor_id=ctx["user"]["id"],
    )
    if not res.get("ok"):
        raise HTTPException(422, res.get("reason") or "reject failed")
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photo_tray",
        op="PHOTO_AI_REJECTED",
        record_id=tray_id, before={"ai_status": tray.get("ai_analysis_status")},
        after={"reason": payload.reason,
                "rejected_by": ctx["user"]["id"]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return res


# (4) POST /api/photo-tray/{id}/override-ai
@router.post("/photo-tray/{tray_id}/override-ai")
async def override_ai(
    tray_id: str, payload: _AiOverridePayload,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    tray = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        raise HTTPException(404, "Tray photo not found")
    await _enforce_tray_access(ctx, tray, _ROLE_OVERRIDE_REJECT)
    overrides: dict = {}
    if payload.photo_class is not None:
        overrides["photo_class"] = payload.photo_class
    if payload.caption is not None:
        overrides["caption"] = payload.caption
    if payload.event_type is not None:
        overrides["event_type"] = payload.event_type
    if payload.trade is not None:
        overrides["trade"] = payload.trade
    if not overrides:
        raise HTTPException(422, "no override fields supplied")
    from services.photo_ai import override_analysis
    res = await override_analysis(
        tenant_id=ctx["tenant_id"], tray_id=tray_id, fields=overrides,
        override_reason=payload.override_reason,
        actor_id=ctx["user"]["id"],
    )
    if not res.get("ok"):
        raise HTTPException(422, res.get("reason") or "override failed")
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photo_tray",
        op="PHOTO_AI_OVERRIDDEN",
        record_id=tray_id,
        before=res.get("ai_originals"),
        after={"user_overrides": res.get("user_overrides"),
                "override_reason": payload.override_reason,
                "overridden_by": ctx["user"]["id"]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Also persist (original_ai, user_correction, reason) tuple to
    # the photo_ai_calibration collection for Phase 4 calibration RAG.
    try:
        await db.photo_ai_calibration.insert_one({
            "id": gen_id(),
            "tenant_id": ctx["tenant_id"],
            "tray_id": tray_id,
            "project_id": tray.get("project_id"),
            "kind": "override",
            "original_ai": res.get("ai_originals"),
            "user_correction": res.get("user_overrides"),
            "reason": payload.override_reason,
            "actor_id": ctx["user"]["id"],
            "created_at": utc_now().isoformat(),
        })
    except Exception:
        pass
    return res


# (5) GET /api/photo-tray/{id}/ai-analysis
@router.get("/photo-tray/{tray_id}/ai-analysis")
async def get_ai_analysis(
    tray_id: str, ctx: dict = Depends(require_tenant),
):
    """Returns just the AI fields off a tray row + the lock state.
    Frontend polls this every 3s while status is pending/analyzing."""
    db = get_db()
    tray = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        raise HTTPException(404, "Tray photo not found")
    await _enforce_tray_access(ctx, tray, _ROLE_VIEW)
    return {
        "tray_id": tray_id,
        "ai_analysis_status": tray.get("ai_analysis_status"),
        "ai_analysis_started_at": tray.get("ai_analysis_started_at"),
        "ai_analysis_completed_at": tray.get("ai_analysis_completed_at"),
        "ai_suggested_class": tray.get("ai_suggested_class"),
        "ai_suggested_caption": tray.get("ai_suggested_caption"),
        "ai_suggested_event_type": tray.get("ai_suggested_event_type"),
        "ai_suggested_trade": tray.get("ai_suggested_trade"),
        "ai_confidence_score": tray.get("ai_confidence_score"),
        "ai_safety_concerns_detected": tray.get("ai_safety_concerns_detected") or [],
        "ai_failure_reason": tray.get("ai_failure_reason"),
        "ai_model_version": tray.get("ai_model_version"),
        "ai_user_overridden": bool(tray.get("ai_user_overridden")),
        "ai_user_override_reason": tray.get("ai_user_override_reason"),
        "ai_reused_from_phash": bool(tray.get("ai_reused_from_phash")),
        "ai_cost_tokens_input": tray.get("ai_cost_tokens_input"),
        "ai_cost_tokens_output": tray.get("ai_cost_tokens_output"),
    }


# (6) GET /api/projects/{id}/photo-ai-stats (PM/admin)
@router.get("/projects/{project_id}/photo-ai-stats")
async def get_project_ai_stats(
    project_id: str, days: int = 30,
    ctx: dict = Depends(require_tenant),
):
    role = _user_role(ctx)
    if role not in _ROLE_STATS:
        raise HTTPException(
            403,
            f"Role {role!r} cannot view AI stats (admin/pm only)",
        )
    # Cross-tenant project -> 404.
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")
    if days < 1 or days > 365:
        raise HTTPException(422, "days must be 1..365")
    from services.photo_ai import get_analysis_stats
    return await get_analysis_stats(
        tenant_id=ctx["tenant_id"],
        project_id=project_id, days=days,
    )


# (7) GET /api/tenants/current/ai-budget (admin)
@router.get("/tenants/current/ai-budget")
async def get_current_tenant_budget(ctx: dict = Depends(require_tenant)):
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- AI budget surfaces tenant-level spend",
        )
    from services.photo_ai import check_budget
    return await check_budget(tenant_id=ctx["tenant_id"])


# =====================================================================
# Bucket F Phase 5 (2026-05-11) -- production polish endpoints
# =====================================================================

# PATCH /tenants/current/ai-budget (admin) -- raise/lower the monthly
# cap or flip ai_enabled. Writes a PHOTO_AI_BUDGET_UPDATED audit entry
# so compliance can trace who changed the cap.
@router.patch("/tenants/current/ai-budget")
async def update_tenant_ai_budget(
    payload: "TenantAiBudgetUpdate",  # forward ref, imported below
    ctx: dict = Depends(require_tenant),
):
    from models import TenantAiBudgetUpdate  # local to avoid circular
    if not isinstance(payload, TenantAiBudgetUpdate):
        payload = TenantAiBudgetUpdate(**(payload or {}))
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- only admins can modify AI budget",
        )
    db = get_db()
    updates: dict = {}
    if payload.ai_enabled is not None:
        updates["ai_enabled"] = bool(payload.ai_enabled)
    if payload.ai_monthly_budget_usd is not None:
        updates["ai_monthly_budget_usd"] = float(payload.ai_monthly_budget_usd)
    if not updates:
        raise HTTPException(422, "Provide ai_enabled and/or ai_monthly_budget_usd")
    before = await db.tenants.find_one(
        {"id": ctx["tenant_id"]},
        {"_id": 0, "ai_enabled": 1, "ai_monthly_budget_usd": 1},
    ) or {}
    await db.tenants.update_one(
        {"id": ctx["tenant_id"]}, {"$set": updates},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tenants",
        op="PHOTO_AI_BUDGET_UPDATED",
        record_id=ctx["tenant_id"], before=before, after=updates,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    from services.photo_ai import check_budget
    return await check_budget(tenant_id=ctx["tenant_id"])


# GET /tenants/current/photo-ai-cost-dashboard (admin)
# Per-day spend buckets, top-10 expensive trays, model split, budget.
@router.get("/tenants/current/photo-ai-cost-dashboard")
async def get_cost_dashboard_endpoint(
    days: int = 30, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- cost dashboard surfaces tenant spend",
        )
    if days < 1 or days > 365:
        raise HTTPException(422, "days must be 1..365")
    from services.photo_ai import get_cost_dashboard
    return await get_cost_dashboard(tenant_id=ctx["tenant_id"], days=days)


# GET /tenants/current/photo-ai-timings (admin)
# P50/P95/P99 call timings for the dashboard performance card.
@router.get("/tenants/current/photo-ai-timings")
async def get_timings_endpoint(
    days: int = 7, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- timings expose ops latency data",
        )
    if days < 1 or days > 90:
        raise HTTPException(422, "days must be 1..90")
    from services.photo_ai import get_call_timing_percentiles
    return await get_call_timing_percentiles(
        tenant_id=ctx["tenant_id"], days=days,
    )


# GET /tenants/current/cui-compliance-report (admin)
# Compliance evidence for federal auditors. Writes an audit op every
# time it's pulled so the access itself is auditable.
@router.get("/tenants/current/cui-compliance-report")
async def get_cui_compliance_report_endpoint(
    days: int = 90, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- compliance reports are admin-restricted",
        )
    if days < 1 or days > 365:
        raise HTTPException(422, "days must be 1..365")
    from services.photo_ai import get_cui_compliance_report
    report = await get_cui_compliance_report(
        tenant_id=ctx["tenant_id"], days=days,
    )
    # Audit the pull so the compliance evidence chain shows who looked
    # at what + when. Federal audit pattern: access to compliance
    # surfaces must be logged.
    try:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="tenants",
            op="PHOTO_AI_CUI_COMPLIANCE_REPORT_PULLED",
            record_id=ctx["tenant_id"], before=None,
            after={
                "window_days": days,
                "cui_project_count": report.get("cui_project_count"),
                "compliance_flag_count": report.get("compliance_flag_count"),
                "is_clean": report.get("is_clean"),
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    except Exception:
        pass
    return report


# =====================================================================
# Bucket F Phase 4 (2026-05-08) -- integration endpoints
# =====================================================================

# Severity inference for the safety-event quick-action. Maps AI safety-
# concern keywords (loose substring match) to SafetyEventType +
# Severity. Default tail catches anything we don't recognize.
_SEVERITY_BY_CONCERN = [
    # (keyword fragments, event_type, severity)
    (("fall", "height", "no harness"),      "recordable", "major"),
    (("ppe", "hard hat", "safety glasses",
      "high-vis", "hi-vis"),                "near_miss",  "minor"),
    (("electrical", "exposed wire",
      "live circuit"),                      "first_aid",  "moderate"),
    (("fire", "ignition", "flammable",
      "extinguisher"),                      "recordable", "major"),
    (("trench", "excavation", "shoring"),   "recordable", "major"),
    (("scaffold", "platform"),              "near_miss",  "moderate"),
    (("rebar", "exposed steel"),            "first_aid",  "minor"),
]


def _infer_severity_from_concerns(concerns: list[str]) -> tuple[str, str, str]:
    """Returns (event_type, severity, matched_concern). Falls back to
    (near_miss, minor, '') when no keyword matches."""
    for concern in (concerns or []):
        lc = (concern or "").lower()
        for keywords, ev, sev in _SEVERITY_BY_CONCERN:
            if any(k in lc for k in keywords):
                return ev, sev, concern
    return "near_miss", "minor", ""


class _CreateSafetyFromPhotoPayload(_AiStrictModel):
    """Override flow. Most fields auto-populated from AI suggestion;
    user can edit before save (the frontend prefills + lets the user
    confirm in a small dialog)."""
    daily_report_id: str
    description: Optional[str] = None
    severity: Optional[str] = None         # "minor"|"moderate"|"major"|"critical"
    event_type: Optional[str] = None       # SafetyEventType
    linked_task_ids: Optional[list[str]] = None


@router.post("/photo-tray/{tray_id}/create-safety-event")
async def create_safety_event_from_photo(
    tray_id: str, payload: _CreateSafetyFromPhotoPayload,
    ctx: dict = Depends(require_tenant),
):
    """One-click safety event creation from an AI-flagged photo.
    Auto-fills description from ai_suggested_caption, infers severity
    + event_type from ai_safety_concerns_detected, attaches the photo
    as evidence via a Photo tag row pointing to the new safety_event.
    User can pre-edit any field via the payload."""
    db = get_db()
    tray = await db.photo_tray.find_one(
        tenant_filter(ctx["tenant_id"], {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        raise HTTPException(404, "Tray photo not found")
    await _enforce_tray_access(ctx, tray, _ROLE_OVERRIDE_REJECT)

    # Confirm DR exists in tenant + matches project.
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.daily_report_id}),
        {"_id": 0, "id": 1, "project_id": 1, "report_date": 1, "status": 1},
    )
    if not dr:
        raise HTTPException(404, "Daily report not found")
    if dr.get("project_id") != tray.get("project_id"):
        raise HTTPException(
            422,
            "Daily report's project does not match the photo's project",
        )

    # Build the SafetyEventCreate body. Override fields take precedence
    # over AI-derived defaults.
    concerns = tray.get("ai_safety_concerns_detected") or []
    inferred_event_type, inferred_severity, _ = _infer_severity_from_concerns(concerns)
    description = (
        (payload.description or "").strip()
        or (tray.get("ai_suggested_caption") or "").strip()
        or f"AI-flagged photo (tray {tray_id[:8]}): {', '.join(concerns[:2]) or 'safety concern'}"
    )
    severity = (payload.severity or inferred_severity)
    event_type = (payload.event_type or inferred_event_type)
    # Infer linked_task_ids from the DR's activity grid -- pick the
    # task ids attached to the activities scheduled on that DR's day.
    if payload.linked_task_ids:
        linked_task_ids = payload.linked_task_ids
    else:
        grid = await db.daily_report_activities.find(
            {"daily_report_id": payload.daily_report_id},
            {"_id": 0, "schedule_activity_id": 1},
        ).to_list(length=None)
        linked_task_ids = [
            r["schedule_activity_id"] for r in grid
            if r.get("schedule_activity_id")
        ][:5]   # cap at 5 -- safety events rarely span every task on a day

    # Create the safety event by calling the existing service path
    # (POST /api/safety-events) -- duplicate the body shape here.
    # We don't HTTP-call ourselves; just insert + audit inline.
    from models import gen_id as _gen_id_evt
    se_id = _gen_id_evt()
    now_iso = utc_now().isoformat()
    se_doc = {
        "id": se_id,
        "tenant_id": ctx["tenant_id"],
        "project_id": tray.get("project_id"),
        "daily_report_id": payload.daily_report_id,
        "date": dr.get("report_date"),
        "description": description[:2000],
        "severity": severity,
        "event_type": event_type,
        "status": "open",
        "linked_task_ids": linked_task_ids,
        "is_supplement": False,
        "reported_by": ctx["user"]["id"],
        "reported_at": now_iso,
        "created_at": now_iso,
    }
    await db.safety_events.insert_one(se_doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="safety_events", op="INSERT",
        record_id=se_id, before=None, after={
            **se_doc,
            "_source": "ai_safety_concern_quick_action",
            "source_tray_id": tray_id,
            "ai_safety_concerns": concerns,
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # Auto-attach the photo as evidence -- create a Photo tag row
    # linking the tray photo to the new safety_event.
    photo_id = gen_id()
    photo_doc = {
        "id": photo_id,
        "tenant_id": ctx["tenant_id"],
        "project_id": tray.get("project_id"),
        "tray_photo_id": tray_id,
        "daily_report_id": payload.daily_report_id,
        "safety_event_id": se_id,
        "storage_path": tray.get("storage_key"),
        "captured_at": tray.get("uploaded_at") or now_iso,
        "captured_by": tray.get("uploaded_by"),
        "created_at": now_iso,
        "photo_class": tray.get("ai_suggested_class") or "safety",
        "tag_caption": tray.get("ai_suggested_caption"),
        "tagged_at": now_iso,
        "tagged_by": ctx["user"]["id"],
        "evidentiary_flag": True,           # safety photos are evidentiary
        "sha256_hash": tray.get("sha256"),
        "phash": tray.get("phash"),
        # Bucket F Phase 1 lineage so TIA + Calibration RAG see this
        # photo originated from an AI suggestion.
        "created_via_ai_suggestion": True,
        "ai_suggestion_id": tray_id,
        "ai_confidence_at_tag_time": tray.get("ai_confidence_score"),
        # Bucket F Phase 5 -- snapshot tray's override state at tag time.
        # Quick-action paths always start from an unaltered AI
        # suggestion, but the user may have already overridden tray
        # fields before clicking "Create safety event"; preserve that.
        "user_overridden_at_tag_time": bool(tray.get("ai_user_overridden", False)),
    }
    await db.photos.insert_one(photo_doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photos",
        op="PHOTO_TAG_CREATED",
        record_id=photo_id, before=None, after={
            "tray_photo_id": tray_id, "safety_event_id": se_id,
            "_source": "ai_safety_concern_quick_action",
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Flip tray status to partially_tagged.
    await db.photo_tray.update_one(
        {"id": tray_id},
        {"$set": {"status": "partially_tagged"}},
    )

    return {
        "ok": True,
        "safety_event_id": se_id,
        "photo_id": photo_id,
        "severity": severity,
        "event_type": event_type,
        "linked_task_ids": linked_task_ids,
        "description": description,
        "source_concerns": concerns,
    }


# GET /api/projects/{id}/photos-flagged-by-ai
# Surfaces tray rows with non-empty safety_concerns that haven't yet
# been tagged to a safety_event. Used by the SafetyPanel notification
# on the DR detail page.
@router.get("/projects/{project_id}/photos-flagged-by-ai")
async def list_photos_flagged_by_ai(
    project_id: str, daily_report_id: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    # Cross-tenant 404.
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    role = _user_role(ctx)
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")
    # Walk tray rows with safety concerns + complete analysis.
    q: dict = {
        "tenant_id": ctx["tenant_id"], "project_id": project_id,
        "ai_analysis_status": "complete",
        "ai_safety_concerns_detected.0": {"$exists": True},
    }
    rows = await db.photo_tray.find(
        q, {"_id": 0, "id": 1, "uploaded_at": 1,
              "ai_suggested_caption": 1,
              "ai_safety_concerns_detected": 1,
              "ai_confidence_score": 1, "status": 1},
    ).sort("uploaded_at", -1).limit(50).to_list(50)
    # If daily_report_id supplied, also surface whether each tray was
    # already tagged to a safety_event from THAT DR (so the UI can
    # mark "already handled").
    out: list[dict] = []
    for r in rows:
        already_tagged = False
        if daily_report_id:
            existing = await db.photos.find_one(
                tenant_filter(ctx["tenant_id"], {
                    "tray_photo_id": r["id"],
                    "daily_report_id": daily_report_id,
                    "safety_event_id": {"$ne": None},
                }),
                {"_id": 0, "id": 1},
            )
            already_tagged = bool(existing)
        out.append({**r, "already_tagged_to_safety_event": already_tagged})
    return out


# GET /api/tenants/current/photo-ai-calibration  (admin)
# Surfaces the calibration corpus for future fine-tuning work. Paginated
# by cursor (created_at desc). Returns the raw (original_ai, user_
# correction, reason, kind) tuples.
@router.get("/tenants/current/photo-ai-calibration")
async def list_calibration_corpus(
    limit: int = 100, cursor: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- calibration corpus contains user feedback",
        )
    if limit < 1 or limit > 500:
        limit = 100
    db = get_db()
    q: dict = {"tenant_id": ctx["tenant_id"]}
    if cursor:
        q["created_at"] = {"$lt": cursor}
    rows = await db.photo_ai_calibration.find(
        q, {"_id": 0},
    ).sort("created_at", -1).limit(limit).to_list(limit)
    next_cursor = rows[-1]["created_at"] if rows and len(rows) == limit else None
    # Aggregate counts so the admin export view can show kind breakdown.
    kind_counts: dict[str, int] = {}
    for r in rows:
        k = r.get("kind") or "override"
        kind_counts[k] = kind_counts.get(k, 0) + 1
    return {"rows": rows, "next_cursor": next_cursor,
            "count": len(rows), "kind_counts_in_page": kind_counts}


# Bucket F Phase 3 -- pending-row sweeper. Admin-only manual trigger.
# A lifespan-startup task in server.py also runs this on boot.
@router.post("/photo-tray/sweep-stuck-pending")
async def sweep_stuck_pending_endpoint(
    older_than_minutes: int = 5, limit: int = 100,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_BUDGET:
        raise HTTPException(
            403, "Admins only -- this resets stuck-pending rows tenant-wide",
        )
    from services.photo_ai import sweep_stuck_pending
    result = await sweep_stuck_pending(
        tenant_id=ctx["tenant_id"],
        older_than_minutes=older_than_minutes, limit=limit,
    )
    return result
