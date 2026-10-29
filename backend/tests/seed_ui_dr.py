"""Seed UI test data for DR delete/notes testing. Prints JSON of ids."""
import os, json
from datetime import date, timedelta
import requests
API = os.environ["API_URL"] + "/api"
s = requests.Session()
s.headers["Authorization"] = "Bearer " + s.post(f"{API}/auth/login", json={"email": "qa.super@example.com", "password": "QaSuper!2026"}).json()["access_token"]
pid = s.post(f"{API}/projects", json={"name": "TEST_UI_DR_Notes"}).json()["id"]
t = date.today()
out = {"project_id": pid, "drs": {}}
tid = s.post(f"{API}/tasks", json={"project_id": pid, "activity_code": "UI100", "description": "Form slab edge",
                                   "planned_start": (t - timedelta(days=20)).isoformat(),
                                   "planned_finish": (t + timedelta(days=20)).isoformat()}).json()["id"]
out["task_id"] = tid
for name, off in (("draft", 1), ("submitted", 2), ("submitted2", 3), ("approved", 4), ("draft_notes", 5)):
    d = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": (t - timedelta(days=off)).isoformat()}).json()
    g = s.get(f"{API}/daily-reports/{d['id']}/grid").json()
    rows = [r for r in g if r["schedule_activity_id"] == tid]
    row = rows[0] if rows else s.post(f"{API}/daily-reports/{d['id']}/grid", json={"schedule_activity_id": tid}).json()
    s.patch(f"{API}/daily-report-activities/{row['id']}", json={"pct_complete_end_of_day": 10 * off, "hours_worked": 2})
    if name.startswith("submitted") or name == "approved":
        r = s.post(f"{API}/daily-reports/{d['id']}/submit", json={})
        print(name, "submit", r.status_code)
    if name == "approved":
        r = s.post(f"{API}/daily-reports/{d['id']}/approve", json={})
        print("approve", r.status_code, r.text[:150])
    out["drs"][name] = {"id": d["id"], "row_id": row["id"], "date": d["report_date"]}
print(json.dumps(out))
