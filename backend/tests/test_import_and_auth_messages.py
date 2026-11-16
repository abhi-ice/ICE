"""Railway move (2026-11-16): schedule import + auth message fixes.

Covers:
  * a corrupt or renamed .xlsx is a 400-style parse error, not a 500;
  * any unexpected parser crash becomes a parse error (for .xlsx, .csv and
    the Microsoft Project / Primavera formats);
  * the Microsoft Project file endpoints tell "project not found" apart from
    "project has no file yet" even though their narrow projections return {}
    for a project that has none of the projected fields;
  * invite emails use the tenant's display_name / legal_name instead of
    falling back to "your team";
  * an invalid login token answers a plain "Invalid token" (the library's
    reason only goes to the server log), and "Token expired" stays distinct.

No live server and no real Mongo: small in-memory fakes that DO apply
projections the way Mongo does (the bug only shows up when they do).
"""
import asyncio
import csv as _csv
import importlib.util
import io
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Satisfy `from database import ...` / `from audit import ...` without motor.
# Keep any stub a sibling test module already installed.
if "database" not in sys.modules:
    _dbmod = types.ModuleType("database")
    _dbmod.get_db = lambda: None
    _dbmod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
    sys.modules["database"] = _dbmod
if "audit" not in sys.modules:
    _auditmod = types.ModuleType("audit")

    async def _noop_audit(**k):
        return None
    _auditmod.write_audit = _noop_audit
    sys.modules["audit"] = _auditmod

from fastapi import HTTPException  # noqa: E402

from services import schedule_parser as sp  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _load_private(name: str, relpath: str, extra_modules: dict | None = None):
    """Load a backend module from its file under a private name, so a stub
    another test put in sys.modules (e.g. a minimal 'auth') cannot get in the
    way, and our copy cannot leak into other tests."""
    saved = {}
    for k, v in (extra_modules or {}).items():
        saved[k] = sys.modules.get(k)
        sys.modules[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_BACKEND, relpath))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


_real_auth = _load_private("_real_auth_for_msg_tests", "auth.py")


# --------------------------------------------------------------------------
# Fake Mongo that applies projections.
# --------------------------------------------------------------------------
def _project(row, p):
    if not p:
        return dict(row)
    include = [k for k, v in p.items() if v and k != "_id"]
    if not include:
        return {k: v for k, v in row.items() if k not in p}
    return {k: row[k] for k in include if k in row}


def _match(row, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict):
            if "$in" in v and row.get(k) not in v["$in"]:
                return False
            if "$ne" in v and row.get(k) == v["$ne"]:
                return False
        elif row.get(k) != v:
            return False
    return True


class _Cur:
    def __init__(self, items):
        self.items = items

    async def to_list(self, n):
        return self.items[:n] if n else self.items


class _Col:
    def __init__(self):
        self.rows = []

    async def find_one(self, q=None, p=None, sort=None):
        for r in self.rows:
            if _match(r, q):
                return _project(r, p)
        return None

    def find(self, q=None, p=None):
        return _Cur([_project(r, p) for r in self.rows if _match(r, q)])


class _DB:
    def __init__(self):
        self.projects = _Col()
        self.files = _Col()
        self.tasks = _Col()
        self.schedule_jobs = _Col()
        self.tenants = _Col()


# --------------------------------------------------------------------------
# (1) Schedule parser.
# --------------------------------------------------------------------------
def _xlsx_bytes(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("content", [
    b"this is plain text renamed to .xlsx",       # not a zip at all
    b"PK\x03\x04" + b"\x00" * 40,                 # zip magic, truncated body
    _xlsx_bytes([["Activity ID", "Description"], ["A1", "Dig"]])[:200],  # cut short
])
def test_corrupt_xlsx_is_a_parse_error_not_a_crash(content):
    rows, errors = sp.parse_xlsx(content)
    assert rows == []
    assert errors == ["File is not a valid .xlsx workbook"]
    # and through the dispatcher the router uses
    rows, errors = sp.parse_schedule_file("plan.xlsx", content)
    assert rows == [] and errors == ["File is not a valid .xlsx workbook"]


def test_zip_that_is_not_a_workbook_is_a_parse_error():
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not a workbook")
    rows, errors = sp.parse_schedule_file("plan.xlsx", buf.getvalue())
    assert rows == [] and errors == ["File is not a valid .xlsx workbook"]


def test_valid_xlsx_still_parses():
    content = _xlsx_bytes([["Activity ID", "Description"], ["A1", "Dig"], ["A2", "Pour"]])
    rows, errors = sp.parse_schedule_file("plan.xlsx", content)
    assert errors == []
    assert [r["activity_code"] for r in rows] == ["A1", "A2"]


def test_valid_csv_still_parses():
    rows, errors = sp.parse_schedule_file("plan.csv", b"Activity ID,Description\nA1,Dig\n")
    assert errors == [] and rows[0]["activity_code"] == "A1"


@pytest.mark.parametrize("filename,target", [
    ("plan.xlsx", "parse_xlsx"),
    ("plan.csv", "parse_csv"),
    ("plan.mpp", "parse_mpp_xer"),
    ("plan.xer", "parse_mpp_xer"),
    ("plan.xml", "parse_mpp_xer"),
])
def test_unexpected_parser_crash_becomes_parse_error(monkeypatch, filename, target):
    def boom(*a, **k):
        raise RuntimeError("internal library detail")
    monkeypatch.setattr(sp, target, boom)
    rows, errors = sp.parse_schedule_file(filename, b"anything")
    assert rows == []
    assert len(errors) == 1
    assert errors[0].startswith("Could not read this ")
    assert "internal library detail" not in errors[0]


def test_csv_reader_error_is_a_parse_error():
    # A field bigger than the csv module's limit raises csv.Error mid-loop.
    huge = "x" * (_csv.field_size_limit() + 10)
    content = f"Activity ID,Description\nA1,\"{huge}\"\n".encode()
    rows, errors = sp.parse_schedule_file("plan.csv", content)
    assert rows == [] and errors and errors[0].startswith("Could not read this .csv")


def test_unsupported_extension_unchanged():
    rows, errors = sp.parse_schedule_file("plan.pdf", b"x")
    assert rows == [] and errors == ["Unsupported schedule file type: plan.pdf"]


# --------------------------------------------------------------------------
# (2) Microsoft Project file endpoints with narrow projections.
# --------------------------------------------------------------------------
import routers.ms_project_internal_router as mir  # noqa: E402

T, P = "t1", "p1"
CTX = {"tenant_id": T, "user": {"id": "u1", "role": "admin", "email": "a@b.c"},
       "request_id": "req-1"}


@pytest.fixture
def msp_db(monkeypatch):
    db = _DB()
    monkeypatch.setattr(mir, "get_db", lambda: db)
    monkeypatch.setattr(mir, "tenant_filter",
                        lambda tid, q=None: {**(q or {}), "tenant_id": tid})
    return db


def test_download_existing_project_without_file_says_no_file(msp_db):
    # A project row with none of the projected file fields: the old code's
    # projection came back {} and answered "Project not found".
    msp_db.projects.rows.append({"id": P, "tenant_id": T, "name": "Alpha"})
    with pytest.raises(HTTPException) as e:
        run(mir.download_ms_project_file(P, CTX))
    assert e.value.status_code == 404
    assert e.value.detail == "No Microsoft Project file stored for this project yet"


def test_download_missing_project_says_project_not_found(msp_db):
    with pytest.raises(HTTPException) as e:
        run(mir.download_ms_project_file("nope", CTX))
    assert e.value.status_code == 404 and e.value.detail == "Project not found"


def test_generate_on_project_without_mode_or_file_says_not_mode2(msp_db):
    # No schedule_mode and no file: projection would be {} before the fix.
    msp_db.projects.rows.append({"id": P, "tenant_id": T, "name": "Alpha"})
    with pytest.raises(HTTPException) as e:
        run(mir.generate_ms_project_file(P, CTX))
    assert e.value.status_code == 409


def test_generate_missing_project_says_project_not_found(msp_db):
    with pytest.raises(HTTPException) as e:
        run(mir.generate_ms_project_file("nope", CTX))
    assert e.value.status_code == 404 and e.value.detail == "Project not found"


# --------------------------------------------------------------------------
# (3) Invite email tenant name.
# --------------------------------------------------------------------------
_ar = _load_private("_auth_router_for_msg_tests", os.path.join("routers", "auth_router.py"),
                    extra_modules={"auth": _real_auth})


@pytest.fixture
def invite_env(monkeypatch):
    db = _DB()
    sent = []

    def fake_send(**kw):
        sent.append(kw)
        return {"sent": True, "provider_message_id": "m1", "error": None}

    import services.invite_email as ie
    monkeypatch.setattr(ie, "send_invite_email", fake_send)
    monkeypatch.setattr(_ar, "_invite_base_url", lambda request: "https://app.example")

    async def no_audit(**k):
        return None
    monkeypatch.setattr(_ar, "write_audit", no_audit)
    return db, sent


_INVITE = {"id": "inv1", "code": "CODE123", "email": "new@x.com", "role": "pm"}


@pytest.mark.parametrize("row,expected", [
    ({"display_name": "Acme Builders", "legal_name": "Acme Builders LLC"}, "Acme Builders"),
    ({"legal_name": "Acme Builders LLC"}, "Acme Builders LLC"),
    ({"name": "Old Style Co"}, "Old Style Co"),
    ({}, "your team"),
])
def test_invite_email_uses_tenant_display_name(invite_env, row, expected):
    db, sent = invite_env
    db.tenants.rows.append({"id": T, **row})
    result, link = run(_ar._send_invite_email_and_audit(db, CTX, None, dict(_INVITE)))
    assert result["sent"] is True
    assert link == "https://app.example/accept-invite?code=CODE123"
    assert sent[-1]["tenant_name"] == expected


# --------------------------------------------------------------------------
# (4) Token error messages.
# --------------------------------------------------------------------------
def test_garbage_token_says_invalid_token_only():
    with pytest.raises(HTTPException) as e:
        _real_auth.decode_token("not.a.jwt")
    assert e.value.status_code == 401 and e.value.detail == "Invalid token"


def test_wrong_signature_says_invalid_token_only():
    import jwt as pyjwt
    tok = pyjwt.encode({"sub": "u1"}, "some-other-secret-that-is-long-enough-xx",
                       algorithm=_real_auth.JWT_ALG)
    with pytest.raises(HTTPException) as e:
        _real_auth.decode_token(tok)
    assert e.value.detail == "Invalid token"


def test_expired_token_still_says_token_expired():
    import jwt as pyjwt
    tok = pyjwt.encode(
        {"sub": "u1", "exp": datetime.now(timezone.utc) - timedelta(hours=1)},
        _real_auth.JWT_SECRET, algorithm=_real_auth.JWT_ALG)
    with pytest.raises(HTTPException) as e:
        _real_auth.decode_token(tok)
    assert e.value.status_code == 401 and e.value.detail == "Token expired"
