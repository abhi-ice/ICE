"""Railway move (2026-11-16): export safety, audit chain, lighter DR context.

Covers:
  * formula injection: user text such as '=HYPERLINK(...)' in a task code
    must not become a live formula in the tasks/look-ahead .xlsx or in the
    CSV exports; ordinary text ('-5', '- see note') stays readable; numbers
    are untouched; the client daily-report template keeps its own formulas
    while text written from the report is stored as plain text;
  * the audit hash chain stays one unbroken chain when many saves land at
    the same moment (also across several server processes), continues
    after entries written by the old code, and never makes a save fail;
  * GET /daily-reports/{id}/context sends only the audit fields the Audit
    trail panel shows, and prints the before/after bundle size.

No network, no real Mongo: a small in-memory fake database stands in.
Every fake call yields to the event loop so concurrent writers interleave.
"""
import asyncio
import csv
import hashlib
import io
import json
import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

openpyxl = pytest.importorskip("openpyxl")

from services import xlsx_safe  # noqa: E402
from services.xlsx_safe import escape_formula, is_formula_like  # noqa: E402

TID = "tenant-a"


# ================================================================ fakes
def _get(doc, key):
    return doc.get(key)


def _match(doc, flt):
    for k, v in flt.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            val = _get(doc, k)
            for op, arg in v.items():
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
                if op == "$exists" and (k in doc) != bool(arg):
                    return False
            continue
        if _get(doc, k) != v:
            return False
    return True


def _project(doc, proj):
    out = dict(doc)
    out.pop("_id", None)
    if proj:
        keep = [k for k, v in proj.items() if v and k != "_id"]
        if keep:
            out = {k: out[k] for k in keep if k in out}
    return out


class _Cursor:
    def __init__(self, docs, proj):
        self.docs = docs
        self.proj = proj
        self._limit = None

    def sort(self, key, direction=None):
        keys = key if isinstance(key, list) else [(key, direction or 1)]
        for k, d in reversed(keys):
            self.docs.sort(key=lambda x: (x.get(k) is None, x.get(k) or ""), reverse=d < 0)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _rows(self):
        rows = self.docs if self._limit is None else self.docs[: self._limit]
        return [_project(d, self.proj) for d in rows]

    async def to_list(self, n=None):
        await asyncio.sleep(0)
        rows = self._rows()
        return rows if n is None else rows[:n]

    def __aiter__(self):
        self._it = iter(self._rows())
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Coll:
    def __init__(self):
        self.docs = []
        self.fail_insert = False
        self.never_swap = False

    async def find_one(self, flt, proj=None, sort=None):
        await asyncio.sleep(0)
        cur = _Cursor([d for d in self.docs if _match(d, flt)], proj)
        if sort:
            cur.sort(sort)
        rows = cur._rows()
        return rows[0] if rows else None

    def find(self, flt, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt)], proj)

    async def insert_one(self, doc):
        await asyncio.sleep(0)
        if self.fail_insert:
            raise RuntimeError("insert failed")
        self.docs.append(dict(doc))

    async def update_one(self, flt, upd, upsert=False):
        await asyncio.sleep(0)
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set", {}))
                return
        if upsert:
            new = {k: v for k, v in flt.items() if not k.startswith("$")}
            new.update(upd.get("$setOnInsert", {}))
            new.update(upd.get("$set", {}))
            self.docs.append(new)

    async def find_one_and_update(self, flt, upd):
        await asyncio.sleep(0)
        if self.never_swap:
            return None
        # Atomic in the fake: no await between the match and the update.
        for d in self.docs:
            if _match(d, flt):
                before = dict(d)
                d.update(upd.get("$set", {}))
                return before
        return None


class _DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


# ====================================================== formula escaping
@pytest.mark.parametrize("text", [
    "=HYPERLINK(\"http://x\",\"click\")", "=1+1", "  =cmd", "@SUM(A1:A2)",
    "+SUM(A1)", "-2+A1", "-cmd|' /C calc'!A0", "+HYPERLINK(1)", "\tfoo", "\rfoo",
    "-Sheet1!B2",
])
def test_formula_like_text_is_caught(text):
    assert is_formula_like(text)
    assert escape_formula(text) == "'" + text


@pytest.mark.parametrize("value", [
    "-5", "-5.25", "- see note", "+1 555 0100", "-", "", "  ", "Pour slab",
    "Level 2 - east", "note: =x is fine mid-text", 5, -5, 3.2, None, True,
])
def test_ordinary_values_are_left_alone(value):
    assert not is_formula_like(value)
    assert escape_formula(value) is value or escape_formula(value) == value


def test_tasks_xlsx_writes_user_text_as_plain_text():
    from services import tasks_exports as te
    evil = '=HYPERLINK("http://evil.example","open me")'
    payload = {
        "project": {"name": "=cmd|' /C calc'!A0"},
        "generated_at": "2026-11-16", "count": 2,
        "rows": [
            {"activity_code": evil, "name": "- see note", "duration_days": 5},
            {"activity_code": "A-100", "name": "-5", "duration_days": -3},
        ],
    }
    ws = openpyxl.load_workbook(io.BytesIO(te.render_tasks_xlsx(payload))).active
    cols = [c.value for c in ws[2]]
    code_col = None
    for i, (key, label) in enumerate(te.TASKS_COLUMNS, start=1):
        if key == "activity_code":
            code_col = i
    assert code_col is not None and cols[code_col - 1]
    cell = ws.cell(row=3, column=code_col)
    assert cell.value == evil
    assert cell.data_type == "s"          # stored as text, not a formula
    # Nothing in the sheet is a formula.
    for row in ws.iter_rows():
        for c in row:
            assert c.data_type != "f", (c.coordinate, c.value)
    # Numbers stay numbers.
    for i, (key, _) in enumerate(te.TASKS_COLUMNS, start=1):
        if key == "duration_days":
            assert ws.cell(row=3, column=i).value == 5
            assert ws.cell(row=4, column=i).value == -3


def test_audit_csv_export_escapes_formula_text():
    from services.exhibit_exports import render_audit_csv
    out = render_audit_csv({"rows": [
        {"created_at": "2026-11-16T10:00:00+00:00", "op": "UPDATE",
         "table_name": "tasks", "record_id": "=HYPERLINK(\"x\")",
         "actor_id": "- see note", "hash": "ab", "prev_hash": ""},
    ]}).decode()
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[1][3] == "'=HYPERLINK(\"x\")"
    assert rows[1][4] == "- see note"
    assert rows[1][0] == "2026-11-16T10:00:00+00:00"


def test_calendar_csv_uses_escape_row():
    # The calendar reconciliation CSV runs its rows through escape_row.
    import inspect
    from routers import calendar_reconciliation_router as cr
    assert "escape_row(" in inspect.getsource(cr)
    assert xlsx_safe.escape_row(["2026-11-16", "=1+1", 4.5, "-2"]) == \
        ["2026-11-16", "'=1+1", 4.5, "-2"]


def test_client_template_keeps_its_formulas_and_report_text_is_plain(monkeypatch):
    dte = pytest.importorskip("services.dr_template_export")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "Notes:"
    ws["A30"] = "=SUM(B1:B2)"
    buf = io.BytesIO()
    wb.save(buf)

    answer = json.dumps({"Sheet1": {
        "B1": '=HYPERLINK("http://evil.example","x")',
        "B2": "- see note", "B3": 12, "A30": "overwrite?"}})

    class _Chat:
        async def send_message(self, _m):
            return answer

    monkeypatch.setattr(dte, "_new_chat", lambda sid, sysmsg: _Chat())
    monkeypatch.setattr(dte, "_track_usage", lambda *a, **k: asyncio.sleep(0))
    out = asyncio.run(dte.inject_dr_into_template(
        buf.getvalue(), dr={"report_date": "2026-11-16"}, project={}, activities=[],
        presence=[], delays=[], signer_name="", tenant_id="t", request_id="r"))
    ws = openpyxl.load_workbook(io.BytesIO(out)).active
    assert ws["A30"].value == "=SUM(B1:B2)" and ws["A30"].data_type == "f"
    assert ws["B1"].value.startswith("=HYPERLINK") and ws["B1"].data_type == "s"
    assert ws["B2"].value == "- see note"
    assert ws["B3"].value == 12


# ============================================================ audit chain
_AUDIT = None


def _audit_module():
    """The real audit.py, loaded under a private name: another test module
    puts a stub 'audit' in sys.modules, and our copy must not leak out."""
    global _AUDIT
    if _AUDIT is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_audit_under_test", os.path.join(_BACKEND, "audit.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _AUDIT = mod
    return _AUDIT


@pytest.fixture
def adb(monkeypatch):
    audit = _audit_module()
    d = _DB()
    monkeypatch.setattr(audit, "get_db", lambda: d)
    return d


async def _write(i, tenant=TID):
    audit = _audit_module()
    return await audit.write_audit(
        tenant_id=tenant, table_name="daily_reports", op="UPDATE",
        record_id=f"dr-{i % 7}", before={"n": i}, after={"n": i + 1},
        actor_id="u1", request_id=None)


def test_concurrent_writes_make_one_unbroken_chain(adb):
    audit = _audit_module()

    async def go():
        await asyncio.gather(*[_write(i) for i in range(300)])
        return await audit.verify_chain(TID, limit=1000)

    res = asyncio.run(go())
    rows = adb.audit_log.docs
    assert len(rows) == 300
    assert len({r["prev_hash"] for r in rows}) == 300   # no fork
    assert not any(r.get("unchained") for r in rows)
    assert res["valid"] is True and res["entries_checked"] == 300


def test_several_processes_still_chain_correctly(adb):
    # Each "process" has its own lock, so only the compare-and-swap keeps
    # them apart: call the chained append directly from 4 concurrent loops.
    audit = _audit_module()

    async def process(p):
        for i in range(25):
            entry = {"id": f"{p}-{i}", "tenant_id": TID, "table_name": "tasks",
                     "op": "UPDATE", "record_id": "t", "before_data": {"p": p},
                     "after_data": {"i": i}, "actor_id": None, "request_id": None}
            assert await audit._append_chained(adb, entry) is not None

    async def go():
        await asyncio.gather(*[process(p) for p in range(4)])
        return await audit.verify_chain(TID)

    res = asyncio.run(go())
    assert res["valid"] is True and res["entries_checked"] == 100


def test_new_entries_continue_after_old_style_entries(adb):
    audit = _audit_module()
    # Entries written by the old code: no head document, created_at without
    # fixed microseconds, plus a row with no hash at all (written directly).
    prev = ""
    for i, ts in enumerate(["2026-11-01T10:00:00+00:00", "2026-11-01T10:00:00.500000+00:00"]):
        body = prev + audit._stable_dumps({"i": i}) + audit._stable_dumps({}) + "INSERT" + "tasks" + ts
        h = hashlib.sha256(body.encode()).hexdigest()
        adb.audit_log.docs.append({"id": f"old{i}", "tenant_id": TID, "table_name": "tasks",
                                   "op": "INSERT", "record_id": "t", "before_data": None,
                                   "after_data": {"i": i}, "prev_hash": prev, "hash": h,
                                   "created_at": ts})
        prev = h
    adb.audit_log.docs.append({"id": "voice", "tenant_id": TID, "table_name": "daily_reports",
                               "op": "DR_VOICE_INTAKE_APPLIED", "record_id": "d",
                               "before_data": {}, "after_data": {}, "created_at": "2026-11-02T00:00:00+00:00"})

    async def go():
        await asyncio.gather(*[_write(i) for i in range(20)])
        return await audit.verify_chain(TID)

    res = asyncio.run(go())
    assert res["valid"] is True
    assert res["entries_checked"] == 22 and res["unchained_entries"] == 1
    first_new = min((r for r in adb.audit_log.docs if r["id"] not in ("old0", "old1", "voice")),
                    key=lambda r: r["created_at"])
    assert first_new["prev_hash"] == prev


def test_audit_never_fails_the_save(adb):
    audit = _audit_module()
    # The chain head can never be moved (endless conflicts): row still written.
    adb.audit_chain_heads.never_swap = True
    entry = asyncio.run(_write(1))
    assert entry["unchained"] is True and entry["unchained_reason"] == "chain_conflict"
    assert adb.audit_log.docs[-1]["unchained"] is True
    assert asyncio.run(audit.verify_chain(TID))["valid"] is True

    # The database refuses everything: still no exception for the caller.
    adb.audit_chain_heads.never_swap = False
    adb.audit_log.fail_insert = True
    entry = asyncio.run(_write(2))
    assert entry["unchained"] is True


def test_tenants_have_separate_chains(adb):
    audit = _audit_module()

    async def go():
        await asyncio.gather(*[_write(i, tenant=("t1" if i % 2 else "t2")) for i in range(40)])
        return await audit.verify_chain("t1"), await audit.verify_chain("t2")

    r1, r2 = asyncio.run(go())
    assert r1["valid"] and r2["valid"]
    assert r1["entries_checked"] == 20 and r2["entries_checked"] == 20


# ========================================================== DR context
def _synthetic_report_db():
    d = _DB()
    d.projects.docs.append({"id": "p1", "tenant_id": TID, "name": "Synthetic Tower",
                            "project_type": "renovation"})
    dr = {
        "id": "dr1", "tenant_id": TID, "project_id": "p1", "report_date": "2026-11-16",
        "status": "draft", "prepared_by": "u1",
        "weather_conditions": "Overcast, light wind from the northwest",
        "temperature_high_f": 54, "temperature_low_f": 41,
        "notes": ("Crews continued demolition on level 3 east wing. Ceiling grid removed "
                  "in rooms 301-318, duct trunk lines capped. Electrical made safe by the "
                  "electrician before demo. Dumpster swap at 2 pm. ") * 2,
        "work_summary": "Demolition, make-safe, debris removal, temporary protection.",
        "safety_topic": "Ladder use and three points of contact",
        "visitors": ["Owner representative", "Architect"],
        "created_at": "2026-11-16T07:02:00+00:00", "updated_at": "2026-11-16T15:40:00+00:00",
    }
    d.daily_reports.docs.append(dr)
    d.users.docs.append({"id": "u1", "first_name": "Pat", "last_name": "Super"})
    for i in range(100):
        before = dict(dr, notes=dr["notes"] + f" edit {i}")
        after = dict(dr, notes=dr["notes"] + f" edit {i + 1}")
        d.audit_log.docs.append({
            "id": f"a{i}", "tenant_id": TID, "table_name": "daily_reports", "op": "UPDATE",
            "record_id": "dr1", "before_data": before, "after_data": after,
            "actor_id": "u1", "request_id": f"r{i}", "prev_hash": "p" * 64, "hash": "h" * 64,
            "created_at": f"2026-11-16T08:{i // 60:02d}:{i % 60:02d}.000000+00:00",
        })
    return d


def test_context_audit_log_is_slim_and_keeps_panel_fields(monkeypatch, capsys):
    dr_router = pytest.importorskip("routers.daily_reports_router")
    d = _synthetic_report_db()
    monkeypatch.setattr(dr_router, "get_db", lambda: d)
    ctx = {"tenant_id": TID, "user": {"id": "u1", "role": "admin"}}

    monkeypatch.setattr(dr_router, "CONTEXT_AUDIT_FIELDS", {"_id": 0})
    full = asyncio.run(dr_router.daily_report_context("dr1", ctx=ctx))
    monkeypatch.undo()
    monkeypatch.setattr(dr_router, "get_db", lambda: d)
    slim = asyncio.run(dr_router.daily_report_context("dr1", ctx=ctx))

    before = len(json.dumps(full, default=str))
    after = len(json.dumps(slim, default=str))
    with capsys.disabled():
        print(f"\n[context size] before={before} bytes after={after} bytes "
              f"(audit_log {len(json.dumps(full['audit_log']))} -> "
              f"{len(json.dumps(slim['audit_log']))})")

    assert len(slim["audit_log"]) == len(full["audit_log"]) == 100   # same count chip
    for row in slim["audit_log"]:
        assert set(row) == {"id", "op", "table_name", "record_id", "actor_id", "created_at"}
    assert [r["id"] for r in slim["audit_log"]] == [r["id"] for r in full["audit_log"]]
    assert after < before / 5
    # Everything else in the bundle is unchanged.
    assert {k: v for k, v in slim.items() if k != "audit_log"} == \
        {k: v for k, v in full.items() if k != "audit_log"}


# ================================================= review-round additions
@pytest.mark.parametrize("text", [" \t=cmd", " =1+1", "\n@SUM(A1)", "-R1C1"])
def test_formula_hidden_behind_other_whitespace_is_caught(text):
    assert is_formula_like(text)


@pytest.mark.parametrize("steps", range(1, 9))
def test_cancelled_request_still_finishes_its_audit_row(adb, steps):
    # A phone dropping the connection cancels the request. The chain head
    # must never move forward for a row that is then never written; try
    # the cancel at each point of the append.
    audit = _audit_module()

    async def go():
        t = asyncio.ensure_future(_write(1))
        for _ in range(steps):
            await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        for _ in range(50):          # let the shielded append finish
            await asyncio.sleep(0)
        await _write(2)
        return await audit.verify_chain(TID)

    res = asyncio.run(go())
    assert res["valid"] is True
    assert res["entries_checked"] == len(adb.audit_log.docs)
    assert len(adb.audit_log.docs) in (1, 2)


def test_before_after_the_database_cannot_store_still_chain(adb):
    audit = _audit_module()

    async def go():
        await audit.write_audit(
            tenant_id=TID, table_name="tasks", op="UPDATE", record_id="t",
            before={"tags": {"a"}}, after={"n": 1, "s": {"b"}},
            actor_id=None, request_id=None)
        await _write(3)
        return await audit.verify_chain(TID)

    res = asyncio.run(go())
    assert res["valid"] is True and res["entries_checked"] == 2
    assert not any(r.get("unchained") for r in adb.audit_log.docs)
    assert isinstance(adb.audit_log.docs[0]["before_data"]["tags"], str)
