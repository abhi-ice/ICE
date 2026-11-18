"""Client look-ahead template (2026-11-18): a project uploads its client's own
two-week look-ahead form and the export fills it, like the client daily report.

Checked here (no AI key needed; the AI is replaced by a fixed answer):
  * the AI's mapping is written into the client's workbook: values filled,
    stale values blanked, formula cells never touched, "YYYY-MM-DD" written as
    a real date so the form's own date formulas keep working;
  * the day marks come from the schedule, not the AI: the AI only locates the
    grid (__day_grid__, at the top level or inside a sheet), the program puts
    an X on each worked day and clears stale marks;
  * an empty AI answer and a missing template give clear errors;
  * the upload / metadata / remove endpoints (admin or PM only, .xlsx only).

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_la_client bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_lookahead_client_template.py -q
"""
import asyncio
import io
import json
import os
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from openpyxl import Workbook, load_workbook
from starlette.datastructures import UploadFile

from services.dr_template_export import DrTemplateExportError
from services.lookahead_template_export import (
    apply_day_grid, apply_mapping, build_lookahead_snapshot, fill_lookahead_template,
)

_HAS_DB = bool(os.environ.get("MONGO_URL")) and (os.environ.get("DB_NAME") or "").startswith(
    ("test_", "ice_test", "unit_"))
needs_db = pytest.mark.skipif(not _HAS_DB, reason="needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)")

START = date(2026, 11, 23)
DAYS = [START + timedelta(days=i) for i in range(14)]


def _grid():
    """A gather_lookahead_grid result for two activities."""
    working = [d.weekday() < 5 and d != date(2026, 11, 26) for d in DAYS]
    m1 = [w and d <= date(2026, 12, 2) for d, w in zip(DAYS, working)]
    m2 = [w and date(2026, 12, 3) <= d for d, w in zip(DAYS, working)]
    return {
        "project": {"name": "Clinic", "contract_number": "C-1", "station_id": "P-9", "contractor_name": "Our Co"},
        "start": START, "end": DAYS[-1], "days": DAYS, "working": working,
        "holidays_in_period": [date(2026, 11, 26)], "date_issued": date(2026, 11, 18),
        "rows": [{"name": "Abatement", "code": "A-1", "marks": m1, "pct": 35, "planned_start": "2026-10-08",
                  "planned_finish": "2026-12-02", "actual_start": "2026-10-08"},
                 {"name": "Fireproofing", "code": "A-3", "marks": m2, "pct": 0, "planned_start": "2026-12-03",
                  "planned_finish": "2026-12-11"}],
        "notes": ["Shop drawings: 20% complete, finish 12/16/26"], "delaying": [],
    }


def _form() -> bytes:
    """A client day-grid form: start date in B5, formulas for the rest, stale data."""
    wb = Workbook()
    ws = wb.active
    ws.title = "LA"
    ws["A1"] = "OWNER REP GROUP"
    ws["A2"] = "Project:"
    ws["A4"] = "ACTIVITIES"
    ws["B5"] = datetime(2026, 11, 2)
    for i in range(1, 14):
        ws.cell(row=5, column=2 + i, value=f"={ws.cell(row=5, column=1 + i).coordinate}+1")
    ws["A6"] = "Old activity"
    ws["C6"] = "X"
    ws["D7"] = "X"
    ws["A12"] = "Notes:"
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


class _FakeChat:
    def __init__(self, answer):
        self.answer = answer

    async def send_message(self, msg):
        self.seen = msg.text
        return self.answer


def test_snapshot_has_worked_days_and_notes():
    snap = build_lookahead_snapshot(_grid())
    a = {x["activity"]: x for x in snap["activities"]}
    assert a["Abatement"]["worked_days"][0] == "2026-11-23"
    assert "2026-11-26" not in a["Abatement"]["worked_days"]
    assert a["Fireproofing"]["worked_days"] == ["2026-12-03", "2026-12-04"]
    assert a["Abatement"]["start"] == "2026-10-08"
    assert snap["notes_for_the_notes_area"][-1] == "No work on 11/26/2026 (holiday)."
    assert snap["period"]["start"] == "2026-11-23"


def test_fill_writes_values_dates_and_program_marks(monkeypatch):
    import services.dr_template_export as dte

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(dte, "_track_usage", _noop)
    import services.lookahead_template_export as lte
    monkeypatch.setattr(lte, "_track_usage", _noop)
    answer = json.dumps({"LA": {
        "B2": "Clinic", "B5": "2026-11-23", "C5": "2026-01-01",  # C5 is a formula: must be skipped
        "A6": "Abatement", "A7": "Fireproofing", "B12": "Shop drawings",
        "C6": "X",  # the AI guessing a mark: replaced by the program's marks
        "__day_grid__": {"mark": "X", "date_columns": {d.isoformat(): chr(ord("B") + i) for i, d in enumerate(DAYS)},
                         "activity_rows": {"a1": 6, "a2": 7}, "first_row": 6, "last_row": 10},
    }})
    out = asyncio.run(fill_lookahead_template(_form(), grid=_grid(), tenant_id="t", request_id="r",
                                              chat_factory=lambda sid, sm: _FakeChat(answer)))
    ws = load_workbook(io.BytesIO(out)).active
    assert ws["B5"].value == datetime(2026, 11, 23)       # real date
    assert ws["C5"].value == "=B5+1"                       # formula kept
    assert ws["A6"].value == "Abatement" and ws["B2"].value == "Clinic"
    row6 = "".join("X" if ws.cell(row=6, column=c).value == "X" else "." for c in range(2, 16))
    row7 = "".join("X" if ws.cell(row=7, column=c).value == "X" else "." for c in range(2, 16))
    assert row6 == "XXX.X..XXX...."
    assert row7 == "..........XX.."                        # stale D7 mark cleared
    assert ws["A1"].value == "OWNER REP GROUP"             # branding untouched


def test_duplicate_names_stacked_weeks_and_bad_marks():
    g = _grid()
    g["rows"][1]["name"] = "Abatement"  # two activities, same name
    snap = build_lookahead_snapshot(g)
    wb = load_workbook(io.BytesIO(_form()))
    cols1 = {d.isoformat(): chr(ord("B") + i) for i, d in enumerate(DAYS[:7])}
    cols2 = {d.isoformat(): chr(ord("B") + i) for i, d in enumerate(DAYS[7:])}
    n = apply_day_grid(wb, [
        {"sheet": "LA", "mark": "X = work", "date_columns": cols1, "activity_rows": {"a1": 6, "a2": 7}},
        {"sheet": "LA", "mark": "X", "date_columns": cols2, "activity_rows": {"a1": 16, "a2": 17}},
    ], snap)
    ws = wb.active
    line = lambda r: "".join("X" if ws.cell(row=r, column=c).value == "X" else "." for c in range(2, 9))
    assert (line(6), line(7), line(16), line(17)) == ("XXX.X..", ".......", "XXX....", "...XX..")
    assert n == 4 + 3 + 2


def test_day_grid_ignores_bad_spec():
    wb = load_workbook(io.BytesIO(_form()))
    snap = build_lookahead_snapshot(_grid())
    assert apply_day_grid(wb, {"sheet": "nope"}, snap) == 0
    assert apply_day_grid(wb, {"sheet": "LA", "date_columns": {"2026-11-23": "??"}}, snap) == 0
    assert apply_day_grid(wb, {"sheet": ["LA"], "date_columns": {}}, snap) == 0
    assert apply_day_grid(wb, {"sheet": "LA", "date_columns": ["B", "C"], "activity_rows": []}, snap) == 0
    assert apply_day_grid(wb, {"sheet": "LA", "date_columns": {"2026-11-23": "XFZ"},
                               "activity_rows": {"a1": 0}}, snap) == 0
    assert apply_mapping(wb, {"LA": {"ZZZZ99999999": "x", "__note__": 1}, "Missing": {"A1": 1}}) == 0


def test_empty_answer_is_a_clear_error(monkeypatch):
    import services.lookahead_template_export as lte

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(lte, "_track_usage", _noop)
    with pytest.raises(Exception) as e:
        asyncio.run(fill_lookahead_template(_form(), grid=_grid(), tenant_id="t", request_id="r",
                                            chat_factory=lambda sid, sm: _FakeChat("sorry")))
    assert type(e.value).__name__ == "DrTemplateExportError"
    with pytest.raises(Exception) as e:
        asyncio.run(fill_lookahead_template(b"not a workbook", grid=_grid(), tenant_id="t", request_id="r",
                                            chat_factory=lambda sid, sm: _FakeChat("{}")))
    assert type(e.value).__name__ == "DrTemplateExportError" and "could not be opened" in str(e.value)


# ---------------------------------------------------------------- endpoints
import database  # noqa: E402


def _ctx(tenant_id, role="admin"):
    return {"tenant_id": tenant_id, "user": {"id": "u-" + tenant_id[:6], "role": role, "email": "a@example.com"},
            "request_id": "req-test"}


def _run(coro_fn):
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-laclient-" + uuid.uuid4().hex[:8]
        db = database.get_db()
        try:
            return await coro_fn(db, tenant)
        finally:
            for c in ("projects", "audit_log", "audit_chain_heads", "users"):
                await db[c].delete_many({"tenant_id": tenant})
            database._client.close()
            database._client, database._db = None, None
    return asyncio.run(wrapper())


@needs_db
def test_upload_meta_remove_and_rules(monkeypatch):
    from routers import projects_router as pr
    from services import storage
    store = {}

    def fake_put(path, data, ct):
        store[path] = data
    monkeypatch.setattr(storage, "put_object", fake_put)

    async def go(db, tenant):
        pid = "p-" + uuid.uuid4().hex[:8]
        await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Clinic"})
        assert await pr.get_lookahead_template_meta(pid, ctx=_ctx(tenant)) == {"present": False}
        with pytest.raises(HTTPException) as e:
            await pr.upload_lookahead_template(pid, file=UploadFile(file=io.BytesIO(_form()), filename="f.xlsx"),
                                               ctx=_ctx(tenant, role="super"))
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e:
            await pr.upload_lookahead_template(pid, file=UploadFile(file=io.BytesIO(b"%PDF"), filename="f.pdf"),
                                               ctx=_ctx(tenant))
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            await pr.upload_lookahead_template(pid, file=UploadFile(file=io.BytesIO(b"not excel"), filename="f.xlsx"),
                                               ctx=_ctx(tenant))
        assert e.value.status_code == 400
        out = await pr.upload_lookahead_template(
            pid, file=UploadFile(file=io.BytesIO(_form()), filename="Client LA.xlsx"), ctx=_ctx(tenant))
        assert out["file_name"] == "Client LA.xlsx" and len(store) == 1
        meta = await pr.get_lookahead_template_meta(pid, ctx=_ctx(tenant))
        assert meta["present"] and meta["file_name"] == "Client LA.xlsx"
        proj = await db.projects.find_one({"id": pid})
        assert proj["lookahead_template_file_url"] in store
        assert not proj.get("dr_template_file_url")  # the daily report template is separate
        assert (await pr.delete_lookahead_template(pid, ctx=_ctx(tenant)))["ok"]
        assert await pr.get_lookahead_template_meta(pid, ctx=_ctx(tenant)) == {"present": False}
        assert (await pr.delete_lookahead_template(pid, ctx=_ctx(tenant))).get("already_empty")
        with pytest.raises(HTTPException) as e:
            await pr.upload_lookahead_template(
                pid, file=UploadFile(file=io.BytesIO(b"0" * (10 * 1024 * 1024 + 1)), filename="big.xlsx"),
                ctx=_ctx(tenant))
        assert e.value.status_code == 413
    _run(go)


@needs_db
def test_client_export_without_template_and_pdf(monkeypatch):
    from services.tasks_exports import render_lookahead_export
    import services.lookahead_template_export as lte
    # Some older test files leave a stand-in "database" module behind; use
    # the same database this test writes to.
    monkeypatch.setattr(lte, "get_db", database.get_db)
    monkeypatch.setattr(lte, "tenant_filter", database.tenant_filter)

    async def go(db, tenant):
        pid = "p-" + uuid.uuid4().hex[:8]
        await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Clinic"})
        # (class identity can differ when another test swaps modules: check by name)
        with pytest.raises(Exception) as e:
            await render_lookahead_export(tenant_id=tenant, project_id=pid, fmt="xlsx", layout="client")
        assert type(e.value).__name__ == "DrTemplateExportError"
        with pytest.raises(ValueError):
            await render_lookahead_export(tenant_id=tenant, project_id=pid, fmt="pdf", layout="client")
        blob, *_ = await render_lookahead_export(tenant_id=tenant, project_id="p-missing", fmt="xlsx", layout="client")
        assert blob == b""
    _run(go)
