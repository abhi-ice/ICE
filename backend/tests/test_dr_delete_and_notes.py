# DR delete (reason gating + schedule reversal) and activity notes (PDF) regression tests
import os
import io
from datetime import date, timedelta
import pytest
import requests


def _base():
    url = os.environ.get("REACT_APP_BACKEND_URL")
    if not url:
        for line in open("/app/frontend/.env"):
            if line.startswith("REACT_APP_BACKEND_URL="):
                url = line.split("=", 1)[1].strip()
    return url.rstrip("/") + "/api"


API = _base()


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
    p = s.post(f"{API}/projects", json={"name": "TEST_DR_Delete_Notes"}).json()
    yield p["id"]
    s.delete(f"{API}/projects/{p['id']}")


def _task(s, pid, code):
    t = date.today()
    r = s.post(f"{API}/tasks", json={"project_id": pid, "activity_code": code, "description": f"TEST {code}",
                                     "planned_start": (t - timedelta(days=30)).isoformat(),
                                     "planned_finish": (t + timedelta(days=30)).isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _dr(s, pid, d):
    r = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": d.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _row(s, dr_id, tid):
    g = s.get(f"{API}/daily-reports/{dr_id}/grid").json()
    rows = [r for r in g if r["schedule_activity_id"] == tid]
    if rows:
        return rows[0]
    r = s.post(f"{API}/daily-reports/{dr_id}/grid", json={"schedule_activity_id": tid})
    assert r.status_code == 200, r.text
    return r.json()


def _task_get(s, tid):
    return s.get(f"{API}/tasks/{tid}").json()


# Draft delete needs no reason; 404 afterwards
def test_draft_delete_no_reason(s, proj):
    dr = _dr(s, proj, date.today() - timedelta(days=20))
    r = s.delete(f"{API}/daily-reports/{dr}")
    assert r.status_code == 200, r.text
    assert s.get(f"{API}/daily-reports/{dr}").status_code == 404


# Submitted delete: short reason (<10) rejected
def test_submitted_delete_short_reason(s, proj):
    dr = _dr(s, proj, date.today() - timedelta(days=19))
    tid = _task(s, proj, "TSUB1")
    rw = _row(s, dr, tid)
    s.patch(f"{API}/daily-report-activities/{rw['id']}", json={"pct_complete_end_of_day": 10, "hours_worked": 2})
    sub = s.post(f"{API}/daily-reports/{dr}/submit", json={})
    assert sub.status_code == 200 and sub.json()["status"] == "submitted", sub.text[:300]
    assert s.delete(f"{API}/daily-reports/{dr}", params={"reason": "short"}).status_code == 422
    assert s.delete(f"{API}/daily-reports/{dr}", params={"reason": "          x"}).status_code == 422
    assert s.delete(f"{API}/daily-reports/{dr}", params={"reason": "valid reason here"}).status_code == 200


# Older DR deleted while newer exists -> % kept, hours subtracted
def test_delete_older_keeps_pct_subtracts_hours(s, proj):
    tid = _task(s, proj, "TOLD1")
    d1 = _dr(s, proj, date.today() - timedelta(days=15))
    r1 = _row(s, d1, tid)
    assert s.patch(f"{API}/daily-report-activities/{r1['id']}", json={"pct_complete_end_of_day": 25, "hours_worked": 5}).status_code == 200
    d2 = _dr(s, proj, date.today() - timedelta(days=14))
    r2 = _row(s, d2, tid)
    assert s.patch(f"{API}/daily-report-activities/{r2['id']}", json={"pct_complete_end_of_day": 55, "hours_worked": 7}).status_code == 200
    t = _task_get(s, tid)
    assert t["pct_complete"] == 55
    hrs = float(t.get("actual_hours_to_date") or 0)
    r = s.delete(f"{API}/daily-reports/{d1}")
    assert r.status_code == 200, r.text
    t = _task_get(s, tid)
    assert t["pct_complete"] == 55, t["pct_complete"]
    assert abs(float(t.get("actual_hours_to_date") or 0) - (hrs - 5)) < 0.01
    # Now delete newer one too -> should go back to 0 (no remaining DR)
    r = s.delete(f"{API}/daily-reports/{d2}")
    assert r.status_code == 200
    t = _task_get(s, tid)
    print("after deleting both out-of-order:", t["pct_complete"], t["status"], t.get("actual_hours_to_date"))
    assert t["pct_complete"] == 0, f"out-of-order delete left pct={t['pct_complete']} (ghost of deleted older DR)"


# Unplanned task created by a DR is removed on DR delete
def test_unplanned_task_removed(s, proj):
    d = _dr(s, proj, date.today() - timedelta(days=10))
    r = s.post(f"{API}/daily-reports/{d}/unplanned-activity", json={"description": "TEST unplanned work"})
    assert r.status_code == 200, r.text
    body = r.json()
    tid = (body.get("task") or {}).get("id") or body.get("id") or body.get("task_id")
    assert tid
    dl = s.delete(f"{API}/daily-reports/{d}")
    assert dl.status_code == 200, dl.text
    assert dl.json()["schedule_reverted"]["unplanned_tasks_removed"] == 1
    t = s.get(f"{API}/tasks/{tid}")
    assert t.status_code == 404 or t.json().get("status") == "deleted"
    tasks = s.get(f"{API}/projects/{proj}/tasks")
    if tasks.status_code == 200:
        assert tid not in [x["id"] for x in tasks.json()], "deleted unplanned task still listed"


# Notes appear in PDF export
def test_note_in_pdf(s, proj):
    tid = _task(s, proj, "TPDF1")
    d = _dr(s, proj, date.today() - timedelta(days=8))
    row = _row(s, d, tid)
    note = "TESTNOTE Rebar inspected by QA"
    r = s.patch(f"{API}/daily-report-activities/{row['id']}", json={"notes": note})
    assert r.status_code == 200 and r.json()["notes"] == note
    pdf = s.get(f"{API}/daily-reports/{d}/pdf-rich", params={"mode": "external"})
    assert pdf.status_code == 200
    assert pdf.content[:4] == b"%PDF"
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf.content)) as doc:
        text = "".join(p.extract_text() or "" for p in doc.pages)
    assert "Note:" in text and "TESTNOTE" in text, text[:1500]
