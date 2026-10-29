"""Manual e2e: empty DR + picker, delay push/in-progress, open-delay gate,
actual dates from DRs, out-of-order logic proposal, names.
Run: API_URL=... python tests/manual_schedule_feedback.py"""
import os
import sys
import time
from datetime import date, timedelta
import requests

API = os.environ["API_URL"] + "/api"
s = requests.Session()
s.headers["Authorization"] = "Bearer " + s.post(f"{API}/auth/login", json={
    "email": "qa.super@example.com", "password": "QaSuper!2026"}).json()["access_token"]


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def bday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


pid = s.post(f"{API}/projects", json={"name": "QA Feedback Project"}).json()["id"]
_t = date.today() + timedelta(days=7)
mon = _t - timedelta(days=_t.weekday())
d1 = date.today() - timedelta(days=1)
d2 = date.today()
A = s.post(f"{API}/tasks", json={"project_id": pid, "activity_code": "D100", "description": "Electrical",
                                 "planned_start": mon.isoformat(), "planned_finish": (mon + timedelta(days=4)).isoformat(),
                                 "duration_days": 5}).json()
B = s.post(f"{API}/tasks", json={"project_id": pid, "activity_code": "D200", "description": "Electrical",
                                 "planned_start": (mon + timedelta(days=7)).isoformat(),
                                 "planned_finish": (mon + timedelta(days=11)).isoformat(), "duration_days": 5,
                                 "predecessors": [{"code": "D100", "type": "FS", "lag_days": 0}]}).json()
ok("id" in A and "id" in B, "tasks created")

dr = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": d1.isoformat()}).json()
grid = s.get(f"{API}/daily-reports/{dr['id']}/grid").json()
ok(grid == [], f"new DR starts empty ({len(grid)} rows)")
pick = s.get(f"{API}/daily-reports/{dr['id']}/activity-picker").json()
ok({p["id"] for p in pick} >= {A["id"], B["id"]}, f"picker lists field activities ({len(pick)})")

d = s.post(f"{API}/delays", json={"project_id": pid, "date": d1.isoformat(), "impact_start_date": mon.isoformat(), "description": "Wet trench",
                                  "linked_task_ids": [A["id"]],
                                  "per_task_impacts": [{"task_id": A["id"], "projected_impact_days": 3}]})
ok(d.status_code == 200, f"delay created {d.status_code} {d.text[:200]}")
d = d.json()
ok(d["status"] == "in_progress", f"delay status in_progress (got {d['status']})")
ta = s.get(f"{API}/tasks/{A['id']}").json()
ok(ta.get("slip_days") == 3, f"A pushed exactly 3 working days (slip {ta.get('slip_days')}, start {ta.get('projected_start_date')})")

r = s.post(f"{API}/daily-reports/{dr['id']}/grid", json={"schedule_activity_id": A["id"]})
ok(r.status_code == 409 and r.json()["detail"]["code"] == "open_delay", f"add blocked by open delay ({r.status_code})")
od = r.json()["detail"]["delays"][0]
ok(od["impact_days"] == 3, "open delay reports 3 impact days")

res = s.patch(f"{API}/delays/{d['id']}", json={"status": "resolved", "resolved_date": bday(mon + timedelta(days=2)).isoformat(),
                                               "resolved_note": "Pumped out"})
ok(res.status_code == 200, f"delay resolved {res.status_code} {res.text[:200]}")
ta = s.get(f"{API}/tasks/{A['id']}").json()
ok(ta.get("slip_days") == 2, f"resolved early -> pushes by real 2 days (slip {ta.get('slip_days')})")

r = s.post(f"{API}/daily-reports/{dr['id']}/grid", json={"schedule_activity_id": A["id"]})
ok(r.status_code == 200, f"A added after resolve {r.status_code}")
rowA = r.json()

# Work B out of order (A not done) on a later DR
dr2 = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": d2.isoformat()}).json()
rowB = s.post(f"{API}/daily-reports/{dr2['id']}/grid", json={"schedule_activity_id": B["id"]}).json()
p = s.patch(f"{API}/daily-report-activities/{rowB['id']}", json={"pct_complete_end_of_day": 10, "hours_worked": 4})
ok(p.status_code == 422 and "admin" in p.json()["detail"]["message"], f"out-of-order progress gated ({p.status_code})")
props = s.get(f"{API}/projects/{pid}/logic-proposals").json()
ok(any(x["task_id"] == B["id"] and x["kind"] == "out_of_order" for x in props), f"out-of-order proposal created ({len(props)})")
prop = next(x for x in props if x["task_id"] == B["id"])
ap = s.post(f"{API}/logic-proposals/{prop['id']}/approve", json={"predecessors": [{"code": "D100", "type": "SS", "lag_days": 0}]})
ok(ap.status_code == 200, f"proposal approved {ap.status_code}")
tb = s.get(f"{API}/tasks/{B['id']}").json()
ok(tb["predecessors"][0]["type"] == "SS", "B predecessor now SS")
p = s.patch(f"{API}/daily-report-activities/{rowA['id']}", json={"pct_complete_end_of_day": 100, "hours_worked": 8})
ok(p.status_code == 200, "A finished on DR1")
ta = s.get(f"{API}/tasks/{A['id']}").json()
ok(str(ta.get("actual_finish"))[:10] == d1.isoformat(), f"A actual_finish = DR date ({ta.get('actual_finish')})")
ok(ta.get("projected_finish_date") == d1.isoformat(), f"real finish takes over ({ta.get('projected_finish_date')})")

p = s.patch(f"{API}/daily-report-activities/{rowB['id']}", json={"pct_complete_end_of_day": 10, "hours_worked": 4})
ok(p.status_code == 200, f"B progress logged after approval {p.status_code} {p.text[:200]}")
tb = s.get(f"{API}/tasks/{B['id']}").json()
ok(str(tb.get("actual_start"))[:10] == d2.isoformat(), f"B actual_start from DR ({tb.get('actual_start')})")

dele = s.delete(f"{API}/daily-reports/{dr2['id']}", params={"reason": "testing actual reversal"})
ok(dele.status_code == 200, "DR2 deleted")
tb = s.get(f"{API}/tasks/{B['id']}").json()
ok(tb.get("actual_start") in (None, ""), f"B actual_start cleared after DR delete ({tb.get('actual_start')})")
print("ALL PASS")
