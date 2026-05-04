"""Iter4 backend tests:
- PATCH /api/voice-annotations/:id linkage update
- Submittals CRUD + auto-numbering + tenant isolation
- Submittals auto-generation from spec '1.4 SUBMITTALS'
- Drawings spatial metadata upsert
- GET /api/projects/:id/spatial-metadata
- GET /api/entities/:type/:id (generic entity fetch)
- PATCH /api/locations/:id/polygon
"""
import io
import os
import time
import uuid
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
        "email": f"TEST_iter4_other_{unique}@example.com",
        "password": "OtherPass123!",
        "full_name": "Other Iter4",
        "tenant_slug": f"other4-{unique}",
    }
    r = requests.post(f"{API}/auth/register", json=payload, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"register not available: {r.status_code}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _pdf_with_submittals_section() -> bytes:
    """Build a tiny PDF with DIVISION 03 / SECTION 03 30 00 / 1.4 SUBMITTALS bullets."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    lines = [
        "DIVISION 03 - CONCRETE",
        "SECTION 03 30 00 - Cast-In-Place Concrete",
        "PART 1 - GENERAL",
        "1.1 SCOPE",
        "This section covers cast-in-place concrete.",
        "1.4 SUBMITTALS",
        "A. Product data for concrete mix designs",
        "B. Shop drawings for reinforcement",
        "C. Test reports for compressive strength",
        "D. Manufacturer cure compound certifications",
        "PART 2 - PRODUCTS",
        "Materials specified herein.",
    ]
    for ln in lines:
        c.drawString(72, y, ln)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


# ============================================================
# SUBMITTALS — manual CRUD + tenant isolation
# ============================================================
class TestSubmittalsCRUD:
    def test_create_submittal_auto_numbered(self, admin_headers, project_id):
        r = requests.post(f"{API}/submittals", headers=admin_headers, json={
            "project_id": project_id,
            "title": f"TEST_ Submittal {uuid.uuid4().hex[:5]}",
            "type": "product_data",
        }, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["submittal_number"].startswith("SUB-")
        assert len(body["submittal_number"]) == 8  # SUB-NNNN
        assert body["status"] == "not_yet_submitted"
        assert body["auto_generated"] is False
        pytest.shared_submittal_id = body["id"]

    def test_list_submittals(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/submittals",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()]
        assert pytest.shared_submittal_id in ids

    def test_patch_status(self, admin_headers):
        r = requests.patch(f"{API}/submittals/{pytest.shared_submittal_id}",
                           headers=admin_headers,
                           json={"status": "submitted"}, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "submitted"
        # GET to verify persisted
        # (no GET-by-id endpoint — verify via list)
        # already handled in list test pattern

    def test_tenant_isolation(self, other_tenant_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/submittals",
                         headers=other_tenant_headers, timeout=20)
        # Other tenant shouldn't see this project's submittals
        assert r.status_code == 200
        assert r.json() == [] or all(
            s["id"] != getattr(pytest, "shared_submittal_id", None) for s in r.json()
        )


# ============================================================
# SUBMITTALS — auto-generation from spec ingestion
# ============================================================
class TestSubmittalsAutogen:
    def test_ingest_spec_creates_submittals(self, admin_headers, project_id):
        # 1. Create spec row
        s = requests.post(f"{API}/specs", headers=admin_headers, json={
            "project_id": project_id,
            "name": f"TEST_iter4_spec_{uuid.uuid4().hex[:5]}",
        }, timeout=20)
        assert s.status_code in (200, 201), s.text
        spec_id = s.json()["id"]
        # 2. Upload PDF with 1.4 SUBMITTALS section
        pdf_bytes = _pdf_with_submittals_section()
        files = {"file": ("subs.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
        up = requests.post(f"{API}/specs/{spec_id}/upload",
                           headers=admin_headers, files=files, timeout=60)
        assert up.status_code in (200, 201, 202), up.text
        # 3. Poll until processing_status == complete
        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            r = requests.get(f"{API}/projects/{project_id}/specs",
                             headers=admin_headers, timeout=20)
            for sp in r.json():
                if sp["id"] == spec_id:
                    status = sp.get("processing_status")
                    break
            if status == "complete":
                break
            time.sleep(1)
        assert status == "complete", f"spec ingestion never completed (status={status})"
        # processing_status flips to 'complete' right after sections persist;
        # _autogenerate_submittals runs immediately after — give it a moment.
        # 4. Submittals should have auto-generated rows tagged with spec_section_code 03 30 00
        auto = []
        deadline2 = time.time() + 15
        while time.time() < deadline2:
            r = requests.get(f"{API}/projects/{project_id}/submittals",
                             headers=admin_headers, timeout=20)
            assert r.status_code == 200
            auto = [s for s in r.json()
                    if s.get("auto_generated")
                    and (s.get("spec_section_code") or "").startswith("03 30")
                    and s.get("title", "").lower().startswith(("product data", "shop drawings", "test reports", "manufacturer"))]
            if len(auto) >= 2:
                break
            time.sleep(1)
        assert len(auto) >= 2, f"expected >=2 auto-gen submittals from 1.4 SUBMITTALS bullets, got {len(auto)}"
        # all auto-gen rows should have submittal_number SUB-NNNN
        for sub in auto:
            assert sub["submittal_number"].startswith("SUB-")
            assert sub.get("spec_section_id"), "spec_section_id should be linked"


# ============================================================
# VOICE ANNOTATION PATCH
# ============================================================
class TestVoiceAnnotationPatch:
    def test_patch_unknown_id_404(self, admin_headers):
        r = requests.patch(f"{API}/voice-annotations/__nope__{uuid.uuid4().hex}",
                           headers=admin_headers,
                           json={"linked_entity_type": "task", "linked_entity_id": "xx"},
                           timeout=20)
        assert r.status_code == 404


# ============================================================
# DRAWINGS SPATIAL METADATA
# ============================================================
@pytest.fixture(scope="module")
def drawing_revision_id(admin_headers, project_id):
    dr = requests.post(f"{API}/drawings", headers=admin_headers, json={
        "project_id": project_id,
        "sheet_number": f"TEST-E{uuid.uuid4().hex[:4]}",
        "title": "TEST Elevation iter4",
        "discipline": "A",
    }, timeout=20)
    assert dr.status_code in (200, 201), dr.text
    drawing_id = dr.json()["id"]
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 800, "TEST ELEVATION NORTH")
    c.showPage()
    c.save()
    files = {"file": ("elev.pdf", io.BytesIO(buf.getvalue()), "application/pdf")}
    data = {"drawing_id": drawing_id, "revision_label": "Rev 0"}
    up = requests.post(f"{API}/drawing-revisions/upload",
                       headers=admin_headers, files=files, data=data, timeout=30)
    assert up.status_code in (200, 201), up.text
    return up.json()["id"]


class TestSpatialMeta:
    def test_create_spatial_metadata(self, admin_headers, drawing_revision_id):
        r = requests.post(f"{API}/drawings-spatial-metadata", headers=admin_headers, json={
            "drawing_revision_id": drawing_revision_id,
            "page_number": 1,
            "drawing_type": "elevation_north",
        }, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["drawing_type"] == "elevation_north"
        pytest.shared_spatial_meta_id = body["id"]

    def test_upsert_does_not_duplicate(self, admin_headers, drawing_revision_id):
        r = requests.post(f"{API}/drawings-spatial-metadata", headers=admin_headers, json={
            "drawing_revision_id": drawing_revision_id,
            "page_number": 1,
            "drawing_type": "elevation_north",
            "rotation_deg": 90,
        }, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == pytest.shared_spatial_meta_id, "upsert should reuse same id"
        assert r.json()["rotation_deg"] == 90, "rotation should be updated"

    def test_list_spatial_metadata(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/spatial-metadata",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()]
        assert pytest.shared_spatial_meta_id in ids


# ============================================================
# GENERIC ENTITY FETCH
# ============================================================
class TestEntityFetch:
    def test_fetch_task_entity(self, admin_headers, project_id):
        tasks = requests.get(f"{API}/projects/{project_id}/tasks",
                             headers=admin_headers, timeout=20).json()
        assert tasks
        task_id = tasks[0]["id"]
        r = requests.get(f"{API}/entities/task/{task_id}",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entity_type"] == "task"
        assert body["entity"]["id"] == task_id

    def test_unknown_entity_type_400(self, admin_headers):
        r = requests.get(f"{API}/entities/widget/abc",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 400

    def test_unknown_entity_id_404(self, admin_headers):
        r = requests.get(f"{API}/entities/task/__missing__{uuid.uuid4().hex}",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 404

    def test_tenant_isolation(self, other_tenant_headers, admin_headers, project_id):
        tasks = requests.get(f"{API}/projects/{project_id}/tasks",
                             headers=admin_headers, timeout=20).json()
        task_id = tasks[0]["id"]
        r = requests.get(f"{API}/entities/task/{task_id}",
                         headers=other_tenant_headers, timeout=20)
        assert r.status_code == 404, "other tenant must not see this task"


# ============================================================
# LOCATION POLYGON PATCH
# ============================================================
class TestLocationPolygon:
    def test_patch_polygon_recomputes_centroid(self, admin_headers, project_id):
        # Get a location
        r = requests.get(f"{API}/projects/{project_id}/locations",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        locs = r.json()
        assert locs
        loc_id = locs[0]["id"]
        # Square polygon at x=0..10, y=0..10
        coords = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        r2 = requests.patch(f"{API}/locations/{loc_id}/polygon",
                            headers=admin_headers,
                            json={"polygon_geojson": {"type": "Polygon", "coordinates": [coords]}},
                            timeout=20)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        # centroid should be ~5,5
        assert abs(body["centroid_x"] - 5.0) < 0.01
        assert abs(body["centroid_y"] - 5.0) < 0.01

    def test_patch_polygon_too_few_points_400(self, admin_headers, project_id):
        locs = requests.get(f"{API}/projects/{project_id}/locations",
                            headers=admin_headers, timeout=20).json()
        loc_id = locs[0]["id"]
        r = requests.patch(f"{API}/locations/{loc_id}/polygon",
                           headers=admin_headers,
                           json={"polygon_geojson": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]}},
                           timeout=20)
        assert r.status_code == 400

    def test_patch_unknown_location_404(self, admin_headers):
        coords = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
        r = requests.patch(f"{API}/locations/__nope__{uuid.uuid4().hex}/polygon",
                           headers=admin_headers,
                           json={"polygon_geojson": {"type": "Polygon", "coordinates": [coords]}},
                           timeout=20)
        assert r.status_code == 404
