# Iteration 19: weekly schedule digest (cron + preview + send-now), delay impact preview, names grouping
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = BASE_URL + "/api"
CFG = dotenv_values("/app/backend/.env")
SECRET = CFG.get("WEBHOOK_CRON_SECRET")
DB = MongoClient(CFG["MONGO_URL"])[CFG["DB_NAME"]]


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
    pid = admin.post(f"{API}/projects", json={"name": "TEST_iter19 Project"}).json()["id"]
    mon = date.today() + timedelta(days=7)
    mon -= timedelta(days=mon.weekday())

    def mk(code, desc, start, finish, preds=None, path=None):
        body = {"project_id": pid, "activity_code": code, "description": desc,
                "planned_start": start.isoformat(), "planned_finish": finish.isoformat(),
                "duration_days": 5}
        if preds:
            body["predecessors"] = preds
        r = admin.post(f"{API}/tasks", json=body)
        assert r.status_code in (200, 201), r.text
        t = r.json()
        if path:
            DB.tasks.update_one({"id": t["id"]}, {"$set": {"section_path": path}})
        return t

    A = mk("A100", "Electrical", mon, mon + timedelta(days=4), path=["Building A", "Level 1"])
    B = mk("A200", "Electrical", mon + timedelta(days=7), mon + timedelta(days=11),
           preds=[{"code": "A100", "type": "FS", "lag_days": 0}], path=["Building A", "Level 2"])
    C = mk("B100", "Plumbing", mon, mon + timedelta(days=4), path=["Building B", "Level 1"])
    D = mk("B200", "Plumbing", mon + timedelta(days=7), mon + timedelta(days=11), path=["Building B", "Level 2"])
    yield {"pid": pid, "A": A, "B": B, "C": C, "D": D, "mon": mon}
    admin.delete(f"{API}/projects/{pid}")


# --- cron auth + idempotency
def test_cron_requires_bearer():
    assert requests.post(f"{API}/cron/schedule-digest").status_code == 401
    r = requests.post(f"{API}/cron/schedule-digest", headers={"Authorization": "Bearer wrong-" + uuid.uuid4().hex})
    assert r.status_code == 401


def test_cron_accepts_and_dedupes():
    assert SECRET, "WEBHOOK_CRON_SECRET missing"
    hid = "TEST_" + uuid.uuid4().hex
    h = {"Authorization": f"Bearer {SECRET}", "X-Webhook-Id": hid}
    r1 = requests.post(f"{API}/cron/schedule-digest", headers=h)
    assert r1.status_code == 200 and r1.json().get("accepted") is True, r1.text
    r2 = requests.post(f"{API}/cron/schedule-digest", headers=h)
    assert r2.status_code == 200 and r2.json().get("duplicate") is True, r2.text
    DB.cron_runs.delete_one({"_id": f"schedule-digest:{hid}"})


# --- delay impact preview (dry run)
def test_delay_impact_preview_no_write(admin, env):
    A, B = env["A"], env["B"]
    before = admin.get(f"{API}/tasks/{A['id']}").json()
    delays_before = DB.delays.count_documents({"project_id": env["pid"]})
    r = admin.post(f"{API}/projects/{env['pid']}/delay-impact-preview",
                   json={"impacts": [{"task_id": A["id"], "days": 3}]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["supported"] is True
    moves = {m["task_id"]: m for m in d["moves"]}
    assert A["id"] in moves and moves[A["id"]]["direct"] is True and moves[A["id"]]["days"] == 3, d
    assert B["id"] in moves and moves[B["id"]]["direct"] is False and moves[B["id"]]["days"] >= 1, d
    assert moves[A["id"]]["finish_from"] != moves[A["id"]]["finish_to"]
    assert d["project_finish_from"] and d["project_finish_to"]
    assert d["project_finish_to"] > d["project_finish_from"], d
    after = admin.get(f"{API}/tasks/{A['id']}").json()
    assert after.get("slip_days") == before.get("slip_days")
    assert after.get("projected_finish_date") == before.get("projected_finish_date")
    assert DB.delays.count_documents({"project_id": env["pid"]}) == delays_before


def test_delay_impact_preview_validation(admin, env):
    r = admin.post(f"{API}/projects/{env['pid']}/delay-impact-preview",
                   json={"impacts": [{"task_id": env["A"]["id"], "days": -1}]})
    assert r.status_code == 422
    r = admin.post(f"{API}/projects/{env['pid']}/delay-impact-preview", json={"impacts": []})
    assert r.status_code == 200 and r.json()["moves"] == []


# --- digest preview / send-now
def test_digest_preview_content(admin, env):
    # real delay on C -> open delay + slipped task
    dr = admin.post(f"{API}/daily-reports", json={"project_id": env["pid"],
                                                  "report_date": (date.today() - timedelta(days=1)).isoformat()}).json()
    r = admin.post(f"{API}/delays", json={"project_id": env["pid"], "date": dr["report_date"], "daily_report_id": dr["id"],
                                          "impact_start_date": env["mon"].isoformat(),
                                          "description": "TEST_ digest delay", "linked_task_ids": [env["C"]["id"]],
                                          "per_task_impacts": [{"task_id": env["C"]["id"], "projected_impact_days": 2}]})
    assert r.status_code == 200, r.text
    p = admin.get(f"{API}/projects/{env['pid']}/schedule-digest/preview")
    assert p.status_code == 200, p.text
    d = p.json()
    for k in ("logic_changes", "open_delays", "slipped", "names_pending", "recipients"):
        assert k in d
    assert any("Plumbing" in x["activities"] and x["impact_days"] == 2 for x in d["open_delays"]), d["open_delays"]
    assert any(x["slip_days"] == 2 for x in d["slipped"]), d["slipped"]
    assert all(x["slip_days"] > 0 for x in d["slipped"])
    assert "qa.super@example.com" in d["recipients"]


def test_digest_send_now(admin, env):
    r = admin.post(f"{API}/projects/{env['pid']}/schedule-digest/send-now")
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) >= {"sent", "failed", "subject"}
    assert "qa.super@example.com" in d["sent"] + d["failed"]
    assert "open delay" in d["subject"]


def test_digest_unknown_project_send_404(admin):
    assert admin.post(f"{API}/projects/{uuid.uuid4()}/schedule-digest/send-now").status_code == 404


# --- names generate + grouping prefix
def test_names_have_area_prefix_no_wbs(admin, env):
    admin.post(f"{API}/projects/{env['pid']}/activity-names/generate")
    for _ in range(30):
        if admin.get(f"{API}/projects/{env['pid']}/activity-names/job").json().get("status") != "running":
            break
        time.sleep(2)
    rows = admin.get(f"{API}/projects/{env['pid']}/activity-names").json()
    assert len(rows) >= 4, rows
    import re
    for r in rows:
        assert not re.search(r"\d+\.\d+", r["suggested_name"]), r["suggested_name"]
    areas = {r["suggested_name"].split(" › ")[0] for r in rows if " › " in r["suggested_name"]}
    assert {"Building A", "Building B"} <= areas, [r["suggested_name"] for r in rows]


# --- RBAC: super role gets 403
def test_super_forbidden(admin, env):
    email = f"test_super_{uuid.uuid4().hex[:6]}@example.com"
    inv = admin.post(f"{API}/team/invites", json={"email": email, "role": "super"})
    if inv.status_code not in (200, 201):
        pytest.skip(f"invite failed {inv.status_code} {inv.text[:200]}")
    code = inv.json()["invite"]["code"]
    acc = requests.post(f"{API}/auth/accept-invite", json={"code": code, "email": email,
                                                          "password": "TestPass!2026", "full_name": "TEST Super"})
    if acc.status_code not in (200, 201):
        pytest.skip(f"accept failed {acc.status_code}")
    su = _login(email, "TestPass!2026")
    pid = env["pid"]
    assert su.get(f"{API}/projects/{pid}/schedule-digest/preview").status_code == 403
    assert su.post(f"{API}/projects/{pid}/schedule-digest/send-now").status_code == 403
    # super CAN preview delay impact (they log delays)
    r = su.post(f"{API}/projects/{pid}/delay-impact-preview", json={"impacts": [{"task_id": env["A"]["id"], "days": 1}]})
    assert r.status_code == 200, r.text
    u = DB.users.find_one({"email": email})
    if u:
        DB.users.delete_one({"id": u["id"]})
        DB.tenant_users.delete_many({"user_id": u["id"]})
