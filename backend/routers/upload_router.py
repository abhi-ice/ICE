"""Spec PDF upload + Drawing PDF upload routes."""
import io
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, BackgroundTasks
from typing import Optional
import pdfplumber
from database import get_db, tenant_filter
from models import gen_id, utc_now, ProjectSpec, Drawing, StrictModel
from auth import require_tenant
from services.spec_ingest import ingest_spec_pdf
from services.storage import put_object, make_path, record_file

router = APIRouter(tags=["uploads"])


@router.post("/specs/{spec_id}/upload")
async def upload_spec_pdf(
    spec_id: str,
    file: UploadFile = File(...),
    bg: BackgroundTasks = None,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    spec = await db.project_specs.find_one(tenant_filter(ctx["tenant_id"], {"id": spec_id}), {"_id": 0})
    if not spec:
        raise HTTPException(404, "Spec not found")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF required")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 250 * 1024 * 1024:
        raise HTTPException(413, "PDF > 250MB")

    await db.project_specs.update_one(
        tenant_filter(ctx["tenant_id"], {"id": spec_id}),
        {"$set": {"processing_status": "processing"}},
    )
    # run ingestion in background
    if bg is not None:
        bg.add_task(ingest_spec_pdf, tenant_id=ctx["tenant_id"], project_spec_id=spec_id,
                    pdf_bytes=pdf_bytes, actor_id=ctx["user"]["id"])
    else:
        await ingest_spec_pdf(tenant_id=ctx["tenant_id"], project_spec_id=spec_id,
                              pdf_bytes=pdf_bytes, actor_id=ctx["user"]["id"])
    return {"ok": True, "queued": True}


@router.get("/specs/{spec_id}/pdf")
async def stream_spec_pdf(spec_id: str, ctx: dict = Depends(require_tenant)):
    """Serve the original spec PDF for in-app viewing (pt.76). The PDF is
    stored at ingest time (services/spec_ingest.ingest_spec_pdf). Mirrors the
    drawing-revision PDF route — full-blob response; HTTP range requests are a
    later optimization for very large files / high concurrency."""
    from fastapi import Response
    from services.storage import get_object
    db = get_db()
    spec = await db.project_specs.find_one(
        tenant_filter(ctx["tenant_id"], {"id": spec_id}), {"_id": 0, "pdf_url": 1},
    )
    if not spec or not spec.get("pdf_url"):
        raise HTTPException(404, "Spec PDF not found")
    try:
        data, _ct = get_object(spec["pdf_url"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    return Response(content=data, media_type="application/pdf")


class DrawingRevisionMeta(StrictModel):
    drawing_id: str
    revision_label: str = "Rev 0"
    issue_description: Optional[str] = None


@router.post("/drawing-revisions/upload")
async def upload_drawing_revision(
    drawing_id: str = Form(...),
    revision_label: str = Form("Rev 0"),
    issue_description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    drawing = await db.drawings.find_one(tenant_filter(ctx["tenant_id"], {"id": drawing_id}), {"_id": 0})
    if not drawing:
        raise HTTPException(404, "Drawing not found")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF required")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 250 * 1024 * 1024:
        raise HTTPException(413, "PDF > 250MB")

    storage_path = make_path(ctx["tenant_id"], "drawings", "pdf")
    try:
        put_object(storage_path, pdf_bytes, "application/pdf")
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")

    # peek page count
    page_count = 0
    width_px = 612
    height_px = 792
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            if pdf.pages:
                p0 = pdf.pages[0]
                width_px = int(p0.width)
                height_px = int(p0.height)
    except Exception:
        pass

    await record_file(
        tenant_id=ctx["tenant_id"], storage_path=storage_path,
        original_filename=file.filename, content_type="application/pdf",
        size=len(pdf_bytes), kind="drawing_revision", actor_id=ctx["user"]["id"],
    )

    rev = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "drawing_id": drawing_id,
        "revision_label": revision_label,
        "issue_description": issue_description,
        "pdf_url": storage_path,
        "pdf_page_count": page_count,
        "pdf_width_px": width_px,
        "pdf_height_px": height_px,
        "uploaded_by": ctx["user"]["id"],
        "uploaded_at": utc_now().isoformat(),
        "processing_status": "complete",
        "created_at": utc_now().isoformat(),
    }
    await db.drawing_revisions.insert_one(rev.copy())
    await db.drawings.update_one(
        tenant_filter(ctx["tenant_id"], {"id": drawing_id}),
        {"$set": {"current_revision_id": rev["id"], "revision_count": (drawing.get("revision_count", 0) + 1)}},
    )
    return {"id": rev["id"], "page_count": page_count, "pdf_url": storage_path}


@router.get("/drawing-revisions/{rev_id}")
async def get_drawing_revision(rev_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    rev = await db.drawing_revisions.find_one(tenant_filter(ctx["tenant_id"], {"id": rev_id}), {"_id": 0})
    if not rev:
        raise HTTPException(404, "Revision not found")
    return rev


@router.get("/drawing-revisions/{rev_id}/pdf")
async def stream_drawing_pdf(rev_id: str, ctx: dict = Depends(require_tenant)):
    from fastapi import Response
    from services.storage import get_object
    db = get_db()
    rev = await db.drawing_revisions.find_one(tenant_filter(ctx["tenant_id"], {"id": rev_id}), {"_id": 0})
    if not rev or not rev.get("pdf_url"):
        raise HTTPException(404, "Revision not found")
    try:
        data, ct = get_object(rev["pdf_url"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    return Response(content=data, media_type="application/pdf")
