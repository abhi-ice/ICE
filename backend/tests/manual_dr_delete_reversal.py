"""Manual e2e: DR delete reverses schedule effects; activity notes. Run: python tests/manual_dr_delete_reversal.py"""
import os
import sys
from datetime import date, timedelta
import requests

API = os.environ["API_URL"] + "/api"
s = requests.Session()
tok = s.post(f"{API}/auth/login", json={"email": "qa.super@example.com", "password": "QaSuper!2026"}).json()["access_token"]
s.headers["Authorization"] = f"Bearer {tok}"

def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)

proj = s.post(f"{API}/projects", json={"name": "QA Reversal Project"}).json()
pid = proj["id"]
today = date.today()
task = s.post(f"{API}/tasks", json={
    "project_id": pid, "activity_code": "A100", "description": "Pour footing",
    "planned_start": (today - timedelta(days=5)).isoformat(),
    "planned_finish": (today + timedelta(days=10)).isoformat(),
}).json()
ok("id" in task, f"task created {task.get('id', task)}")
tid = task["id"]

def make_dr(d):
    r = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": d.isoformat()})
    ok(r.status_code == 200, f"DR {d} created {r.status_code} {r.text[:200]}")
    return r.json()["id"]

def grid_row(dr_id):
    g = s.get(f"{API}/daily-reports/{dr_id}/grid").json()
    rows = [r for r in g if r["schedule_activity_id"] == tid]
    if not rows:
        r = s.post(f"{API}/daily-reports/{dr_id}/grid", json={"schedule_activity_id": tid})
        ok(r.status_code == 200, f"grid row added {r.status_code} {r.text[:200]}")
        return r.json()
    return rows[0]

def task_now():
    return s.get(f"{API}/tasks/{tid}").json()

dr1 = make_dr(today - timedelta(days=2))
r1 = grid_row(dr1)
p = s.patch(f"{API}/daily-report-activities/{r1['id']}", json={"pct_complete_end_of_day": 30, "hours_worked": 8})
ok(p.status_code == 200, f"DR1 progress 30% {p.status_code} {p.text[:200]}")
t = task_now(); ok(t["pct_complete"] == 30, f"task at 30 after DR1 (got {t['pct_complete']})")

n = s.patch(f"{API}/daily-report-activities/{r1['id']}", json={"notes": "Rebar inspected, pour tomorrow"})
ok(n.status_code == 200 and n.json()["notes"] == "Rebar inspected, pour tomorrow", "note saved on activity")
ok(task_now()["pct_complete"] == 30, "note-only save did not touch task")

sub = s.post(f"{API}/daily-reports/{dr1}/submit", json={})
ok(sub.status_code == 200 and sub.json()["status"] == "submitted", f"DR1 submitted {sub.status_code}")

ln = s.patch(f"{API}/daily-report-activities/{r1['id']}", json={"notes": "edited after submit"})
ok(ln.status_code in (400, 403, 409, 422), f"locked note edit without reason refused ({ln.status_code})")
ln2 = s.patch(f"{API}/daily-report-activities/{r1['id']}", json={"notes": "edited after submit", "edit_reason": "Correcting the field note"})
ok(ln2.status_code == 200, f"locked note edit with reason ok ({ln2.status_code} {ln2.text[:200]})")

dr2 = make_dr(today - timedelta(days=1))
r2 = grid_row(dr2)
p = s.patch(f"{API}/daily-report-activities/{r2['id']}", json={"pct_complete_end_of_day": 60, "hours_worked": 6})
ok(p.status_code == 200, f"DR2 progress 60% {p.status_code} {p.text[:200]}")
t = task_now(); ok(t["pct_complete"] == 60, f"task at 60 after DR2 (got {t['pct_complete']})")
hours_before = float(t.get("actual_hours_to_date") or 0)
s.post(f"{API}/daily-reports/{dr2}/submit", json={})

d = s.delete(f"{API}/daily-reports/{dr2}")
ok(d.status_code == 422, f"submitted delete w/o reason refused ({d.status_code})")
d = s.delete(f"{API}/daily-reports/{dr2}", params={"reason": "Duplicate report entered by mistake"})
ok(d.status_code == 200, f"submitted delete with reason ok ({d.status_code} {d.text[:300]})")
t = task_now()
ok(t["pct_complete"] == 30, f"task reverted to 30 after deleting DR2 (got {t['pct_complete']})")
ok(abs(float(t.get("actual_hours_to_date") or 0) - (hours_before - 6)) < 0.01, f"hours reduced by 6 (got {t.get('actual_hours_to_date')})")

d = s.delete(f"{API}/daily-reports/{dr1}", params={"reason": "Wrong project, re-entering"})
ok(d.status_code == 200, "DR1 deleted")
t = task_now()
ok(t["pct_complete"] == 0, f"task reverted to 0 after deleting DR1 (got {t['pct_complete']})")
ok(t["status"] in ("not_started", "planned"), f"task status reverted (got {t['status']})")
print("ALL PASS")
