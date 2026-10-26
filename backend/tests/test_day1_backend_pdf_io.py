"""Day-1 audit (2026-10-25) -- PDF safety + storage off the event loop.

Covers, without a live MongoDB or server:

  * #3   safe_url_fetcher refuses file:, http:, https:, ftp:, Windows
         drive paths and scheme-less URLs; serves data: URLs.
  * #3   render_pdf of HTML carrying <a rel=attachment href=file://...>,
         <link rel=attachment> and <img src=file://...> does NOT embed the
         file. A control render with WeasyPrint's default fetcher DOES embed
         it, which proves the assertion can actually catch a leak.
  * #3   the daily-report PDF builder escapes the open-delay reminder
         description (and status / weather values) -- the handler is
         called directly against an in-memory fake of the collections and
         the HTML handed to the renderer is inspected.
  * #39/42/45  aput_object / aget_object / aget_object_cached call the
         unchanged sync functions in a worker thread (not on the loop), and
         arender_pdf renders in a worker thread too.

Tests that need a working WeasyPrint (it needs the Pango/GTK native
libraries) are skipped where it cannot load, e.g. plain Windows.

Run:  cd backend && python -m pytest tests/test_day1_backend_pdf_io.py -q
"""
import asyncio
import threading
import uuid

import pytest

from services import pdf_safety
from services.pdf_safety import safe_url_fetcher, render_pdf


def _weasyprint_or_skip():
    try:
        import weasyprint  # noqa: F401
        return weasyprint
    except Exception as exc:  # OSError when Pango/GTK is missing
        pytest.skip(f"WeasyPrint cannot load here: {type(exc).__name__}")


# ------------------------------------------------------------------
# safe_url_fetcher
# ------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "file:///proc/self/environ",
    "file:///C:/Windows/win.ini",
    "FILE:///etc/passwd",
    "http://169.254.169.254/",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "https://example.com/x.png",
    "ftp://example.com/x",
    "C:/Windows/win.ini",
    "/etc/passwd",
    "relative/path.png",
    "",
])
def test_safe_url_fetcher_refuses_non_data(url):
    with pytest.raises(ValueError):
        safe_url_fetcher(url)


def test_safe_url_fetcher_accepts_data_png():
    _weasyprint_or_skip()
    # 1x1 transparent PNG
    png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
           "lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
    resp = safe_url_fetcher(png)
    try:
        body = resp.read() if hasattr(resp, "read") else (resp.get("string") or b"")
    finally:
        if hasattr(resp, "close"):
            resp.close()
    assert body.startswith(b"\x89PNG")


# ------------------------------------------------------------------
# render_pdf never embeds server files
# ------------------------------------------------------------------
def _leaky_html(file_uri: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<link rel='attachment' href='{file_uri}'>"
        "</head><body><p>report</p>"
        f"<a rel=attachment href=\"{file_uri}\">x</a>"
        f"<img src=\"{file_uri}\">"
        f"<div style=\"background-image:url('{file_uri}')\">y</div>"
        "</body></html>"
    )


def test_render_pdf_does_not_embed_local_files(tmp_path):
    wp = _weasyprint_or_skip()
    sentinel = f"ICE-SENTINEL-{uuid.uuid4().hex}".encode()
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"JWT_SECRET=" + sentinel + b"\n")
    html = _leaky_html(secret.as_uri())

    # Control: WeasyPrint's default fetcher really would embed it, so the
    # absence check below is meaningful (uncompressed so bytes are visible).
    unsafe = wp.HTML(string=html).write_pdf(uncompressed_pdf=True)
    assert sentinel in unsafe

    safe = render_pdf(html, uncompressed_pdf=True)
    assert safe.startswith(b"%PDF")
    assert sentinel not in safe
    assert b"/EmbeddedFile" not in safe
    # And with normal compression too.
    assert sentinel not in render_pdf(html)


def test_arender_pdf_does_not_embed_local_files(tmp_path):
    _weasyprint_or_skip()
    sentinel = f"ICE-SENTINEL-{uuid.uuid4().hex}".encode()
    secret = tmp_path / "environ"
    secret.write_bytes(sentinel)
    pdf = asyncio.run(pdf_safety.arender_pdf(_leaky_html(secret.as_uri()),
                                             uncompressed_pdf=True))
    assert pdf.startswith(b"%PDF")
    assert sentinel not in pdf


def test_render_pdf_keeps_inline_data_images():
    _weasyprint_or_skip()
    png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
           "lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
    pdf = render_pdf(f"<html><body><img src='{png}' style='width:50px'></body></html>",
                     uncompressed_pdf=True)
    assert b"/Subtype /Image" in pdf


def test_arender_pdf_runs_off_the_event_loop(monkeypatch):
    seen = {}

    def fake_render(html_string, **kw):
        seen["thread"] = threading.get_ident()
        seen["args"] = (html_string, kw)
        return b"%PDF-fake"

    monkeypatch.setattr(pdf_safety, "render_pdf", fake_render)

    async def main():
        seen["loop_thread"] = threading.get_ident()
        return await pdf_safety.arender_pdf("<p>x</p>", zoom=1)

    assert asyncio.run(main()) == b"%PDF-fake"
    assert seen["args"] == ("<p>x</p>", {"zoom": 1})
    assert seen["thread"] != seen["loop_thread"]


def test_arender_pdf_uses_its_own_thread_not_the_shared_pool(monkeypatch):
    """Queued PDF exports must not tie up the shared default pool that
    photo / voice storage calls use."""
    names = []

    def fake_render(html_string, **kw):
        names.append(threading.current_thread().name)
        return b"%PDF-fake"

    monkeypatch.setattr(pdf_safety, "render_pdf", fake_render)

    async def main():
        await asyncio.gather(*(pdf_safety.arender_pdf("<p>x</p>") for _ in range(3)))

    asyncio.run(main())
    assert len(names) == 3
    assert all(n.startswith("pdf-render") for n in names)


# ------------------------------------------------------------------
# storage async wrappers
# ------------------------------------------------------------------
def test_aput_aget_call_sync_functions_in_worker_thread(monkeypatch):
    from services import storage
    calls = []

    def fake_put(path, data, content_type):
        calls.append(("put", path, data, content_type, threading.get_ident()))
        return {"ok": True, "path": path}

    def fake_get(path):
        calls.append(("get", path, threading.get_ident()))
        return b"bytes-for-" + path.encode(), "image/jpeg"

    monkeypatch.setattr(storage, "put_object", fake_put)
    monkeypatch.setattr(storage, "get_object", fake_get)

    async def main():
        loop_thread = threading.get_ident()
        put_res = await storage.aput_object("a/b.jpg", b"\x01\x02", "image/jpeg")
        get_res = await storage.aget_object("a/b.jpg")
        return loop_thread, put_res, get_res

    loop_thread, put_res, get_res = asyncio.run(main())
    assert put_res == {"ok": True, "path": "a/b.jpg"}
    assert get_res == (b"bytes-for-a/b.jpg", "image/jpeg")
    assert calls[0][:4] == ("put", "a/b.jpg", b"\x01\x02", "image/jpeg")
    assert calls[1][:2] == ("get", "a/b.jpg")
    assert calls[0][-1] != loop_thread and calls[1][-1] != loop_thread


def test_aput_object_propagates_storage_errors(monkeypatch):
    """A failed upload must still raise so the caller reports it --
    never a silent success."""
    from services import storage

    def boom(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "put_object", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(storage.aput_object("p", b"x", "image/jpeg"))


def test_aget_object_cached_fetches_once(monkeypatch):
    from services import storage
    n = {"calls": 0}

    def fake_get(path):
        n["calls"] += 1
        return b"%PDF-1.7 spec", "application/pdf"

    monkeypatch.setattr(storage, "get_object", fake_get)
    path = f"spec/{uuid.uuid4().hex}.pdf"

    async def main():
        a = await storage.aget_object_cached(path)
        b = await storage.aget_object_cached(path)
        return a, b

    try:
        a, b = asyncio.run(main())
        assert a == b == (b"%PDF-1.7 spec", "application/pdf")
        assert n["calls"] == 1
    finally:
        hit = storage._BLOB_CACHE.pop(path, None)
        if hit is not None:
            storage._blob_cache_bytes -= len(hit[0])


# ------------------------------------------------------------------
# Daily-report PDF HTML builder escapes user text
# ------------------------------------------------------------------
def _match(doc, flt):
    for k, cond in (flt or {}).items():
        val = doc.get(k)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$in":
                    vals = val if isinstance(val, list) else [val]
                    if not any(v in arg for v in vals):
                        return False
                elif op == "$nin" and val in arg:
                    return False
                elif op == "$ne" and val == arg:
                    return False
        elif val != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, flt=None, proj=None):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt)])


class _FakeDb:
    def __init__(self, **colls):
        self._colls = colls

    def __getattr__(self, name):
        return self._colls.setdefault(name, _Coll())


def test_daily_report_pdf_escapes_reminder_and_header_fields(monkeypatch):
    from routers import iter6_router

    evil_desc = "<b>x</b> <a rel=attachment href=file:///proc/self/environ>y</a>"
    db = _FakeDb(
        daily_reports=_Coll([{
            "id": "dr1", "tenant_id": "t1", "project_id": "p1",
            "report_date": "2026-10-26", "status": "<i>draft</i>",
            "weather_conditions": "<u>rain</u>",
            "weather_temp_high_f": "<img src=file:///etc/passwd>",
            "weather_temp_low_f": 40, "weather_precipitation_in": 0.2,
            "weather_wind_mph": "<s>9</s>",
            "notes": "waiting on <ConEd crew>",
        }]),
        projects=_Coll([{"id": "p1", "tenant_id": "t1", "name": "Job & Co"}]),
        daily_report_activities=_Coll([{
            "daily_report_id": "dr1", "schedule_activity_id": "task1",
            "activity_code": "A100", "activity_description": "Pour",
            "pct_complete_start_of_day": 10, "pct_complete_end_of_day": "<b>20</b>",
            "status_at_end_of_day": "in_progress", "position": 1,
        }]),
        daily_presence=_Coll([{
            "tenant_id": "t1", "daily_report_id": "dr1", "trade": "Concrete",
            "company_name": "<b>Crew Co</b>", "headcount": 3,
            "hours_per_person": 8, "notes": "<script>n</script>",
        }]),
        delays=_Coll([{
            "id": "d1", "tenant_id": "t1", "project_id": "p1",
            "status": "open", "severity": "high",
            "linked_task_ids": ["task1"], "description": evil_desc,
            "date": "2026-10-20",
        }]),
    )
    monkeypatch.setattr(iter6_router, "get_db", lambda: db)

    captured = {}

    async def fake_arender(html_string, **kw):
        captured["html"] = html_string
        return b"%PDF-fake"

    monkeypatch.setattr(pdf_safety, "arender_pdf", fake_arender)

    ctx = {"tenant_id": "t1", "user": {"id": "u1", "full_name": "Sam Super"}}
    resp = asyncio.run(iter6_router.export_daily_report_pdf("dr1", "internal", ctx))
    assert resp.body == b"%PDF-fake"
    html = captured["html"]

    # The reminder card is rendered, with the description escaped.
    assert "Open delay reminders (1)" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<b>x</b>" not in html
    assert "<a rel=attachment" not in html
    assert "&lt;a rel=attachment" in html
    # Header / weather / grid / crew values are escaped too.
    assert "<i>draft</i>" not in html and "&lt;i&gt;draft&lt;/i&gt;" in html
    assert "<img src=file:///etc/passwd>" not in html
    assert "<s>9</s>" not in html and "&lt;s&gt;9&lt;/s&gt;" in html
    assert "<b>20</b>" not in html and "&lt;b&gt;20&lt;/b&gt;%" in html
    assert "<b>Crew Co</b>" not in html and "&lt;b&gt;Crew Co&lt;/b&gt;" in html
    assert "<script>" not in html and "&lt;script&gt;n&lt;/script&gt;" in html
    assert "&lt;img src=file:///etc/passwd&gt;" in html
    # Plain text with '<' survives instead of being swallowed.
    assert "waiting on &lt;ConEd crew&gt;" in html


def test_daily_report_pdf_external_hides_reminders(monkeypatch):
    from routers import iter6_router

    db = _FakeDb(
        daily_reports=_Coll([{"id": "dr1", "tenant_id": "t1", "project_id": "p1",
                              "report_date": "2026-10-26", "status": "draft"}]),
        daily_report_activities=_Coll([{"daily_report_id": "dr1",
                                        "schedule_activity_id": "task1"}]),
        delays=_Coll([{"id": "d1", "tenant_id": "t1", "project_id": "p1",
                       "status": "open", "linked_task_ids": ["task1"],
                       "description": "<b>x</b>"}]),
    )
    monkeypatch.setattr(iter6_router, "get_db", lambda: db)
    captured = {}

    async def fake_arender(html_string, **kw):
        captured["html"] = html_string
        return b"%PDF-fake"

    monkeypatch.setattr(pdf_safety, "arender_pdf", fake_arender)
    ctx = {"tenant_id": "t1", "user": {"id": "u1", "full_name": ""}}
    asyncio.run(iter6_router.export_daily_report_pdf("dr1", "external", ctx))
    assert "Open delay reminders" not in captured["html"]
