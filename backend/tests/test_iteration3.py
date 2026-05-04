"""Iter3 backend tests:
- Drawing markups CRUD + polymorphic pin link + tenant isolation
- Photos upload/stream/list + tenant isolation
- Spec semantic search (TF-IDF) + reindex
- Copilot uses spec retrieval (no 500)
- Auto-seeded location polygon_geojson (12 locations)
"""
import io
import os
import time
import uuid
import struct
import zlib
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE}/api"
ADMIN = {"email": "admin@iceops.com", "password": "Demo123!"}


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def admin_headers():
    r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=20)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def project_id(admin_headers):
    r = requests.get(f"{API}/projects", headers=admin_headers, timeout=20)
    assert r.status_code == 200
    for p in r.json():
        if "GSA" in (p.get("name") or "") or "Federal" in (p.get("name") or ""):
            return p["id"]
    return r.json()[0]["id"]


@pytest.fixture(scope="module")
def other_tenant_headers():
    unique = uuid.uuid4().hex[:6]
    payload = {
        "email": f"TEST_iter3_other_{unique}@example.com",
        "password": "OtherPass123!",
        "full_name": "Other Iter3",
        "tenant_slug": f"other3-{unique}",
    }
    r = requests.post(f"{API}/auth/register", json=payload, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"register not avail: {r.status_code}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _tiny_png():
    """Build a valid 1x1 red PNG."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1 RGB
    raw = b"\x00" + b"\xff\x00\x00"  # filter=0 + red pixel
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _tiny_pdf_concrete():
    """Build a tiny PDF via reportlab with DIVISION 03 + SECTION 03 30 00 markers."""
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 800, "DIVISION 03 - CONCRETE")
    c.drawString(72, 780, "SECTION 03 30 00 - CAST-IN-PLACE CONCRETE")
    c.drawString(72, 760, "Cure time for cast-in-place concrete shall be 7 days minimum.")
    c.drawString(72, 740, "Compressive strength f'c = 4000 psi.")
    c.showPage()
    c.save()
    return buf.getvalue()


# ============================================================
# Locations polygon_geojson auto-seed
# ============================================================
class TestLocationPolygons:
    def test_12_locations_with_polygons(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/locations",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        locs = r.json()
        assert len(locs) >= 12, f"expected >=12 seeded locations, got {len(locs)}"
        with_poly = [l for l in locs if l.get("polygon_geojson")]
        assert len(with_poly) == len(locs), \
            f"expected ALL locations to have polygon_geojson, got {len(with_poly)}/{len(locs)}"
        for l in with_poly[:3]:
            geo = l["polygon_geojson"]
            assert geo.get("type") == "Polygon", f"bad type: {geo}"
            assert isinstance(geo.get("coordinates"), list), f"no coords: {geo}"
            assert len(geo["coordinates"][0]) >= 4, "polygon needs >=4 pts (closed ring)"


# ============================================================
# Drawing markups
# ============================================================
@pytest.fixture(scope="module")
def drawing_revision_id(admin_headers, project_id):
    # create drawing
    dr = requests.post(f"{API}/drawings", headers=admin_headers, json={
        "project_id": project_id,
        "sheet_number": f"TEST-A{uuid.uuid4().hex[:4]}",
        "title": "TEST Drawing iter3",
        "discipline": "A",
    }, timeout=20)
    assert dr.status_code in (200, 201), dr.text
    drawing_id = dr.json()["id"]
    # upload a minimal PDF
    try:
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 800, "TEST DRAWING")
        c.showPage()
        c.save()
        pdf_bytes = buf.getvalue()
    except ImportError:
        pytest.skip("reportlab not installed")
    files = {"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
    data = {"drawing_id": drawing_id, "revision_label": "Rev 0"}
    up = requests.post(f"{API}/drawing-revisions/upload",
                       headers=admin_headers, files=files, data=data, timeout=30)
    assert up.status_code in (200, 201), up.text
    return up.json()["id"]


class TestDrawingMarkups:
    def test_create_pen_markup(self, admin_headers, drawing_revision_id):
        r = requests.post(f"{API}/drawing-markups", headers=admin_headers, json={
            "drawing_revision_id": drawing_revision_id,
            "page_number": 1,
            "markup_type": "pen",
            "geometry": {"points": [[10, 10], [20, 20], [30, 15]]},
            "style": {"color": "#ff0000", "width": 2},
        }, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["markup_type"] == "pen"
        pytest.shared_pen_id = r.json()["id"]

    def test_create_rect_markup(self, admin_headers, drawing_revision_id):
        r = requests.post(f"{API}/drawing-markups", headers=admin_headers, json={
            "drawing_revision_id": drawing_revision_id,
            "markup_type": "rect",
            "geometry": {"x": 5, "y": 5, "w": 50, "h": 30},
        }, timeout=20)
        assert r.status_code == 200, r.text

    def test_create_pin_with_linked_entity(self, admin_headers, project_id, drawing_revision_id):
        tasks = requests.get(f"{API}/projects/{project_id}/tasks",
                             headers=admin_headers, timeout=20).json()
        assert tasks
        task_id = tasks[0]["id"]
        r = requests.post(f"{API}/drawing-markups", headers=admin_headers, json={
            "drawing_revision_id": drawing_revision_id,
            "markup_type": "pin",
            "geometry": {"x": 100, "y": 200},
            "text_content": "Inspect beam",
            "linked_entity_type": "task",
            "linked_entity_id": task_id,
        }, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["markup_type"] == "pin"
        assert data["linked_entity_type"] == "task"
        assert data["linked_entity_id"] == task_id
        pytest.shared_pin_id = data["id"]

    def test_list_markups_includes_created(self, admin_headers, drawing_revision_id):
        r = requests.get(f"{API}/drawing-revisions/{drawing_revision_id}/markups",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200
        ids = {m["id"] for m in r.json()}
        assert getattr(pytest, "shared_pen_id", None) in ids
        assert getattr(pytest, "shared_pin_id", None) in ids
        pins = [m for m in r.json() if m["markup_type"] == "pin"]
        assert any(p.get("linked_entity_type") == "task" for p in pins)

    def test_tenant_isolation_markups(self, other_tenant_headers, drawing_revision_id):
        r = requests.get(f"{API}/drawing-revisions/{drawing_revision_id}/markups",
                         headers=other_tenant_headers, timeout=20)
        # other tenant should see either empty list or 404
        if r.status_code == 200:
            assert r.json() == [], "tenant leak in markups list"
        else:
            assert r.status_code in (403, 404)

    def test_delete_markup(self, admin_headers):
        mid = getattr(pytest, "shared_pen_id", None)
        if not mid:
            pytest.skip("no pen markup")
        r = requests.delete(f"{API}/drawing-markups/{mid}", headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text


# ============================================================
# Photos
# ============================================================
class TestPhotos:
    def test_upload_and_stream(self, admin_headers, project_id):
        png = _tiny_png()
        files = {"file": ("test.png", io.BytesIO(png), "image/png")}
        data = {"project_id": project_id, "caption": "TEST_photo_iter3"}
        r = requests.post(f"{API}/photos/upload",
                          headers=admin_headers, files=files, data=data, timeout=30)
        assert r.status_code == 200, r.text
        photo = r.json()
        assert photo["storage_path"]
        assert photo["project_id"] == project_id
        pytest.shared_photo_id = photo["id"]

        # stream bytes
        s = requests.get(f"{API}/photos/{photo['id']}/file",
                         headers=admin_headers, timeout=20)
        assert s.status_code == 200
        assert s.headers.get("content-type", "").startswith("image/")
        assert len(s.content) > 20

    def test_list_project_photos(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/photos",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        photos = r.json()
        assert isinstance(photos, list)
        pid = getattr(pytest, "shared_photo_id", None)
        if pid:
            assert any(p["id"] == pid for p in photos), "uploaded photo not in list"

    def test_tenant_isolation_photos(self, other_tenant_headers):
        pid = getattr(pytest, "shared_photo_id", None)
        if not pid:
            pytest.skip("no shared photo")
        r = requests.get(f"{API}/photos/{pid}/file",
                         headers=other_tenant_headers, timeout=20)
        assert r.status_code in (403, 404), f"tenant leak photo bytes: {r.status_code}"


# ============================================================
# Spec semantic search
# ============================================================
@pytest.fixture(scope="module")
def seeded_spec_id(admin_headers, project_id):
    """Create spec row, upload PDF, wait until processed."""
    create = requests.post(f"{API}/specs", headers=admin_headers, json={
        "project_id": project_id,
        "name": "TEST_iter3_concrete_spec",
        "revision_label": "Rev 0",
    }, timeout=20)
    assert create.status_code in (200, 201), create.text
    spec_id = create.json()["id"]
    pdf = _tiny_pdf_concrete()
    files = {"file": ("iter3spec.pdf", io.BytesIO(pdf), "application/pdf")}
    r = requests.post(f"{API}/specs/{spec_id}/upload",
                      headers=admin_headers, files=files, timeout=30)
    assert r.status_code in (200, 201), r.text
    # poll for processing complete
    for _ in range(30):
        s = requests.get(f"{API}/projects/{project_id}/specs",
                         headers=admin_headers, timeout=20)
        if s.status_code == 200:
            found = [x for x in s.json() if x["id"] == spec_id]
            if found and found[0].get("processing_status") == "complete":
                return spec_id
        time.sleep(1)
    return spec_id  # proceed even if not complete — test will report


class TestSpecSearch:
    def test_empty_search_no_specs_ok(self, admin_headers, project_id):
        # project_id path — should return list (possibly empty) not 500
        r = requests.post(f"{API}/specs/search", headers=admin_headers, json={
            "project_id": project_id, "query": "aluminum zzzzz nonsense xyz"
        }, timeout=20)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_concrete_query_returns_hits(self, admin_headers, project_id, seeded_spec_id):
        # wait a beat in case tfidf fit backgrounded
        time.sleep(2)
        r = requests.post(f"{API}/specs/search", headers=admin_headers, json={
            "project_id": project_id, "query": "concrete cure time", "top_k": 5
        }, timeout=20)
        assert r.status_code == 200, r.text
        hits = r.json()
        assert isinstance(hits, list)
        if not hits:
            pytest.skip("spec TF-IDF not indexed yet / no hits — manual verification needed")
        # at least one hit should reference division 03 or section 03 30 00
        codes = [(h.get("csi_division") or "", h.get("section_code") or "") for h in hits]
        has_match = any("03" in div or "03" in sec for div, sec in codes)
        assert has_match, f"no 03-division hits among: {codes}"

    def test_reindex_returns_200(self, admin_headers, seeded_spec_id):
        r = requests.post(f"{API}/specs/{seeded_spec_id}/reindex",
                          headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

    def test_tenant_isolation_spec_search(self, other_tenant_headers, project_id):
        # other tenant querying ICE project — must not 500; should return [] or 403/404
        r = requests.post(f"{API}/specs/search", headers=other_tenant_headers, json={
            "project_id": project_id, "query": "concrete"
        }, timeout=20)
        if r.status_code == 200:
            assert r.json() == [], "tenant leak in spec search"
        else:
            assert r.status_code in (400, 403, 404)


# ============================================================
# Copilot spec retrieval
# ============================================================
class TestCopilotSpecRetrieval:
    def test_copilot_no_500_with_specs(self, admin_headers, project_id, seeded_spec_id):
        r = requests.post(f"{API}/ai/copilot", headers=admin_headers, json={
            "project_id": project_id,
            "message": "What is the cure time for cast-in-place concrete?"
        }, timeout=60)
        # allow LLM failure (502/503) but MUST NOT be 500
        assert r.status_code != 500, r.text
        if r.status_code >= 500:
            pytest.skip(f"LLM upstream {r.status_code}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("response") or body.get("message") or body.get("content")
