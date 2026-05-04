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
from database import get_db, tenant_filter
from models import gen_id, utc_now
from services.storage import put_object, make_path

DIVISION_RE = re.compile(r"^\s*DIVISION\s+(\d{1,2})\s*[-–—:]?\s*(.+?)\s*$", re.IGNORECASE)
SECTION_RE = re.compile(r"^\s*SECTION\s+(\d{2}\s+\d{2}\s+\d{2}(?:\.\d+)?)\s*[-–—:]?\s*(.+?)\s*$", re.IGNORECASE)
PART_RE = re.compile(r"^\s*PART\s+(\d+)\s*[-–—:]?\s*(.+?)?\s*$", re.IGNORECASE)


async def ingest_spec_pdf(
    *,
    tenant_id: str,
    project_spec_id: str,
    pdf_bytes: bytes,
    actor_id: Optional[str] = None,
) -> dict:
    db = get_db()
    # 1. store PDF
    storage_path = make_path(tenant_id, "specs", "pdf")
    try:
        put_object(storage_path, pdf_bytes, "application/pdf")
    except Exception as e:
        await db.project_specs.update_one(
            tenant_filter(tenant_id, {"id": project_spec_id}),
            {"$set": {"processing_status": "failed", "processing_error": f"storage: {e}"}},
        )
        return {"ok": False, "error": f"storage: {e}"}

    # 2. parse pages
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            sections: list[dict] = []
            current_division: dict | None = None
            current_section: dict | None = None
            current_text_buf: list[str] = []

            def _flush_section():
                if current_section is not None:
                    current_section["text_content"] = "\n".join(current_text_buf).strip()
                    current_section["text_length_chars"] = len(current_section["text_content"])
                    current_section["page_end"] = current_page_idx
                    sections.append(current_section)

            current_page_idx = 0
            for page in pdf.pages:
                current_page_idx = page.page_number
                text = page.extract_text() or ""
                for line in text.splitlines():
                    div_m = DIVISION_RE.match(line)
                    sec_m = SECTION_RE.match(line)
                    if div_m:
                        # close prior section
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
    except Exception as e:
        await db.project_specs.update_one(
            tenant_filter(tenant_id, {"id": project_spec_id}),
            {"$set": {"processing_status": "failed", "processing_error": f"parse: {e}"}},
        )
        return {"ok": False, "error": f"parse: {e}"}

    # 3. write sections
    if sections:
        await db.spec_sections.insert_many([s.copy() for s in sections])

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
    # 4. fit TF-IDF embeddings (best-effort)
    try:
        from services.spec_search import fit_spec_index
        await fit_spec_index(tenant_id, project_spec_id)
    except Exception as e:
        # Non-fatal — sections still saved
        pass
    return {"ok": True, "page_count": page_count, "section_count": len(sections), "pdf_url": storage_path}
