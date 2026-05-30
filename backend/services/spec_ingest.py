"""Spec PDF ingestion. Master prompt §14.

pdfplumber → MasterFormat regex (DIVISION NN, SECTION NN NN NN, PART N) →
hierarchical sections persisted to spec_sections. Embedding step deferred
(would need Voyage SDK or fallback TF-IDF).
"""
import re
import io
from datetime import datetime, timezone
from typing import Optional
import pdfplumber
from starlette.concurrency import run_in_threadpool
from database import get_db, tenant_filter
from models import gen_id, utc_now
from services.storage import put_object, make_path

DIVISION_RE = re.compile(r"^\s*DIVISION\s+(\d{1,2})\s*[-–—:]?\s*(.+?)\s*$", re.IGNORECASE)
SECTION_RE = re.compile(r"^\s*SECTION\s+(\d{2}\s+\d{2}\s+\d{2}(?:\.\d+)?)\s*[-–—:]?\s*(.+?)\s*$", re.IGNORECASE)
PART_RE = re.compile(r"^\s*PART\s+(\d+)\s*[-–—:]?\s*(.+?)?\s*$", re.IGNORECASE)


def _parse_spec_pdf_sync(pdf_bytes: bytes, tenant_id: str, project_spec_id: str):
    """pt.79: the CPU-bound pdfplumber parse, run in a WORKER THREAD (via
    run_in_threadpool in ingest_spec_pdf) so a large (e.g. 700+ page) spec can
    no longer freeze the async event loop / the whole app while it ingests.
    Pure CPU + no DB/await — returns (page_count, sections); raises on error,
    which the async caller maps to processing_status 'failed'."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        sections: list[dict] = []
        current_division: dict | None = None
        current_section: dict | None = None
        current_text_buf: list[str] = []
        current_page_idx = 0

        def _flush_section():
            if current_section is not None:
                current_section["text_content"] = "\n".join(current_text_buf).strip()
                current_section["text_length_chars"] = len(current_section["text_content"])
                current_section["page_end"] = current_page_idx
                sections.append(current_section)

        for page in pdf.pages:
            current_page_idx = page.page_number
            text = page.extract_text() or ""
            for line in text.splitlines():
                div_m = DIVISION_RE.match(line)
                sec_m = SECTION_RE.match(line)
                if div_m:
                    _flush_section()
                    current_section = None
                    current_text_buf = []
                    current_division = {
                        "code": div_m.group(1).zfill(2),
                        "title": div_m.group(2).strip(),
                    }
                    continue
                if sec_m:
                    _flush_section()
                    current_text_buf = []
                    current_section = {
                        "id": gen_id(),
                        "tenant_id": tenant_id,
                        "project_spec_id": project_spec_id,
                        "division": current_division["code"] if current_division else sec_m.group(1).split()[0],
                        "section_code": re.sub(r"\s+", " ", sec_m.group(1).strip()),
                        "section_title": sec_m.group(2).strip(),
                        "parent_section_id": None,
                        "hierarchy_path": (current_division["code"] if current_division else "00") + "/" + sec_m.group(1).strip(),
                        "heading_level": 2,
                        "page_start": page.page_number,
                        "page_end": page.page_number,
                        "text_content": "",
                        "text_length_chars": 0,
                        "masterformat_year": "2020",
                        "created_at": utc_now().isoformat(),
                    }
                    continue
                if current_section is not None:
                    current_text_buf.append(line)
        _flush_section()
    return page_count, sections


async def ingest_spec_pdf(
    *,
    tenant_id: str,
    project_spec_id: str,
    pdf_bytes: bytes,
    actor_id: Optional[str] = None,
) -> dict:
    db = get_db()
    # 1. store PDF  (pt.79: sync requests.put -> offload so it can't block the loop)
    storage_path = make_path(tenant_id, "specs", "pdf")
    try:
        await run_in_threadpool(put_object, storage_path, pdf_bytes, "application/pdf")
    except Exception as e:
        await db.project_specs.update_one(
            tenant_filter(tenant_id, {"id": project_spec_id}),
            {"$set": {"processing_status": "failed", "processing_error": f"storage: {e}"}},
        )
        return {"ok": False, "error": f"storage: {e}"}

    # 2. parse pages  (pt.79: offloaded to a worker thread — see
    # _parse_spec_pdf_sync — so a large spec's CPU-bound parse never freezes
    # the event loop / the rest of the app)
    try:
        page_count, sections = await run_in_threadpool(
            _parse_spec_pdf_sync, pdf_bytes, tenant_id, project_spec_id,
        )
    except Exception as e:
        await db.project_specs.update_one(
            tenant_filter(tenant_id, {"id": project_spec_id}),
            {"$set": {"processing_status": "failed", "processing_error": f"parse: {e}"}},
        )
        return {"ok": False, "error": f"parse: {e}"}

    # 3. write sections
    if sections:
        await db.spec_sections.insert_many([s.copy() for s in sections])

    # 4. fit TF-IDF embeddings (best-effort)
    try:
        from services.spec_search import fit_spec_index
        await fit_spec_index(tenant_id, project_spec_id)
    except Exception as e:
        # Non-fatal — sections still saved
        pass

    # 5. Auto-generate submittals from any "1.4 SUBMITTALS" paragraph
    # (Run BEFORE flipping processing_status='complete' so consumers polling
    # on complete are guaranteed to see auto-generated submittals.)
    try:
        await _autogenerate_submittals(tenant_id, project_spec_id, sections)
    except Exception:
        pass

    await db.project_specs.update_one(
        tenant_filter(tenant_id, {"id": project_spec_id}),
        {"$set": {
            "pdf_url": storage_path,
            "pdf_page_count": page_count,
            "total_sections": len(sections),
            "processing_status": "complete",
            "processing_error": None,
        }},
    )

    return {"ok": True, "page_count": page_count, "section_count": len(sections), "pdf_url": storage_path}


async def _autogenerate_submittals(tenant_id: str, project_spec_id: str, sections: list[dict]):
    """Detect '1.4 SUBMITTALS' (or 'PART 1' '1.4' headings), parse bulleted/
    numbered items underneath, and create one Submittal row per item.
    """
    from database import get_db, tenant_filter
    from models import gen_id, utc_now
    db = get_db()
    # Find spec → project
    spec = await db.project_specs.find_one(
        tenant_filter(tenant_id, {"id": project_spec_id}), {"_id": 0},
    )
    if not spec:
        return
    project_id = spec["project_id"]

    SUBMITTALS_RE = re.compile(r"^\s*1\.\s*4\s+SUBMITTALS\b", re.IGNORECASE | re.MULTILINE)
    BULLET_RE = re.compile(r"^\s*(?:[A-Z]\.|[a-z]\.|\d+\.|[-•*])\s+(.+?)$", re.MULTILINE)

    for sec in sections:
        body = sec.get("text_content") or ""
        m = SUBMITTALS_RE.search(body)
        if not m:
            continue
        # Take text from match position to the end of section (or next "PART 2")
        chunk = body[m.end():]
        next_part = re.search(r"^\s*PART\s+2\b", chunk, re.IGNORECASE | re.MULTILINE)
        if next_part:
            chunk = chunk[:next_part.start()]
        # Extract bullet items
        items = []
        for bm in BULLET_RE.finditer(chunk):
            item = bm.group(1).strip()
            if 5 < len(item) < 240 and item.upper() != "SUBMITTALS":
                items.append(item)
        if not items:
            # fallback: split on lines that look like product/data nouns
            for line in chunk.splitlines():
                line = line.strip()
                if 8 < len(line) < 180 and re.match(r"^[A-Za-z]", line):
                    items.append(line)
                if len(items) >= 12:
                    break

        # Idempotent check: skip if we already auto-generated for this section
        existing = await db.submittals.count_documents(tenant_filter(tenant_id, {
            "project_id": project_id,
            "spec_section_id": sec["id"],
            "auto_generated": True,
        }))
        if existing:
            continue

        n_existing = await db.submittals.count_documents(tenant_filter(tenant_id, {"project_id": project_id}))
        for i, item in enumerate(items[:20]):
            n_existing += 1
            await db.submittals.insert_one({
                "id": gen_id(),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "submittal_number": f"SUB-{n_existing:04d}",
                "spec_section_id": sec["id"],
                "spec_section_code": sec["section_code"],
                "title": item,
                "type": "product_data",
                "status": "not_yet_submitted",
                "sub_responsible": None,
                "schedule_activity_id": None,
                "drawings_affected": [],
                "submitted_at": None,
                "approved_at": None,
                "auto_generated": True,
                "created_by": None,
                "created_at": utc_now().isoformat(),
            })
