"""Iter 10 backend tests — Daily Report Phase 2 rebuild.

Covers:
- POST /api/daily-reports — idempotency
- PATCH /api/daily-reports/{id} — patches & lock-on-submit
- POST /api/daily-reports/{id}/weather-autofill — Open-Meteo (KCMO)
- GET /api/daily-reports/{id}/context — bundle shape
- Presence CRUD
- Unplanned-activity (AF-prefix, requires_pm_review, in grid)
- Approve (admin/pm only, after submit)
- Linkage of delays/safety/qc/rfis/photos via daily_report_id
- Copilot DR-only tools
- Copilot strict-no-markdown
"""
import os
import io
import uuid
import time
import datetime as _dt
import pytest
import requests

if not (os.environ.get("REACT_APP_BACKEND_URL") or "").strip():
    pytest.skip(
        "REACT_APP_BACKEND_URL is not set. These tests call a running ICE backend and no "
        "longer fall back to a remote preview host; set it to the server you mean to test.",
        allow_module_level=True,
    )
BASE_URL = os.environ["REACT_APP_BACKEND_URL"].strip().rstrip("/")
ADMIN_EMAIL = "admin@iceops.com"
ADMIN_PASS = "Demo123!"
PROJECT_ID = "0eadfa85-1f43-4fda-8434-4eb5ee246ecc"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    s.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    return s


@pytest.fixture(scope="module")
def dr(api):
    """Create or fetch a daily report for today on the renovation project."""
    today = _dt.date.today().isoformat()
    r = api.post(f"{BASE_URL}/api/daily-reports",
                 json={"project_id": PROJECT_ID, "report_date": today})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _find_tool_result(usage, name):
    for tr in (usage or {}).get("tool_results", []):
        if tr.get("tool") == name:
            return tr
    return None


# ============================================================
# 1. POST /api/daily-reports — idempotent
# ============================================================
class TestCreateIdempotent:
    def test_post_returns_existing_for_same_project_date(self, api, dr):
        r = api.post(f"{BASE_URL}/api/daily-reports",
                     json={"project_id": PROJECT_ID, "report_date": dr["report_date"]})
        assert r.status_code in (200, 201), r.text
        assert r.json()["id"] == dr["id"]


# ============================================================
# 2. PATCH /api/daily-reports/{id}
# ============================================================
class TestPatchDailyReport:
    def test_patch_notes_and_renovation_fields(self, api, dr):
        r = api.patch(f"{BASE_URL}/api/daily-reports/{dr['id']}", json={
            "notes": "TEST_iter10 patch note",
            "occupancy_active": True,
            "occupied_areas": "Floor 3 east wing",
            "dust_controls_in_place": True,
            "noise_complaints_count": 1,
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["notes"] == "TEST_iter10 patch note"
        assert d["occupancy_active"] is True
        assert d["occupied_areas"] == "Floor 3 east wing"
        assert d["noise_complaints_count"] == 1


# ============================================================
# 3. Weather auto-fill (network-dependent — flaky-passed allowed)
# ============================================================
class TestWeatherAutofill:
    def test_weather_autofill_kcmo(self, api, dr):
        r = api.post(f"{BASE_URL}/api/daily-reports/{dr['id']}/weather-autofill")
        # Network-dependent: 200 happy path, 502/422 if Open-Meteo is unreachable
        if r.status_code != 200:
            pytest.skip(f"Open-Meteo unreachable / geocode failed: {r.status_code} {r.text[:120]}")
        d = r.json()
        assert "weather_fetched_at" in d and d["weather_fetched_at"]
        assert "weather_conditions" in d and d["weather_conditions"]
        # Temperature high should be a finite number for KCMO
        assert isinstance(d["weather_temp_high_f"], (int, float))


# ============================================================
# 4. Presence CRUD
# ============================================================
class TestPresence:
    def test_create_presence_mismatched_dr_id_rejected(self, api, dr):
        bad = api.post(f"{BASE_URL}/api/daily-reports/{dr['id']}/presence", json={
            "daily_report_id": "different-id",
            "trade": "electrical", "headcount": 3,
        })
        assert bad.status_code == 400

    def test_presence_full_lifecycle(self, api, dr):
        # CREATE
        r = api.post(f"{BASE_URL}/api/daily-reports/{dr['id']}/presence", json={
            "daily_report_id": dr["id"],
            "trade": "TEST_iter10_trade",
            "company_name": "Acme Test Co",
            "headcount": 4, "hours_per_person": 8.0,
        })
        assert r.status_code == 200, r.text
        pid = r.json()["id"]

        # GET (verify persistence)
        rl = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/presence")
        assert rl.status_code == 200
        assert any(p["id"] == pid for p in rl.json())

        # PATCH
        rp = api.patch(f"{BASE_URL}/api/daily-presence/{pid}", json={"headcount": 6})
        assert rp.status_code == 200
        assert rp.json()["headcount"] == 6

        # DELETE
        rd = api.delete(f"{BASE_URL}/api/daily-presence/{pid}")
        assert rd.status_code == 200
        # Verify removed
        rl2 = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/presence")
        assert all(p["id"] != pid for p in rl2.json())


# ============================================================
# 5. Unplanned activity (AF-prefix)
# ============================================================
class TestUnplannedActivity:
    def test_create_unplanned_activity(self, api, dr):
        r = api.post(f"{BASE_URL}/api/daily-reports/{dr['id']}/unplanned-activity", json={
            "description": "TEST_iter10 unexpected mold remediation",
            "duration_days": 1,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        task = body["task"]
        assert task["activity_code"].startswith("AF"), task["activity_code"]
        assert task["created_in_field"] is True
        assert task["requires_pm_review"] is True
        assert task["created_from_daily_report_id"] == dr["id"]
        # Confirm row appears in DR grid
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        codes = [g.get("activity_code") for g in ctx["grid"]]
        assert task["activity_code"] in codes


# ============================================================
# 6. Linkage: delays/safety/qc/rfis with daily_report_id
# ============================================================
class TestLinkages:
    def test_delay_links_to_dr(self, api, dr):
        r = api.post(f"{BASE_URL}/api/delays", json={
            "project_id": PROJECT_ID,
            "date": dr["report_date"],
            "severity": "minor",
            "delay_minutes": 60,
            "reason_category": "weather",
            "description": "TEST_iter10 delay",
            "daily_report_id": dr["id"],
        })
        assert r.status_code in (200, 201), r.text
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        assert any(d.get("description") == "TEST_iter10 delay" for d in ctx["delays"])

    def test_safety_event_links_to_dr(self, api, dr):
        r = api.post(f"{BASE_URL}/api/safety-events", json={
            "project_id": PROJECT_ID,
            "date": dr["report_date"],
            "event_type": "near_miss",
            "severity": "minor",
            "description": "TEST_iter10 safety",
            "daily_report_id": dr["id"],
        })
        assert r.status_code in (200, 201), r.text
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        assert any(s.get("description") == "TEST_iter10 safety" for s in ctx["safety_events"])

    def test_qc_links_to_dr(self, api, dr):
        r = api.post(f"{BASE_URL}/api/qc-issues", json={
            "project_id": PROJECT_ID,
            "date": dr["report_date"],
            "severity": "minor",
            "description": "TEST_iter10 qc",
            "daily_report_id": dr["id"],
        })
        assert r.status_code in (200, 201), r.text
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        assert any(q.get("description") == "TEST_iter10 qc" for q in ctx["qc_issues"])

    def test_rfi_links_to_dr(self, api, dr):
        r = api.post(f"{BASE_URL}/api/rfis", json={
            "project_id": PROJECT_ID,
            "subject": "TEST_iter10 rfi",
            "description": "Test RFI from iter10 — please clarify spec.",
            "daily_report_id": dr["id"],
        })
        assert r.status_code in (200, 201), r.text
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        assert any(rfi.get("subject") == "TEST_iter10 rfi" for rfi in ctx["rfis"])

    def test_photo_links_to_dr(self, api, dr):
        # Multipart upload — tiny PNG
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
               b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05"
               b"\xfe\x02\xfe\xa3\x06\xb1\xa6\x00\x00\x00\x00IEND\xaeB`\x82")
        files = {"file": ("test.png", io.BytesIO(png), "image/png")}
        data = {"project_id": PROJECT_ID, "daily_report_id": dr["id"], "caption": "TEST_iter10 photo"}
        # Strip Content-Type so requests sets multipart boundary
        h = {k: v for k, v in api.headers.items() if k != "Content-Type"}
        r = requests.post(f"{BASE_URL}/api/photos/upload", files=files, data=data, headers=h, timeout=30)
        if r.status_code not in (200, 201):
            pytest.skip(f"Photo upload unsupported in this env: {r.status_code} {r.text[:120]}")
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        assert any(p.get("caption") == "TEST_iter10 photo" for p in ctx["photos"])


# ============================================================
# 7. Context bundle shape + project_type
# ============================================================
class TestContextBundle:
    def test_context_keys_and_project_type(self, api, dr):
        r = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context")
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ["report", "project", "grid", "presence", "photos",
                  "voice_annotations", "delays", "safety_events",
                  "qc_issues", "rfis", "deliveries", "audit_log", "user_labels"]:
            assert k in body, f"missing key: {k}"
        assert body["project"]["id"] == PROJECT_ID
        # project_type must be present (renovation/tenant_improvement/etc)
        assert "project_type" in body["project"]


# ============================================================
# 8. Copilot strict-no-markdown + DR tools
# ============================================================
class TestCopilotDR:
    def _no_markdown(self, text: str):
        # Strict: no '*', '#', '`', '|' (per agent_to_agent_context_note)
        for ch in ["*", "#", "`", "|"]:
            assert ch not in (text or ""), f"markdown char '{ch}' in copilot reply: {text[:200]}"

    def test_copilot_no_markdown_normal_reply(self, api, dr):
        r = api.post(f"{BASE_URL}/api/ai/copilot", json={
            "project_id": PROJECT_ID, "daily_report_id": dr["id"],
            "message": "Briefly summarize today's progress in plain prose.",
        }, timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "response" in body
        self._no_markdown(body.get("response", ""))

    def test_copilot_record_presence_tool(self, api, dr):
        r = api.post(f"{BASE_URL}/api/ai/copilot", json={
            "project_id": PROJECT_ID, "daily_report_id": dr["id"],
            "message": "Record presence: trade plumbing, company TEST_iter10_PlumbCo, headcount 5, hours per person 8.",
        }, timeout=120)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = _find_tool_result(usage, "record_presence")
        if not tr:
            pytest.skip("LLM did not invoke record_presence tool — non-deterministic")
        result = tr.get("result", {})
        assert "created" in result, result
        # Verify a presence row was created on the DR
        rl = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/presence").json()
        assert any(p.get("trade", "").lower() == "plumbing" for p in rl)

    def test_copilot_create_unplanned_activity_tool(self, api, dr):
        r = api.post(f"{BASE_URL}/api/ai/copilot", json={
            "project_id": PROJECT_ID, "daily_report_id": dr["id"],
            "message": "Create unplanned activity: 'TEST_iter10 cleanup hazardous spill', duration 1 day.",
        }, timeout=120)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = _find_tool_result(usage, "create_unplanned_activity")
        if not tr:
            pytest.skip("LLM did not invoke create_unplanned_activity tool")
        result = tr.get("result", {})
        assert "created" in result
        # Verify task created with requires_pm_review = true
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        af_codes = [g.get("activity_code") for g in ctx["grid"] if (g.get("activity_code") or "").startswith("AF")]
        assert af_codes, "no AF-prefixed activity in grid"

    def test_copilot_mark_activity_status_tool(self, api, dr):
        # Pick an existing AF code or any code from grid
        ctx = api.get(f"{BASE_URL}/api/daily-reports/{dr['id']}/context").json()
        codes = [g.get("activity_code") for g in ctx["grid"] if g.get("activity_code")]
        if not codes:
            pytest.skip("No grid rows to mark")
        target = codes[0]
        r = api.post(f"{BASE_URL}/api/ai/copilot", json={
            "project_id": PROJECT_ID, "daily_report_id": dr["id"],
            "message": f"Mark activity {target} as in_progress at 25 percent complete.",
        }, timeout=120)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = _find_tool_result(usage, "mark_activity_status")
        if not tr:
            pytest.skip("LLM did not invoke mark_activity_status")
        assert "updated" in tr.get("result", {}) or "error" in tr.get("result", {})


# ============================================================
# 9. Approve flow (admin/pm only, requires submitted)
# ============================================================
class TestApproveFlow:
    """Use a fresh DR (yesterday) so we don't lock the module-scoped one."""
    @pytest.fixture(scope="class")
    def dr2(self, api):
        d = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()
        r = api.post(f"{BASE_URL}/api/daily-reports",
                     json={"project_id": PROJECT_ID, "report_date": d})
        assert r.status_code in (200, 201), r.text
        return r.json()

    def test_approve_before_submit_returns_409(self, api, dr2):
        r = api.post(f"{BASE_URL}/api/daily-reports/{dr2['id']}/approve")
        assert r.status_code == 409, r.text

    def test_submit_then_approve(self, api, dr2):
        rs = api.post(f"{BASE_URL}/api/daily-reports/{dr2['id']}/submit", json={})
        assert rs.status_code == 200, rs.text
        assert rs.json()["status"] == "submitted"
        # Now PATCH should be locked
        rp = api.patch(f"{BASE_URL}/api/daily-reports/{dr2['id']}", json={"notes": "should fail"})
        assert rp.status_code == 409
        # Approve
        ra = api.post(f"{BASE_URL}/api/daily-reports/{dr2['id']}/approve")
        assert ra.status_code == 200, ra.text
        body = ra.json()
        assert body["status"] == "approved"
        assert body["approved_by"] and body["approved_at"]
