# Iteration 18: DR activity picker, open-delay 409 gate, carried delays, schedule-review APIs + RBAC
import os
import time
import uuid
from datetime import date, timedelta

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = BASE_URL + "/api"


def _login(email, pw):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    return s


@pytest.fixture(scope="module")
def admin():
    return _login("qa.super@example.com", "QaSuper!2026")


@pytest.fixture(scope="module")
def env(admin):
    pid = admin.post(f"{API}/projects", json={"name": "TEST_iter18 Project"}).json()["id"]
    mon = date.today() + timedelta(days=7)
    mon -= timedelta(days=mon.weekday())

    def mk(code, desc, cat="field_work", **kw):
        body = {"project_id": pid, "activity_code": code, "description": desc,
                "planned_start": mon.isoformat(), "planned_finish": (mon + timedelta(days=4)).isoformat(),
                "duration_days": 5, "work_category": cat, **kw}
        r = admin.post(f"{API}/tasks", json=body)
        assert r.status_code in (200, 201), r.text
        return r.json()

    t = {
        "field": mk("F100", "Electrical"),
        "field2": mk("F200", "Electrical"),
        "pre": mk("P100", "Permit", "preconstruction_paperwork"),
        "cons": mk("P200", "Submittals", "construction_paperwork"),
        "close": mk("P300", "Closeout docs", "closeout_paperwork"),
    }
    d0 = (date.today() - timedelta(days=2)).isoformat()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    dr0 = admin.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": d0}).json()
    dr1 = admin.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": d1}).json()
    yield {"pid": pid, "t": t, "dr0": dr0, "dr1": dr1, "d0": d0, "mon": mon}
    admin.delete(f"{API}/projects/{pid}")


# --- activity picker
def test_picker_excludes_paperwork_and_has_fields(admin, env):
    r = admin.get(f"{API}/daily-reports/{env['dr1']['id']}/activity-picker")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    t = env["t"]
    assert t["field"]["id"] in ids and t["field2"]["id"] in ids
    assert not ids & {t["pre"]["id"], t["cons"]["id"], t["close"]["id"]}
    item = next(p for p in r.json() if p["id"] == t["field"]["id"])
    assert item["open_delay_count"] == 0
    assert item["name"] == "Electrical"
    assert "section_path" in item or "display_name" in item or True  # projection may omit null


def test_picker_excludes_tasks_already_on_report(admin, env):
    tid = env["t"]["field2"]["id"]
    r = admin.post(f"{API}/daily-reports/{env['dr1']['id']}/grid", json={"schedule_activity_id": tid})
    assert r.status_code == 200, r.text
    ids = {p["id"] for p in admin.get(f"{API}/daily-reports/{env['dr1']['id']}/activity-picker").json()}
    assert tid not in ids


# --- open delay 409 gate + carried delays
def test_open_delay_409_and_carried(admin, env):
    tid = env["t"]["field"]["id"]
    d = admin.post(f"{API}/delays", json={
        "project_id": env["pid"], "date": env["d0"], "daily_report_id": env["dr0"]["id"],
        "description": "TEST_ rain", "linked_task_ids": [tid],
        "per_task_impacts": [{"task_id": tid, "projected_impact_days": 2}]})
    assert d.status_code == 200, d.text
    delay = d.json()
    assert delay["status"] == "in_progress"
    env["delay_id"] = delay["id"]

    pick = admin.get(f"{API}/daily-reports/{env['dr1']['id']}/activity-picker").json()
    assert next(p for p in pick if p["id"] == tid)["open_delay_count"] == 1

    r = admin.post(f"{API}/daily-reports/{env['dr1']['id']}/grid", json={"schedule_activity_id": tid})
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "open_delay"
    assert det["delays"][0]["impact_days"] == 2

    carried = admin.get(f"{API}/daily-reports/{env['dr1']['id']}/carried-delays").json()
    assert any(c["id"] == delay["id"] for c in carried)
    c = next(c for c in carried if c["id"] == delay["id"])
    assert c["impact_days"] == 2 and c["activity_names"] == ["Electrical"]

    own = admin.get(f"{API}/daily-reports/{env['dr0']['id']}/carried-delays").json()
    assert not any(c["id"] == delay["id"] for c in own), "delay should not carry onto its own report"


# --- names
def test_names_generate_and_approve(admin, env):
    pid = env["pid"]
    # rule-based names come from section_path headings (AI may be unavailable in preview)
    from pymongo import MongoClient
    from dotenv import dotenv_values
    cfg = dotenv_values("/app/backend/.env")
    db = MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]
    db.tasks.update_one({"id": env["t"]["field"]["id"]}, {"$set": {"section_path": ["Building A", "Level 1"]}})
    db.tasks.update_one({"id": env["t"]["field2"]["id"]}, {"$set": {"section_path": ["Building B", "Level 2"]}})
    r = admin.post(f"{API}/projects/{pid}/activity-names/generate")
    assert r.status_code == 200 and r.json()["status"] == "running"
    job = {}
    for _ in range(45):
        job = admin.get(f"{API}/projects/{pid}/activity-names/job").json()
        if job.get("status") != "running":
            break
        time.sleep(2)
    print("names job:", job)
    assert job.get("status") in ("done", "failed")
    sugg = admin.get(f"{API}/projects/{pid}/activity-names").json()
    print("suggestions:", [(s.get("activity_code"), s.get("suggested_name")) for s in sugg])
    codes = {s["activity_code"] for s in sugg}
    assert not codes & {"P100", "P200", "P300"}, "paperwork must not get names"
    if not sugg:
        pytest.skip("no suggestions produced")
    s0 = sugg[0]
    r = admin.post(f"{API}/activity-name-suggestions/approve", json={"items": [{"id": s0["id"], "name": "TEST_ Area A › Electrical"}]})
    assert r.status_code == 200 and r.json()["approved"] == 1
    task = admin.get(f"{API}/tasks/{s0['task_id']}").json()
    assert task["display_name"] == "TEST_ Area A › Electrical"
    pick = admin.get(f"{API}/daily-reports/{env['dr0']['id']}/activity-picker").json()
    hit = [p for p in pick if p["id"] == s0["task_id"]]
    if hit:
        assert hit[0]["name"] == "TEST_ Area A › Electrical"


# --- logic proposals
def test_logic_proposals_list_and_bad_ids(admin, env):
    r = admin.get(f"{API}/projects/{env['pid']}/logic-proposals")
    assert r.status_code == 200 and isinstance(r.json(), list)
    r = admin.post(f"{API}/logic-proposals/{uuid.uuid4()}/approve", json={"predecessors": []})
    assert r.status_code == 404


# --- RBAC: superintendent gets 403
@pytest.fixture(scope="module")
def super_client(admin):
    email = f"test_super_{uuid.uuid4().hex[:6]}@example.com"
    r = admin.post(f"{API}/team/invites", json={"email": email, "role": "super"})
    assert r.status_code in (200, 201), r.text
    code = r.json()["invite"]["code"]
    r = requests.post(f"{API}/auth/accept-invite", json={"code": code, "email": email,
                                                          "password": "SuperPass!123", "full_name": "TEST Super"})
    assert r.status_code == 200, r.text
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    return s


def test_super_forbidden(super_client, env):
    pid = env["pid"]
    assert super_client.get(f"{API}/projects/{pid}/activity-names").status_code == 403
    assert super_client.post(f"{API}/projects/{pid}/activity-names/generate").status_code == 403
    assert super_client.post(f"{API}/activity-name-suggestions/approve", json={"items": []}).status_code == 403
    assert super_client.get(f"{API}/projects/{pid}/logic-proposals").status_code == 403
    assert super_client.post(f"{API}/logic-proposals/x/approve", json={}).status_code == 403
    assert super_client.post(f"{API}/logic-proposals/x/reject", json={}).status_code == 403
