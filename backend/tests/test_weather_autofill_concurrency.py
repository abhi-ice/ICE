"""Iteration 15 — Verify weather-autofill concurrency hardening + observability.

Covers:
  1. Structured logger.warning inside every requests-exception path
     of _geocode / _fetch_weather (TIMEOUT / CONNECTION_ERROR / HTTP_5xx / OTHER).
  2. Freshness cache — 2nd..Nth POST within 5min returns 200 w/o outbound.
  3. Coalescing — 50 concurrent POSTs to same (tenant, project, date) →
     only ONE round-trip to Open-Meteo (1 geocode + 1 weather).
  4. Global semaphore(20) — even with 30 concurrent POSTs to DIFFERENT
     coalesce keys, no more than 20 outbound calls in flight at once.
  5. Negative cache — _fetch_weather None result caches for 60s.
  6. All-4xx contract — ConnectionError → HTTP 422 JSON detail (< 200 chars).
  7. Regression — real Kansas City / MO happy path still 200.
"""
import os
import sys
import time
import logging
import threading
import pytest
import requests as _requests

sys.path.insert(0, "/app/backend")

# Load backend .env so JWT_SECRET / MONGO_URL / DB_NAME match the running backend.
try:
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
except Exception:
    pass

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


# ---------------------------------------------------------------
# Module-scope tenant / project / DR setup (HTTP against live app)
# ---------------------------------------------------------------
@pytest.fixture(scope="module")
def ctx():
    ts = int(time.time())
    email = f"wxconc+{ts}@example.com"
    password = "TestPass123!"
    reg = _requests.post(f"{API}/auth/register", json={
        "organization_name": f"wxconc-{ts}",
        "email": email,
        "password": password,
        "full_name": "Wx Conc",
    }, timeout=30)
    assert reg.status_code in (200, 201), f"register failed: {reg.status_code} {reg.text[:400]}"
    body = reg.json()
    token = body.get("access_token") or body.get("token") or (body.get("data") or {}).get("access_token")
    if not token:
        lg = _requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
        assert lg.status_code == 200, lg.text[:400]
        token = lg.json().get("access_token") or lg.json().get("token")
    assert token, f"no token: {body}"
    h = {"Authorization": f"Bearer {token}"}

    from datetime import date, timedelta
    today = date.today().isoformat()

    # Primary KC/MO project + DR (for happy-path regression, freshness, coalescing)
    p = _requests.post(f"{API}/projects", headers=h, json={
        "name": f"WxConc {ts}",
        "client_name": "QA",
        "city": "Kansas City",
        "state": "MO",
        "postal_code": "64108",
    }, timeout=30)
    assert p.status_code in (200, 201), p.text[:400]
    project_id = p.json()["id"]
    dr = _requests.post(f"{API}/daily-reports", headers=h, json={
        "project_id": project_id, "report_date": today,
    }, timeout=30)
    assert dr.status_code in (200, 201), dr.text[:400]
    dr_id = dr.json()["id"]

    # 30 additional DRs (different report_date -> different coalesce keys) for semaphore test.
    # Coalesce key = (tenant, project, date), so vary report_date on the SAME project.
    sem_dr_ids = []
    for i in range(30):
        d = (date.today() - timedelta(days=i + 5)).isoformat()
        r = _requests.post(f"{API}/daily-reports", headers=h, json={
            "project_id": project_id, "report_date": d,
        }, timeout=30)
        assert r.status_code in (200, 201), f"DR#{i} create failed: {r.text[:200]}"
        sem_dr_ids.append(r.json()["id"])

    return {
        "headers": h,
        "email": email,
        "project_id": project_id,
        "dr_id": dr_id,
        "sem_dr_ids": sem_dr_ids,
    }


# ---------------------------------------------------------------
# ASGI in-process client — needed for monkeypatch tests
# ---------------------------------------------------------------
@pytest.fixture
async def asgi_client():
    from server import app  # type: ignore
    import database  # type: ignore
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    database._client = cli  # type: ignore[attr-defined]
    database._db = cli[os.environ["DB_NAME"]]  # type: ignore[attr-defined]
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60.0) as client:
        yield client
    cli.close()


@pytest.fixture(autouse=True)
def _clear_wx_caches():
    """Reset module-level caches between tests so state doesn't bleed."""
    from routers import daily_reports_router as drr
    drr._GEOCODE_CACHE.clear()
    drr._WEATHER_CACHE.clear()
    yield
    drr._GEOCODE_CACHE.clear()
    drr._WEATHER_CACHE.clear()


async def _clear_dr_weather(dr_id: str):
    """Remove weather_fetched_at so freshness check doesn't short-circuit."""
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    await db.daily_reports.update_many(
        {"id": dr_id},
        {"$unset": {
            "weather_fetched_at": "", "weather_conditions": "",
            "weather_temp_high_f": "", "weather_temp_low_f": "",
            "weather_precipitation_in": "", "weather_wind_mph": "",
        }},
    )
    cli.close()


# ---------------------------------------------------------------
# 1) Observability — structured logging in every exception path
# ---------------------------------------------------------------
class TestObservabilityLogging:

    def _mk_requests_module_raising(self, exc):
        """Return a fake `requests` module that raises `exc` on .get()."""
        class _Fake:
            exceptions = _requests.exceptions
            @staticmethod
            def get(*a, **kw):
                raise exc
        return _Fake

    @pytest.mark.parametrize("exc_factory, category", [
        (lambda: _requests.exceptions.Timeout("simulated timeout"), "TIMEOUT"),
        (lambda: _requests.exceptions.ConnectionError("simulated conn err"), "CONNECTION_ERROR"),
        (lambda: _make_http_error(503), "HTTP_503"),
        (lambda: ValueError("simulated other"), "OTHER"),
    ])
    def test_fetch_weather_logs_category(self, exc_factory, category, monkeypatch, caplog):
        from routers import daily_reports_router as drr

        def _raise(*a, **kw):
            raise exc_factory()

        monkeypatch.setattr(drr.requests, "get", _raise)
        caplog.set_level(logging.WARNING, logger="ice_schedule_ops.dr")
        result = drr._fetch_weather(39.1, -94.6, "2026-01-15")
        assert result is None
        combined = " | ".join(rec.getMessage() for rec in caplog.records)
        assert "fetch_weather:" in combined, f"no fetch_weather log line: {combined!r}"
        assert category in combined, f"expected {category} in log; got: {combined!r}"

    @pytest.mark.parametrize("exc_factory, category", [
        (lambda: _requests.exceptions.Timeout("simulated timeout"), "TIMEOUT"),
        (lambda: _requests.exceptions.ConnectionError("simulated conn err"), "CONNECTION_ERROR"),
        (lambda: _make_http_error(502), "HTTP_502"),
        (lambda: ValueError("simulated other"), "OTHER"),
    ])
    def test_geocode_logs_category(self, exc_factory, category, monkeypatch, caplog):
        from routers import daily_reports_router as drr

        def _raise(*a, **kw):
            raise exc_factory()

        monkeypatch.setattr(drr.requests, "get", _raise)
        caplog.set_level(logging.WARNING, logger="ice_schedule_ops.dr")
        result = drr._geocode("Nowhereville", "ZZ", None)
        assert result is None
        combined = " | ".join(rec.getMessage() for rec in caplog.records)
        assert "geocode:" in combined, f"no geocode log line: {combined!r}"
        assert category in combined, f"expected {category} in log; got: {combined!r}"


def _make_http_error(status):
    """Craft a real HTTPError with a response that has .status_code."""
    resp = _requests.models.Response()
    resp.status_code = status
    return _requests.exceptions.HTTPError(f"{status} Server Error", response=resp)


# ---------------------------------------------------------------
# 2) Freshness cache — cold call then hot calls
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_freshness_cache_second_call_fast(asgi_client, ctx):
    """After a successful call, subsequent calls within 5min return 200
    without touching Open-Meteo (freshness check re-reads DR)."""
    await _clear_dr_weather(ctx["dr_id"])
    from routers import daily_reports_router as drr

    call_counter = {"n": 0}
    real_get = _requests.get

    def _counting_get(*a, **kw):
        call_counter["n"] += 1
        return real_get(*a, **kw)

    # Cold call: real network, count how many outbound
    orig = drr.requests.get
    drr.requests.get = _counting_get
    try:
        r1 = await asgi_client.post(
            f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
            headers=ctx["headers"],
        )
        assert r1.status_code == 200, f"cold call: {r1.status_code} {r1.text[:300]}"
        cold_calls = call_counter["n"]
        assert cold_calls >= 1, "expected at least one outbound on cold path"

        # Subsequent calls should short-circuit at freshness cache
        t_start = time.monotonic()
        for _ in range(3):
            r = await asgi_client.post(
                f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
                headers=ctx["headers"],
            )
            assert r.status_code == 200, r.text[:300]
        elapsed = time.monotonic() - t_start
        # 3 hot calls should be fast in-process
        assert elapsed < 3.0, f"hot calls too slow: {elapsed:.2f}s"
        assert call_counter["n"] == cold_calls, (
            f"expected NO extra outbound; cold={cold_calls}, after_hot={call_counter['n']}"
        )
    finally:
        drr.requests.get = orig


# ---------------------------------------------------------------
# 3) Coalescing — 50 concurrent same-DR POSTs → 1 outbound roundtrip
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_coalescing_50_concurrent_same_dr(asgi_client, ctx, monkeypatch):
    """50 concurrent hits to the SAME DR → the freshness cache + coalesce
    lock collapse them to just the FIRST request's outbound calls
    (1 geocode + 1 weather = ~2 requests.get invocations)."""
    import asyncio
    await _clear_dr_weather(ctx["dr_id"])
    from routers import daily_reports_router as drr

    call_counter = {"n": 0}
    counter_lock = threading.Lock()

    def _fake_get(url, params=None, timeout=None, **kw):
        with counter_lock:
            call_counter["n"] += 1
        # slight delay so first request is still in-flight when others arrive
        time.sleep(0.20)
        resp = _requests.models.Response()
        resp.status_code = 200
        resp.encoding = "utf-8"
        if "geocoding-api" in url:
            resp._content = (
                b'{"results":[{"latitude":39.1,"longitude":-94.6,'
                b'"admin1":"Missouri","country_code":"US"}]}'
            )
        else:
            resp._content = (
                b'{"current":{"temperature_2m":70,"precipitation":0,'
                b'"wind_speed_10m":5,"weather_code":1},'
                b'"daily":{"temperature_2m_max":[75],"temperature_2m_min":[60],'
                b'"precipitation_sum":[0],"weather_code":[1],'
                b'"wind_speed_10m_max":[8]}}'
            )
        return resp

    monkeypatch.setattr(drr.requests, "get", _fake_get)

    async def _one():
        r = await asgi_client.post(
            f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
            headers=ctx["headers"],
        )
        return r.status_code

    results = await asyncio.gather(*[_one() for _ in range(50)])
    assert all(s == 200 for s in results), f"non-200s: {[s for s in results if s != 200]}"
    # Expect at most 2 outbound calls (1 geocode + 1 weather from the winning request).
    # Being lenient in case _geocode does the "city, state" fallback (rare on happy path).
    assert call_counter["n"] <= 3, (
        f"coalescing broke — expected <=3 outbound, got {call_counter['n']}"
    )


# ---------------------------------------------------------------
# 4) Semaphore cap — max 20 concurrent outbound
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_outbound(asgi_client, ctx, monkeypatch):
    """30 concurrent POSTs to DIFFERENT coalesce keys → semaphore(20)
    must keep peak in-flight outbound calls ≤ 20."""
    import asyncio
    from routers import daily_reports_router as drr

    # Ensure all target DRs start with no fresh weather_fetched_at
    for did in ctx["sem_dr_ids"]:
        await _clear_dr_weather(did)

    in_flight = {"cur": 0, "peak": 0}
    counter_lock = threading.Lock()

    def _fake_get(url, params=None, timeout=None, **kw):
        with counter_lock:
            in_flight["cur"] += 1
            if in_flight["cur"] > in_flight["peak"]:
                in_flight["peak"] = in_flight["cur"]
        try:
            time.sleep(0.25)
            resp = _requests.models.Response()
            resp.status_code = 200
            if "geocoding-api" in url:
                resp._content = (
                    b'{"results":[{"latitude":39.1,"longitude":-94.6,'
                    b'"admin1":"Missouri","country_code":"US"}]}'
                )
            else:
                resp._content = (
                    b'{"current":{"temperature_2m":70,"precipitation":0,'
                    b'"wind_speed_10m":5,"weather_code":1},'
                    b'"daily":{"temperature_2m_max":[75],"temperature_2m_min":[60],'
                    b'"precipitation_sum":[0],"weather_code":[1],'
                    b'"wind_speed_10m_max":[8]}}'
                )
            return resp
        finally:
            with counter_lock:
                in_flight["cur"] -= 1

    monkeypatch.setattr(drr.requests, "get", _fake_get)

    async def _one(did):
        r = await asgi_client.post(
            f"/api/daily-reports/{did}/weather-autofill",
            headers=ctx["headers"],
        )
        return r.status_code

    results = await asyncio.gather(*[_one(d) for d in ctx["sem_dr_ids"]])
    assert all(s == 200 for s in results), f"non-200s: {[s for s in results if s != 200]}"
    assert in_flight["peak"] <= 20, (
        f"semaphore breached — peak concurrent outbound = {in_flight['peak']} (cap=20)"
    )
    # Sanity: we should observe SOME concurrency, otherwise the test isn't
    # actually exercising the semaphore.
    assert in_flight["peak"] >= 2, (
        f"suspiciously low peak concurrency = {in_flight['peak']} — test may be serial"
    )


# ---------------------------------------------------------------
# 5) Negative cache — None result cached 60s
# ---------------------------------------------------------------
def test_negative_cache_60s(monkeypatch):
    from routers import daily_reports_router as drr

    calls = {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        raise _requests.exceptions.ConnectionError("simulated DNS fail")

    monkeypatch.setattr(drr.requests, "get", _boom)

    r1 = drr._fetch_weather(40.0, -95.0, "2026-01-15")
    assert r1 is None
    n_after_first = calls["n"]
    assert n_after_first >= 1

    # 2nd + 3rd call within 60s should hit the negative cache — no new outbound
    r2 = drr._fetch_weather(40.0, -95.0, "2026-01-15")
    r3 = drr._fetch_weather(40.0, -95.0, "2026-01-15")
    assert r2 is None and r3 is None
    assert calls["n"] == n_after_first, (
        f"negative cache broke: expected {n_after_first} calls, got {calls['n']}"
    )


# ---------------------------------------------------------------
# 6) All-4xx contract — ConnectionError → 422 JSON, short detail
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_connection_error_returns_422(asgi_client, ctx, monkeypatch):
    from routers import daily_reports_router as drr
    await _clear_dr_weather(ctx["dr_id"])

    def _boom(*a, **kw):
        raise _requests.exceptions.ConnectionError("simulated DNS fail")

    monkeypatch.setattr(drr.requests, "get", _boom)

    r = await asgi_client.post(
        f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
        headers=ctx["headers"],
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:400]}"
    assert "application/json" in r.headers.get("content-type", "")
    detail = r.json().get("detail")
    assert isinstance(detail, str) and 0 < len(detail) < 200


# ---------------------------------------------------------------
# 7) Regression — real Kansas City / MO happy path 200
# ---------------------------------------------------------------
def test_happy_path_real_kansas_city_returns_200(ctx):
    r = _requests.post(
        f"{API}/daily-reports/{ctx['dr_id']}/weather-autofill",
        headers=ctx["headers"], timeout=20,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    # Weather fields populated
    assert body.get("weather_conditions") is not None
    assert (body.get("weather_temp_high_f") is not None
            or body.get("weather_temp_low_f") is not None)


# ---------------------------------------------------------------
# Teardown — drop all collections for this test tenant
# ---------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _cleanup(ctx):
    yield
    try:
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        async def _wipe():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ["DB_NAME"]]
            # remove per-tenant docs by user email
            u = await db.users.find_one({"email": ctx["email"]})
            if u:
                tid = u.get("tenant_id")
                if tid:
                    for coll in ["daily_reports", "projects", "users", "tenants",
                                 "daily_presence", "audit_logs"]:
                        try:
                            await db[coll].delete_many({"tenant_id": tid})
                        except Exception:
                            pass
                    try:
                        await db.tenants.delete_many({"id": tid})
                    except Exception:
                        pass
            cli.close()
        asyncio.get_event_loop().run_until_complete(_wipe()) if False else asyncio.run(_wipe())
    except Exception as e:
        print(f"cleanup skipped: {e}")
