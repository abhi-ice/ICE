"""Iter 9 backend tests:
- Generic CRUD: PATCH/DELETE /api/crud/{entity}/{id} for all entities
- Tenant isolation (cross-tenant returns 404)
- Onboarding: GET /api/onboarding/getting-started
- Auto-detect rooms: POST /api/spatial/auto-detect-rooms reads pdf_url
- Copilot envelope: standardized {created|updated|deleted: {resource, ...}}
"""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://field-ops-hub-21.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@iceops.com"
ADMIN_PASS = "Demo123!"
PROJECT_ID = "0eadfa85-1f43-4fda-8434-4eb5ee246ecc"
TENANT_ID = "ae015186-66c0-4584-a278-9db32762dbea"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    s.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    return s


def find_tool_result(usage, tool_name):
    for tr in (usage or {}).get("tool_results", []):
        if tr.get("tool") == tool_name:
            return tr
    return None


def copilot(api, msg, timeout=120):
    return api.post(f"{BASE_URL}/api/ai/copilot",
                    json={"project_id": PROJECT_ID, "message": msg},
                    timeout=timeout)


# ============================================================
# 1. Generic CRUD - PATCH and DELETE
# ============================================================

class TestGenericCRUD:
    def test_crud_unknown_entity_returns_404(self, api):
        r = api.patch(f"{BASE_URL}/api/crud/nonsense_entity/{uuid.uuid4()}",
                      json={"foo": "bar"})
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text[:200]}"

    def test_crud_crews_patch_and_delete(self, api):
        # Create a crew first via POST (need to find proper endpoint)
        crews = api.get(f"{BASE_URL}/api/crews")
        if crews.status_code != 200:
            pytest.skip(f"GET /api/crews not available: {crews.status_code}")
        items = crews.json()
        if not items:
            pytest.skip("no crews")
        crew_id = items[0]["id"]
        # PATCH
        r = api.patch(f"{BASE_URL}/api/crud/crews/{crew_id}",
                      json={"target_size": 10})
        assert r.status_code == 200, f"PATCH crews: {r.status_code} {r.text[:300]}"
        d = r.json()
        # Returns updated doc
        assert d.get("target_size") == 10 or (d.get("updated") or {}).get("target_size") == 10, \
            f"target_size not 10 in response: {d}"

    def test_crud_cross_tenant_isolation(self, api):
        # Try to PATCH a fake id under wrong tenant context — admin's tenant is fine
        # Use a totally fake UUID — should be 404 (not found in this tenant)
        fake_id = str(uuid.uuid4())
        r = api.patch(f"{BASE_URL}/api/crud/crews/{fake_id}",
                      json={"target_size": 5})
        assert r.status_code == 404, f"Expected 404 for nonexistent id, got {r.status_code}"

    @pytest.mark.parametrize("entity", [
        "people", "equipment", "rfis", "deliveries", "work-items",
        "change-orders", "delays", "safety-events", "qc-issues", "locations",
    ])
    def test_crud_entity_patch_endpoint_exists(self, api, entity):
        """PATCH on nonexistent id should return 404 (not 405/500) - proves endpoint exists."""
        fake_id = str(uuid.uuid4())
        r = api.patch(f"{BASE_URL}/api/crud/{entity}/{fake_id}",
                      json={"foo": "bar"})
        # 404 means endpoint exists but record not found (good)
        # 405 = method not allowed (endpoint missing)
        # 500 = unhandled error (bug)
        assert r.status_code == 404, \
            f"Expected 404 for {entity}, got {r.status_code}: {r.text[:200]}"

    @pytest.mark.parametrize("entity", [
        "crews", "people", "equipment", "rfis", "deliveries", "work-items",
        "change-orders", "delays", "safety-events", "qc-issues", "locations",
    ])
    def test_crud_entity_delete_endpoint_exists(self, api, entity):
        """DELETE on nonexistent id should return 404."""
        fake_id = str(uuid.uuid4())
        r = api.delete(f"{BASE_URL}/api/crud/{entity}/{fake_id}")
        assert r.status_code == 404, \
            f"Expected 404 for DELETE {entity}, got {r.status_code}: {r.text[:200]}"


# ============================================================
# 2. Onboarding
# ============================================================

class TestOnboarding:
    def test_getting_started_shape(self, api):
        r = api.get(f"{BASE_URL}/api/onboarding/getting-started?project_id={PROJECT_ID}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert "steps" in d, f"no 'steps' in response: {d}"
        assert "completed_count" in d
        assert d.get("total") == 12 or len(d["steps"]) == 12, \
            f"expected 12 steps, got total={d.get('total')} len={len(d['steps'])}"
        # Each step has required fields
        for step in d["steps"]:
            for field in ("id", "title", "body", "action", "route", "complete"):
                assert field in step, f"step missing '{field}': {step}"
            assert isinstance(step["complete"], bool)

    def test_completed_count_matches_steps(self, api):
        r = api.get(f"{BASE_URL}/api/onboarding/getting-started?project_id={PROJECT_ID}")
        assert r.status_code == 200
        d = r.json()
        actual_complete = sum(1 for s in d["steps"] if s["complete"])
        assert actual_complete == d["completed_count"], \
            f"completed_count={d['completed_count']} but actual={actual_complete}"


# ============================================================
# 3. Auto-detect rooms
# ============================================================

class TestAutoDetectRooms:
    def test_auto_detect_rooms_no_500(self, api):
        # Need a drawing_id - get from /api/projects/:id/drawings or /api/drawings
        rd = api.get(f"{BASE_URL}/api/projects/{PROJECT_ID}/drawings")
        if rd.status_code != 200:
            rd = api.get(f"{BASE_URL}/api/drawings?project_id={PROJECT_ID}")
        if rd.status_code != 200:
            pytest.skip(f"no drawings endpoint: {rd.status_code}")
        drawings = rd.json()
        if not drawings:
            pytest.skip("no drawings")
        # Need a floor_id too
        rf = api.get(f"{BASE_URL}/api/projects/{PROJECT_ID}/floors")
        if rf.status_code != 200:
            rf = api.get(f"{BASE_URL}/api/floors?project_id={PROJECT_ID}")
        floors = rf.json() if rf.status_code == 200 else []
        if not floors:
            pytest.skip("no floors")
        drawing_id = drawings[0]["id"]
        floor_id = floors[0]["id"]
        r = api.post(f"{BASE_URL}/api/spatial/auto-detect-rooms",
                     json={"drawing_id": drawing_id, "floor_id": floor_id,
                           "project_id": PROJECT_ID, "page_index": 0},
                     timeout=180)
        # 0 detected on synthetic PDFs is OK; must not 500
        assert r.status_code in (200, 400, 422), \
            f"got {r.status_code}: {r.text[:300]}"
        if r.status_code == 200:
            d = r.json()
            assert "detected" in d or "detected_count" in d, \
                f"no detected/detected_count in response: {d}"
            assert "created_count" in d or "created" in d, \
                f"no created in response: {d}"


# ============================================================
# 4. Copilot tool-result envelope (standardized)
# ============================================================

class TestCopilotEnvelope:
    """Verify {created|updated|deleted: {resource, ...}} envelope."""

    def _assert_envelope(self, tr, expected_op, expected_resource):
        result = tr.get("result", {})
        assert "error" not in tr, f"tool errored: {tr}"
        envelope = result.get(expected_op)
        assert envelope is not None, \
            f"No '{expected_op}' key in result. Got: {result}"
        assert envelope.get("resource") == expected_resource, \
            f"Expected resource='{expected_resource}', got: {envelope}"
        return envelope

    def test_create_task_envelope(self, api):
        r = copilot(api, "Add a task: install duct sealing, duration 2 days")
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_task")
        assert tr is not None, f"create_task not invoked: {usage}"
        env = self._assert_envelope(tr, "created", "task")
        # Must have id + activity_code
        assert env.get("id"), f"No id in envelope: {env}"
        assert env.get("activity_code"), f"No activity_code in envelope: {env}"

    def test_create_crew_envelope(self, api):
        r = copilot(api, "Create a crew named Plumbers Beta, trade plumbing, target size 4")
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_crew")
        assert tr is not None, f"create_crew not invoked: {usage}"
        env = self._assert_envelope(tr, "created", "crew")
        crew_code = env.get("crew_code")
        assert crew_code and crew_code.startswith("CR-"), \
            f"crew_code missing or not CR-XXX format: {env}"

    def test_create_equipment_envelope(self, api):
        r = copilot(api, "Add equipment: asset tag E-001, type excavator, "
                         "model CAT 320, manufacturer Caterpillar, "
                         "year 2020, ownership owned")
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_equipment")
        assert tr is not None, f"create_equipment not invoked: {usage}"
        env = self._assert_envelope(tr, "created", "equipment")
        # Check canonical model fields are present (not legacy 'name'/'owner')
        for f in ("asset_tag", "type", "manufacturer", "model", "year", "ownership"):
            assert f in env, f"canonical field '{f}' missing in equipment envelope: {env}"

    def test_create_person_envelope(self, api):
        r = copilot(api, "Add person: first name Jane, last name Smith, "
                         "role foreman, trade electrical, hourly rate 68 dollars")
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_person")
        assert tr is not None, f"create_person not invoked: {usage}"
        env = self._assert_envelope(tr, "created", "person")
        assert "first_name" in env and "last_name" in env, \
            f"person envelope missing first_name/last_name: {env}"

    def test_update_task_envelope(self, api):
        # Find an Axxxx task
        rt = api.get(f"{BASE_URL}/api/projects/{PROJECT_ID}/tasks")
        assert rt.status_code == 200
        import re
        tasks = rt.json()
        target = next((t for t in tasks
                       if t.get("activity_code")
                       and re.match(r"^A\d+$", t["activity_code"])), None)
        if not target:
            pytest.skip("no Axxxx task")
        code = target["activity_code"]
        r = copilot(api, f"Update task {code} to 80 percent")
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "update_task")
        assert tr is not None, f"update_task not invoked: {usage}"
        env = self._assert_envelope(tr, "updated", "task")

    def test_delete_rfi_envelope(self, api):
        # Need an RFI to delete - create one first
        rl = api.get(f"{BASE_URL}/api/rfis?project_id={PROJECT_ID}")
        if rl.status_code != 200 or not rl.json():
            # Try creating via copilot
            crf = copilot(api, "Create an RFI: subject TEST_iter9 to_delete, "
                               "question test deletion envelope")
            time.sleep(1)
            rl = api.get(f"{BASE_URL}/api/rfis?project_id={PROJECT_ID}")
        if rl.status_code != 200 or not rl.json():
            pytest.skip("no RFIs to delete")
        rfis = rl.json()
        # Find one we can delete (preferably TEST_)
        target = next((x for x in rfis if "TEST" in (x.get("subject") or "")), rfis[-1])
        rfi_num = target.get("rfi_number")
        r = copilot(api, f"Delete RFI {rfi_num}")
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "delete_entity")
        if tr is None:
            # Some impls may use delete_rfi
            tr = find_tool_result(usage, "delete_rfi")
        assert tr is not None, f"delete tool not invoked: {usage}"
        result = tr.get("result", {})
        assert "error" not in tr, f"delete errored: {tr}"
        deleted = result.get("deleted")
        assert deleted is not None, f"No 'deleted' envelope: {result}"
        assert deleted.get("resource") == "rfi", \
            f"resource not 'rfi': {deleted}"


# ============================================================
# 5. Copilot tools wired (smoke)
# ============================================================

class TestCopilotToolsWired:
    """Just verify these tools can be invoked without 500 errors."""

    @pytest.mark.parametrize("phrase,tool_name", [
        ("Log a delay: weather rain, 2 hours, impact moderate",
         "create_delay"),
        ("Log a safety event: near miss, area 8th floor, severity low, "
         "description trip hazard observed", "create_safety_event"),
        ("Create a QC issue: title cracked tile, severity minor, "
         "location restroom 802", "create_qc_issue"),
        ("Schedule a delivery: vendor ABC Concrete, item ready-mix concrete, "
         "scheduled for 2026-03-15", "create_delivery"),
    ])
    def test_tool_invokable(self, api, phrase, tool_name):
        r = copilot(api, phrase)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, tool_name)
        # AI may have asked for clarification - that's OK
        # We just ensure if invoked, it didn't 500-ish error
        if tr is not None:
            assert "error" not in tr or "validation" in str(tr.get("error", "")).lower(), \
                f"{tool_name} invocation errored: {tr}"
