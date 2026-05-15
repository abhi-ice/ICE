"""Iter 4 router:
- voice annotation linkage PATCH
- submittals CRUD + auto-generated from spec ingestion
- drawings_spatial_metadata (face binding for Spatial Path 3)
- generic entity GET for pin deep-link

All endpoints respect tenant isolation via tenant_filter().
"""
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from typing import Any, Optional, Literal, List
from pydantic import Field, field_validator
from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel
from auth import require_tenant

# CPM Part K-1 (2026-05-15) -- module-level logger for the per-task
# impacts normalizer's "drop with log warning" semantics on malformed
# entries (e.g., missing required field). Centralized so future
# Submittal helper additions can reuse it.
log = logging.getLogger("iter4_router")

router = APIRouter(tags=["iter4"])


# ============================================================
# VOICE ANNOTATION LINKAGE PATCH
# ============================================================
class VoiceLinkUpdate(StrictModel):
    linked_entity_type: Optional[str] = None
    linked_entity_id: Optional[str] = None


@router.patch("/voice-annotations/{annotation_id}")
async def patch_voice_annotation(
    annotation_id: str, payload: VoiceLinkUpdate, ctx: dict = Depends(require_tenant),
):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields")
    res = await db.voice_annotations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": annotation_id}),
        {"$set": updates},
    )
    if not res.matched_count:
        raise HTTPException(404, "Voice annotation not found")
    row = await db.voice_annotations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": annotation_id}), {"_id": 0},
    )
    return row


# ============================================================
# SUBMITTALS
# ============================================================
SubmittalStatus = Literal[
    "not_yet_submitted", "submitted", "in_review", "approved",
    "approved_as_noted", "rejected", "revise_resubmit", "closed",
]


# ============================================================
# CPM Part K (2026-05-15) -- Submittal per_task_impacts schema.
#
# Over-SLA submittals contribute numeric impact_days to the CPM
# cascade (Path 1 Slip column + Path 2 cumulative remaining-float
# verdict), same shape as delay and RFI per_task_impacts arrays.
#
# Hybrid auto-suggest UX (the K-specific trait not in Parts I/J):
#   - When a submittal flips to over-SLA (detect_breaches in
#     services/submittal_sla.py), the system auto-suggests
#     projected_impact_days = aging_days_past_sla for each linked
#     task and stamps source="system_suggested".
#   - When the super edits an impact value, the source flips to
#     "super_overridden" and the auto-suggest is suppressed for
#     that task_id on subsequent re-breach events (audit-defensible
#     "super judgment supersedes the heuristic").
#
# Per-task entry shape:
#   { task_id: str,                       # required, non-empty
#     projected_impact_days: int >= 0,    # required
#     source: "system_suggested"
#             | "super_overridden",       # required (Part K-specific)
#     # Server-stamped (set by _resolve_submittal_task_impacts in K-3):
#     float_at_logging_days,
#     prior_open_impacts_at_logging_days,
#     remaining_float_at_logging_days,
#     consumes_float_only,
#     pushes_critical_path }
#
# Items missing any required field are DROPPED with a log warning
# (defensive against legacy/malformed rows; the row keeps the rest
# of its per_task_impacts entries). Items with NEGATIVE
# projected_impact_days or UNKNOWN source values raise ValueError
# loudly (422 to the API caller) -- those are real contract
# violations worth surfacing.
# ============================================================
SubmittalImpactSource = Literal["system_suggested", "super_overridden"]
_SUBMITTAL_IMPACT_SOURCES = frozenset(("system_suggested", "super_overridden"))


def _normalize_submittal_per_task_impacts(value: Any) -> List[dict]:
    """Field validator helper for Submittal.per_task_impacts.

    Behavior contract (matches the docstring above):
      * bare list[dict] in: each item must have task_id (non-empty
        string), projected_impact_days (coercible to non-negative int),
        AND source (one of the SubmittalImpactSource values). Items
        missing any required field are DROPPED with a log warning.
      * Negative projected_impact_days raise ValueError -> 422.
      * Unknown source values raise ValueError -> 422.
      * Float projected_impact_days: 3.0 -> 3 (coerce); 3.5 raises
        ValueError ("whole number of days") to match the RFI
        integer convention -- aging_days is always an int from the
        SLA breach computation, and a super override is captured
        via a number input that emits int.
      * None / non-list inputs coerce to [].
      * Server-stamped fields ride through unchanged on read-model
        deserialization (validator never strips the 5 stamp keys).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    out: List[dict] = []
    for item in value:
        if not isinstance(item, dict):
            log.warning(
                "Submittal per_task_impacts: dropping non-dict entry: %r",
                item,
            )
            continue
        tid = item.get("task_id")
        if not isinstance(tid, str) or not tid.strip():
            log.warning(
                "Submittal per_task_impacts: dropping entry with "
                "missing/empty task_id: %r", item,
            )
            continue
        raw_days = item.get("projected_impact_days")
        if raw_days is None:
            log.warning(
                "Submittal per_task_impacts: dropping entry with "
                "missing projected_impact_days for task_id=%r", tid,
            )
            continue
        if isinstance(raw_days, bool):
            # bool is technically int subclass; meaningless here.
            log.warning(
                "Submittal per_task_impacts: dropping entry with "
                "bool projected_impact_days for task_id=%r", tid,
            )
            continue
        if isinstance(raw_days, int):
            days_int = raw_days
        elif isinstance(raw_days, float):
            if raw_days != int(raw_days):
                raise ValueError(
                    f"Submittal per_task_impacts.projected_impact_days "
                    f"must be a whole number of days; got fractional "
                    f"value {raw_days!r} for task_id={tid!r}"
                )
            days_int = int(raw_days)
        else:
            log.warning(
                "Submittal per_task_impacts: dropping entry with "
                "non-numeric projected_impact_days (type=%s) for "
                "task_id=%r", type(raw_days).__name__, tid,
            )
            continue
        if days_int < 0:
            raise ValueError(
                f"Submittal per_task_impacts.projected_impact_days "
                f"must be >= 0; got {days_int} for task_id={tid!r}"
            )
        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            log.warning(
                "Submittal per_task_impacts: dropping entry with "
                "missing/empty source for task_id=%r", tid,
            )
            continue
        if source not in _SUBMITTAL_IMPACT_SOURCES:
            raise ValueError(
                f"Submittal per_task_impacts.source must be one of "
                f"{sorted(_SUBMITTAL_IMPACT_SOURCES)!r}; got "
                f"{source!r} for task_id={tid!r}"
            )
        # Preserve any server-stamped fields the entry already carries
        # (round-trip on read). User-supplied keys override.
        merged = dict(item)
        merged["task_id"] = tid.strip()
        merged["projected_impact_days"] = days_int
        merged["source"] = source
        out.append(merged)
    return out


class SubmittalCreate(StrictModel):
    project_id: str
    spec_section_id: Optional[str] = None
    spec_section_code: Optional[str] = None
    title: str
    type: str = "product_data"
    sub_responsible: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    drawings_affected: list[str] = Field(default_factory=list)
    # Bucket E Phase 1 (2026-05-11) -- register depth. Discipline lives
    # on the submittal so the register can filter (Civil / Structural
    # / Architectural / Mechanical / Electrical / Plumbing / etc.);
    # accepts free text since divisions are project-specific.
    discipline: Optional[str] = None
    type_other_description: Optional[str] = None
    # Procurement lead time -- super or PM enters this; drives
    # must_approve_by computation working backward from the linked
    # task's planned_start.
    procurement_lead_time_days: Optional[int] = Field(default=None, ge=0)
    # Per-submittal SLA override. None -> use the project default.
    # Set when a contract specifies a different SLA for a specific
    # submittal type (e.g. "fire protection submittals reviewed
    # within 7 days").
    sla_days_override: Optional[int] = Field(default=None, ge=1, le=365)
    # CPM Part K-1 (2026-05-15) -- per-task schedule-impact array.
    # OPTIONAL on create (default []). Auto-suggested by
    # detect_breaches when the submittal flips to over-SLA;
    # editable by the super to override (source flips to
    # super_overridden). Empty array on a within-SLA submittal is
    # the expected default; populated only when SLA-breached
    # (or pre-populated via API for testing / data migration).
    per_task_impacts: List[dict] = Field(default_factory=list)

    @field_validator("per_task_impacts", mode="before")
    @classmethod
    def _coerce_submittal_per_task_impacts_create(cls, v):
        return _normalize_submittal_per_task_impacts(v)


class Submittal(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    submittal_number: str
    spec_section_id: Optional[str] = None
    spec_section_code: Optional[str] = None
    title: str
    type: str = "product_data"
    status: SubmittalStatus = "not_yet_submitted"
    sub_responsible: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    drawings_affected: list[str] = Field(default_factory=list)
    submitted_at: Optional[str] = None
    approved_at: Optional[str] = None
    auto_generated: bool = False
    created_by: Optional[str] = None
    created_at: str
    # Bucket C3a Phase 1 — back-pointer to gating_entities CPM node.
    gating_entity_id: Optional[str] = None

    # =========================================================
    # Bucket E Phase 1 (2026-05-11) -- A/E Review SLA + register
    # depth. All fields nullable / default safe so legacy rows
    # deserialize cleanly until migration 013 backfills.
    # =========================================================

    # ----- Register depth -----
    discipline: Optional[str] = None
    revision_number: int = 0
    superseded_by: Optional[str] = None       # newer submittal_id
    type_other_description: Optional[str] = None
    # Procurement lead time + must-approve-by (forward planning).
    procurement_lead_time_days: Optional[int] = None
    must_approve_by: Optional[str] = None     # computed at compute_sla()
    urgency_flag: bool = False                # would miss must_approve_by
    # Lead time on each cycle so the register can show "this is
    # cycle 3" without a separate join.
    review_cycle_count: int = 0
    total_review_calendar_days: int = 0
    total_review_working_days: int = 0

    # ----- A/E review SLA chain timestamps -----
    submitted_to_ae_at: Optional[str] = None
    a_e_review_started_at: Optional[str] = None
    a_e_review_due_at: Optional[str] = None
    a_e_review_completed_at: Optional[str] = None
    received_back_at: Optional[str] = None
    forwarded_to_sub_at: Optional[str] = None
    # SLA status -- "on_track" / "approaching" / "breached" / "n_a".
    sla_status: Literal[
        "n_a", "on_track", "approaching", "breached", "paused",
    ] = "n_a"
    sla_breach_reason: Optional[str] = None
    # Per-submittal SLA override (defaults to project's
    # submittal_review_sla_days when None).
    sla_days_override: Optional[int] = None
    # Whether THIS submittal's SLA is computed in calendar or
    # working days. None -> inherit project setting.
    sla_uses_calendar_days_override: Optional[bool] = None

    # ----- Aging metrics (computed at read time / recompute) -----
    aging_days: Optional[int] = None
    aging_working_days: Optional[int] = None
    is_overdue: bool = False
    overdue_days: Optional[int] = None

    # CPM Part K-1 (2026-05-15) -- per-task schedule-impact array.
    # Auto-suggested by detect_breaches on the SLA-breach moment;
    # editable by the super (source flips to super_overridden).
    # Path 1 (compute_projected_finish_dates K-4 extension) +
    # Path 2 (_sum_prior_open_blocker_impacts_on_task K-5 extension)
    # walk over-SLA submittals' per_task_impacts when computing Slip
    # and cumulative remaining-float verdicts. Legacy submittals
    # backfilled to [] by migration 020.
    #
    # The aging_days / overdue_days / aging_working_days fields above
    # remain pure aging metrics (NOT touched by Part K) -- this is a
    # separate field with its own propagation semantic.
    per_task_impacts: List[dict] = Field(default_factory=list)

    @field_validator("per_task_impacts", mode="before")
    @classmethod
    def _coerce_submittal_per_task_impacts_read(cls, v):
        return _normalize_submittal_per_task_impacts(v)


async def _next_submittal_number(db, tenant_id: str, project_id: str) -> str:
    n = await db.submittals.count_documents(tenant_filter(tenant_id, {"project_id": project_id}))
    return f"SUB-{n + 1:04d}"


@router.post("/submittals", response_model=Submittal)
async def create_submittal(
    payload: SubmittalCreate, bg: BackgroundTasks,
    ctx: dict = Depends(require_tenant),
):
    # Bucket J Phase 2 (2026-05-11) -- submittal_tech + pm + admin
    # are the write tier for submittals. super stays read-only here
    # per spec (super logs field events but doesn't author the
    # formal submittal package).
    from auth import role_can_write_submittals
    user_role = (ctx["user"].get("role") or "").lower()
    if not role_can_write_submittals(user_role):
        raise HTTPException(
            403,
            f"Role {user_role!r} cannot create submittals "
            f"(submittal_tech / pm / admin only)",
        )
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    sub_no = await _next_submittal_number(db, ctx["tenant_id"], payload.project_id)
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "submittal_number": sub_no,
        "status": "not_yet_submitted",
        "submitted_at": None,
        "approved_at": None,
        "auto_generated": False,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
        # Bucket E Phase 1 -- ensure register-depth defaults stamp
        # on fresh rows (matches migration 013 pass B).
        "revision_number": 0, "superseded_by": None,
        "must_approve_by": None, "urgency_flag": False,
        "review_cycle_count": 0,
        "total_review_calendar_days": 0, "total_review_working_days": 0,
        "submitted_to_ae_at": None, "a_e_review_started_at": None,
        "a_e_review_due_at": None, "a_e_review_completed_at": None,
        "received_back_at": None, "forwarded_to_sub_at": None,
        "sla_status": "n_a", "sla_breach_reason": None,
        "sla_uses_calendar_days_override": None,
        "aging_days": None, "aging_working_days": None,
        "is_overdue": False, "overdue_days": None,
    })
    await db.submittals.insert_one(doc.copy())
    # Bucket C3a Phase 1 — auto-create gating_entities row.
    from services.gating_entities import create_for_entity
    gates = [doc["schedule_activity_id"]] if doc.get("schedule_activity_id") else []
    ge = await create_for_entity(
        tenant_id=ctx["tenant_id"], project_id=doc["project_id"],
        entity_type="submittal", entity=doc, gates_task_ids=gates,
    )
    await db.submittals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": doc["id"]}),
        {"$set": {"gating_entity_id": ge["id"]}},
    )
    doc["gating_entity_id"] = ge["id"]

    # Bucket E Phase 4c (2026-05-11) -- ai_submittal_classifier hook.
    # Filename-based classification fires synchronously (fast); PDF
    # text extraction (heavier) happens via BG task. Both create
    # field_suggestions in the Jigsaw framework so the register
    # form lights up with discipline + type + spec_section_code
    # pending-confirmation pills.
    title_as_filename = doc.get("title") or ""
    async def _bg_classify(tid: str, pid: str, sid: str, filename: str,
                            actor: str, req_id: str):
        try:
            from services.submittal_classifier import classify_and_suggest
            await classify_and_suggest(
                tenant_id=tid, project_id=pid, submittal_id=sid,
                filename=filename,
                pdf_bytes=None,   # PDF attach flow lands the bytes via separate route
                actor_id=actor, request_id=req_id,
            )
        except Exception:
            import logging
            logging.getLogger("iter4_router").warning(
                "submittal_classifier BG task failed", exc_info=True,
            )
    bg.add_task(
        _bg_classify, ctx["tenant_id"], doc["project_id"],
        doc["id"], title_as_filename,
        ctx["user"]["id"], ctx["request_id"],
    )

    # Bucket E Phase 2 -- if a schedule_activity_id was supplied,
    # compute must_approve_by inline so the register surfaces the
    # urgency flag immediately. Recompute pass also fires after the
    # submittal is later submitted-to-ae.
    if doc.get("schedule_activity_id") and doc.get("procurement_lead_time_days"):
        try:
            from services import submittal_sla
            await submittal_sla.recompute_aging_for_submittal(
                tenant_id=ctx["tenant_id"], submittal_id=doc["id"],
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
            row = await db.submittals.find_one(
                tenant_filter(ctx["tenant_id"], {"id": doc["id"]}),
                {"_id": 0},
            )
            if row:
                doc = row
        except Exception:
            pass

    return Submittal(**doc)


@router.get("/projects/{project_id}/submittals", response_model=list[Submittal])
async def list_submittals(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.submittals.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("submittal_number", 1).to_list(2000)
    return [Submittal(**d) for d in docs]


class SubmittalUpdate(StrictModel):
    status: Optional[SubmittalStatus] = None
    sub_responsible: Optional[str] = None
    submitted_at: Optional[str] = None
    approved_at: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    spec_section_id: Optional[str] = None
    # Bucket E Phase 1 (2026-05-11) -- register depth + SLA editable
    # fields. The chain timestamps + computed aging metrics are
    # written by services/submittal_sla.py (Phase 2); editable from
    # this PATCH endpoint covers the human-supplied configuration.
    title: Optional[str] = None
    type: Optional[str] = None
    type_other_description: Optional[str] = None
    discipline: Optional[str] = None
    procurement_lead_time_days: Optional[int] = Field(default=None, ge=0)
    sla_days_override: Optional[int] = Field(default=None, ge=1, le=365)
    sla_uses_calendar_days_override: Optional[bool] = None
    drawings_affected: Optional[list[str]] = None
    spec_section_code: Optional[str] = None
    # CPM Part K-1 (2026-05-15) -- per-task schedule-impact array
    # patchable. None on PATCH means "leave unchanged" (the typical
    # state-transition PATCH); empty list explicitly clears all
    # impacts (super decides this submittal has no schedule impact
    # despite being over SLA); populated array replaces the existing
    # snapshot (super override -- on save, the K-3 helper stamps
    # source="super_overridden" if not already set, then routes
    # through auto_recompute_after_event).
    per_task_impacts: Optional[List[dict]] = None

    @field_validator("per_task_impacts", mode="before")
    @classmethod
    def _coerce_submittal_per_task_impacts_update(cls, v):
        # None on PATCH means "don't change this field" -- pass through.
        if v is None:
            return None
        return _normalize_submittal_per_task_impacts(v)


@router.patch("/submittals/{submittal_id}", response_model=Submittal)
async def update_submittal(submittal_id: str, payload: SubmittalUpdate, ctx: dict = Depends(require_tenant)):
    # Bucket J Phase 2 -- write gate (submittal_tech / pm / admin).
    from auth import role_can_write_submittals
    user_role = (ctx["user"].get("role") or "").lower()
    if not role_can_write_submittals(user_role):
        raise HTTPException(
            403,
            f"Role {user_role!r} cannot edit submittals "
            f"(submittal_tech / pm / admin only)",
        )
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        row = await db.submittals.find_one(tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"_id": 0})
        if not row: raise HTTPException(404)
        return Submittal(**row)
    # Bucket E Phase 2 (2026-05-11) -- auto-trigger record_review_event
    # on status transitions. The map below mirrors _STATUS_TRANSITIONS
    # on the service module; we fire the right event_type so the
    # chain-of-custody log + audit chain stay synchronized.
    before_row = await db.submittals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": submittal_id}),
        {"_id": 0, "status": 1},
    )
    before_status = (before_row or {}).get("status")
    new_status = updates.get("status")
    res = await db.submittals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"$set": updates},
    )
    if not res.matched_count:
        raise HTTPException(404, "Submittal not found")
    if new_status and new_status != before_status:
        # Map status -> event_type. submitted/in_review carry the SLA
        # start + review-started semantics; the disposition statuses
        # carry the corresponding terminal event.
        _STATUS_TO_EVENT = {
            "submitted":          "submitted_to_ae",
            "in_review":          "ae_review_started",
            "approved":           "approved",
            "approved_as_noted":  "approved_as_noted",
            "rejected":           "rejected",
            "revise_resubmit":    "revise_resubmit",
            "closed":             "closed",
        }
        event_type = _STATUS_TO_EVENT.get(new_status)
        if event_type:
            try:
                from services import submittal_sla
                await submittal_sla.record_review_event(
                    tenant_id=ctx["tenant_id"],
                    submittal_id=submittal_id,
                    event_type=event_type,
                    performed_by=ctx["user"]["id"],
                    notes=f"Status transition via PATCH /submittals/{submittal_id[:8]}: "
                          f"{before_status} -> {new_status}",
                    request_id=ctx["request_id"],
                )
            except Exception:
                import logging
                logging.getLogger("iter4_router").warning(
                    "submittal SLA auto-event on status change failed",
                    exc_info=True,
                )
    row = await db.submittals.find_one(tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"_id": 0})
    # Bucket C3a Phase 1 — mirror status / response timestamp onto the
    # gating_entities CPM node.
    if "status" in updates or "approved_at" in updates:
        from services.gating_entities import sync_status
        from datetime import datetime as _dt
        responded_at = None
        if updates.get("approved_at"):
            try:
                responded_at = _dt.fromisoformat(str(updates["approved_at"]).replace("Z", "+00:00"))
            except Exception:
                responded_at = None
        await sync_status(
            tenant_id=ctx["tenant_id"], entity_type="submittal", entity_id=submittal_id,
            underlying_status=row.get("status"),
            actual_responded_at=responded_at,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    return Submittal(**row)


# ============================================================
# DRAWINGS SPATIAL METADATA — Path 3 face binding
# ============================================================
DrawingType = Literal["plan", "elevation_north", "elevation_south", "elevation_east", "elevation_west", "section", "rcp", "detail"]


class SpatialMetaUpsert(StrictModel):
    drawing_revision_id: str
    page_number: int = 1
    drawing_type: DrawingType
    floor_id: Optional[str] = None
    scale_ft_per_px: Optional[float] = None
    origin_offset_x_ft: float = 0
    origin_offset_y_ft: float = 0
    rotation_deg: float = 0
    calibrated: bool = False


class SpatialMeta(ReadModel):
    id: str
    tenant_id: str
    drawing_revision_id: str
    page_number: int
    drawing_type: str
    floor_id: Optional[str] = None
    scale_ft_per_px: Optional[float] = None
    origin_offset_x_ft: float = 0
    origin_offset_y_ft: float = 0
    rotation_deg: float = 0
    calibrated: bool = False
    calibrated_by: Optional[str] = None
    calibrated_at: Optional[str] = None
    created_at: str


@router.post("/drawings-spatial-metadata", response_model=SpatialMeta)
async def upsert_spatial_meta(payload: SpatialMetaUpsert, ctx: dict = Depends(require_tenant)):
    db = get_db()
    rev = await db.drawing_revisions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.drawing_revision_id}), {"_id": 0, "id": 1, "drawing_id": 1},
    )
    if not rev:
        raise HTTPException(404, "Drawing revision not found")
    # find existing for (revision, page)
    existing = await db.drawings_spatial_metadata.find_one(
        tenant_filter(ctx["tenant_id"], {
            "drawing_revision_id": payload.drawing_revision_id,
            "page_number": payload.page_number,
        }), {"_id": 0},
    )
    base = payload.model_dump(mode="json")
    base["calibrated_by"] = ctx["user"]["id"] if payload.calibrated else None
    base["calibrated_at"] = utc_now().isoformat() if payload.calibrated else None
    base["drawing_id"] = rev["drawing_id"]
    if existing:
        await db.drawings_spatial_metadata.update_one(
            {"id": existing["id"]}, {"$set": base},
        )
        out = await db.drawings_spatial_metadata.find_one({"id": existing["id"]}, {"_id": 0})
        return SpatialMeta(**out)
    base.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "created_at": utc_now().isoformat(),
    })
    await db.drawings_spatial_metadata.insert_one(base.copy())
    return SpatialMeta(**base)


@router.get("/projects/{project_id}/spatial-metadata", response_model=list[SpatialMeta])
async def list_spatial_meta(project_id: str, ctx: dict = Depends(require_tenant)):
    """Return spatial metadata for all drawing revisions belonging to the project."""
    db = get_db()
    revs = await db.drawing_revisions.find(
        tenant_filter(ctx["tenant_id"]), {"_id": 0, "id": 1, "drawing_id": 1, "pdf_page_count": 1},
    ).to_list(5000)
    drawings = await db.drawings.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0, "id": 1},
    ).to_list(5000)
    drawing_ids = {d["id"] for d in drawings}
    rev_ids = [r["id"] for r in revs if r["drawing_id"] in drawing_ids]
    if not rev_ids:
        return []
    docs = await db.drawings_spatial_metadata.find(
        tenant_filter(ctx["tenant_id"], {"drawing_revision_id": {"$in": rev_ids}}), {"_id": 0},
    ).to_list(2000)
    return [SpatialMeta(**d) for d in docs]


# ============================================================
# GENERIC ENTITY FETCH — for pin deep-link
# ============================================================
ENTITY_COLLECTIONS = {
    "task": "tasks",
    "rfi": "rfis",
    "work_item": "work_items",
    "delay": "delays",
    "safety_event": "safety_events",
    "qc_issue": "qc_issues",
    "photo": "photos",
    "delivery": "deliveries",
    "change_order": "change_orders",
    "submittal": "submittals",
}


@router.get("/entities/{entity_type}/{entity_id}")
async def get_entity(entity_type: str, entity_id: str, ctx: dict = Depends(require_tenant)):
    coll = ENTITY_COLLECTIONS.get(entity_type)
    if not coll:
        raise HTTPException(400, f"Unknown entity type: {entity_type}")
    db = get_db()
    doc = await db[coll].find_one(tenant_filter(ctx["tenant_id"], {"id": entity_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Entity not found")
    return {"entity_type": entity_type, "entity": doc}


# ============================================================
# UPDATE LOCATION POLYGON (used by polygon editor)
# ============================================================
class LocationPolygonUpdate(StrictModel):
    polygon_geojson: dict


@router.patch("/locations/{location_id}/polygon")
async def update_location_polygon(location_id: str, payload: LocationPolygonUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    coords = payload.polygon_geojson.get("coordinates", [[]])[0]
    if len(coords) < 4:
        raise HTTPException(400, "Polygon needs at least 3 vertices (closed ring of 4 points)")
    cx = sum(c[0] for c in coords[:-1]) / max(1, len(coords) - 1)
    cy = sum(c[1] for c in coords[:-1]) / max(1, len(coords) - 1)
    res = await db.locations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": location_id}),
        {"$set": {
            "polygon_geojson": payload.polygon_geojson,
            "centroid_x": cx,
            "centroid_y": cy,
        }},
    )
    if not res.matched_count:
        raise HTTPException(404, "Location not found")
    doc = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"_id": 0})
    return doc
