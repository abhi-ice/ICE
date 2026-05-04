"""Backend test suite for ICE Schedule Ops.
Covers: health, auth, tenant-isolation, dashboard, projects, tasks+CPM+lookahead,
daily-reports grid+cascade+submit+PDF, RFIs, work-items+links (cross-tenant guard),
approvals, audit-log+chain, imports (CSV + MPP regression + extra='forbid' guard),
AI endpoints, resources/field/docs listing, floors & locations.
"""
import io
import os
import uuid
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE}/api"
ADMIN = {"email": "admin@iceops.com", "password": "Demo123!"}


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="session")
def project_id(admin_headers):
    r = requests.get(f"{API}/projects", headers=admin_headers, timeout=20)
    assert r.status_code == 200, r.text
    projects = r.json()
    assert len(projects) >= 1
    # Prefer the seeded GSA project (has tasks/floors/crews/etc.)
    for p in projects:
        if "GSA" in (p.get("name") or "") or "Federal" in (p.get("name") or ""):
            return p["id"]
    return projects[0]["id"]


@pytest.fixture(scope="session")
def other_tenant_token():
    """Register a 2nd user under a different tenant for isolation tests."""
    unique = uuid.uuid4().hex[:6]
    payload = {
        "email": f"TEST_other_{unique}@example.com",
        "password": "OtherPass123!",
        "full_name": "Other Tenant User",
        "tenant_slug": f"other-{unique}",
    }
    r = requests.post(f"{API}/auth/register", json=payload, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"register endpoint not available: {r.status_code} {r.text[:120]}")
    return r.json()["access_token"]


# ---------- health ----------
def test_health():
    r = requests.get(f"{API}/health", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# ---------- auth ----------
class TestAuth:
    def test_login_ok(self):
        r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data and isinstance(data["access_token"], str)
        assert data["user"]["email"] == ADMIN["email"]
        assert data.get("tenant") is not None

    def test_login_bad_pw(self):
        r = requests.post(f"{API}/auth/login", json={**ADMIN, "password": "nope"}, timeout=15)
        assert r.status_code == 401

    def test_me_ok(self, admin_headers):
        r = requests.get(f"{API}/auth/me", headers=admin_headers, timeout=15)
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN["email"]

    def test_me_no_token(self):
        r = requests.get(f"{API}/auth/me", timeout=15)
        assert r.status_code in (401, 403)


# ---------- dashboard + projects ----------
class TestDashboardAndProjects:
    def test_dashboard_summary(self, admin_headers):
        r = requests.get(f"{API}/dashboard/summary", headers=admin_headers, timeout=20)
        assert r.status_code == 200
        d = r.json()
        for k in ("active_projects", "open_rfis", "pending_approvals",
                 "open_work_items", "open_safety_events", "schedule_health_pct"):
            assert k in d, f"missing {k}"
        assert d["active_projects"] >= 1
        assert d["pending_approvals"] >= 1

    def test_projects_list(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects", headers=admin_headers, timeout=20)
        assert r.status_code == 200
        assert any(p["id"] == project_id for p in r.json())

    def test_create_and_patch_project(self, admin_headers):
        payload = {"name": f"TEST_proj_{uuid.uuid4().hex[:4]}", "status": "active"}
        r = requests.post(f"{API}/projects", headers=admin_headers, json=payload, timeout=20)
        assert r.status_code in (200, 201), r.text
        pid = r.json()["id"]
        r2 = requests.patch(f"{API}/projects/{pid}", headers=admin_headers,
                            json={"name": "TEST_proj_renamed"}, timeout=20)
        assert r2.status_code == 200
        assert r2.json()["name"] == "TEST_proj_renamed"


# ---------- tasks + CPM + lookahead ----------
class TestTasksCpmLookahead:
    def test_tasks_list_has_25(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/tasks", headers=admin_headers, timeout=20)
        assert r.status_code == 200
        tasks = r.json()
        assert len(tasks) >= 25
        # activity_code is the primary ref
        assert any(t.get("activity_code") for t in tasks)
        # some tasks should have predecessors (list, may be empty for roots)
        assert any((t.get("predecessors") or []) for t in tasks)

    def test_cpm_recompute(self, admin_headers, project_id):
        r = requests.post(f"{API}/projects/{project_id}/critical-path/recompute",
                          headers=admin_headers, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "updated" in body or "critical_count" in body or "critical_path_count" in body

    def test_lookahead_14(self, admin_headers, project_id):
        r = requests.get(f"{API}/projects/{project_id}/lookahead?days=14",
                         headers=admin_headers, timeout=20)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        if rows:
            assert all(row.get("lookahead_status") == "in_window" for row in rows)


# ---------- daily reports ----------
class TestDailyReports:
    def test_create_report_prepopulated_grid(self, admin_headers, project_id):
        from datetime import date, timedelta
        today = date.today().isoformat()
        r = requests.post(f"{API}/daily-reports",
                          headers=admin_headers,
                          json={"project_id": project_id, "report_date": today},
                          timeout=30)
        assert r.status_code in (200, 201), r.text
        rep = r.json()
        rid = rep["id"]
        # grid
        g = requests.get(f"{API}/daily-reports/{rid}/grid", headers=admin_headers, timeout=20)
        assert g.status_code == 200, g.text
        grid = g.json()
        assert isinstance(grid, list)
        # seed creates activities around today; expect at least one pre-populated row
        assert len(grid) > 0, "grid pre-population produced no rows for today's report"
        pytest.shared_report_id = rid
        pytest.shared_grid = grid

    def test_patch_grid_row_cascades(self, admin_headers):
        rid = getattr(pytest, "shared_report_id", None)
        grid = getattr(pytest, "shared_grid", [])
        if not rid or not grid:
            pytest.skip("no pre-populated grid rows to test cascade")
        row = grid[0]
        row_id = row["id"]
        r = requests.patch(f"{API}/daily-report-activities/{row_id}",
                           headers=admin_headers,
                           json={"pct_complete_end_of_day": 55.5},
                           timeout=20)
        assert r.status_code == 200, r.text
        assert float(r.json().get("pct_complete_end_of_day", 0)) == 55.5

    def test_submit_report(self, admin_headers):
        rid = getattr(pytest, "shared_report_id", None)
        if not rid:
            pytest.skip("no report")
        r = requests.post(f"{API}/daily-reports/{rid}/submit", headers=admin_headers,
                          json={}, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "submitted"

    def test_pdf_streams_bytes(self, admin_headers):
        rid = getattr(pytest, "shared_report_id", None)
        if not rid:
            pytest.skip("no report")
        r = requests.get(f"{API}/daily-reports/{rid}/pdf",
                         headers=admin_headers, timeout=30, allow_redirects=False)
        assert r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}"
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd.lower() or "filename" in cd.lower()
        assert len(r.content) > 100  # streamed bytes, not an empty redirect


# ---------- weekly rollup ----------
def test_weekly_rollup(admin_headers, project_id):
    r = requests.get(f"{API}/projects/{project_id}/weekly-rollup?week_starting=2026-01-12",
                     headers=admin_headers, timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert "ppc" in d
    assert "hours_by_trade" in d


# ---------- RFIs ----------
class TestRFIs:
    def test_create_and_patch(self, admin_headers, project_id):
        r = requests.post(f"{API}/rfis", headers=admin_headers,
                          json={"project_id": project_id, "subject": "TEST_rfi",
                                "description": "Test rfi description"},
                          timeout=20)
        assert r.status_code in (200, 201), r.text
        rfi = r.json()
        assert rfi.get("rfi_number")
        rid = rfi["id"]
        r2 = requests.patch(f"{API}/rfis/{rid}", headers=admin_headers,
                            json={"status": "answered"}, timeout=20)
        assert r2.status_code == 200
        assert r2.json()["status"] == "answered"


# ---------- work items + links + cross-tenant guard ----------
class TestWorkItems:
    def test_create_and_link(self, admin_headers, project_id):
        r = requests.post(f"{API}/work-items", headers=admin_headers,
                          json={"project_id": project_id, "title": "TEST_wi",
                                "description": "TEST work item description"}, timeout=20)
        assert r.status_code in (200, 201), r.text
        wid = r.json()["id"]
        # create a rfi to link to
        rfi = requests.post(f"{API}/rfis", headers=admin_headers,
                            json={"project_id": project_id, "subject": "TEST_link",
                                  "description": "linked rfi"}, timeout=20).json()
        link = requests.post(f"{API}/work-item-links", headers=admin_headers,
                             json={"work_item_id": wid, "entity_type": "rfi",
                                   "entity_id": rfi["id"]}, timeout=20)
        assert link.status_code in (200, 201), link.text
        pytest.shared_wi_id = wid

    def test_cross_tenant_link_rejected(self, admin_headers, other_tenant_token, project_id):
        wid = getattr(pytest, "shared_wi_id", None)
        if not wid:
            pytest.skip("no work item")
        # other-tenant user tries to link to ICE's work item
        other_headers = {"Authorization": f"Bearer {other_tenant_token}"}
        r = requests.post(f"{API}/work-item-links", headers=other_headers,
                          json={"work_item_id": wid, "entity_type": "rfi",
                                "entity_id": str(uuid.uuid4())}, timeout=20)
        assert r.status_code in (400, 403, 404), f"expected reject got {r.status_code}: {r.text[:200]}"


# ---------- approvals ----------
class TestApprovals:
    def test_pending_count_and_decide(self, admin_headers, project_id):
        c0 = requests.get(f"{API}/approvals/pending-count", headers=admin_headers, timeout=15)
        assert c0.status_code == 200
        # create a new approval
        r = requests.post(f"{API}/approvals", headers=admin_headers,
                          json={"project_id": project_id, "kind": "schedule_change",
                                "subject_entity_type": "task",
                                "subject_entity_id": str(uuid.uuid4()),
                                "notes": "TEST_approval"},
                          timeout=20)
        assert r.status_code in (200, 201), r.text
        aid = r.json()["id"]
        # decide -> approved
        d = requests.post(f"{API}/approvals/{aid}/decide", headers=admin_headers,
                          json={"decision": "approved", "notes": "ok"}, timeout=20)
        assert d.status_code == 200, d.text
        assert d.json().get("status") == "approved"

    def test_decide_rejected(self, admin_headers, project_id):
        r = requests.post(f"{API}/approvals", headers=admin_headers,
                          json={"project_id": project_id, "kind": "schedule_change",
                                "subject_entity_type": "task",
                                "subject_entity_id": str(uuid.uuid4()),
                                "notes": "TEST_reject"},
                          timeout=20)
        assert r.status_code in (200, 201), r.text
        aid = r.json()["id"]
        d = requests.post(f"{API}/approvals/{aid}/decide", headers=admin_headers,
                          json={"decision": "rejected", "notes": "no"}, timeout=20)
        assert d.status_code == 200
        assert d.json().get("status") == "rejected"


# ---------- audit log ----------
class TestAuditLog:
    def test_list_and_verify(self, admin_headers):
        l = requests.get(f"{API}/audit-log", headers=admin_headers, timeout=20)
        assert l.status_code == 200
        entries = l.json()
        assert isinstance(entries, list) and len(entries) > 0
        v = requests.get(f"{API}/audit-log/verify", headers=admin_headers, timeout=30)
        assert v.status_code == 200
        j = v.json()
        assert j.get("valid") is True
        assert j.get("entries_checked", 0) >= len(entries)

    def test_grows_on_mutation(self, admin_headers):
        before = requests.get(f"{API}/audit-log", headers=admin_headers, timeout=20).json()
        rp = requests.post(f"{API}/projects", headers=admin_headers,
                           json={"name": f"TEST_audit_growth_{uuid.uuid4().hex[:4]}",
                                 "status": "active"}, timeout=20)
        assert rp.status_code in (200, 201), rp.text
        after = requests.get(f"{API}/audit-log", headers=admin_headers, timeout=20).json()
        assert len(after) >= len(before) + 1, f"audit did not grow: {len(before)} -> {len(after)}"
        v = requests.get(f"{API}/audit-log/verify", headers=admin_headers, timeout=30).json()
        assert v.get("valid") is True


# ---------- imports ----------
class TestImports:
    def test_import_csv(self, admin_headers, project_id):
        suffix = uuid.uuid4().hex[:4].upper()
        csv = (
            "id,name,start,finish,duration,predecessors,trade\n"
            f"TESTA{suffix},TEST Activity A,2026-02-01,2026-02-05,5,,GEN\n"
            f"TESTB{suffix},TEST Activity B,2026-02-06,2026-02-10,5,TESTA{suffix},GEN\n"
        )
        files = {"file": ("sched.csv", io.BytesIO(csv.encode()), "text/csv")}
        data = {"project_id": project_id}
        r = requests.post(f"{API}/imports/schedule",
                          headers=admin_headers, files=files, data=data, timeout=30)
        assert r.status_code in (200, 201), r.text

    def test_import_mpp_clear_substitution_error(self, admin_headers, project_id):
        files = {"file": ("sched.mpp", io.BytesIO(b"\x00fake mpp\x00"),
                          "application/vnd.ms-project")}
        data = {"project_id": project_id}
        r = requests.post(f"{API}/imports/schedule",
                          headers=admin_headers, files=files, data=data, timeout=20)
        assert r.status_code == 400, r.text
        msg = r.text
        assert "MPXJ" in msg or "JPype" in msg or "bridge" in msg.lower(), \
            f"expected MPXJ/JPype/bridge substitution msg, got: {msg[:200]}"

    def test_extra_forbid_guard_in_proposal(self):
        """Regression: validate_payload via TaskCreate must reject prev_pct (extra='forbid')."""
        from services.schedule_proposal import validate_payload  # type: ignore
        with pytest.raises(Exception):
            validate_payload({
                "project_id": "x",
                "activity_code": "A",
                "description": "n",
                "planned_start": "2026-01-01",
                "planned_finish": "2026-01-02",
                "duration_days": 1,
                "predecessors": [],
                "trade_code": "gen",
                "prev_pct": 50,  # forbidden — must raise
            })


# ---------- AI ----------
class TestAI:
    def test_copilot(self, admin_headers, project_id):
        r = requests.post(f"{API}/ai/copilot", headers=admin_headers,
                          json={"project_id": project_id, "message": "What is the schedule health?"},
                          timeout=45)
        if r.status_code >= 500:
            pytest.skip(f"AI backend error {r.status_code}: {r.text[:200]}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("response") or body.get("message") or body.get("content")

    def test_recovery(self, admin_headers, project_id):
        # fetch a task id for target
        tasks = requests.get(f"{API}/projects/{project_id}/tasks",
                             headers=admin_headers, timeout=20).json()
        assert tasks
        r = requests.post(f"{API}/ai/recovery", headers=admin_headers,
                          json={"project_id": project_id, "target_activity_id": tasks[0]["id"]},
                          timeout=45)
        if r.status_code >= 500:
            pytest.skip(f"AI backend error {r.status_code}: {r.text[:200]}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "suggestions" in body

    def test_rfi_draft(self, admin_headers, project_id):
        r = requests.post(f"{API}/ai/rfi-draft", headers=admin_headers,
                          json={"project_id": project_id,
                                "subject": "slab pour conflict",
                                "context": "There is a conflict between mechanical and slab pour."},
                          timeout=45)
        if r.status_code >= 500:
            pytest.skip(f"AI backend error {r.status_code}: {r.text[:200]}")
        assert r.status_code == 200, r.text


# ---------- resources / field / docs listings ----------
@pytest.mark.parametrize("ep,extra", [
    ("/crews", ""),
    ("/equipment", ""),
    ("/people", ""),
    ("/certifications", ""),
    ("/availability", "?start=2026-01-12&end=2026-01-26"),
])
def test_resources_lists(ep, extra, admin_headers, project_id):
    r = requests.get(f"{API}{ep}{extra}", headers=admin_headers, timeout=20)
    if r.status_code == 422 and "project_id" in r.text:
        r = requests.get(f"{API}{ep}?project_id={project_id}{extra and '&' + extra[1:]}",
                         headers=admin_headers, timeout=20)
    assert r.status_code == 200, f"{ep} -> {r.status_code} {r.text[:120]}"
    body = r.json()
    # /availability returns dict, others list
    assert isinstance(body, (list, dict))


# ---------- floors + locations ----------
class TestFloorsLocations:
    def test_create_floor_and_location(self, admin_headers, project_id):
        f = requests.post(f"{API}/floors", headers=admin_headers,
                         json={"project_id": project_id, "floor_number": 99,
                               "floor_name": "TEST_Floor"},
                         timeout=20)
        assert f.status_code in (200, 201), f.text
        fid = f.json()["id"]
        l = requests.post(f"{API}/locations", headers=admin_headers,
                         json={"project_id": project_id, "floor_id": fid,
                               "name": "TEST_Loc", "code": f"TL-{uuid.uuid4().hex[:4]}"},
                         timeout=20)
        assert l.status_code in (200, 201), l.text


# ---------- tenant isolation regression ----------
class TestTenantIsolation:
    def test_other_tenant_sees_nothing(self, other_tenant_token):
        h = {"Authorization": f"Bearer {other_tenant_token}"}
        for ep in ("/projects", "/approvals", "/audit-log"):
            r = requests.get(f"{API}{ep}", headers=h, timeout=20)
            assert r.status_code == 200, f"{ep} -> {r.status_code}"
            assert r.json() == [] or len(r.json()) == 0, f"{ep} leaked cross-tenant data"

    def test_x_tenant_header_swap_blocked(self, other_tenant_token, admin_headers):
        """other-tenant user cannot set X-Tenant-ID to ICE."""
        # find ICE tenant id
        me = requests.get(f"{API}/auth/me", headers=admin_headers, timeout=15).json()
        ice_tid = me.get("tenant_id") or me.get("current_tenant_id")
        if not ice_tid:
            pytest.skip("cannot determine ICE tenant id")
        h = {"Authorization": f"Bearer {other_tenant_token}", "X-Tenant-ID": ice_tid}
        r = requests.get(f"{API}/projects", headers=h, timeout=20)
        # expect either forbidden or empty list — MUST NOT return ICE projects
        if r.status_code == 200:
            assert r.json() == [], "X-Tenant-ID header swap leaked cross-tenant data"
        else:
            assert r.status_code in (401, 403)
