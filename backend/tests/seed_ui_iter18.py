"""Seed UI data for iter18 (picker / carried delays / names). Prints JSON ids.
Run: API_URL=... python tests/seed_ui_iter18.py"""
import os, json
from datetime import date, timedelta
import requests
from pymongo import MongoClient
from dotenv import dotenv_values

API = os.environ["API_URL"] + "/api"
s = requests.Session()
s.headers["Authorization"] = "Bearer " + s.post(f"{API}/auth/login", json={"email": "qa.super@example.com", "password": "QaSuper!2026"}).json()["access_token"]
cfg = dotenv_values("/app/backend/.env")
db = MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]
pid = s.post(f"{API}/projects", json={"name": "TEST_UI_iter18"}).json()["id"]
t = date.today()
mon = t + timedelta(days=7)
mon -= timedelta(days=mon.weekday())


def mk(code, desc, cat="field_work", path=None):
    r = s.post(f"{API}/tasks", json={"project_id": pid, "activity_code": code, "description": desc,
                                    "planned_start": mon.isoformat(), "planned_finish": (mon + timedelta(days=4)).isoformat(),
                                    "duration_days": 5, "work_category": cat}).json()
    if path:
        db.tasks.update_one({"id": r["id"]}, {"$set": {"section_path": path}})
    return r["id"]


out = {"project_id": pid, "tasks": {
    "elecA": mk("E100", "Electrical", path=["Building A", "Level 1"]),
    "elecB": mk("E200", "Electrical", path=["Building B", "Level 2"]),
    "conc": mk("C100", "Concrete footings", path=["Building A", "Foundations"]),
    "conc2": mk("C200", "Concrete slab", path=["Building B", "Foundations"]),
    "paper": mk("P100", "Submittal package", "construction_paperwork"),
}}
dr_old = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": (t - timedelta(days=3)).isoformat()}).json()
dr_new = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": (t - timedelta(days=1)).isoformat()}).json()
dr_mob = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": (t - timedelta(days=2)).isoformat()}).json()
out["dr_old"], out["dr_new"], out["dr_mob"] = dr_old["id"], dr_new["id"], dr_mob["id"]
for key, desc, dr in (("conc", "TEST_ Wet trench", dr_old), ("conc2", "TEST_ Rebar late", dr_old)):
    d = s.post(f"{API}/delays", json={"project_id": pid, "date": dr["report_date"], "daily_report_id": dr["id"],
                                      "description": desc, "linked_task_ids": [out["tasks"][key]],
                                      "per_task_impacts": [{"task_id": out["tasks"][key], "projected_impact_days": 3}]}).json()
    out["delay_" + key] = d["id"]
# approve a name for elecA via schedule-review API
s.post(f"{API}/projects/{pid}/activity-names/generate")
import time
for _ in range(30):
    if s.get(f"{API}/projects/{pid}/activity-names/job").json().get("status") != "running":
        break
    time.sleep(2)
print(json.dumps(out))
