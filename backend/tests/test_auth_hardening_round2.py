"""2026-11 security round -- auth group.

Offline: no MongoDB or server. Functions are called directly and the
database is swapped for a small in-memory fake that understands the few
query operators these code paths use ($gte, $in, $inc, $set, $unset).
"""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth
from models import InviteCreate, UserLogin, InviteAccept, utc_now


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- fakes
def _match_value(actual, cond):
    if isinstance(cond, dict) and any(str(k).startswith("$") for k in cond):
        for op, v in cond.items():
            if op == "$gte" and not (actual is not None and actual >= v):
                return False
            if op == "$gt" and not (actual is not None and actual > v):
                return False
            if op == "$in" and actual not in v:
                return False
            if op == "$ne" and actual == v:
                return False
        return True
    return actual == cond


def _match(doc, flt):
    return all(_match_value(doc.get(k), v) for k, v in (flt or {}).items())


def _project(doc, proj):
    d = dict(doc)
    if not proj:
        return d
    inc = {k for k, v in proj.items() if v and k != "_id"}
    exc = {k for k, v in proj.items() if not v}
    if inc:
        return {k: v for k, v in d.items() if k in inc}
    return {k: v for k, v in d.items() if k not in exc}


class _Res:
    def __init__(self, n=1):
        self.matched_count = n
        self.modified_count = n
        self.deleted_count = n


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


def _apply(doc, upd):
    doc.update(upd.get("$set", {}))
    for k, v in upd.get("$inc", {}).items():
        doc[k] = (doc.get(k) or 0) + v
    for k in upd.get("$unset", {}):
        doc.pop(k, None)


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.indexes = []

    async def create_index(self, keys, **kw):
        self.indexes.append((keys, kw))

    async def find_one(self, flt=None, proj=None, sort=None, **k):
        rows = [d for d in self.docs if _match(d, flt)]
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return _project(rows[0], proj) if rows else None

    def find(self, flt=None, proj=None, **k):
        return _Cursor([_project(d, proj) for d in self.docs if _match(d, flt)])

    async def count_documents(self, flt):
        return sum(1 for d in self.docs if _match(d, flt))

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _Res()

    async def update_one(self, flt, upd, **k):
        for d in self.docs:
            if _match(d, flt):
                _apply(d, upd)
                return _Res(1)
        return _Res(0)

    async def find_one_and_update(self, flt, upd, projection=None, **k):
        for d in self.docs:
            if _match(d, flt):
                before = _project(d, projection)
                _apply(d, upd)
                return before
        return None

    async def update_many(self, flt, upd, **k):
        n = 0
        for d in self.docs:
            if _match(d, flt):
                _apply(d, upd)
                n += 1
        return _Res(n)

    async def delete_one(self, flt):
        for d in list(self.docs):
            if _match(d, flt):
                self.docs.remove(d)
                return _Res(1)
        return _Res(0)

    async def delete_many(self, flt):
        keep = [d for d in self.docs if not _match(d, flt)]
        n = len(self.docs) - len(keep)
        self.docs = keep
        return _Res(n)


class _DB:
    def __init__(self, **colls):
        self._colls = colls

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


def _ctx(role, uid="u1", tenant="t1"):
    return {"user": {"id": uid, "role": role, "email": f"{uid}@example.com"},
            "tenant_id": tenant, "request_id": "r1"}


def _request(ip="10.0.0.1", headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw,
                    "client": (ip, 5555)})


PW = "Correct-horse-1"
_PW_HASH = None


def _pw_hash():
    global _PW_HASH
    if _PW_HASH is None:
        _PW_HASH = auth.hash_password(PW)
    return _PW_HASH


def _base_db():
    return _DB(
        users=_Coll([{"id": "u1", "email": "super@example.com", "full_name": "Sam Super",
                      "tenant_id": "t1", "role": "super", "password_hash": _pw_hash(),
                      "created_at": "2026-01-01T00:00:00+00:00",
                      "password_reset_token_hash": "abc", "password_reset_at": "x"}]),
        tenants=_Coll([{"id": "t1", "legal_name": "ICE LLC", "display_name": "ICE",
                        "slug": "ice", "type": "general_contractor", "status": "active",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "ai_monthly_budget_usd": 100.0}]),
        tenant_users=_Coll([{"id": "m1", "user_id": "u1", "tenant_id": "t1",
                             "status": "active"}]),
    )


@pytest.fixture
def db(monkeypatch):
    d = _base_db()
    ar = importlib.import_module("routers.auth_router")
    monkeypatch.setattr(auth, "get_db", lambda: d)
    monkeypatch.setattr(ar, "get_db", lambda: d)
    monkeypatch.setattr(auth, "_attempt_indexes_ready", False)

    async def _capture_audit(**kw):
        d.audit_log.docs.append({"before_data": kw.get("before"),
                                 "after_data": kw.get("after"), "op": kw.get("op")})
    monkeypatch.setattr(ar, "write_audit", _capture_audit)
    return d


def _ar():
    return importlib.import_module("routers.auth_router")


# ------------------------------------------------- 1. audit secrets + gate
def test_redact_secrets_masks_secret_fields_any_depth():
    row = {"email": "a@b.c", "code": "SECRET", "role": "admin",
           "password_hash": "$2b$...", "password_reset_token_hash": "h",
           "password_reset_at": "2026-01-01", "password_reset_by": "u9",
           "nested": {"access_token": "tok", "pin": "123456", "ok": 1},
           "items": [{"token": "t"}], "code_empty": None, "token": None}
    out = auth.redact_secrets(row, auth.INVITE_SECRET_KEYS)
    assert out["code"] == auth.REDACTED
    # "code" is an ordinary field elsewhere (cost / room codes): kept
    assert auth.redact_secrets({"code": "03-300"})["code"] == "03-300"
    assert out["password_hash"] == auth.REDACTED
    assert out["password_reset_token_hash"] == auth.REDACTED
    assert out["nested"]["access_token"] == auth.REDACTED
    assert out["nested"]["pin"] == auth.REDACTED
    assert out["items"][0]["token"] == auth.REDACTED
    assert out["password_reset_at"] == "2026-01-01"
    assert out["password_reset_by"] == "u9"
    assert out["email"] == "a@b.c" and out["role"] == "admin"
    assert out["token"] is None
    assert row["code"] == "SECRET"  # original untouched


def test_create_invite_never_writes_code_to_audit(db, monkeypatch):
    import services.invite_email as ie
    monkeypatch.setattr(ie, "send_invite_email", lambda **kw: {"sent": True})
    monkeypatch.setenv("INVITE_BASE_URL", "https://prod.example.com")
    out = run(_ar().create_invite(InviteCreate(email="new@example.com", role="admin"),
                                  _request(), _ctx("admin")))
    code = out["invite"]["code"]
    assert code  # the admin still gets the code in the response
    inserts = [r for r in db.audit_log.docs if r["op"] == "INSERT"]
    assert inserts and code not in str(inserts[0])
    assert inserts[0]["after_data"]["code"] == auth.REDACTED
    assert all(code not in str(r) for r in db.audit_log.docs)


def _sys_router(monkeypatch, d):
    import sys
    # Another test file may leave a stand-in `audit` module without
    # verify_chain behind; load the real one for this test only
    # (monkeypatch puts sys.modules back afterwards).
    stub = sys.modules.get("audit")
    if stub is not None and not hasattr(stub, "verify_chain"):
        monkeypatch.delitem(sys.modules, "audit")
        monkeypatch.delitem(sys.modules, "routers.system_router", raising=False)
    sr = importlib.import_module("routers.system_router")
    monkeypatch.setattr(sr, "get_db", lambda: d)
    return sr


@pytest.mark.parametrize("role", ["super", "pm", "foreman", "viewer", None])
def test_audit_log_list_and_verify_refused_for_non_admin(monkeypatch, role):
    sr = _sys_router(monkeypatch, _DB())
    with pytest.raises(HTTPException) as e:
        run(sr.list_audit(limit=10, op=None, table_name=None, ctx=_ctx(role)))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        run(sr.verify_audit_chain(ctx=_ctx(role)))
    assert e.value.status_code == 403


def test_audit_log_admin_sees_rows_with_old_secrets_masked(monkeypatch):
    old = {"id": "a1", "tenant_id": "t1", "table_name": "team_invites", "op": "INSERT",
           "before_data": None,
           "after_data": {"email": "x@y.z", "role": "admin", "code": "OLDCODE123"},
           "hash": "h1", "prev_hash": "", "created_at": "2026-01-01"}
    d = _DB(audit_log=_Coll([old]))
    sr = _sys_router(monkeypatch, d)
    rows = run(sr.list_audit(limit=10, op=None, table_name=None, ctx=_ctx("admin")))
    assert rows[0]["after_data"]["code"] == auth.REDACTED
    assert rows[0]["after_data"]["email"] == "x@y.z"
    assert rows[0]["hash"] == "h1"  # chain fields untouched
    # read-side only: the stored row is not rewritten
    assert d.audit_log.docs[0]["after_data"]["code"] == "OLDCODE123"


# ------------------------------------------------------------ 3. throttle
def test_login_success_returns_token_with_tv(db):
    out = run(_ar().login(UserLogin(email="super@example.com", password=PW), _request()))
    claims = pyjwt.decode(out.access_token, auth.JWT_SECRET, algorithms=[auth.JWT_ALG])
    assert claims["tv"] == 0 and claims["sub"] == "u1"
    # TTL index made through the idempotent ensure step
    keys = [k for k, kw in db.auth_attempts.indexes if kw.get("expireAfterSeconds")]
    assert keys == ["at"]


def test_login_throttles_account_after_10_failures_and_success_resets(db):
    ar = _ar()
    for _ in range(10):
        with pytest.raises(HTTPException) as e:
            run(ar.login(UserLogin(email="super@example.com", password="wrong-pass"),
                         _request()))
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        run(ar.login(UserLogin(email="super@example.com", password=PW), _request()))
    assert e.value.status_code == 429
    retry = int(e.value.headers["Retry-After"])
    assert 1 <= retry <= 15 * 60 + 1
    # the window passes -> old failures no longer count
    for row in db.auth_attempts.docs:
        row["at"] = row["at"] - timedelta(minutes=16)
    out = run(ar.login(UserLogin(email="super@example.com", password=PW), _request()))
    assert out.access_token
    assert not [r for r in db.auth_attempts.docs if r["key"].startswith("login:acct:")]


def test_login_success_clears_earlier_account_failures(db):
    ar = _ar()
    for _ in range(9):
        with pytest.raises(HTTPException):
            run(ar.login(UserLogin(email="super@example.com", password="nope-nope"), _request()))
    run(ar.login(UserLogin(email="super@example.com", password=PW), _request()))
    # nine fresh failures are allowed again (counter was reset)
    for _ in range(9):
        with pytest.raises(HTTPException) as e:
            run(ar.login(UserLogin(email="super@example.com", password="nope-nope"), _request()))
        assert e.value.status_code == 401


def test_login_ip_limit_is_50_and_other_offices_unaffected(db):
    ar = _ar()
    # 50 failures spread over many accounts from one office address
    for i in range(50):
        with pytest.raises(HTTPException) as e:
            run(ar.login(UserLogin(email=f"p{i}@example.com", password="bad-pass"),
                         _request("1.1.1.1")))
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        run(ar.login(UserLogin(email="super@example.com", password=PW), _request("1.1.1.1")))
    assert e.value.status_code == 429
    # a different address signs in fine
    assert run(ar.login(UserLogin(email="super@example.com", password=PW),
                        _request("2.2.2.2"))).access_token


def test_login_unknown_email_runs_dummy_bcrypt(db, monkeypatch):
    calls = []
    real = auth.dummy_password_check
    monkeypatch.setattr(_ar(), "dummy_password_check", lambda p: (calls.append(p), real(p)))
    with pytest.raises(HTTPException) as e:
        run(_ar().login(UserLogin(email="nobody@example.com", password="whatever1"), _request()))
    assert e.value.status_code == 401 and e.value.detail == "Invalid email or password"
    assert calls == ["whatever1"]


def test_throttle_lets_attempt_through_when_db_fails(monkeypatch):
    class Broken:
        async def create_index(self, *a, **k):
            raise RuntimeError("down")

        async def count_documents(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(auth, "get_db", lambda: type("D", (), {"auth_attempts": Broken()})())
    monkeypatch.setattr(auth, "_attempt_indexes_ready", False)
    run(auth.check_throttle([("k", 1)]))  # no exception


def test_forgot_password_throttled_per_email(db, monkeypatch):
    ar = _ar()
    monkeypatch.setattr(ar, "send_password_reset_email", lambda **kw: {"sent": True})
    for _ in range(5):
        assert run(ar.forgot_password(ar.ForgotPasswordRequest(email="nobody@example.com"),
                                      _request()))["ok"]
    with pytest.raises(HTTPException) as e:
        run(ar.forgot_password(ar.ForgotPasswordRequest(email="nobody@example.com"), _request()))
    assert e.value.status_code == 429 and "Retry-After" in e.value.headers


def test_accept_invite_throttled(db):
    ar = _ar()
    for _ in range(10):
        with pytest.raises(HTTPException) as e:
            run(ar.accept_invite(InviteAccept(code="guess-0000", email="v@example.com",
                                              full_name="V", password="Password-123"),
                                 _request()))
        assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        run(ar.accept_invite(InviteAccept(code="guess-0000", email="v@example.com",
                                          full_name="V", password="Password-123"), _request()))
    assert e.value.status_code == 429


# --------------------------------------------------------- 5. token version
def _token(tv=None, sub="u1", tid="t1"):
    payload = {"sub": sub, "tid": tid, "tenant_id": tid, "email": "x",
               "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
    if tv is not None:
        payload["tv"] = tv
    return pyjwt.encode(payload, auth.JWT_SECRET, algorithm=auth.JWT_ALG)


def test_old_tokens_without_tv_keep_working(db):
    assert run(auth._resolve_via_jwt(_token(tv=None)))["id"] == "u1"
    assert run(auth._resolve_via_jwt(_token(tv=0)))["id"] == "u1"


def test_admin_reset_retires_old_tokens(db):
    ar = _ar()
    old = _token(tv=None)
    run(ar.admin_reset_password("u1", ar.PasswordResetPayload(new_password="Brand-new-pw-1"),
                                _ctx("admin", uid="admin1")))
    assert db.users.docs[0]["token_version"] == 1
    assert run(auth._resolve_via_jwt(old)) is None
    with pytest.raises(HTTPException) as e:
        run(auth.get_current_user(type("C", (), {"credentials": old})(), None))
    assert e.value.status_code == 401
    # a fresh sign-in gets tv=1 and works
    out = run(ar.login(UserLogin(email="super@example.com", password="Brand-new-pw-1"),
                       _request()))
    assert run(auth._resolve_via_jwt(out.access_token))["id"] == "u1"


def test_self_reset_bumps_token_version(db):
    ar = _ar()
    import hashlib
    db.users.docs[0]["password_reset_token_hash"] = hashlib.sha256(b"t" * 40).hexdigest()
    db.users.docs[0]["password_reset_expires_at"] = (utc_now() + timedelta(minutes=10)).isoformat()
    old = _token(tv=None)
    run(ar.reset_password(ar.ResetPasswordRequest(token="t" * 40, new_password="Another-pw-22"),
                          _request()))
    assert db.users.docs[0]["token_version"] == 1
    assert run(auth._resolve_via_jwt(old)) is None


def test_remove_member_bumps_then_deletes(db):
    ar = _ar()
    run(ar.remove_member("u1", _ctx("admin", uid="admin1")))
    assert not db.users.docs
    assert db.tenant_users.docs[0]["status"] == "revoked"


def test_close_account_retires_cross_company_member_tokens(db):
    ar = _ar()
    db.users.docs.append({"id": "u2", "email": "guest@example.com", "tenant_id": "t2",
                          "role": "pm"})
    db.tenant_users.docs.append({"id": "m2", "user_id": "u2", "tenant_id": "t1",
                                 "status": "active"})
    old_guest = _token(tv=None, sub="u2", tid="t1")
    run(ar.close_account(_ctx("admin", uid="u1")))
    guest = next(u for u in db.users.docs if u["id"] == "u2")
    assert guest["token_version"] == 1
    assert run(auth._resolve_via_jwt(old_guest)) is None
    assert all(m["status"] == "revoked" for m in db.tenant_users.docs if m["tenant_id"] == "t1")


# ------------------------------------------------------- 6. member fields
def test_team_members_only_allow_listed_fields(db):
    rows = run(_ar().list_members(_ctx("super")))
    allowed = set(_ar()._MEMBER_FIELDS) | set(_ar()._MEMBER_ACCESS_FIELDS)
    assert rows and set(rows[0]) <= allowed
    for f in ("id", "email", "full_name", "role", "created_at"):
        assert f in rows[0]
    for f in ("password_hash", "password_reset_token_hash", "password_reset_at"):
        assert f not in rows[0]


# ----------------------------------------------------------- 2. websocket
def _ws(monkeypatch, d):
    wr = importlib.import_module("routers.ws_router")
    monkeypatch.setattr(wr, "get_db", lambda: d)
    return wr


def test_ws_authorize_accepts_member_and_rejects_others(db, monkeypatch):
    wr = _ws(monkeypatch, db)
    assert run(wr._authorize(_token())) == ("t1", "u1")
    assert run(wr._authorize("garbage")) is None
    # unknown user
    assert run(wr._authorize(_token(sub="ghost"))) is None
    # company not on the user's memberships
    db.tenants.docs.append({"id": "t9", "slug": "other"})
    assert run(wr._authorize(_token(tid="t9"))) is None
    # retired token
    db.users.docs[0]["token_version"] = 2
    assert run(wr._authorize(_token(tv=1))) is None
    assert run(wr._authorize(_token(tv=2))) == ("t1", "u1")
    # closed company (tenant record gone)
    db.tenants.docs = [t for t in db.tenants.docs if t["id"] != "t1"]
    assert run(wr._authorize(_token(tv=2))) is None


def test_ws_endpoint_closes_1008_for_removed_user(db, monkeypatch):
    wr = _ws(monkeypatch, db)
    db.users.docs.clear()

    class FakeWS:
        def __init__(self):
            self.closed = None
            self.accepted = False

        async def close(self, code=None):
            self.closed = code

        async def accept(self):
            self.accepted = True
    ws = FakeWS()
    run(wr.ws_endpoint(ws, token=_token()))
    assert ws.closed == 1008 and not ws.accepted


def test_ws_user_notifications_go_only_to_that_user(monkeypatch):
    wr = importlib.import_module("routers.ws_router")

    class Sock:
        def __init__(self):
            self.sent = []

        async def send_text(self, m):
            self.sent.append(m)
    a, b = Sock(), Sock()
    monkeypatch.setattr(wr, "_subs", {"t1": {a, b}})
    monkeypatch.setattr(wr, "_owners", {a: "u1", b: "u2"})
    monkeypatch.setattr(wr, "_lock", asyncio.Lock())

    async def go():
        await wr.broadcast("t1", {"kind": "notification", "payload": {"user_id": "u1"}})
        await wr.broadcast("t1", {"kind": "notification", "payload": {"user_id": None}})
        await wr.broadcast("t1", {"kind": "task_updated", "payload": {"user_id": "u1"}})
    run(go())
    assert len(a.sent) == 3
    assert len(b.sent) == 2  # missed only the one addressed to u1


# ------------------------------------------------------------ 7. voice PIN
def _iter7(monkeypatch, d):
    ir = importlib.import_module("routers.iter7_router")
    monkeypatch.setattr(ir, "get_db", lambda: d)
    return ir


def test_voice_pin_uses_secrets_and_is_six_digits(db, monkeypatch):
    ir = _iter7(monkeypatch, db)
    import random
    monkeypatch.setattr(random, "randint", lambda *a: (_ for _ in ()).throw(AssertionError("random used")))
    out = run(ir.issue_voice_pin(ir.IssueVoicePinRequest(name="F", project_id="p1"),
                                 _ctx("admin")))
    assert len(out["pin"]) == 6 and out["pin"].isdigit()


def test_voice_pin_exchange_uniform_401_minimal_tenant_and_throttle(db, monkeypatch):
    ir = _iter7(monkeypatch, db)
    db.voice_pins.docs.append({"id": "v1", "tenant_id": "t1", "project_id": "p1",
                               "name": "Foreman", "pin": "123456", "redeemed_at": None,
                               "expires_at": (utc_now() + timedelta(hours=1)).isoformat()})
    with pytest.raises(HTTPException) as e1:
        run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="123456", tenant_slug="nope"),
                                  _request("5.5.5.5")))
    with pytest.raises(HTTPException) as e2:
        run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="000000", tenant_slug="ice"),
                                  _request("5.5.5.5")))
    assert e1.value.status_code == e2.value.status_code == 401
    assert e1.value.detail == e2.value.detail
    out = run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="123456", tenant_slug="ice"),
                                    _request("5.5.5.6")))
    assert out["tenant"] == {"id": "t1", "display_name": "ICE", "slug": "ice"}
    assert out["access_token"]
    # 20 wrong tries on one company from one address -> 429 for that
    # address (2026-11-16: the lock is per company AND address, 200 per
    # company from everywhere; see test_project_access_foundation.py)
    for i in range(20):
        with pytest.raises(HTTPException):
            run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="999999", tenant_slug="ice"),
                                      _request("9.9.9.1")))
    with pytest.raises(HTTPException) as e:
        run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="999999", tenant_slug="ice"),
                                  _request("9.9.9.1")))
    assert e.value.status_code == 429


# ------------------------------------------------ review round additions
def test_audit_log_keeps_ordinary_code_fields_visible(monkeypatch):
    rows_in = [
        {"id": "a1", "tenant_id": "t1", "table_name": "cost_rate_categories", "op": "INSERT",
         "before_data": None, "after_data": {"code": "LAB-01", "name": "Labor"},
         "created_at": "2026-01-02"},
        {"id": "a2", "tenant_id": "t1", "table_name": "team_invites", "op": "INSERT",
         "before_data": None, "after_data": {"code": "OLDCODE123", "api_key": "k"},
         "created_at": "2026-01-01"},
    ]
    sr = _sys_router(monkeypatch, _DB(audit_log=_Coll(rows_in)))
    rows = {r["id"]: r for r in run(sr.list_audit(limit=10, op=None, table_name=None,
                                                  ctx=_ctx("admin")))}
    assert rows["a1"]["after_data"]["code"] == "LAB-01"
    assert rows["a2"]["after_data"]["code"] == auth.REDACTED
    assert rows["a2"]["after_data"]["api_key"] == auth.REDACTED


def test_simultaneous_login_guesses_cannot_pass_the_account_limit(db, monkeypatch):
    """Many wrong guesses sent at the same moment: at most 10 get a real
    password check, the rest are refused with 429."""
    ar = _ar()

    class YieldingColl(_Coll):
        # Hand control back to the event loop like a real database call,
        # so the 40 requests really interleave.
        async def insert_one(self, doc):
            await asyncio.sleep(0)
            return await super().insert_one(doc)

        async def count_documents(self, flt):
            await asyncio.sleep(0)
            return await super().count_documents(flt)
    db._colls["auth_attempts"] = YieldingColl()
    checked = []
    real_verify = ar.verify_password
    monkeypatch.setattr(ar, "verify_password",
                        lambda p, h: (checked.append(p), real_verify(p, h))[1])

    async def burst():
        return await asyncio.gather(*[
            # One address: the per-(account, address) limit of 10 applies.
            # (2026-11-16: guesses spread over many addresses meet the
            # per-account limit of 100 instead.)
            ar.login(UserLogin(email="super@example.com", password=f"guess-{i:04d}"),
                     _request("7.7.7.7"))
            for i in range(40)
        ], return_exceptions=True)
    results = run(burst())
    codes = sorted(r.status_code for r in results if isinstance(r, HTTPException))
    assert len(codes) == 40
    assert len(checked) <= 10
    assert codes.count(429) >= 30


def test_locked_account_retry_does_not_extend_the_lock(db):
    ar = _ar()
    for _ in range(10):
        with pytest.raises(HTTPException):
            run(ar.login(UserLogin(email="super@example.com", password="wrong-pass"), _request()))
    before = len(db.auth_attempts.docs)
    for _ in range(3):
        with pytest.raises(HTTPException) as e:
            run(ar.login(UserLogin(email="super@example.com", password=PW), _request()))
        assert e.value.status_code == 429
    assert len(db.auth_attempts.docs) == before


def test_correct_invite_and_good_pin_do_not_count_as_failures(db, monkeypatch):
    ar = _ar()
    db.team_invites.docs.append({"id": "i1", "tenant_id": "t1", "email": "new@example.com",
                                 "role": "super", "code": "GOODCODE1234", "status": "pending",
                                 "expires_at": (utc_now() + timedelta(days=1)).isoformat()})
    run(ar.accept_invite(InviteAccept(code="GOODCODE1234", email="new@example.com",
                                      full_name="N", password="Password-123"), _request()))
    assert not [r for r in db.auth_attempts.docs if r["key"].startswith("invite:")]
    ir = _iter7(monkeypatch, db)
    db.voice_pins.docs.append({"id": "v1", "tenant_id": "t1", "project_id": "p1",
                               "name": "Foreman", "pin": "123456", "redeemed_at": None,
                               "expires_at": (utc_now() + timedelta(hours=1)).isoformat()})
    run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="123456", tenant_slug="ice"),
                              _request()))
    assert not [r for r in db.auth_attempts.docs if r["key"].startswith("voicepin:")]


def test_ws_recheck_runs_for_a_silent_client(db, monkeypatch):
    """A client that never pings is still re-checked and closed once removed."""
    wr = _ws(monkeypatch, db)
    monkeypatch.setattr(wr, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(wr, "_subs", {})
    monkeypatch.setattr(wr, "_owners", {})
    monkeypatch.setattr(wr, "_lock", asyncio.Lock())

    class SilentWS:
        def __init__(self):
            self.closed = None
            self.sent = []

        async def accept(self):
            pass

        async def send_text(self, m):
            self.sent.append(m)

        async def receive_text(self):
            await asyncio.sleep(3600)

        async def close(self, code=None):
            self.closed = code

    ws = SilentWS()

    async def go():
        task = asyncio.create_task(wr.ws_endpoint(ws, token=_token()))
        await asyncio.sleep(0.02)
        db.users.docs.clear()  # member removed while connected
        await asyncio.wait_for(task, timeout=2)
    run(go())
    assert ws.closed == 1008
    assert not wr._subs.get("t1") and not wr._owners
