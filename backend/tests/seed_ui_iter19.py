"""Seed UI data for iter19 (delay preview + names by area + digest). Prints JSON ids.
Run: API_URL=... python tests/seed_ui_iter19.py"""
import os, json, time
from datetime import date, timedelta
import requests
from pymongo import MongoClient
from dotenv import dotenv_values

API = os.environ["API_URL"] + "/api"
s = requests.Session()
s.headers["Authorization"] = "Bearer " + s.post(f"{API}/auth/login", json={"email": "qa.super@example.com", "password": "QaSuper!2026"}).json()["access_token"]
cfg = dotenv_values("/app/backend/.env")
db = MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]
pid = s.post(f"{API}/projects", json={"name": "TEST_UI_iter19"}).json()["id"]
t = date.today()
mon = t + timedelta(days=7)
mon -= timedelta(days=mon.weekday())


def mk(code, desc, start, preds=None, path=None):
    body = {"project_id": pid, "activity_code": code, "description": desc,
            "planned_start": start.isoformat(), "planned_finish": (start + timedelta(days=4)).isoformat(),
            "duration_days": 5}
    if preds:
        body["predecessors"] = [{"code": p, "type": "FS", "lag_days": 0} for p in preds]
    r = s.post(f"{API}/tasks", json=body).json()
    if path:
        db.tasks.update_one({"id": r["id"]}, {"$set": {"section_path": path}})
    return r["id"]


out = {"project_id": pid, "tasks": {
    "a1": mk("A100", "Electrical", mon, path=["Building A", "Level 1"]),
    "a2": mk("A200", "Electrical", mon + timedelta(days=7), ["A100"], path=["Building A", "Level 2"]),
    "a3": mk("A300", "Drywall", mon + timedelta(days=14), ["A200"], path=["Building A", "Level 2"]),
    "b1": mk("B100", "Plumbing", mon, path=["Building B", "Level 1"]),
    "b2": mk("B200", "Plumbing", mon + timedelta(days=7), path=["Building B", "Level 2"]),
    "o1": mk("P100", "Painting", mon),
    "o2": mk("P200", "Painting", mon + timedelta(days=21)),
}}
dr_d = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": (t - timedelta(days=1)).isoformat()}).json()
dr_m = s.post(f"{API}/daily-reports", json={"project_id": pid, "report_date": (t - timedelta(days=2)).isoformat()}).json()
out["dr_desk"], out["dr_mob"] = dr_d["id"], dr_m["id"]
for dr in (dr_d, dr_m):
    r = s.post(f"{API}/daily-reports/{dr['id']}/grid", json={"schedule_activity_id": out["tasks"]["a1"]})
    out.setdefault("rows", []).append(r.json().get("id"))
s.post(f"{API}/projects/{pid}/activity-names/generate")
for _ in range(30):
    if s.get(f"{API}/projects/{pid}/activity-names/job").json().get("status") != "running":
        break
    time.sleep(2)
out["names"] = [(x["id"], x["activity_code"], x["suggested_name"]) for x in s.get(f"{API}/projects/{pid}/activity-names").json()]
print(json.dumps(out))
