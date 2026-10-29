# Iteration 17 retests: out-of-order DR delete reversal (2 and 3 DRs), notes-only PATCH stale-stamp skip
from datetime import date, timedelta
import pytest
import requests
from test_dr_delete_and_notes import API, _task, _dr, _row, _task_get


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    r = sess.post(f"{API}/auth/login", json={"email": "qa.super@example.com", "password": "QaSuper!2026"})
    if r.status_code != 200:
        pytest.skip("login failed")
    sess.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return sess


@pytest.fixture(scope="module")
def proj(s):
    p = s.post(f"{API}/projects", json={"name": "TEST_DR_Iter17"}).json()
    yield p["id"]
    s.delete(f"{API}/projects/{p['id']}")


def _set(s, dr, tid, pct, hrs):
    rw = _row(s, dr, tid)
    r = s.patch(f"{API}/daily-report-activities/{rw['id']}", json={"pct_complete_end_of_day": pct, "hours_worked": hrs})
    assert r.status_code == 200, r.text
    return rw


# 2 DRs: delete older then newer -> 0 / not_started / last_synced null, hours back to base
def test_two_dr_out_of_order(s, proj):
    tid = _task(s, proj, "T17A")
    base_hrs = float(_task_get(s, tid).get("actual_hours_to_date") or 0)
    d1 = _dr(s, proj, date.today() - timedelta(days=12))
    _set(s, d1, tid, 25, 4)
    d2 = _dr(s, proj, date.today() - timedelta(days=11))
    _set(s, d2, tid, 55, 6)
    assert s.post(f"{API}/daily-reports/{d1}/submit", json={}).status_code == 200
    t = _task_get(s, tid)
    assert t["pct_complete"] == 55
    assert s.delete(f"{API}/daily-reports/{d1}", params={"reason": "duplicate report entered"}).status_code == 200
    t = _task_get(s, tid)
    assert t["pct_complete"] == 55
    assert t["last_synced_from_dr"]["daily_report_id"] == d2
    assert abs(float(t.get("actual_hours_to_date") or 0) - (base_hrs + 6)) < 0.01
    assert s.delete(f"{API}/daily-reports/{d2}").status_code == 200
    t = _task_get(s, tid)
    print("after both:", t["pct_complete"], t["status"], t.get("last_synced_from_dr"), t.get("actual_hours_to_date"))
    assert t["pct_complete"] == 0
    assert t["status"] == "not_started"
    assert t.get("last_synced_from_dr") is None
    assert abs(float(t.get("actual_hours_to_date") or 0) - base_hrs) < 0.01


# 3 DRs 10/30/60: delete middle -> 60; delete newest -> 10 pointing at oldest; delete oldest -> 0
def test_three_dr(s, proj):
    tid = _task(s, proj, "T17B")
    da = _dr(s, proj, date.today() - timedelta(days=9))
    _set(s, da, tid, 10, 1)
    db_ = _dr(s, proj, date.today() - timedelta(days=8))
    _set(s, db_, tid, 30, 2)
    dc = _dr(s, proj, date.today() - timedelta(days=7))
    _set(s, dc, tid, 60, 3)
    assert _task_get(s, tid)["pct_complete"] == 60
    assert s.delete(f"{API}/daily-reports/{db_}").status_code == 200
    t = _task_get(s, tid)
    assert t["pct_complete"] == 60 and t["last_synced_from_dr"]["daily_report_id"] == dc
    assert s.delete(f"{API}/daily-reports/{dc}").status_code == 200
    t = _task_get(s, tid)
    print("after newest:", t["pct_complete"], t["status"], t.get("last_synced_from_dr"))
    assert t["pct_complete"] == 10
    assert t["status"] == "in_progress"
    assert t["last_synced_from_dr"]["daily_report_id"] == da
    assert s.delete(f"{API}/daily-reports/{da}").status_code == 200
    t = _task_get(s, tid)
    assert t["pct_complete"] == 0 and t.get("last_synced_from_dr") is None


# Notes-only PATCH with old task_last_modified_at -> 200; pct PATCH with same old stamp -> 409
def test_notes_only_skips_stale(s, proj):
    tid = _task(s, proj, "T17C")
    d = _dr(s, proj, date.today() - timedelta(days=5))
    rw = _row(s, d, tid)
    old = "2020-01-01T00:00:00+00:00"
    assert s.patch(f"{API}/daily-report-activities/{rw['id']}", json={"pct_complete_end_of_day": 20}).status_code == 200
    r = s.patch(f"{API}/daily-report-activities/{rw['id']}", json={"notes": "TEST stale note", "task_last_modified_at": old})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "TEST stale note"
    r2 = s.patch(f"{API}/daily-report-activities/{rw['id']}", json={"pct_complete_end_of_day": 40, "task_last_modified_at": old})
    assert r2.status_code == 409, r2.text
    g = s.get(f"{API}/daily-reports/{d}/grid").json()
    assert [x for x in g if x["id"] == rw["id"]][0]["notes"] == "TEST stale note"
