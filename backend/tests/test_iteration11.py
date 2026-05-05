"""Iteration 11 — Auth/Tenant/Team/DailyReports validation per review_request.

Runs against the live REACT_APP_BACKEND_URL and DROPS all collections at the
end so the user's real signup remains the first-ever tenant.
"""
import os
import time
import io
import pytest
import requests
from datetime import date, timedelta

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
assert BASE, "REACT_APP_BACKEND_URL not set"

TS = int(time.time())
ADMIN_EMAIL = f"qa+{TS}@gtest.example"
ADMIN_PW = "Secret123!"
ORG = f"QA Org {TS}"
INVITEE_EMAIL = f"qa+inv{TS}@gtest.example"
INVITEE_PW = "Invite123!"

S = {}  # shared state


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


# ---------- Auth: google removed ----------
def test_google_login_removed():
    r = requests.get(f"{BASE}/api/auth/google-login")
    assert r.status_code in (404, 405), r.text


def test_health():
    r = requests.get(f"{BASE}/api/health")
    assert r.status_code == 200


# ---------- Register / login / me ----------
def test_register_creates_tenant_and_admin():
    r = requests.post(f"{BASE}/api/auth/register", json={
        "email": ADMIN_EMAIL, "password": ADMIN_PW,
        "full_name": "QA Tester", "organization_name": ORG,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["user"]["email"] == ADMIN_EMAIL
    assert d["user"]["role"] == "admin"
    assert d["tenant"]["display_name"] == ORG
    S["admin_token"] = d["access_token"]
    S["admin_user_id"] = d["user"]["id"]
    S["tenant_id"] = d["tenant"]["id"]


def test_register_duplicate_email_409():
    r = requests.post(f"{BASE}/api/auth/register", json={
        "email": ADMIN_EMAIL, "password": ADMIN_PW,
        "full_name": "Dup", "organization_name": "Dup Org",
    })
    assert r.status_code == 409


def test_login_works():
    r = requests.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    assert r.status_code == 200
    S["admin_token"] = r.json()["access_token"]


def test_me_returns_admin():
    r = requests.get(f"{BASE}/api/auth/me", headers=_hdr(S["admin_token"]))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


# ---------- Team invites ----------
def test_create_invite_admin():
    r = requests.post(f"{BASE}/api/team/invites", headers=_hdr(S["admin_token"]),
                      json={"email": INVITEE_EMAIL, "role": "pm"})
    assert r.status_code == 200, r.text
    inv = r.json()["invite"]
    assert len(inv["code"]) == 12
    assert inv["status"] == "pending"
    assert inv["tenant_id"] == S["tenant_id"]
    S["invite_code"] = inv["code"]
    S["invite_id"] = inv["id"]


def test_list_invites():
    r = requests.get(f"{BASE}/api/team/invites", headers=_hdr(S["admin_token"]))
    assert r.status_code == 200
    assert any(i["id"] == S["invite_id"] for i in r.json())


def test_accept_invite_mismatched_email_403():
    r = requests.post(f"{BASE}/api/auth/accept-invite", json={
        "code": S["invite_code"], "email": "wrong@gtest.example",
        "password": "x" * 9, "full_name": "x",
    })
    assert r.status_code == 403


def test_accept_invite_unknown_code_404():
    r = requests.post(f"{BASE}/api/auth/accept-invite", json={
        "code": "nopecode1234", "email": INVITEE_EMAIL,
        "password": "Secret999!", "full_name": "x",
    })
    assert r.status_code == 404


def test_accept_invite_creates_user_in_same_tenant():
    r = requests.post(f"{BASE}/api/auth/accept-invite", json={
        "code": S["invite_code"], "email": INVITEE_EMAIL,
        "password": INVITEE_PW, "full_name": "QA Invitee",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tenant"]["id"] == S["tenant_id"]
    S["invitee_token"] = d["access_token"]
    S["invitee_user_id"] = d["user"]["id"]


def test_non_admin_cannot_invite():
    r = requests.post(f"{BASE}/api/team/invites", headers=_hdr(S["invitee_token"]),
                      json={"email": f"x+{TS}@gtest.example", "role": "pm"})
    assert r.status_code == 403


def test_list_members():
    r = requests.get(f"{BASE}/api/team/members", headers=_hdr(S["admin_token"]))
    assert r.status_code == 200
    emails = [m["email"] for m in r.json()]
    assert ADMIN_EMAIL in emails and INVITEE_EMAIL in emails


def test_cannot_remove_self():
    r = requests.delete(f"{BASE}/api/team/members/{S['admin_user_id']}",
                        headers=_hdr(S["admin_token"]))
    assert r.status_code == 400


# ---------- Project + tasks + DR pre-populate ----------
def test_create_project_and_activities():
    r = requests.post(f"{BASE}/api/projects", headers=_hdr(S["admin_token"]),
                      json={"name": "QA Project", "city": "Kansas City", "state": "MO"})
    assert r.status_code in (200, 201), r.text
    S["project_id"] = r.json()["id"]
    today = date.today()
    cases = [
        ("A001", today - timedelta(days=2), today + timedelta(days=2)),  # spans
        ("A002", today, today),  # exact
        ("A003", today + timedelta(days=1), today + timedelta(days=3)),  # future
        ("A004", today - timedelta(days=5), today - timedelta(days=1)),  # past
        ("A005", today - timedelta(days=1), today + timedelta(days=1)),  # spans
    ]
    S["task_ids"] = {}
    for code, ps, pf in cases:
        r = requests.post(f"{BASE}/api/tasks", headers=_hdr(S["admin_token"]), json={
            "project_id": S["project_id"], "activity_code": code,
            "description": f"task {code}", "planned_start": ps.isoformat(),
            "planned_finish": pf.isoformat(),
        })
        assert r.status_code in (200, 201), r.text
        S["task_ids"][code] = r.json()["id"]


def test_daily_report_grid_filters_by_interval():
    today = date.today().isoformat()
    r = requests.post(f"{BASE}/api/daily-reports", headers=_hdr(S["admin_token"]),
                      json={"project_id": S["project_id"], "report_date": today})
    assert r.status_code == 200, r.text
    S["dr_id"] = r.json()["id"]
    g = requests.get(f"{BASE}/api/daily-reports/{S['dr_id']}/grid", headers=_hdr(S["admin_token"])).json()
    codes = sorted([row["activity_code"] for row in g])
    assert codes == ["A001", "A002", "A005"], f"got {codes}"


def test_refresh_grid_idempotent():
    r1 = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/refresh-grid",
                       headers=_hdr(S["admin_token"]))
    assert r1.status_code == 200 and r1.json()["inserted"] == 0


def test_refresh_grid_picks_up_new_activity():
    today = date.today()
    r = requests.post(f"{BASE}/api/tasks", headers=_hdr(S["admin_token"]), json={
        "project_id": S["project_id"], "activity_code": "A006",
        "description": "added later",
        "planned_start": today.isoformat(),
        "planned_finish": (today + timedelta(days=1)).isoformat(),
    })
    assert r.status_code in (200, 201)
    r = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/refresh-grid",
                      headers=_hdr(S["admin_token"]))
    assert r.status_code == 200 and r.json()["inserted"] == 1


# ---------- Inline events ----------
def test_delay_uses_delay_days():
    r = requests.post(f"{BASE}/api/delays", headers=_hdr(S["admin_token"]), json={
        "project_id": S["project_id"], "daily_report_id": S["dr_id"],
        "date": date.today().isoformat(), "description": "rain",
        "severity": "minor", "delay_days": 0.5,
    })
    assert r.status_code in (200, 201), r.text
    assert r.json().get("delay_days") == 0.5


def test_delay_minutes_rejected():
    r = requests.post(f"{BASE}/api/delays", headers=_hdr(S["admin_token"]), json={
        "project_id": S["project_id"], "daily_report_id": S["dr_id"],
        "date": date.today().isoformat(), "description": "x",
        "delay_minutes": 10,
    })
    assert r.status_code == 422


def test_safety_qc_rfi_inline():
    today = date.today().isoformat()
    rs = requests.post(f"{BASE}/api/safety-events", headers=_hdr(S["admin_token"]), json={
        "project_id": S["project_id"], "daily_report_id": S["dr_id"],
        "date": today, "description": "near miss", "event_type": "near_miss",
    })
    assert rs.status_code in (200, 201), rs.text
    rq = requests.post(f"{BASE}/api/qc-issues", headers=_hdr(S["admin_token"]), json={
        "project_id": S["project_id"], "daily_report_id": S["dr_id"],
        "date": today, "description": "qc issue",
    })
    assert rq.status_code in (200, 201), rq.text
    rr = requests.post(f"{BASE}/api/rfis", headers=_hdr(S["admin_token"]), json={
        "project_id": S["project_id"], "daily_report_id": S["dr_id"],
        "subject": "RFI", "description": "?",
    })
    assert rr.status_code in (200, 201), rr.text
    ctx = requests.get(f"{BASE}/api/daily-reports/{S['dr_id']}/context",
                       headers=_hdr(S["admin_token"])).json()
    assert len(ctx["delays"]) >= 1
    assert len(ctx["safety_events"]) >= 1
    assert len(ctx["qc_issues"]) >= 1
    assert len(ctx["rfis"]) >= 1


def test_presence_create():
    r = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/presence",
                      headers=_hdr(S["admin_token"]), json={
                          "daily_report_id": S["dr_id"], "trade": "ELEC",
                          "headcount": 4, "hours_per_person": 8,
                      })
    assert r.status_code in (200, 201), r.text


def test_unplanned_activity_AF_prefix():
    r = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/unplanned-activity",
                      headers=_hdr(S["admin_token"]),
                      json={"description": "field cleanup"})
    assert r.status_code in (200, 201), r.text
    t = r.json()["task"]
    assert t["activity_code"].startswith("AF")
    assert t["created_in_field"] is True
    assert t["requires_pm_review"] is True


def test_weather_autofill():
    r = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/weather-autofill",
                      headers=_hdr(S["admin_token"]))
    if r.status_code == 502:
        pytest.skip("Open-Meteo unreachable (flaky network)")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("weather_fetched_at")


def test_submit_locks_dr():
    r = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/submit",
                      headers=_hdr(S["admin_token"]), json={"notes": "submitting"})
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"
    # Subsequent inline POST should 409 (DR-aware lock)
    rl = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/refresh-grid",
                       headers=_hdr(S["admin_token"]))
    assert rl.status_code == 409


def test_approve_dr():
    r = requests.post(f"{BASE}/api/daily-reports/{S['dr_id']}/approve",
                      headers=_hdr(S["admin_token"]))
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


# ---------- Cleanup: drop all collections ----------
def test_zzz_cleanup_drop_all():
    """Wipe DB so the user's real signup at /signup is the first-ever tenant."""
    import asyncio
    import sys
    sys.path.insert(0, "/app/backend")
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _wipe():
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = cli[os.environ["DB_NAME"]]
        for c in await db.list_collection_names():
            if not c.startswith("system."):
                await db.drop_collection(c)
        cli.close()
    asyncio.run(_wipe())
    # Old token should now be unauthorized
    r = requests.get(f"{BASE}/api/auth/me", headers=_hdr(S["admin_token"]))
    assert r.status_code in (401, 403, 404)
