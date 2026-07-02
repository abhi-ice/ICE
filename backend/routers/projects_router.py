"""Projects + floors + locations CRUD."""
from datetime import date, datetime, timezone, timedelta
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, UploadFile, File
from typing import Optional
from database import get_db, tenant_filter
from models import (
    Project, ProjectCreate, ProjectUpdate,
    Floor, FloorCreate, Location, LocationCreate,
    gen_id, utc_now,
)
from auth import require_tenant, can_manage_project_members
from audit import write_audit

router = APIRouter(tags=["projects"])


# Bucket C3a Phase 1 — populates project_calendar_days for the project's
# planned date range. Mon-Fri = working day; weekends not. C3b will
# extend with holidays / closures / weather.
async def _initialize_project_calendar(
    db, tenant_id: str, project_id: str,
    start: Optional[date] = None, end: Optional[date] = None,
) -> int:
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}), {"_id": 0},
    )
    if not proj:
        return 0
    s = start
    if not s:
        raw = proj.get("start_date") or proj.get("created_at")
        try:
            s = date.fromisoformat(str(raw)[:10]) if raw else date.today()
        except Exception:
            s = date.today()
    e = end
    if not e:
        raw = proj.get("planned_completion_date") or proj.get("actual_completion_date")
        try:
            e = date.fromisoformat(str(raw)[:10]) if raw else (s + timedelta(days=730))  # 2y default
        except Exception:
            e = s + timedelta(days=730)
    if e < s:
        return 0
    # Find which dates are already populated (idempotent re-run).
    existing = await db.project_calendar_days.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "calendar_date": {"$gte": s.isoformat(), "$lte": e.isoformat()},
        }),
        {"_id": 0, "calendar_date": 1},
    ).to_list(5000)
    seen = {str(d["calendar_date"])[:10] for d in existing}
    now_iso = utc_now().isoformat()
    inserted = 0
    cur = s
    batch: list[dict] = []
    while cur <= e:
        iso = cur.isoformat()
        if iso not in seen:
            batch.append({
                "id": gen_id(),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "calendar_date": iso,
                "is_working_day": cur.weekday() < 5,
                "is_holiday": False,
                "is_building_closed": False,
                "is_restricted_access": False,
                "notes": None,
                "created_at": now_iso,
            })
            inserted += 1
        cur += timedelta(days=1)
        if len(batch) >= 500:
            await db.project_calendar_days.insert_many(batch)
            batch = []
    if batch:
        await db.project_calendar_days.insert_many(batch)
    return inserted


# --- projects ---
@router.post("/projects", response_model=Project)
async def create_project(payload: ProjectCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    # Engine pair rule (file pipeline, 2026-07-02): the Microsoft Project
    # connector always rides on the impact-days engine -- mode2 + enterprise
    # must be impossible, so mode2 forces the engine in the same write.
    if doc.get("schedule_mode") == "mode2":
        doc["schedule_engine"] = "impact_days"
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "actual_completion_date": None,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    })
    await db.projects.insert_one(doc.copy())
    # UI engine dropdown (2026-07-02): same one-action contract as the PATCH
    # path -- picking an engine at creation also enables its global AND-gate.
    from services import runtime_settings
    actor_email = (ctx.get("user") or {}).get("email") or ""
    if doc.get("schedule_engine") == "enterprise":
        await runtime_settings.ensure_flag_on("FF_ENTERPRISE_CPM", updated_by=actor_email)
    if doc.get("schedule_mode") == "mode2":
        await runtime_settings.ensure_flag_on("FF_SCHEDULE_MODE_2", updated_by=actor_email)
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects", op="INSERT",
        record_id=doc["id"], before=None, after=doc, actor_id=ctx["user"]["id"],
        request_id=ctx["request_id"],
    )
    # Bucket C3a Phase 1 — auto-populate the working-day calendar so SLA
    # math on RFIs / submittals / etc has a calendar to reference. Mon-Fri
    # default; user can call /initialize-calendar later to re-seed a
    # different range.
    try:
        await _initialize_project_calendar(db, ctx["tenant_id"], doc["id"])
    except Exception:
        # Calendar init is best-effort on create — don't block project
        # creation if it fails. /initialize-calendar can be called later.
        pass
    return Project(**doc)


@router.post("/projects/{project_id}/initialize-calendar")
async def initialize_calendar(
    project_id: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    ctx: dict = Depends(require_tenant),
):
    """Populate project_calendar_days for the project's planned date range
    (or [start_date, end_date] if specified). Mon-Fri = working day;
    weekends not. Idempotent — re-runs skip dates already present."""
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    inserted = await _initialize_project_calendar(
        db, ctx["tenant_id"], project_id, start=start_date, end=end_date,
    )
    return {"inserted": inserted}


@router.get("/projects", response_model=list[Project])
async def list_projects(
    status_filter: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if status_filter:
        f["status"] = status_filter
    docs = await db.projects.find(f, {"_id": 0}).sort("created_at", 1).to_list(500)
    return [Project(**d) for d in docs]


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Project not found")
    return Project(**doc)


@router.patch("/projects/{project_id}", response_model=Project)
async def update_project(project_id: str, payload: ProjectUpdate, bg: BackgroundTasks = None,
                          ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        return await get_project(project_id, ctx)
    before = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Project not found")
    # Engine lock (file pipeline, 2026-07-02): once a project adopts an engine
    # (mode2 or enterprise) the choice is permanent -- swapping engines would
    # orphan the hosted .mpp / the enterprise columns mid-project. The dialog
    # always sends both fields on every save, so a same-value write must pass.
    if before.get("schedule_engine") == "enterprise" or before.get("schedule_mode") == "mode2":
        cur_engine = before.get("schedule_engine") or "impact_days"
        cur_mode = before.get("schedule_mode") or "mode1"
        if (("schedule_engine" in updates and updates["schedule_engine"] != cur_engine)
                or ("schedule_mode" in updates and updates["schedule_mode"] != cur_mode)):
            raise HTTPException(
                409,
                "Schedule engine is locked once selected. It cannot be changed "
                "after a project adopts an engine.")
    # Engine pair rule: mode2 always rides on the impact-days engine (mode2 +
    # enterprise must be impossible) -- forced in the same $set.
    if updates.get("schedule_mode") == "mode2":
        updates["schedule_engine"] = "impact_days"
    first_enterprise = (
        updates.get("schedule_engine") == "enterprise"
        and (before.get("schedule_engine") or "impact_days") != "enterprise")
    await db.projects.update_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"$set": updates},
    )
    # UI engine dropdown (2026-07-02): choosing an engine is ONE action.
    # The global feature flag is an AND-gate with the per-project field, so
    # flipping it on here can't change any project that didn't opt in.
    from services import runtime_settings
    actor_email = (ctx.get("user") or {}).get("email") or ""
    if updates.get("schedule_engine") == "enterprise":
        await runtime_settings.ensure_flag_on("FF_ENTERPRISE_CPM", updated_by=actor_email)
    if updates.get("schedule_mode") == "mode2":
        await runtime_settings.ensure_flag_on("FF_SCHEDULE_MODE_2", updated_by=actor_email)
    # First enterprise adoption: run the engine once right away so the
    # project's numbers appear without waiting for a schedule event. mode2
    # adoption intentionally gets NO recompute here -- its numbers arrive via
    # the file-upload auto-sync.
    if first_enterprise and bg is not None:
        from services.cpm import auto_recompute_after_event
        bg.add_task(auto_recompute_after_event, ctx["tenant_id"], project_id)
    after = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects", op="UPDATE",
        record_id=project_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return Project(**after)


# ============================================================
# Hard delete a project (2026-02-XX). Cascades across all collections
# scoped to this project_id. Admin only. Audit row written before
# the cascade so the project_id stays referenceable.
# ============================================================
PROJECT_SCOPED_COLLECTIONS = [
    "daily_reports", "daily_report_activities", "daily_presence",
    "delays", "rfis", "safety_events", "qc_issues", "deliveries",
    "coordination_events", "photos", "tasks", "floors", "locations",
    "resources", "specs", "submittals", "drawings", "schedule_imports",
    "schedule_revisions", "recovery_reports", "marketplace_listings",
    "notifications", "approvals", "audit_log",
]


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, ctx: dict = Depends(require_tenant)):
    """Permanently delete a project and every record scoped to it.
    Admin only. Irreversible. Writes one audit row pre-cascade so the
    deletion intent is preserved in the tenant's history.
    """
    role = (ctx["user"].get("role") or "").lower()
    if role != "admin":
        raise HTTPException(403, "Only project admins can delete a project")
    db = get_db()
    project = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0},
    )
    if not project:
        raise HTTPException(404, "Project not found")
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects", op="DELETE_PROJECT",
        record_id=project_id, before=project, after={},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    deleted_counts = {}
    for coll in PROJECT_SCOPED_COLLECTIONS:
        try:
            res = await db[coll].delete_many({"project_id": project_id})
            deleted_counts[coll] = res.deleted_count
        except Exception:
            deleted_counts[coll] = -1
    await db.projects.delete_one(tenant_filter(ctx["tenant_id"], {"id": project_id}))
    return {"ok": True, "project_id": project_id, "cascaded": deleted_counts}




# ============================================================
# Feature Client DR Template Export (2026-05-20)
# Per-project DR template management. One active template per
# project; admin or PM can upload / replace / delete via these
# endpoints. The Project model's dr_template_* fields are NOT
# editable through ProjectUpdate -- this is the only write path.
# ============================================================

@router.post("/projects/{project_id}/dr-template")
async def upload_dr_template(
    project_id: str,
    file: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    """Upload (or replace) the client DR template for a project.
    Accepts .xlsx (export engine target) or .pdf (slot saved but export
    returns 400 'not yet supported'). Admin or PM only."""
    role = (ctx["user"].get("role") or "").lower()
    if not can_manage_project_members(role):
        raise HTTPException(
            403,
            "Only project admins or PMs can upload a DR template.",
        )
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0},
    )
    if not proj:
        raise HTTPException(404, "Project not found")

    filename = file.filename or "dr_template"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    if ext not in ("xlsx", "pdf"):
        raise HTTPException(
            400, f"Only .xlsx or .pdf templates are accepted (got .{ext}).",
        )
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")

    from services.storage import put_object, make_path
    path = make_path(ctx["tenant_id"], "dr_templates", ext)
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if ext == "xlsx" else "application/pdf"
    )
    put_object(path, data, content_type)

    now = utc_now().isoformat()
    updates = {
        "dr_template_file_url": path,
        "dr_template_file_name": filename,
        "dr_template_uploaded_at": now,
        "dr_template_uploaded_by": ctx["user"]["id"],
    }
    await db.projects.update_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"$set": updates},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects",
        op="DR_TEMPLATE_UPLOADED", record_id=project_id,
        before={"dr_template_file_name": proj.get("dr_template_file_name")},
        after={"dr_template_file_name": filename, "size_bytes": len(data)},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {
        "file_name": filename,
        "uploaded_at": now,
        "uploaded_by": ctx["user"]["id"],
        "size_bytes": len(data),
    }


@router.delete("/projects/{project_id}/dr-template")
async def delete_dr_template(
    project_id: str,
    ctx: dict = Depends(require_tenant),
):
    """Remove the project's DR template (clears the four
    dr_template_* fields). Admin or PM only. Idempotent."""
    role = (ctx["user"].get("role") or "").lower()
    if not can_manage_project_members(role):
        raise HTTPException(
            403,
            "Only project admins or PMs can remove the DR template.",
        )
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "dr_template_file_name": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    if not proj.get("dr_template_file_name"):
        return {"ok": True, "already_empty": True}
    await db.projects.update_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"$set": {
            "dr_template_file_url": None,
            "dr_template_file_name": None,
            "dr_template_uploaded_at": None,
            "dr_template_uploaded_by": None,
        }},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects",
        op="DR_TEMPLATE_REMOVED", record_id=project_id,
        before={"dr_template_file_name": proj.get("dr_template_file_name")},
        after=None,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True}


@router.get("/projects/{project_id}/dr-template")
async def get_dr_template_meta(
    project_id: str,
    ctx: dict = Depends(require_tenant),
):
    """Return template metadata only (no file bytes). Surfaced to any
    user with project access so the DR list page can enable / disable
    the "Export: Client Format" button."""
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "dr_template_file_name": 1, "dr_template_uploaded_at": 1,
         "dr_template_uploaded_by": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    name = proj.get("dr_template_file_name")
    if not name:
        return {"present": False}
    uploaded_by_id = proj.get("dr_template_uploaded_by")
    uploaded_by_name = ""
    if uploaded_by_id:
        u = await db.users.find_one(
            tenant_filter(ctx["tenant_id"], {"id": uploaded_by_id}),
            {"_id": 0, "full_name": 1},
        )
        uploaded_by_name = (u or {}).get("full_name", "") or ""
    return {
        "present": True,
        "file_name": name,
        "uploaded_at": proj.get("dr_template_uploaded_at"),
        "uploaded_by": uploaded_by_id,
        "uploaded_by_name": uploaded_by_name,
    }


# --- floors ---
@router.post("/floors", response_model=Floor)
async def create_floor(payload: FloorCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    # ensure project belongs to tenant
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "created_at": utc_now().isoformat()})
    await db.floors.insert_one(doc.copy())
    return Floor(**doc)


@router.get("/projects/{project_id}/floors", response_model=list[Floor])
async def list_floors(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.floors.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0}
    ).sort("floor_number", 1).to_list(200)
    return [Floor(**d) for d in docs]


# --- locations ---
@router.post("/locations", response_model=Location)
async def create_location(payload: LocationCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "created_at": utc_now().isoformat()})
    await db.locations.insert_one(doc.copy())
    return Location(**doc)


@router.get("/projects/{project_id}/locations", response_model=list[Location])
async def list_locations(project_id: str, floor_id: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": project_id})
    if floor_id:
        f["floor_id"] = floor_id
    docs = await db.locations.find(f, {"_id": 0}).sort("name", 1).to_list(2000)
    return [Location(**d) for d in docs]


@router.patch("/locations/{location_id}", response_model=Location)
async def update_location(location_id: str, payload: LocationCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = payload.model_dump(mode="json")
    before = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Location not found")
    await db.locations.update_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"$set": updates})
    after = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"_id": 0})
    return Location(**after)
