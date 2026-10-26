"""Day-1 security fixes (2026-10-25) -- backend-auth group.

No running MongoDB or server needed: functions are called directly and the
database is replaced with a tiny in-memory fake.
"""
import asyncio
import importlib
from datetime import timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth
from models import InviteCreate, UserRegister, utc_now


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- fakes
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


class _Res:
    def __init__(self, n=1):
        self.matched_count = n
        self.modified_count = n


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.inserted = []
        self.updates = []

    @staticmethod
    def _match(doc, flt):
        for k, v in (flt or {}).items():
            if isinstance(v, dict):
                continue  # operators: treat as match (not needed here)
            if doc.get(k) != v:
                return False
        return True

    async def find_one(self, flt=None, proj=None, **k):
        for d in self.docs:
            if self._match(d, flt):
                return dict(d)
        return None

    def find(self, flt=None, proj=None, **k):
        return _Cursor([d for d in self.docs if self._match(d, flt)])

    async def insert_one(self, doc):
        self.inserted.append(doc)
        self.docs.append(doc)
        return _Res()

    async def update_one(self, flt, upd, **k):
        self.updates.append((flt, upd))
        for d in self.docs:
            if self._match(d, flt):
                d.update(upd.get("$set", {}))
                return _Res(1)
        return _Res(0)


class _DB:
    def __init__(self, **colls):
        self._colls = colls

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, _Coll())


def _ctx(role, email="u@example.com"):
    return {"user": {"id": "u1", "role": role, "email": email},
            "tenant_id": "t1", "request_id": "r1"}


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})


async def _noop_audit(**kw):
    return None


# ------------------------------------------------------ 1. role_required
@pytest.mark.parametrize("role", ["super", "foreman", "viewer", "pm", None, "", "  "])
def test_role_required_refuses_non_admin_for_admin_only(role):
    with pytest.raises(HTTPException) as e:
        auth.role_required(_ctx(role), ["admin"])
    assert e.value.status_code == 403
    assert e.value.detail == "Your role can't do this."


def test_role_required_refuses_missing_user():
    with pytest.raises(HTTPException):
        auth.role_required({}, ["admin"])


@pytest.mark.parametrize("role", ["admin", "ADMIN", " Admin "])
def test_role_required_admin_always_passes(role):
    auth.role_required(_ctx(role), ["admin"])
    auth.role_required(_ctx(role), ["pm"])


def test_role_required_pm_passes_admin_pm():
    auth.role_required(_ctx("pm"), ["admin", "pm"])
    auth.role_required(_ctx("PM "), ["admin", "pm"])


@pytest.mark.parametrize("role", ["super", "foreman", "viewer", None])
def test_role_required_refuses_others_for_admin_pm(role):
    with pytest.raises(HTTPException):
        auth.role_required(_ctx(role), ["admin", "pm"])


# ------------------------------------------------------------ 8. JWT secret
@pytest.mark.parametrize("raw", [None, "", "   ", "dev-secret"])
def test_jwt_fallback_is_random_never_dev_secret(raw):
    s1 = auth._resolve_jwt_secret(raw)
    s2 = auth._resolve_jwt_secret(raw)
    assert s1 != "dev-secret" and len(s1) >= 64
    assert s1 != s2  # random per call / per process


def test_jwt_placeholder_kept_but_real_secret_kept_too():
    ph = "ice-schedule-ops-dev-secret-change-in-prod"
    assert auth._resolve_jwt_secret(ph) == ph
    assert auth._resolve_jwt_secret("a-real-long-secret-value") == "a-real-long-secret-value"


def test_module_jwt_secret_is_not_dev_secret():
    assert auth.JWT_SECRET != "dev-secret"


# ------------------------------------------- 2. platform-owner restriction
def _settings_router():
    return importlib.import_module("routers.admin_settings_router")


def test_platform_admin_unset_falls_back_to_tenant_admin(monkeypatch):
    m = _settings_router()
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    m.require_platform_admin(_ctx("admin"))
    with pytest.raises(HTTPException) as e:
        m.require_platform_admin(_ctx("super"))
    assert e.value.status_code == 403


def test_platform_admin_set_only_listed_emails(monkeypatch):
    m = _settings_router()
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", " Owner@ICE-aec.com , other@x.com ")
    m.require_platform_admin(_ctx("admin", "owner@ice-aec.com"))
    m.require_platform_admin(_ctx("admin", "OTHER@X.COM"))
    # A company admin who is not on the list is refused (e.g. self-signup admin).
    with pytest.raises(HTTPException) as e:
        m.require_platform_admin(_ctx("admin", "someone@else.com"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        m.require_platform_admin(_ctx("admin", ""))


def test_settings_endpoints_refuse_super(monkeypatch):
    m = _settings_router()
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    for call in (
        lambda: m.get_settings(_ctx("super")),
        lambda: m.patch_settings({"flags": {}}, _ctx("super")),
        lambda: m.rotate_msp_token(_ctx("super")),
        lambda: m.download_worker(_ctx("super")),
    ):
        with pytest.raises(HTTPException) as e:
            run(call())
        assert e.value.status_code == 403


# ------------------------------------------------------------- 3. sign-up
def _auth_router():
    return importlib.import_module("routers.auth_router")


def _reg_payload():
    return UserRegister(email="new@example.com", password="Password123!",
                        full_name="New Person", organization_name="New Org")


@pytest.mark.parametrize("val", [None, "", "0", "false", "no", "maybe"])
def test_signup_refused_by_default(monkeypatch, val):
    ar = _auth_router()
    if val is None:
        monkeypatch.delenv("ALLOW_PUBLIC_SIGNUP", raising=False)
    else:
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", val)

    def _boom():
        raise AssertionError("database must not be touched when sign-up is off")
    monkeypatch.setattr(ar, "get_db", _boom)
    with pytest.raises(HTTPException) as e:
        run(ar.register(_reg_payload()))
    assert e.value.status_code == 403
    assert "invitation only" in e.value.detail


@pytest.mark.parametrize("val", ["1", "true", "YES"])
def test_signup_allowed_with_env(monkeypatch, val):
    ar = _auth_router()
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", val)
    import sys
    import types
    fake_aux = types.ModuleType("seed_aux")

    async def _no_seed(tenant_id):
        return None
    fake_aux.seed_taxonomy_and_rates = _no_seed
    monkeypatch.setitem(sys.modules, "seed_aux", fake_aux)
    db = _DB()
    monkeypatch.setattr(ar, "get_db", lambda: db)
    out = run(ar.register(_reg_payload()))
    assert out.access_token
    assert db.users.inserted and db.users.inserted[0]["role"] == "admin"


# ------------------------------------------------------------- 4/5. invites
def test_invite_default_role_is_super():
    assert InviteCreate(email="s@example.com").role == "super"
    assert InviteCreate(email="s@example.com", role="super").role == "super"


def _invites_db(expires_at=None):
    exp = expires_at or (utc_now() + timedelta(days=5)).isoformat()
    inv = {"id": "i1", "tenant_id": "t1", "email": "s@example.com", "role": "pm",
           "code": "CODE12345678", "status": "pending", "invited_at": "x",
           "expires_at": exp}
    return _DB(team_invites=_Coll([inv]), users=_Coll(), tenants=_Coll([{"id": "t1", "name": "ICE"}]))


@pytest.mark.parametrize("role", ["super", "foreman", "viewer", None])
def test_list_invites_hidden_from_field_roles(monkeypatch, role):
    ar = _auth_router()
    monkeypatch.setattr(ar, "get_db", lambda: _invites_db())
    assert run(ar.list_invites(_ctx(role))) == []


def test_list_invites_pm_sees_rows_without_code_admin_sees_code(monkeypatch):
    ar = _auth_router()
    monkeypatch.setattr(ar, "get_db", lambda: _invites_db())
    pm_rows = run(ar.list_invites(_ctx("pm")))
    assert len(pm_rows) == 1 and "code" not in pm_rows[0]
    admin_rows = run(ar.list_invites(_ctx("admin")))
    assert admin_rows[0]["code"] == "CODE12345678"


def test_list_invites_reports_expired_pending(monkeypatch):
    ar = _auth_router()
    past = (utc_now() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(ar, "get_db", lambda: _invites_db(past))
    assert run(ar.list_invites(_ctx("admin")))[0]["status"] == "expired"


def _patch_email(monkeypatch, sent):
    import services.invite_email as ie
    calls = []

    def fake_send(**kw):
        calls.append(kw)
        return {"sent": True, "provider_message_id": "m1"}
    monkeypatch.setattr(ie, "send_invite_email", fake_send)
    return calls


def test_resend_pending_invite_sends_email_keeps_role(monkeypatch):
    ar = _auth_router()
    db = _invites_db()
    monkeypatch.setattr(ar, "get_db", lambda: db)
    monkeypatch.setattr(ar, "write_audit", _noop_audit)
    monkeypatch.setenv("INVITE_BASE_URL", "https://prod.example.com")
    calls = _patch_email(monkeypatch, True)
    out = run(ar.create_invite(InviteCreate(email="s@example.com", role="super"),
                               _request({"origin": "https://stale.preview.example"}),
                               _ctx("admin")))
    assert out["already_existed"] is True
    assert out["email_sent"] is True
    assert len(calls) == 1
    assert calls[0]["invite_link"] == "https://prod.example.com/accept-invite?code=CODE12345678"
    assert out["invite"]["role"] == "pm"
    assert db.team_invites.docs[0]["role"] == "pm"
    assert db.team_invites.docs[0]["code"] == "CODE12345678"


def test_resend_expired_invite_issues_new_code(monkeypatch):
    ar = _auth_router()
    past = (utc_now() - timedelta(days=1)).isoformat()
    db = _invites_db(past)
    monkeypatch.setattr(ar, "get_db", lambda: db)
    monkeypatch.setattr(ar, "write_audit", _noop_audit)
    monkeypatch.setenv("INVITE_BASE_URL", "https://prod.example.com")
    calls = _patch_email(monkeypatch, True)
    out = run(ar.create_invite(InviteCreate(email="s@example.com", role="super"),
                               _request(), _ctx("admin")))
    assert out["already_existed"] is False
    assert out["invite"]["code"] != "CODE12345678"
    assert db.team_invites.docs[0]["status"] == "expired"
    assert len(calls) == 1


@pytest.mark.parametrize("role", ["pm", "super", "viewer"])
def test_create_and_revoke_invite_admin_only(monkeypatch, role):
    ar = _auth_router()
    monkeypatch.setattr(ar, "get_db", lambda: _invites_db())
    with pytest.raises(HTTPException) as e:
        run(ar.create_invite(InviteCreate(email="x@example.com"), _request(), _ctx(role)))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        run(ar.revoke_invite("i1", _ctx(role)))
    assert e.value.status_code == 403


def test_invite_base_url_uses_allowlist_not_raw_origin(monkeypatch):
    ar = _auth_router()
    for k in ("INVITE_BASE_URL", "REACT_APP_BACKEND_URL", "PRODUCTION_URL", "ALLOWED_FRONTEND_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INVITE_BASE_URL", "https://prod.example.com/api")
    monkeypatch.setenv("ALLOWED_FRONTEND_HOSTS", "https://staging.example.com")
    assert ar._invite_base_url(_request({"origin": "https://evil.example"})) == "https://prod.example.com"
    assert ar._invite_base_url(_request({"origin": "https://staging.example.com"})) == "https://staging.example.com"


def test_invite_base_url_unconfigured_falls_back_to_origin(monkeypatch):
    ar = _auth_router()
    for k in ("INVITE_BASE_URL", "REACT_APP_BACKEND_URL", "PRODUCTION_URL", "ALLOWED_FRONTEND_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    assert ar._invite_base_url(_request({"origin": "https://app.example.com"})) == "https://app.example.com"
    # Allowlist configured but origin not on it and no base URL -> never the
    # raw origin; use the operator's first allowed host instead of a dead link.
    monkeypatch.setenv("ALLOWED_FRONTEND_HOSTS", " https://app.example.com/ ,https://b.example.com")
    assert ar._invite_base_url(_request({"origin": "https://evil.example"})) == "https://app.example.com"
    monkeypatch.setenv("PRODUCTION_URL", "https://prod.example.com/api")
    assert ar._invite_base_url(_request({"origin": "https://evil.example"})) == "https://prod.example.com"
    # Configured with junk only (no scheme) -> no raw origin, relative link.
    monkeypatch.delenv("PRODUCTION_URL")
    monkeypatch.setenv("ALLOWED_FRONTEND_HOSTS", "app.example.com")
    assert ar._invite_base_url(_request({"origin": "https://evil.example"})) == ""


# -------------------------------------------- 6. project / task / dev seed
@pytest.mark.parametrize("role", ["super", "foreman", "viewer"])
def test_project_create_and_edit_refused_for_field_roles(monkeypatch, role):
    pr = importlib.import_module("routers.projects_router")
    from models import ProjectCreate, ProjectUpdate

    def _boom():
        raise AssertionError("database must not be touched")
    monkeypatch.setattr(pr, "get_db", _boom)
    with pytest.raises(HTTPException) as e:
        run(pr.create_project(ProjectCreate.model_construct(), _ctx(role)))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        run(pr.update_project("p1", ProjectUpdate.model_construct(), None, _ctx(role)))
    assert e.value.status_code == 403


@pytest.mark.parametrize("role", ["super", "foreman", "viewer", "scheduling_engineer"])
def test_task_bulk_delete_refused_for_non_admin_pm(monkeypatch, role):
    tr = importlib.import_module("routers.tasks_router")

    def _boom():
        raise AssertionError("database must not be touched")
    monkeypatch.setattr(tr, "get_db", _boom)
    with pytest.raises(HTTPException) as e:
        run(tr.bulk_delete("p1", tr.BulkDeletePayload(task_ids=["a"]), None, _ctx(role)))
    assert e.value.status_code == 403


@pytest.mark.parametrize("role", ["pm", "super", "viewer"])
def test_dev_seed_recovery_admin_only(monkeypatch, role):
    ir = importlib.import_module("routers.iter7_router")

    def _boom():
        raise AssertionError("database must not be touched")
    monkeypatch.setattr(ir, "get_db", _boom)
    with pytest.raises(HTTPException) as e:
        run(ir.seed_recovery(_ctx(role)))
    assert e.value.status_code == 403


def test_voice_pin_issue_super_refused_pm_allowed_by_role(monkeypatch):
    ir = importlib.import_module("routers.iter7_router")
    payload = ir.IssueVoicePinRequest(name="Crew", project_id="p1")

    def _boom():
        raise AssertionError("database must not be touched")
    monkeypatch.setattr(ir, "get_db", _boom)
    with pytest.raises(HTTPException) as e:
        run(ir.issue_voice_pin(payload, _ctx("super")))
    assert e.value.status_code == 403
    db = _DB()
    monkeypatch.setattr(ir, "get_db", lambda: db)
    out = run(ir.issue_voice_pin(payload, _ctx("pm")))
    assert len(out["pin"]) == 6


# ------------------------------------------------------------ 7. live voice
def test_live_voice_off_by_default(monkeypatch):
    vr = importlib.import_module("routers.dr_voice_router")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-master-key")
    monkeypatch.delenv("LIVE_VOICE_ENABLED", raising=False)
    with pytest.raises(HTTPException) as e:
        run(vr.voice_live_token("dr1", _ctx("super")))
    assert e.value.status_code == 503
    assert "secret-master-key" not in str(e.value.detail)
    cfg = run(vr.voice_config(_ctx("super")))
    assert cfg["live_voice_enabled"] is False
    assert cfg["voice_memo_enabled"] is True


def test_live_voice_on_with_env(monkeypatch):
    vr = importlib.import_module("routers.dr_voice_router")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("LIVE_VOICE_ENABLED", "1")
    assert run(vr.voice_config(_ctx("super")))["live_voice_enabled"] is True


# --------------------------------------------------------------- 9. demo seed
def test_demo_seed_refuses_without_flag(monkeypatch):
    seed = importlib.import_module("seed")
    monkeypatch.delenv("ICE_DEMO_SEED", raising=False)
    with pytest.raises(RuntimeError):
        run(seed.seed_demo())
