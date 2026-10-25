"""Iter 8 backend tests — MPP/XER schedule import (real MPXJ JVM),
AI Copilot READ+WRITE tool-use, no-markdown rule, audit, role gating."""
import os
import re
import time
import json
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
TENANT_SLUG = "ice"

# ------------ Shared fixtures ------------

@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    d = r.json()
    api.headers.update({"Authorization": f"Bearer {d['access_token']}"})
    return {"token": d["access_token"], "user": d["user"]}


@pytest.fixture(scope="module")
def project(api, auth):
    r = api.get(f"{BASE_URL}/api/projects")
    assert r.status_code == 200
    projs = r.json()
    assert projs
    # Prefer the seeded GSA project
    for p in projs:
        if p["id"] == "0eadfa85-1f43-4fda-8434-4eb5ee246ecc":
            return p
    return projs[0]


# ------------ Helpers ------------

MD_PATTERNS = [
    (r"\*\*[^*\n]+\*\*", "bold **"),
    (r"^\s{0,3}#{1,6}\s+", "ATX header"),
    (r"`[^`\n]+`", "inline backticks"),
    (r"^\s*[-*+]\s+\S", "bullet list marker"),
    (r"\|.*\|.*\|", "table pipes"),
    (r"\[[^\]]+\]\([^)]+\)", "markdown link"),
    (r"```", "fenced code"),
]


def assert_no_markdown(text: str):
    """Returns list of violations (empty if clean)."""
    violations = []
    for pat, name in MD_PATTERNS:
        if re.search(pat, text, flags=re.MULTILINE):
            violations.append(f"{name}: matched /{pat}/")
    return violations


def copilot_post(api, project_id, message, timeout=120):
    r = api.post(f"{BASE_URL}/api/ai/copilot",
                 json={"project_id": project_id, "message": message},
                 timeout=timeout)
    return r


def find_tool_result(usage, tool_name):
    for tr in (usage or {}).get("tool_results", []):
        if tr.get("tool") == tool_name:
            return tr
    return None


# ============================================================
# 1. MPP/XER PARSER — real MPXJ via JPype
# ============================================================

class TestScheduleImportMPP:
    def test_import_real_mpx(self, api, project):
        """POST /api/imports/schedule with /tmp/test_real.mpx"""
        if not os.path.exists("/tmp/test_real.mpx"):
            pytest.skip("/tmp/test_real.mpx not present")
        with open("/tmp/test_real.mpx", "rb") as f:
            content = f.read()
        # Multipart: must NOT use json content-type
        s = requests.Session()
        s.headers.update({"Authorization": api.headers["Authorization"]})
        r = s.post(
            f"{BASE_URL}/api/imports/schedule",
            files={"file": ("test_real.mpx", content, "application/octet-stream")},
            data={"project_id": project["id"]},
            timeout=120,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["row_count"] > 0, f"row_count must be > 0; got {d}"
        assert (d.get("created", 0) + d.get("updated", 0)) > 0
        assert "import_id" in d

    def test_import_xer_graceful(self, api, project):
        """Posting a synthetic .xer (empty) should not 500 — graceful 400 or 200."""
        s = requests.Session()
        s.headers.update({"Authorization": api.headers["Authorization"]})
        # Minimal fake XER content (will fail to parse, but must not 500 unhandled)
        fake = b"ERMHDR\t14.7\nPROJECT\nENDPROJECT\n"
        r = s.post(
            f"{BASE_URL}/api/imports/schedule",
            files={"file": ("test.xer", fake, "application/octet-stream")},
            data={"project_id": project["id"]},
            timeout=120,
        )
        # Acceptable: 200 (parsed 0 rows), 400 (graceful errors), 422
        # NOT acceptable: 500 unhandled exception
        assert r.status_code in (200, 400, 422), f"got {r.status_code}: {r.text[:300]}"

    def test_csv_xlsx_regression_not_broken(self, api, project):
        """Regression: synthetic CSV import path still works (or returns clean error)."""
        s = requests.Session()
        s.headers.update({"Authorization": api.headers["Authorization"]})
        csv = (
            b"activity_code,description,planned_start,planned_finish,duration_days\n"
            b"TESTCSV001,TEST_iter8 csv regression task,2026-07-01,2026-07-05,4\n"
        )
        r = s.post(
            f"{BASE_URL}/api/imports/schedule",
            files={"file": ("regression.csv", csv, "text/csv")},
            data={"project_id": project["id"]},
            timeout=60,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["row_count"] >= 1


# ============================================================
# 2. COPILOT — READ + no-markdown
# ============================================================

class TestCopilotRead:
    def test_read_status_no_markdown(self, api, project):
        r = copilot_post(api, project["id"],
                         "What is the status of the project right now?")
        assert r.status_code == 200, r.text
        d = r.json()
        text = d.get("response", "")
        assert text and len(text) > 5
        violations = assert_no_markdown(text)
        # Allow tolerant failure: report but don't hard-fail on inline backticks
        # in [A1234] codes (square brackets are NOT markdown)
        assert not violations, f"Markdown found in copilot reply: {violations}\nReply:\n{text[:500]}"


# ============================================================
# 3. COPILOT — WRITE tool-use
# ============================================================

class TestCopilotWrite:
    created_task_code = None

    def test_create_task(self, api, project):
        msg = ("Add a new task: install conference room AV equipment, "
               "planned start 2026-06-01, duration 4 days")
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200, r.text
        d = r.json()
        usage = d.get("usage", {})
        tr = find_tool_result(usage, "create_task")
        assert tr is not None, f"create_task not in tool_results: {usage}"
        assert "error" not in tr, f"create_task errored: {tr}"
        result = tr.get("result", {})
        # Either created_task or task or activity_code in result
        code = (result.get("created_task") or {}).get("activity_code") \
               or (result.get("task") or {}).get("activity_code") \
               or result.get("activity_code")
        assert code, f"No activity_code in create_task result: {result}"
        TestCopilotWrite.created_task_code = code

        # Verify GET /api/projects/:id/tasks shows the new task
        time.sleep(0.5)
        rt = api.get(f"{BASE_URL}/api/projects/{project['id']}/tasks")
        assert rt.status_code == 200
        codes = [t.get("activity_code") for t in rt.json()]
        assert code in codes, f"created task {code} not found in tasks list"

        # No markdown in reply
        violations = assert_no_markdown(d.get("response", ""))
        assert not violations, f"Markdown in create_task reply: {violations}"

    def test_create_daily_report(self, api, project):
        # First call
        r1 = copilot_post(api, project["id"], "Create a daily report for today")
        assert r1.status_code == 200, r1.text
        usage1 = r1.json().get("usage", {})
        tr1 = find_tool_result(usage1, "create_daily_report")
        assert tr1 is not None, f"create_daily_report not invoked: {usage1}"
        # Second call — should be idempotent (already_existed=true OR same id)
        r2 = copilot_post(api, project["id"], "Create a daily report for today")
        assert r2.status_code == 200
        usage2 = r2.json().get("usage", {})
        tr2 = find_tool_result(usage2, "create_daily_report")
        if tr2 and "result" in tr2:
            res2 = tr2["result"]
            res1 = tr1.get("result") or {}
            # Result may be nested under 'daily_report' or top-level
            dr1 = res1.get("daily_report") or res1
            dr2 = res2.get("daily_report") or res2
            id1 = dr1.get("id") or dr1.get("daily_report_id")
            id2 = dr2.get("id") or dr2.get("daily_report_id")
            idempotent = (dr2.get("already_existed") is True
                          or dr1.get("already_existed") is True
                          or (id1 and id2 and id1 == id2))
            assert idempotent, f"Daily report not idempotent. tr1={tr1}, tr2={tr2}"

    def test_create_rfi(self, api, project):
        msg = ("Create an RFI: subject Roof penetration coordination, question "
               "The roof curb dimensions on M-5.01 conflict with structural "
               "opening on S-4.02 - please clarify which controls")
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_rfi")
        assert tr is not None, f"create_rfi not invoked: {usage}"
        result = tr.get("result", {})
        rfi_num = (result.get("rfi") or {}).get("rfi_number") \
                  or (result.get("created_rfi") or {}).get("rfi_number") \
                  or result.get("rfi_number")
        assert rfi_num and rfi_num.startswith("RFI-"), f"No valid rfi_number: {result}"

    def test_create_work_item(self, api, project):
        msg = ("Create a work item: priority high, title Loose handrail Stairwell B, "
               "description Handrail at landing 6 has 3/4-inch lateral movement, "
               "code requires zero deflection")
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_work_item")
        assert tr is not None, f"create_work_item not invoked: {usage}"
        assert "error" not in tr, f"errored: {tr}"

    def test_create_kb_entry(self, api, project):
        msg = ("Add a knowledge-base entry titled Concrete cure-time below 40F, "
               "body Per ACI 306 the minimum protection period is 7 days when "
               "temps drop below 40F. Use insulated blankets or heated enclosure.")
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_kb_entry")
        assert tr is not None, f"create_kb_entry not invoked: {usage}"
        assert "error" not in tr

    def test_create_submittal(self, api, project):
        msg = ("Create a submittal for spec section 03 30 00, "
               "title Concrete mix design 4500psi, type sample")
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "create_submittal")
        assert tr is not None, f"create_submittal not invoked: {usage}"
        assert "error" not in tr

    def test_update_task(self, api, project):
        # Find any existing task
        rt = api.get(f"{BASE_URL}/api/projects/{project['id']}/tasks")
        assert rt.status_code == 200
        tasks = rt.json()
        if not tasks:
            pytest.skip("no tasks")
        # Pick first task with an activity_code matching A\d+ pattern
        target = next((t for t in tasks
                       if t.get("activity_code")
                       and re.match(r"^A\d+$", t["activity_code"])), None)
        if not target:
            pytest.skip("no task with Axxxx-style activity_code")
        code = target["activity_code"]
        msg = f"Update task {code} to 75 percent complete"
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200, r.text
        usage = r.json().get("usage", {})
        tr = find_tool_result(usage, "update_task")
        assert tr is not None, f"update_task not invoked: {usage}"
        # Verify pct=75
        args = tr.get("args", {})
        assert args.get("pct_complete") == 75 or args.get("pct") == 75 \
               or 70 <= float(args.get("pct_complete") or 0) <= 80, \
               f"update_task pct not 75: {args}"


# ============================================================
# 4. COPILOT AUDIT — copilot_messages persists turn
# ============================================================

class TestCopilotAudit:
    def test_persisted_to_copilot_messages(self, api, project):
        # Send a unique message we can search for
        marker = f"TEST_iter8 audit marker {int(time.time())}"
        msg = f"What is the status? note: {marker}"
        r = copilot_post(api, project["id"], msg)
        assert r.status_code == 200
        # Wait for write
        time.sleep(0.5)
        # Read back via /ai/copilot/sessions endpoints
        sess = r.json().get("session_id")
        if not sess:
            pytest.skip("no session_id in response")
        rs = api.get(f"{BASE_URL}/api/ai/copilot/sessions/{sess}")
        # acceptable: 200 with messages array
        assert rs.status_code == 200, rs.text
        data = rs.json()
        # data may be list-of-messages or {messages: []}
        msgs = data if isinstance(data, list) else data.get("messages", [])
        assert msgs, "copilot_messages empty for session"
        # Verify required fields exist on at least one row
        sample = msgs[0]
        # Field names may be 'request_text' / 'reply_text' or wrapped
        flat = json.dumps(sample)
        assert "latency_ms" in flat or "latency" in flat or "created_at" in flat


# ============================================================
# 5. ROLE GATE — voice can't write
# ============================================================

class TestCopilotRoleGate:
    def test_voice_role_filtered(self, api, project):
        # Issue and exchange a voice PIN to get a voice JWT
        pin_resp = api.post(f"{BASE_URL}/api/auth/voice-pin/issue",
                            json={"name": "TEST_iter8 Voice", "project_id": project["id"], "ttl_hours": 1})
        if pin_resp.status_code != 200:
            pytest.skip(f"can't issue voice pin: {pin_resp.status_code}")
        pin = pin_resp.json()["pin"]
        pub = requests.Session()
        pub.headers.update({"Content-Type": "application/json"})
        ex = pub.post(f"{BASE_URL}/api/auth/voice-pin/exchange",
                      json={"pin": pin, "tenant_slug": TENANT_SLUG})
        assert ex.status_code == 200, ex.text
        voice_token = ex.json()["access_token"]

        # Attempt write via voice token
        v = requests.Session()
        v.headers.update({"Content-Type": "application/json",
                          "Authorization": f"Bearer {voice_token}"})
        r = v.post(f"{BASE_URL}/api/ai/copilot",
                   json={"project_id": project["id"],
                         "message": "Add a task: TEST_iter8 voice write attempt, duration 1 day"},
                   timeout=120)
        # Acceptable behaviours: 200 (write filtered out), 401/403 (voice can't access copilot)
        assert r.status_code in (200, 401, 403), r.text
        if r.status_code == 200:
            usage = r.json().get("usage", {})
            for tr in usage.get("tool_results", []):
                assert tr.get("tool") not in {
                    "create_task", "update_task", "mark_task_complete",
                    "create_rfi", "create_daily_report", "create_work_item",
                    "create_kb_entry", "create_submittal",
                    "link_submittal_to_activity",
                }, f"WRITE tool leaked through voice gate: {tr}"


# ============================================================
# 6. Iter 7 regression — quick smoke
# ============================================================

class TestIter7Regression:
    def test_kb_search_concrete(self, api):
        r = api.get(f"{BASE_URL}/api/kb/entries?q=concrete")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_nist_controls(self, api):
        r = api.get(f"{BASE_URL}/api/compliance/nist-800-171")
        assert r.status_code == 200
        d = r.json()
        assert d["summary"]["total"] == 16

    def test_audit_chain(self, api):
        r = api.get(f"{BASE_URL}/api/audit-log/verify-chain")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_escrow_summary(self, api):
        r = api.get(f"{BASE_URL}/api/marketplace/escrow-summary")
        assert r.status_code == 200
        d = r.json()
        for k in ("total_funded_usd", "total_released_usd",
                  "in_escrow_usd", "transaction_count"):
            assert k in d
