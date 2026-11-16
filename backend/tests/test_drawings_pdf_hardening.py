"""Railway move (2026-11-16): drawing / spec PDF hardening.

Covers:
  * uploads refuse files that are named .pdf but are not PDFs (400
    "Not a valid PDF"), and nothing is stored for them;
  * a real PDF (with or without a short prefix before "%PDF-") is accepted
    and its page count is read;
  * HTTP Range handling: normal ranges, open-ended ranges, the suffix form
    "bytes=-N" (last N bytes), and 416 for ranges outside the file;
  * drawing OCR: page past the end and unreadable PDFs give a clean 422,
    page 0 is refused, and raw tool output never reaches the client;
  * create endpoints that take a project_id 404 for a project in another
    company;
  * revision_count: new drawings start at 0, each upload adds one with $inc,
    and the list endpoint reports the real number of uploaded revisions even
    for older drawings whose stored count is one too high.

No network, no real Mongo: a small in-memory fake database stands in.
"""
import asyncio
import io
import sys
import types

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routers import docs_router, upload_router


TID = "tenant-a"
OTHER = "tenant-b"
CTX = {"tenant_id": TID, "user": {"id": "u1", "role": "admin"}}


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- fakes
def _match(doc, flt):
    for k, v in flt.items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *a, **kw):
        return self

    async def to_list(self, n):
        return [dict(d) for d in self.docs]


class _Coll:
    def __init__(self):
        self.docs = []
        self.updates = []

    async def find_one(self, flt, proj=None):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    def find(self, flt, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def count_documents(self, flt):
        return len([d for d in self.docs if _match(d, flt)])

    async def update_one(self, flt, upd):
        self.updates.append(upd)
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set", {}))
                for k, n in upd.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + n
                return

    def aggregate(self, pipeline):
        match = pipeline[0]["$match"]
        counts = {}
        for d in self.docs:
            if _match(d, match):
                counts[d["drawing_id"]] = counts.get(d["drawing_id"], 0) + 1
        return _Cursor([{"_id": k, "n": v} for k, v in counts.items()])


class _DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def db(monkeypatch):
    d = _DB()
    d.projects.docs.append({"id": "p1", "tenant_id": TID})
    d.projects.docs.append({"id": "p-other", "tenant_id": OTHER})
    monkeypatch.setattr(docs_router, "get_db", lambda: d)
    monkeypatch.setattr(upload_router, "get_db", lambda: d)
    return d


# ------------------------------------------------------- tiny real PDF
def _make_pdf(n_pages=2, prefix=b""):
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages)).encode()
    objs.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(n_pages).encode() + b" >>")
    for _ in range(n_pages):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1224 792] >>")
    out = io.BytesIO()
    out.write(prefix + b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell() - len(prefix))
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell() - len(prefix)
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


class _Upload:
    def __init__(self, data, filename="sheet.pdf"):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


@pytest.fixture
def storage(monkeypatch):
    stored = []

    async def _put(path, data, ct):
        stored.append(path)
        return {}

    async def _record(**kw):
        return {}

    monkeypatch.setattr(upload_router, "aput_object", _put)
    monkeypatch.setattr(upload_router, "record_file", _record)
    return stored


# --------------------------------------------------------------- uploads
def _upload(drawing_id, data, filename="sheet.pdf"):
    return _run(upload_router.upload_drawing_revision(
        drawing_id=drawing_id, revision_label="Rev 0", issue_description=None,
        file=_Upload(data, filename), ctx=CTX))


def _add_drawing(db, did="d1", revision_count=0):
    db.drawings.docs.append({"id": did, "tenant_id": TID, "project_id": "p1",
                             "sheet_number": "A-101", "title": "Plan",
                             "drawing_set_name": "IFC", "revision_count": revision_count,
                             "created_at": "2026-11-01T00:00:00+00:00"})


@pytest.mark.parametrize("data", [
    b"",                                   # empty file
    b"PK\x03\x04 this is a zip, renamed",  # some other format
    b"hello world" * 200,                  # text
    b" " * 2000 + b"%PDF-1.4\n",           # marker past the first 1024 bytes
    b"%PDF-1.4\nthis is not really a pdf", # marker but cannot be opened
])
def test_upload_refuses_files_that_are_not_pdfs(db, storage, data):
    _add_drawing(db)
    with pytest.raises(HTTPException) as ei:
        _upload("d1", data)
    assert ei.value.status_code == 400
    assert ei.value.detail == "Not a valid PDF"
    assert storage == []                   # nothing stored
    assert db.drawing_revisions.docs == []


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf", b"\r\n  \t", b"x" * 1000])
def test_upload_accepts_real_pdf_and_counts_pages(db, storage, prefix):
    _add_drawing(db)
    out = _upload("d1", _make_pdf(3, prefix=prefix))
    assert out["page_count"] == 3
    rev = db.drawing_revisions.docs[0]
    assert rev["pdf_page_count"] == 3
    assert rev["pdf_width_px"] == 1224 and rev["pdf_height_px"] == 792
    assert len(storage) == 1


def test_spec_upload_refuses_non_pdf(db, monkeypatch):
    db.project_specs.docs.append({"id": "s1", "tenant_id": TID, "project_id": "p1"})
    called = []

    async def _ingest(**kw):
        called.append(kw)
    monkeypatch.setattr(upload_router, "ingest_spec_pdf", _ingest)
    with pytest.raises(HTTPException) as ei:
        _run(upload_router.upload_spec_pdf("s1", file=_Upload(b"not a pdf"), bg=None, ctx=CTX))
    assert ei.value.status_code == 400 and ei.value.detail == "Not a valid PDF"
    assert called == []
    assert "processing_status" not in db.project_specs.docs[0]
    # A real PDF still goes through.
    out = _run(upload_router.upload_spec_pdf("s1", file=_Upload(_make_pdf(1)), bg=None, ctx=CTX))
    assert out["ok"] is True and len(called) == 1


# -------------------------------------------------------- revision_count
def test_new_drawing_starts_at_zero_and_upload_uses_inc(db, storage):
    out = _run(docs_router.create_drawing(
        docs_router.DrawingCreate(project_id="p1", sheet_number="A-101", title="Plan"), CTX))
    assert out.revision_count == 0
    _upload(out.id, _make_pdf(1))
    upd = db.drawings.updates[-1]
    assert upd["$inc"] == {"revision_count": 1}
    assert "revision_count" not in upd["$set"]
    assert db.drawings.docs[0]["revision_count"] == 1
    _upload(out.id, _make_pdf(1))
    assert db.drawings.docs[0]["revision_count"] == 2
    listed = _run(docs_router.list_drawings("p1", CTX))
    assert listed[0].revision_count == 2


def test_list_reports_real_count_for_old_rows_without_changing_them(db):
    # Old drawing: created at 1, then two uploads -> stored 3, really 2.
    _add_drawing(db, "old", revision_count=3)
    # Old drawing never uploaded: stored 1, really 0.
    _add_drawing(db, "empty", revision_count=1)
    for _ in range(2):
        db.drawing_revisions.docs.append({"id": "r", "tenant_id": TID, "drawing_id": "old"})
    # Another company's revision with a clashing drawing id is not counted.
    db.drawing_revisions.docs.append({"id": "x", "tenant_id": OTHER, "drawing_id": "old"})
    listed = {d.id: d.revision_count for d in _run(docs_router.list_drawings("p1", CTX))}
    assert listed == {"old": 2, "empty": 0}
    # Stored rows are untouched (no migration).
    assert {d["id"]: d["revision_count"] for d in db.drawings.docs} == {"old": 3, "empty": 1}
    assert db.drawings.updates == []


def test_list_with_no_drawings(db):
    assert _run(docs_router.list_drawings("p1", CTX)) == []


# --------------------------------------------- project belongs to tenant
def _create_calls(project_id):
    return [
        docs_router.create_drawing(
            docs_router.DrawingCreate(project_id=project_id, sheet_number="A-1", title="T"), CTX),
        docs_router.create_spec(docs_router.SpecCreate(project_id=project_id, name="Specs"), CTX),
        docs_router.create_co(docs_router.ChangeOrderCreate(
            project_id=project_id, title="CO", description="d"), CTX),
    ]


@pytest.mark.parametrize("project_id", ["p-other", "does-not-exist"])
def test_create_endpoints_reject_foreign_project(db, project_id):
    for coro in _create_calls(project_id):
        with pytest.raises(HTTPException) as ei:
            _run(coro)
        assert ei.value.status_code == 404 and ei.value.detail == "Project not found"
    assert db.drawings.docs == [] and db.project_specs.docs == [] and db.change_orders.docs == []


def test_create_rfi_and_work_item_reject_foreign_project(db):
    # Checked before anything else runs, so a bare model is enough.
    rfi = docs_router.RfiCreate.model_construct(project_id="p-other")
    wi = docs_router.WorkItemCreate.model_construct(project_id="p-other")
    for coro in (docs_router.create_rfi(rfi, CTX), docs_router.create_work_item(wi, CTX)):
        with pytest.raises(HTTPException) as ei:
            _run(coro)
        assert ei.value.status_code == 404
    assert db.rfis.docs == [] and db.work_items.docs == []


def test_create_endpoints_allow_own_project(db):
    for coro in _create_calls("p1"):
        _run(coro)
    assert len(db.drawings.docs) == 1 and len(db.project_specs.docs) == 1
    assert len(db.change_orders.docs) == 1


# ---------------------------------------------------------------- ranges
DATA = bytes(range(256)) * 4   # 1024 bytes


def _rr(header, data=DATA):
    return upload_router._pdf_range_response(data, "application/pdf", header)


def test_range_no_header_is_full_file():
    r = _rr(None)
    assert r.status_code == 200 and r.body == DATA
    assert r.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize("header,start,end", [
    ("bytes=0-99", 0, 99),
    ("bytes=100-", 100, 1023),
    ("bytes=1000-5000", 1000, 1023),     # end past the file is clamped
    ("bytes=-100", 924, 1023),           # suffix: LAST 100 bytes
    ("bytes=-5000", 0, 1023),            # suffix longer than the file
    ("bytes=1023-1023", 1023, 1023),
    ("BYTES= 10-19 ", 10, 19),
    ("bytes=5-9, 20-30", 5, 9),          # only the first range is served
])
def test_range_satisfiable(header, start, end):
    r = _rr(header)
    assert r.status_code == 206
    assert r.body == DATA[start:end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/1024"
    assert r.headers["content-length"] == str(end - start + 1)


@pytest.mark.parametrize("header", ["bytes=1024-", "bytes=5000-6000", "bytes=-0"])
def test_range_unsatisfiable_is_416(header):
    r = _rr(header)
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */1024"
    assert r.body == b""


def test_range_on_empty_file_is_416():
    r = _rr("bytes=-10", data=b"")
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */0"


@pytest.mark.parametrize("header", ["bytes=abc", "bytes=9-3", "bytes=-", "items=0-5", "bytes=1-x"])
def test_range_unreadable_is_ignored(header):
    r = _rr(header)
    assert r.status_code == 200 and r.body == DATA


# ------------------------------------------------------------------- OCR
class _Img:
    size = (1000, 800)

    def crop(self, box):
        return self


@pytest.fixture
def ocr(monkeypatch):
    """Fake pdf2image / pytesseract / storage for the OCR endpoint."""
    from routers import iter7_router
    d = _DB()
    d.drawing_revisions.docs.append({"id": "r1", "tenant_id": TID, "pdf_url": "x/r1.pdf"})
    monkeypatch.setattr(iter7_router, "get_db", lambda: d)
    state = {"pages": 3, "info_error": None, "render_error": None, "tess_error": None}

    def pdfinfo_from_bytes(b):
        if state["info_error"]:
            raise Exception(state["info_error"])
        return {"Pages": state["pages"]}

    def convert_from_bytes(b, first_page, last_page, dpi):
        if state["render_error"]:
            raise Exception(state["render_error"])
        return [_Img()] if first_page <= state["pages"] else []

    def image_to_string(img):
        if state["tess_error"]:
            raise Exception(state["tess_error"])
        return "SHEET A-101\nFIRST FLOOR PLAN\nSCALE 1/8\" = 1'-0\"\n10/01/2026"

    monkeypatch.setitem(sys.modules, "pdf2image", types.SimpleNamespace(
        pdfinfo_from_bytes=pdfinfo_from_bytes, convert_from_bytes=convert_from_bytes))
    monkeypatch.setitem(sys.modules, "pytesseract", types.SimpleNamespace(
        image_to_string=image_to_string))
    import services.storage as st
    monkeypatch.setattr(st, "get_object", lambda p: (b"%PDF-1.4", "application/pdf"))
    return iter7_router, state


def _ocr(mod, page=1, rev="r1"):
    return _run(mod.drawing_ocr(mod.DrawingOcrRequest(drawing_revision_id=rev, page=page), CTX))


def test_ocr_happy_path(ocr):
    mod, _ = ocr
    out = _ocr(mod, page=3)
    assert out["sheet_number"] == "A-101"


def test_ocr_page_past_end_is_clean_422(ocr):
    mod, _ = ocr
    with pytest.raises(HTTPException) as ei:
        _ocr(mod, page=4)
    assert ei.value.status_code == 422
    assert ei.value.detail == "Page 4 is out of range (1..3)"


@pytest.mark.parametrize("page", [0, -2])
def test_ocr_page_below_one_is_refused(ocr, page):
    mod, _ = ocr
    with pytest.raises(ValidationError):
        mod.DrawingOcrRequest(drawing_revision_id="r1", page=page)


@pytest.mark.parametrize("where", ["info_error", "render_error"])
def test_ocr_corrupt_pdf_hides_raw_error(ocr, where):
    mod, state = ocr
    state[where] = "Syntax Error: Couldn't find trailer dictionary /tmp/secret"
    with pytest.raises(HTTPException) as ei:
        _ocr(mod)
    assert ei.value.status_code == 422
    assert ei.value.detail == "Could not read this PDF"


def test_ocr_tesseract_error_hides_raw_error(ocr):
    mod, state = ocr
    state["tess_error"] = "tesseract is not installed at /usr/bin/tesseract"
    with pytest.raises(HTTPException) as ei:
        _ocr(mod)
    assert ei.value.status_code == 500
    assert "tesseract" not in ei.value.detail


def test_ocr_missing_revision_is_404(ocr):
    mod, _ = ocr
    with pytest.raises(HTTPException) as ei:
        _ocr(mod, rev="nope")
    assert ei.value.status_code == 404


def test_range_with_non_ascii_digit_is_ignored_not_500():
    # "\xb2" (superscript two) passes str.isdigit() but int() rejects it.
    r = _rr("bytes=\xb2-5")
    assert r.status_code == 200
