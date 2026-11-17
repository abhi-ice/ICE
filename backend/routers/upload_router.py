"""Spec PDF upload + Drawing PDF upload routes.

Per-project access (2026-11-16): the spec and drawing revision ids in the
URL are checked by the route-level dependency
(services/project_access.enforce_project_scope). The drawing revision
upload names its drawing in a form field, which the dependency cannot see,
so it is checked here (a write: a refusal is a 403)."""
import asyncio
import io
import logging
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, BackgroundTasks, Request, Response
from typing import Optional
import pdfplumber
from database import get_db, tenant_filter
from models import gen_id, utc_now, ProjectSpec, Drawing, StrictModel
from auth import require_tenant
from services.project_access import check_project_access
from services.spec_ingest import ingest_spec_pdf
from services.storage import put_object, aput_object, make_path, record_file

router = APIRouter(tags=["uploads"])
_log = logging.getLogger("ice_schedule_ops.uploads")


def _parse_range(range_header: Optional[str], total: int):
    """Read a `Range: bytes=...` header (RFC 7233) for a file of `total` bytes.

    Returns None when there is no usable header (no header, another unit, or
    text we cannot read) -- the caller then sends the whole file with a 200.
    Returns "unsatisfiable" when the range is well formed but lies outside the
    file -- the caller then sends a 416.
    Otherwise returns (start, end), both inclusive.

    Only the first range is served when several are asked for (PDF.js asks for
    one at a time)."""
    if not range_header:
        return None
    header = range_header.strip()
    if not header.lower().startswith("bytes="):
        return None
    rng = header.split("=", 1)[1].split(",")[0].strip()
    start_s, dash, end_s = rng.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    if not dash or not (start_s or end_s):
        return None
    # Plain ASCII digits only: str.isdigit() also accepts characters such as
    # "²" that int() cannot read, which would turn a bad header into a 500.
    if not all(c in "0123456789" for c in start_s + end_s):
        return None
    if not start_s:
        # Suffix form "bytes=-N": the LAST N bytes of the file.
        n = int(end_s)
        if n == 0 or total == 0:
            return "unsatisfiable"
        return (max(0, total - n), total - 1)
    start = int(start_s)
    if end_s:
        end = int(end_s)
        if end < start:
            return None  # not a valid range -> ignore it, per RFC 7233
    else:
        end = total - 1
    if start >= total:
        return "unsatisfiable"
    return (start, min(end, total - 1))


def _pdf_range_response(data: bytes, content_type: str, range_header: Optional[str]) -> Response:
    """Serve a PDF with HTTP range support (pt.77). PDF.js streams large specs
    by requesting byte ranges; we honor `Range: bytes=start-end` (and the
    suffix form `bytes=-N`, meaning the last N bytes) with a 206 so the
    browser fetches pages on demand instead of the whole file. A range that
    lies outside the file gets a 416. A request WITHOUT a Range header (or
    with one we cannot read) gets the full 200 (so the blob path still
    works) plus `Accept-Ranges: bytes` to advertise range support."""
    total = len(data)
    rng = _parse_range(range_header, total)
    if rng == "unsatisfiable":
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )
    if rng is not None:
        start, end = rng
        chunk = data[start:end + 1]
        return Response(
            content=chunk, status_code=206, media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )
    return Response(
        content=data, media_type=content_type,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
    )


def _looks_like_pdf(data: bytes) -> bool:
    """True when the file starts with the PDF marker "%PDF-". The PDF
    specification lets readers find the marker anywhere in the first 1024
    bytes, so a leading byte-order mark, whitespace or other short prefix is
    allowed."""
    return b"%PDF-" in data[:1024 + 5]


def _read_pdf_info(data: bytes) -> dict:
    """Open the PDF and return its page count and first-page size in points.
    Raises HTTPException(400, "Not a valid PDF") when the file is not a PDF
    or cannot be opened, so a bad upload is refused instead of being stored
    with a page count of 0."""
    if not _looks_like_pdf(data):
        raise HTTPException(400, "Not a valid PDF")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages
            if not pages:
                raise ValueError("PDF has no pages")
            p0 = pages[0]
            return {"page_count": len(pages), "width_px": int(p0.width), "height_px": int(p0.height)}
    except HTTPException:
        raise
    except Exception as e:
        _log.warning("PDF upload refused, could not open it: %s", e)
        raise HTTPException(400, "Not a valid PDF")


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
    # Refuse files that are named .pdf but are not PDFs (checked off the
    # event loop: opening a large spec takes a moment).
    await asyncio.to_thread(_read_pdf_info, pdf_bytes)

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
async def stream_spec_pdf(spec_id: str, request: Request, ctx: dict = Depends(require_tenant)):
    """Serve the original spec PDF for in-app viewing (pt.76; pt.77 adds HTTP
    range support + an LRU blob cache so PDF.js streams 900-page specs page-by-
    page instead of downloading the whole file). The PDF is stored at ingest
    time (services/spec_ingest.ingest_spec_pdf)."""
    from services.storage import aget_object_cached
    db = get_db()
    spec = await db.project_specs.find_one(
        tenant_filter(ctx["tenant_id"], {"id": spec_id}), {"_id": 0, "pdf_url": 1},
    )
    if not spec or not spec.get("pdf_url"):
        raise HTTPException(404, "Spec PDF not found")
    try:
        data, _ct = await aget_object_cached(spec["pdf_url"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    return _pdf_range_response(data, "application/pdf", request.headers.get("range"))


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
    # The drawing id arrives as a form field, so check its project here.
    await check_project_access(ctx, drawing.get("project_id"), write=True,
                               route="/api/drawing-revisions/upload")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF required")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 250 * 1024 * 1024:
        raise HTTPException(413, "PDF > 250MB")
    # Check the file really is a PDF before storing anything, and read its
    # page count and first-page size.
    info = await asyncio.to_thread(_read_pdf_info, pdf_bytes)
    page_count = info["page_count"]
    width_px = info["width_px"]
    height_px = info["height_px"]

    storage_path = make_path(ctx["tenant_id"], "drawings", "pdf")
    try:
        await aput_object(storage_path, pdf_bytes, "application/pdf")
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")

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
        # $inc so two uploads at the same moment both count.
        {"$set": {"current_revision_id": rev["id"]}, "$inc": {"revision_count": 1}},
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
async def stream_drawing_pdf(rev_id: str, request: Request, ctx: dict = Depends(require_tenant)):
    from services.storage import aget_object_cached
    db = get_db()
    rev = await db.drawing_revisions.find_one(tenant_filter(ctx["tenant_id"], {"id": rev_id}), {"_id": 0})
    if not rev or not rev.get("pdf_url"):
        raise HTTPException(404, "Revision not found")
    try:
        data, _ct = await aget_object_cached(rev["pdf_url"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    return _pdf_range_response(data, "application/pdf", request.headers.get("range"))
