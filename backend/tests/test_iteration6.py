"""Iter6 backend tests:
- Aging summary + detail
- Daily-report PDF + weekly-rollup PDF (WeasyPrint)
- AI recovery apply + eval-harness
- Submittals link patch + auto-link
- Spatial section
- Photo EXIF extract
- Iter5 regression: marketplace, notifications, search, weather, productivity, cost-rates, work-item-history
"""
import io
import os
import uuid
import pytest
import requests
from datetime import date, timedelta

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE}/api"
ADMIN = {"email": "admin@iceops.com", "password": "Demo123!"}


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
        "email": f"TEST_iter6_other_{unique}@example.com",
        "password": "OtherPass123!",
        "full_name": "Other Iter6",
        "tenant_slug": f"other6-{unique}",
    }
    r = requests.post(f"{API}/auth/register", json=payload, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"register not available: {r.status_code}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ============================================================
# Aging
# ============================================================
class TestAging:
    def test_aging_summary(self, admin_headers):
        r = requests.get(f"{API}/aging-summary", headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        # All values must be ints
        for k, v in data.items():
            assert isinstance(v, int), f"{k} should be int, got {type(v)}"

    def test_aging_detail(self, admin_headers):
        r = requests.get(f"{API}/aging-detail", headers=admin_headers, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        for key in ("rfis", "submittals", "work_items", "approvals"):
            assert key in data, f"missing {key}"
            assert isinstance(data[key], list)
        # If any RFIs returned, days_open must exist and be a non-negative int
        for r_ in data["rfis"]:
            assert "days_open" in r_
            assert isinstance(r_["days_open"], int)
            assert r_["days_open"] >= 0

    def test_aging_detail_tenant_isolation(self, other_tenant_headers):
        r = requests.get(f"{API}/aging-detail", headers=other_tenant_headers, timeout=20)
        assert r.status_code == 200
        d = r.json()
        # Other tenant should have all empty
        assert d["rfis"] == []
        assert d["submittals"] == []
        assert d["work_items"] == []
        assert d["approvals"] == []


# ============================================================
# PDF rendering
# ============================================================
class TestPDFs:
    def test_daily_report_pdf_rich(self, admin_headers, project_id):
        # Find a daily report
        r = requests.get(f"{API}/projects/{project_id}/daily-reports",
                         headers=admin_headers, timeout=20)
        if r.status_code != 200 or not r.json():
            # Try create one
            today = date.today().isoformat()
            cr = requests.post(f"{API}/projects/{project_id}/daily-reports",
                               headers=admin_headers,
                               json={"report_date": today}, timeout=20)
            if cr.status_code not in (200, 201):
                pytest.skip(f"no daily reports & cannot create: {cr.status_code}")
            dr_id = cr.json()["id"]
        else:
            dr_id = r.json()[0]["id"]

        pdf = requests.get(f"{API}/daily-reports/{dr_id}/pdf-rich",
                           headers=admin_headers, timeout=60)
        assert pdf.status_code == 200, pdf.text[:500]
        assert pdf.headers.get("content-type", "").startswith("application/pdf")
        assert pdf.content[:4] == b"%PDF"
        assert len(pdf.content) > 500

    def test_weekly_rollup_pdf(self, admin_headers, project_id):
        # week_starting = monday-ish
        ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        r = requests.get(f"{API}/projects/{project_id}/weekly-rollup/pdf",
                         params={"week_starting": ws},
                         headers=admin_headers, timeout=60)
        assert r.status_code == 200, r.text[:500]
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert r.content[:4] == b"%PDF"


# ============================================================
# AI Recovery — apply + eval harness
# ============================================================
class TestRecovery:
    def test_recovery_eval_harness_returns_shape(self, admin_headers):
        r = requests.get(f"{API}/ai/recovery/eval-harness",
                         headers=admin_headers, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        for k in ("total_applied", "total_predicted_days", "total_realized_days",
                  "accuracy_score", "rows"):
            assert k in data, f"missing {k}"
        assert isinstance(data["rows"], list)

    def test_recovery_apply_flow(self, admin_headers):
        # Find an existing recovery suggestion (or skip)
        r = requests.get(f"{API}/ai/recovery", headers=admin_headers, timeout=30)
        if r.status_code != 200 or not r.json():
            pytest.skip("no recovery suggestions available")
        suggestions = r.json()
        # Find one not yet applied with at least one suggestion
        target = None
        for s in suggestions:
            if s.get("status") != "applied" and s.get("suggestions"):
                target = s
                break
        if not target:
            pytest.skip("no unapplied recovery suggestion with options")

        rid = target["id"]
        ap = requests.post(f"{API}/ai/recovery/{rid}/apply",
                           headers=admin_headers,
                           json={"suggestion_index": 0}, timeout=30)
        assert ap.status_code == 200, ap.text
        body = ap.json()
        assert body["ok"] is True
        assert "applied" in body

        # Verify status flipped to applied via list
        r2 = requests.get(f"{API}/ai/recovery", headers=admin_headers, timeout=20)
        assert r2.status_code == 200
        match = next((x for x in r2.json() if x["id"] == rid), None)
        assert match is not None
        assert match["status"] == "applied"
        assert match.get("chosen_suggestion_index") == 0
        assert match.get("applied_at")
        assert match.get("applied_by")


# ============================================================
# Submittals — link + auto-link
# ============================================================
class TestSubmittalLinks:
    def test_patch_links(self, admin_headers, project_id):
        # Find a submittal and a task
        subs = requests.get(f"{API}/projects/{project_id}/submittals",
                            headers=admin_headers, timeout=20)
        assert subs.status_code == 200
        if not subs.json():
            pytest.skip("no submittals")
        sub_id = subs.json()[0]["id"]

        tasks = requests.get(f"{API}/projects/{project_id}/tasks",
                             headers=admin_headers, timeout=20)
        assert tasks.status_code == 200
        if not tasks.json():
            pytest.skip("no tasks")
        task_id = tasks.json()[0]["id"]

        p = requests.patch(f"{API}/submittals/{sub_id}/links",
                           headers=admin_headers,
                           json={"schedule_activity_id": task_id}, timeout=20)
        assert p.status_code == 200, p.text
        assert p.json()["schedule_activity_id"] == task_id

        # Verify persistence via project list GET (no single-GET endpoint)
        g = requests.get(f"{API}/projects/{project_id}/submittals",
                         headers=admin_headers, timeout=20)
        assert g.status_code == 200
        match = next((x for x in g.json() if x["id"] == sub_id), None)
        assert match is not None
        assert match["schedule_activity_id"] == task_id

    def test_auto_link_activities(self, admin_headers, project_id):
        r = requests.post(f"{API}/projects/{project_id}/submittals/auto-link-activities",
                          headers=admin_headers, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "linked" in data
        assert "total_submittals_examined" in data
        assert isinstance(data["linked"], int)
        assert isinstance(data["total_submittals_examined"], int)


# ============================================================
# Spatial section
# ============================================================
class TestSpatialSection:
    def test_section_valid(self, admin_headers, project_id):
        r = requests.post(f"{API}/spatial/section",
                          headers=admin_headers,
                          json={"project_id": project_id,
                                "cut_line": [[0, 0], [1000, 1000]]}, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "floors" in data
        assert isinstance(data["floors"], list)
        for fl in data["floors"]:
            assert "intersections" in fl
            assert "floor_number" in fl

    def test_section_bad_cut_line(self, admin_headers, project_id):
        r = requests.post(f"{API}/spatial/section",
                          headers=admin_headers,
                          json={"project_id": project_id,
                                "cut_line": [[0, 0]]}, timeout=20)
        assert r.status_code == 400

    def test_section_tenant_isolation(self, other_tenant_headers, project_id):
        # Other tenant trying to access GSA project's section → empty floors
        r = requests.post(f"{API}/spatial/section",
                          headers=other_tenant_headers,
                          json={"project_id": project_id,
                                "cut_line": [[0, 0], [1000, 1000]]}, timeout=20)
        assert r.status_code == 200
        assert r.json()["floors"] == []


# ============================================================
# Photo EXIF extract
# ============================================================
class TestPhotoExif:
    def test_extract_exif_after_upload(self, admin_headers, project_id):
        # Create a tiny PNG
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), color=(255, 0, 0)).save(buf, "PNG")
        buf.seek(0)
        files = {"file": ("test_iter6.png", buf, "image/png")}
        data = {"project_id": project_id, "caption": "TEST_iter6_exif"}
        up = requests.post(f"{API}/photos/upload", headers=admin_headers,
                           files=files, data=data, timeout=30)
        assert up.status_code in (200, 201), up.text
        pid = up.json()["id"]

        ex = requests.post(f"{API}/photos/{pid}/extract-exif",
                           headers=admin_headers, timeout=30)
        assert ex.status_code == 200, ex.text
        body = ex.json()
        assert "extracted" in body
        assert "matches" in body
        assert isinstance(body["matches"], list)


# ============================================================
# Iter5 regression
# ============================================================
class TestIter5Regression:
    def test_marketplace_postings_create_and_list(self, admin_headers):
        title = f"TEST_iter6_posting_{uuid.uuid4().hex[:6]}"
        c = requests.post(f"{API}/marketplace/postings", headers=admin_headers,
                          json={"title": title,
                                "description": "test posting",
                                "posting_type": "sub_invitation"}, timeout=20)
        assert c.status_code in (200, 201), c.text
        pid = c.json()["id"]
        lst = requests.get(f"{API}/marketplace/postings", headers=admin_headers, timeout=20)
        assert lst.status_code == 200
        assert any(p["id"] == pid for p in lst.json())

    def test_notifications_endpoints(self, admin_headers):
        r = requests.get(f"{API}/notifications", headers=admin_headers, timeout=20)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        u = requests.get(f"{API}/notifications/unread-count", headers=admin_headers, timeout=20)
        assert u.status_code == 200
        assert "count" in u.json() or isinstance(u.json(), dict)

    def test_search(self, admin_headers):
        r = requests.get(f"{API}/search", params={"q": "GSA"},
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200
        data = r.json()
        # Expect projects + tasks keys (or list)
        assert isinstance(data, (dict, list))

    def test_weather_now(self, admin_headers):
        r = requests.get(f"{API}/weather/now",
                         params={"lat": 38.9, "lng": -77.0},
                         headers=admin_headers, timeout=30)
        # External API; allow flaky 502/504 with a re-skip
        if r.status_code in (502, 503, 504):
            pytest.skip(f"weather upstream {r.status_code}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert isinstance(d, dict)

    def test_productivity_rates(self, admin_headers, project_id):
        r = requests.get(f"{API}/productivity-rates",
                         params={"project_id": project_id, "days": 90},
                         headers=admin_headers, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        # Should include rates list (key may vary)
        assert isinstance(data, (list, dict))

    def test_cost_rates_create_list(self, admin_headers):
        body = {
            "code": f"TEST_R_{uuid.uuid4().hex[:6]}",
            "name": "Carpenter labor",
            "unit": "hour",
            "base_rate_usd": 75.0,
            "prevailing_wage_rate_usd": 95.0,
            "trade_code": "03 30 00",
        }
        c = requests.post(f"{API}/cost-rates", headers=admin_headers,
                          json=body, timeout=20)
        if c.status_code == 404:
            pytest.skip("cost-rates not exposed")
        assert c.status_code in (200, 201), c.text
        lst = requests.get(f"{API}/cost-rates", headers=admin_headers, timeout=20)
        assert lst.status_code == 200
        assert any(r["code"] == body["code"] for r in lst.json())

    def test_work_item_history(self, admin_headers, project_id):
        # Find a work item
        wis = requests.get(f"{API}/projects/{project_id}/work-items",
                           headers=admin_headers, timeout=20)
        if wis.status_code != 200 or not wis.json():
            pytest.skip("no work items")
        wid = wis.json()[0]["id"]
        # Patch status (toggle)
        cur_status = wis.json()[0].get("status", "open")
        new_status = "in_progress" if cur_status != "in_progress" else "open"
        p = requests.patch(f"{API}/work-items/{wid}", headers=admin_headers,
                           json={"status": new_status}, timeout=20)
        assert p.status_code == 200, p.text
        h = requests.get(f"{API}/work-items/{wid}/history",
                         headers=admin_headers, timeout=20)
        assert h.status_code == 200
        assert isinstance(h.json(), list)
        assert len(h.json()) >= 1
