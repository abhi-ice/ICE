"""Per-project access from a field supervisor's phone (2026-11-16).

  * The phone's background offline warm-up (X-ICE-Prefetch: 1) reads every
    project while the switch is off; those reads are not counted in the
    watch log, but a real view and every save still are.
  * The admin's report lists refused saves before views, so a pile of views
    can never push a save a phone is holding off the list.
  * With the switch on, an assigned supervisor's daily-report save passes
    and an unassigned one gets 403 (a phone keeps it), never 404.

Reuses the in-memory fake database and seeded world from
test_project_access_foundation. Nothing is left in sys.modules.
"""
import importlib

from test_project_access_foundation import (  # noqa: F401  (fixtures)
    A, P1, P2, _app, _ctx, _h, db, enforced, run,
)

PREFETCH = {"X-ICE-Prefetch": "1"}


def test_prefetch_reads_counted_apart_views_and_saves_counted(db):
    c = _app(db)
    r = c.get(f"/api/projects/{P2}", headers={**_h("super1"), **PREFETCH})
    assert r.status_code == 200
    # Recorded (anyone can send the header, so it must leave a trace) but
    # only as a background download, not as the person opening it.
    [w] = db.project_access_watch.docs
    assert w.get("prefetch_count") == 1 and not w.get("count")
    # The same read without the header is a person opening the project.
    assert c.get(f"/api/projects/{P2}", headers=_h("super1")).status_code == 200
    [w] = db.project_access_watch.docs
    assert w.get("count") == 1 and w.get("prefetch_count") == 1
    # A save is always recorded, header or not.
    assert c.patch(f"/api/projects/{P2}", headers={**_h("super1"), **PREFETCH}).status_code == 200
    methods = sorted(w["method"] for w in db.project_access_watch.docs)
    assert methods == ["GET", "PATCH"]


def test_report_lists_saves_before_views(db):
    db.project_access_watch.docs += [
        {"tenant_id": A, "user_id": "super1", "project_id": P2, "route": f"/r{i}",
         "method": "GET", "write": False, "count": 1, "last_at": f"2026-11-16T10:{i:02d}"}
        for i in range(30)
    ] + [{"tenant_id": A, "user_id": "super1", "project_id": P2, "route": "/save",
          "method": "POST", "write": True, "count": 1, "last_at": "2026-11-01T00:00"}]
    pm = importlib.import_module("routers.project_members_router")
    out = run(pm.project_access_report(ctx=_ctx("admin1")))
    assert out["watch"][0]["route"] == "/save"


def test_enforced_assigned_save_passes_unassigned_save_403(db, enforced):
    c = _app(db)
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": A, "project_id": P1})
    db.daily_report_activities.docs.append({"id": "act1", "daily_report_id": "dr1"})
    assert c.patch("/api/daily-report-activities/act1", headers=_h("super1")).status_code == 200
    r = c.patch("/api/daily-report-activities/act9", headers=_h("super1"))
    assert r.status_code == 403
    assert r.json()["detail"] == "You are not assigned to this project"
    # A temporary offline id that does not resolve yet is left to the handler.
    assert c.patch("/api/daily-report-activities/offline-row-1",
                   headers=_h("super1")).status_code == 200
