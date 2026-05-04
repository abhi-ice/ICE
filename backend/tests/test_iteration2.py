"""Backend tests for ICE Schedule Ops iteration 2 features.
Covers: Google OAuth (cookie auth + invalid session 401), Activity CRUD,
Daily-report available-tasks/grid add/remove, Voice annotation pipeline,
Spec PDF ingestion, Drawing PDF upload + streaming, tenant isolation
on new endpoints.
"""
import io
import os
import sys
import time
import uuid
import wave
import pytest
import requests
from datetime import datetime, timedelta, timezone

# allow importing backend modules
import pathlib
_backend = pathlib.Path(__file__).resolve().parents[1]
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE}/api"
ADMIN = {"email": "admin@iceops.com", "password": "Demo123!"}


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def project_id(admin_headers):
    r = requests.get(f"{API}/projects", headers=admin_headers, timeout=20)
    assert r.status_code == 200
    for p in r.json():
        if "GSA" in (p.get("name") or "") or "Federal" in (p.get("name") or ""):
            return p["id"]
    return r.json()[0]["id"]


@pytest.fixture(scope="module")
def cookie_session():
    """Pre-create a user_sessions row so we can test cookie-based auth."""
    try:
        from pymongo import MongoClient
    except Exception:
        pytest.skip("pymongo unavailable")
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "ice_schedule_ops")
    db = MongoClient(mongo_url)[db_name]
    tenant = db.tenants.find_one({"slug": "ice"})
    if not tenant:
        pytest.skip("ice tenant not seeded")
    uid = "test-user-" + uuid.uuid4().hex[:8]
    sess = "TEST_session_" + uuid.uuid4().hex[:12]
    db.users.insert_one({
        "id": uid, "email": f"test.{uid}@example.com",
        "full_name": "Test Cookie User", "picture": None,
        "tenant_id": tenant["id"], "role": "pm",
        "auth_provider": "google",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    db.tenant_users.insert_one({
        "id": str(uuid.uuid4()), "user_id": uid,
        "tenant_id": tenant["id"], "role": "pm",
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    db.user_sessions.insert_one({
        "user_id": uid, "session_token": sess,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    yield {"uid": uid, "session_token": sess}
    # cleanup
    db.users.delete_many({"id": uid})
    db.tenant_users.delete_many({"user_id": uid})
    db.user_sessions.delete_many({"session_token": sess})


@pytest.fixture(scope="module")
def other_tenant_token():
    unique = uuid.uuid4().hex[:6]
    payload = {
        "email": f"TEST_other2_{unique}@example.com",
        "password": "OtherPass123!",
        "full_name": "Other Tenant User2",
        "tenant_slug": f"other2-{unique}",
    }
    r = requests.post(f"{API}/auth/register", json=payload, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"register endpoint not available: {r.status_code}")
    return r.json()["access_token"]


# ---------- helpers ----------
def _silence_wav_bytes(seconds=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * seconds))
    return buf.getvalue()


def _spec_pdf_bytes():
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, 720, "DIVISION 03 - CONCRETE")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, 690, "SECTION 03 30 00 - Cast-In-Place Concrete")
    c.setFont("Helvetica", 10)
    c.drawString(72, 660, "Part 1 - General. Concrete shall be normal weight 4000 psi.")
    c.drawString(72, 640, "Part 2 - Products. Cement shall conform to ASTM C150.")
    c.drawString(72, 620, "Part 3 - Execution. Place per ACI 301.")
    c.showPage()
    c.save()
    return buf.getvalue()


def _drawing_pdf_bytes():
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, 720, "TEST DRAWING SHEET A-101")
    c.drawString(72, 690, "Floor Plan - Level 1")
    c.showPage()
    # second page
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, 720, "TEST DRAWING SHEET A-102")
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------- regression: legacy login still works ----------
def test_login_regression():
    r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=15)
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_health():
    r = requests.get(f"{API}/health", timeout=10)
    assert r.status_code == 200


# ---------- Google OAuth / cookie auth ----------
class TestGoogleAuth:
    def test_invalid_session_id_returns_401_not_500(self):
        r = requests.post(f"{API}/auth/google/session",
                          json={"session_id": "not_a_real_session"},
                          timeout=20)
        assert r.status_code != 500, f"got 500: {r.text[:200]}"
        assert r.status_code in (400, 401, 403), f"unexpected: {r.status_code} {r.text[:200]}"

    def test_cookie_auth_me(self, cookie_session):
        cookies = {"session_token": cookie_session["session_token"]}
        r = requests.get(f"{API}/auth/me", cookies=cookies, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        # email should match the test user we inserted
        assert "test." in body.get("email", ""), body

    def test_logout_clears_cookie(self, cookie_session):
        # use a fresh session for logout to avoid interfering with other tests
        from pymongo import MongoClient
        db = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[
            os.environ.get("DB_NAME", "ice_schedule_ops")]
        tenant = db.tenants.find_one({"slug": "ice"})
        uid = "test-user-" + uuid.uuid4().hex[:8]
        sess = "TEST_session_" + uuid.uuid4().hex[:12]
        db.users.insert_one({
            "id": uid, "email": f"test.lo.{uid}@example.com",
            "full_name": "T", "tenant_id": tenant["id"], "role": "pm",
            "auth_provider": "google",
            "created_at": datetime.now(timezone.utc).isoformat()})
        db.tenant_users.insert_one({
            "id": str(uuid.uuid4()), "user_id": uid, "tenant_id": tenant["id"],
            "role": "pm", "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat()})
        db.user_sessions.insert_one({
            "user_id": uid, "session_token": sess,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat()})

        cookies = {"session_token": sess}
        r = requests.post(f"{API}/auth/logout", cookies=cookies, timeout=15)
        assert r.status_code in (200, 204), r.text
        # session should be deleted from DB
        assert db.user_sessions.find_one({"session_token": sess}) is None

        # subsequent /auth/me with same cookie should fail
        r2 = requests.get(f"{API}/auth/me", cookies=cookies, timeout=15)
        assert r2.status_code in (401, 403)
        # cleanup
        db.users.delete_many({"id": uid})
        db.tenant_users.delete_many({"user_id": uid})


# ---------- Activity (Task) CRUD ----------
class TestActivityCRUD:
    def test_create_patch_delete_task(self, admin_headers, project_id):
        code = f"TEST{uuid.uuid4().hex[:5].upper()}"
        payload = {
            "project_id": project_id,
            "activity_code": code,
            "description": "TEST activity create",
            "planned_start": "2026-02-01",
            "planned_finish": "2026-02-05",
            "duration_days": 5,
            "predecessors": [],
            "trade_code": "GEN",
        }
        r = requests.post(f"{API}/tasks", headers=admin_headers, json=payload, timeout=20)
        assert r.status_code in (200, 201), r.text
        tid = r.json()["id"]

        # PATCH
        r2 = requests.patch(f"{API}/tasks/{tid}", headers=admin_headers,
                            json={"description": "TEST activity updated"}, timeout=20)
        assert r2.status_code == 200, r2.text
        assert r2.json()["description"] == "TEST activity updated"

        # DELETE (soft)
        r3 = requests.delete(f"{API}/tasks/{tid}", headers=admin_headers, timeout=20)
        assert r3.status_code in (200, 204), r3.text

        # not in list anymore
        lst = requests.get(f"{API}/projects/{project_id}/tasks",
                           headers=admin_headers, timeout=20).json()
        assert all(t["id"] != tid for t in lst), "deleted task still listed"

        # audit log captures DELETE for tasks
        log = requests.get(f"{API}/audit-log", headers=admin_headers, timeout=20).json()
        assert any(
            e.get("record_id") == tid
            and (e.get("op") or "").upper() == "DELETE"
            and (e.get("table_name") or "") == "tasks"
            for e in log
        ), "audit-log missing DELETE entry for task"


# ---------- Daily Report add-activity flow ----------
class TestDailyReportActivityPicker:
    def test_available_tasks_and_grid_add_remove(self, admin_headers, project_id):
        from datetime import date
        today = date.today().isoformat()
        rep = requests.post(f"{API}/daily-reports", headers=admin_headers,
                            json={"project_id": project_id, "report_date": today},
                            timeout=30)
        assert rep.status_code in (200, 201), rep.text
        rid = rep.json()["id"]

        avail = requests.get(f"{API}/daily-reports/{rid}/available-tasks",
                             headers=admin_headers, timeout=20)
        assert avail.status_code == 200, avail.text
        avail_list = avail.json()
        assert isinstance(avail_list, list)

        # If pre-population already filled the grid, remove one row to make
        # an activity "available" for testing the add/remove flow.
        grid_before = requests.get(f"{API}/daily-reports/{rid}/grid",
                                   headers=admin_headers, timeout=15).json()
        if not avail_list:
            if not grid_before:
                pytest.skip("no available tasks AND no pre-populated grid for today")
            row = grid_before[0]
            requests.delete(f"{API}/daily-report-activities/{row['id']}",
                            headers=admin_headers, timeout=15)
            avail_list = requests.get(f"{API}/daily-reports/{rid}/available-tasks",
                                      headers=admin_headers, timeout=15).json()
            assert avail_list, "after deleting a grid row, expected it to appear in available-tasks"
            grid_before = requests.get(f"{API}/daily-reports/{rid}/grid",
                                       headers=admin_headers, timeout=15).json()

        # validate filtering: planned_start <= today <= planned_finish AND not in grid
        grid_ids = {row.get("schedule_activity_id") for row in grid_before}
        for t in avail_list:
            assert t["id"] not in grid_ids, "available-tasks leaked already-in-grid task"
            ps = t.get("planned_start", "")[:10]
            pf = t.get("planned_finish", "")[:10]
            assert ps <= today <= pf, f"available task {t.get('activity_code')} not planned for today"

        # POST add to grid
        pick = avail_list[0]
        add = requests.post(f"{API}/daily-reports/{rid}/grid",
                            headers=admin_headers,
                            json={"schedule_activity_id": pick["id"]},
                            timeout=20)
        assert add.status_code in (200, 201), add.text
        new_row_id = add.json().get("id")
        assert new_row_id, add.text

        grid_after = requests.get(f"{API}/daily-reports/{rid}/grid",
                                  headers=admin_headers, timeout=15).json()
        assert any(r.get("id") == new_row_id for r in grid_after)

        # available-tasks should now exclude the added one
        avail2 = requests.get(f"{API}/daily-reports/{rid}/available-tasks",
                              headers=admin_headers, timeout=15).json()
        assert all(t["id"] != pick["id"] for t in avail2)

        # DELETE row
        d = requests.delete(f"{API}/daily-report-activities/{new_row_id}",
                            headers=admin_headers, timeout=15)
        assert d.status_code in (200, 204), d.text
        grid_final = requests.get(f"{API}/daily-reports/{rid}/grid",
                                  headers=admin_headers, timeout=15).json()
        assert all(r.get("id") != new_row_id for r in grid_final)


# ---------- Voice annotation pipeline ----------
class TestVoiceAnnotation:
    def test_voice_annotate_does_not_500(self, admin_headers):
        wav = _silence_wav_bytes(1)
        files = {"file": ("silence.wav", io.BytesIO(wav), "audio/wav")}
        data = {"kind": "work_item"}
        r = requests.post(f"{API}/voice/annotate", headers=admin_headers,
                          files=files, data=data, timeout=120)
        assert r.status_code != 500, f"voice/annotate returned 500: {r.text[:300]}"
        assert r.status_code in (200, 201), r.text
        body = r.json()
        for key in ("id", "transcript", "structured_fields", "status", "audio_url"):
            assert key in body, f"missing key {key} in response: {body}"
        assert body["status"] in ("completed", "failed", "pending", "processing")
        pytest.shared_voice_id = body["id"]

    def test_voice_annotations_list(self, admin_headers):
        r = requests.get(f"{API}/voice/annotations", headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert isinstance(rows, list)
        vid = getattr(pytest, "shared_voice_id", None)
        if vid:
            assert any(row.get("id") == vid for row in rows)


# ---------- Spec PDF ingestion ----------
class TestSpecIngest:
    def test_spec_create_upload_and_sections(self, admin_headers, project_id):
        # create spec
        sp = requests.post(f"{API}/specs", headers=admin_headers,
                           json={"project_id": project_id,
                                 "name": f"TEST_spec_{uuid.uuid4().hex[:4]}"},
                           timeout=20)
        assert sp.status_code in (200, 201), sp.text
        sid = sp.json()["id"]

        pdf = _spec_pdf_bytes()
        files = {"file": ("spec.pdf", io.BytesIO(pdf), "application/pdf")}
        up = requests.post(f"{API}/specs/{sid}/upload", headers=admin_headers,
                           files=files, timeout=60)
        assert up.status_code in (200, 201, 202), up.text

        # poll for completion (up to ~15s)
        deadline = time.time() + 15
        spec_row = None
        while time.time() < deadline:
            time.sleep(1.5)
            lst = requests.get(f"{API}/projects/{project_id}/specs",
                               headers=admin_headers, timeout=15).json()
            spec_row = next((s for s in lst if s["id"] == sid), None)
            if spec_row and spec_row.get("processing_status") in ("complete", "failed"):
                break
        assert spec_row is not None, "spec not in list"
        # complete preferred; failed acceptable but flagged
        assert spec_row.get("processing_status") == "complete", (
            f"processing_status={spec_row.get('processing_status')}: {spec_row}")
        assert (spec_row.get("total_sections") or 0) >= 1

        # sections
        secs = requests.get(f"{API}/specs/{sid}/sections",
                            headers=admin_headers, timeout=20)
        assert secs.status_code == 200, secs.text
        assert isinstance(secs.json(), list) and len(secs.json()) >= 1
        pytest.shared_spec_id = sid


# ---------- Drawing PDF upload + streaming ----------
class TestDrawingPDF:
    def test_drawing_create_upload_view(self, admin_headers, project_id):
        d = requests.post(f"{API}/drawings", headers=admin_headers,
                          json={"project_id": project_id,
                                "sheet_number": f"A-{uuid.uuid4().hex[:4]}",
                                "title": "TEST_drawing",
                                "discipline": "A"},
                          timeout=20)
        assert d.status_code in (200, 201), d.text
        did = d.json()["id"]

        pdf = _drawing_pdf_bytes()
        files = {"file": ("plan.pdf", io.BytesIO(pdf), "application/pdf")}
        data = {"drawing_id": did, "revision_label": "R1"}
        up = requests.post(f"{API}/drawing-revisions/upload",
                           headers=admin_headers, files=files, data=data, timeout=60)
        assert up.status_code in (200, 201), up.text
        body = up.json()
        assert "page_count" in body and body["page_count"] >= 1
        assert "pdf_url" in body and body["pdf_url"]
        rev_id = body["id"]

        # GET row
        g = requests.get(f"{API}/drawing-revisions/{rev_id}",
                        headers=admin_headers, timeout=15)
        assert g.status_code == 200, g.text

        # GET pdf stream
        s = requests.get(f"{API}/drawing-revisions/{rev_id}/pdf",
                         headers=admin_headers, timeout=30,
                         allow_redirects=True)
        assert s.status_code == 200, f"{s.status_code} {s.text[:120]}"
        ct = s.headers.get("content-type", "")
        assert "pdf" in ct.lower(), f"unexpected content-type: {ct}"
        assert s.content[:4] == b"%PDF", "response is not a PDF"
        pytest.shared_rev_id = rev_id


# ---------- Tenant isolation regression on iter2 endpoints ----------
class TestTenantIsolationIter2:
    def test_other_tenant_cannot_see_voice_or_drawings_or_specs(self, other_tenant_token):
        h = {"Authorization": f"Bearer {other_tenant_token}"}
        # voice annotations
        r = requests.get(f"{API}/voice/annotations", headers=h, timeout=15)
        assert r.status_code == 200
        assert r.json() == [] or len(r.json()) == 0

        # drawing revision (created by ICE) — direct GET should 403/404 (or 200 only if filtered)
        rev_id = getattr(pytest, "shared_rev_id", None)
        if rev_id:
            r2 = requests.get(f"{API}/drawing-revisions/{rev_id}",
                              headers=h, timeout=15)
            assert r2.status_code in (401, 403, 404), \
                f"drawing-revision leaked: {r2.status_code} {r2.text[:200]}"

        # spec sections (created by ICE)
        sid = getattr(pytest, "shared_spec_id", None)
        if sid:
            r3 = requests.get(f"{API}/specs/{sid}/sections",
                              headers=h, timeout=15)
            # Either reject (401/403/404) or filtered empty list
            if r3.status_code == 200:
                assert r3.json() == [], "spec sections leaked across tenants"
            else:
                assert r3.status_code in (401, 403, 404)
