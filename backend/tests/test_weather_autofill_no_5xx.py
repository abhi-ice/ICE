"""Iteration 14 - Verify weather-autofill never returns a 5xx status.

Cloudflare intercepts any origin 5xx and substitutes its own 520 HTML page
before the browser sees JSON, defeating frontend sanitizers. All failure
paths in POST /api/daily-reports/{id}/weather-autofill must be < 500.
"""
import os
import time
import uuid
import pytest
import requests

pytestmark_asyncio = pytest.mark.asyncio  # noqa: F841

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


# -----------------------------
# One-time tenant/project/DR setup
# -----------------------------
@pytest.fixture(scope="module")
def ctx():
    ts = int(time.time())
    email = f"wxqa+{ts}@example.com"
    password = "TestPass123!"
    reg = requests.post(f"{API}/auth/register", json={
        "organization_name": f"wxqa-{ts}",
        "email": email,
        "password": password,
        "full_name": "Wx QA",
    }, timeout=30)
    assert reg.status_code in (200, 201), f"register failed: {reg.status_code} {reg.text[:400]}"
    body = reg.json()
    token = body.get("access_token") or body.get("token") or (body.get("data") or {}).get("access_token")
    if not token:
        # maybe need explicit login
        lg = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
        assert lg.status_code == 200, lg.text[:400]
        token = lg.json().get("access_token") or lg.json().get("token")
    assert token, f"no token in register/login response: {body}"
    h = {"Authorization": f"Bearer {token}"}

    # Create a project with real KC / MO
    p = requests.post(f"{API}/projects", headers=h, json={
        "name": f"WxQA Project {ts}",
        "client_name": "QA",
        "city": "Kansas City",
        "state": "MO",
    }, timeout=30)
    assert p.status_code in (200, 201), f"project create failed: {p.status_code} {p.text[:400]}"
    project = p.json()
    project_id = project["id"]

    # Create a project WITHOUT city/state
    p2 = requests.post(f"{API}/projects", headers=h, json={
        "name": f"WxQA NoAddr {ts}",
        "client_name": "QA",
    }, timeout=30)
    assert p2.status_code in (200, 201), p2.text[:400]
    project_noaddr = p2.json()

    # A DR for KC/MO project (today)
    from datetime import date
    today = date.today().isoformat()
    dr = requests.post(f"{API}/daily-reports", headers=h, json={
        "project_id": project_id,
        "report_date": today,
    }, timeout=30)
    assert dr.status_code in (200, 201), dr.text[:400]
    dr_id = dr.json()["id"]

    # A DR for the no-address project (yesterday to avoid conflict)
    from datetime import timedelta
    yday = (date.today() - timedelta(days=1)).isoformat()
    dr2 = requests.post(f"{API}/daily-reports", headers=h, json={
        "project_id": project_noaddr["id"],
        "report_date": yday,
    }, timeout=30)
    assert dr2.status_code in (200, 201), dr2.text[:400]
    dr_noaddr_id = dr2.json()["id"]

    return {
        "headers": h,
        "project_id": project_id,
        "project_noaddr_id": project_noaddr["id"],
        "dr_id": dr_id,
        "dr_noaddr_id": dr_noaddr_id,
    }


def _assert_json_and_status_lt_500(r):
    assert r.status_code < 500, (
        f"5xx returned! status={r.status_code} body={r.text[:400]}"
    )
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct, f"Content-Type not JSON: {ct}; body={r.text[:200]}"


# -----------------------------
# Tests: HTTP paths
# -----------------------------
class TestWeatherAutofillNo5xx:

    # Happy path: valid KC/MO project - real Open-Meteo call, expect 200
    def test_happy_path_returns_200(self, ctx):
        r = requests.post(
            f"{API}/daily-reports/{ctx['dr_id']}/weather-autofill",
            headers=ctx["headers"], timeout=20,
        )
        _assert_json_and_status_lt_500(r)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:400]}"
        body = r.json()
        # weather fields populated
        assert body.get("weather_conditions") is not None
        assert body.get("weather_temp_high_f") is not None or body.get("weather_temp_low_f") is not None

    # Failure path 1: no city/state on project -> 422 (NOT 5xx)
    def test_no_city_state_returns_422(self, ctx):
        r = requests.post(
            f"{API}/daily-reports/{ctx['dr_noaddr_id']}/weather-autofill",
            headers=ctx["headers"], timeout=20,
        )
        _assert_json_and_status_lt_500(r)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:400]}"
        detail = r.json().get("detail")
        assert detail and isinstance(detail, str)
        assert len(detail) < 200

    # Failure path 2: bad DR id -> 404 (NOT 5xx)
    def test_bad_dr_id_returns_404(self, ctx):
        bogus = str(uuid.uuid4())
        r = requests.post(
            f"{API}/daily-reports/{bogus}/weather-autofill",
            headers=ctx["headers"], timeout=20,
        )
        _assert_json_and_status_lt_500(r)
        assert r.status_code == 404, r.text[:400]
        assert r.json().get("detail")

    # Failure path 3: project deleted after DR created -> 404
    def test_project_deleted_returns_404(self, ctx):
        # Create a throwaway project + DR, then delete project, then autofill.
        from datetime import date, timedelta
        h = ctx["headers"]
        p = requests.post(f"{API}/projects", headers=h, json={
            "name": f"WxQA ToDelete {int(time.time())}",
            "client_name": "QA",
            "city": "Kansas City", "state": "MO",
        }, timeout=30)
        assert p.status_code in (200, 201), p.text[:400]
        pid = p.json()["id"]
        d = (date.today() - timedelta(days=2)).isoformat()
        dr = requests.post(f"{API}/daily-reports", headers=h, json={
            "project_id": pid, "report_date": d,
        }, timeout=30)
        assert dr.status_code in (200, 201), dr.text[:400]
        drid = dr.json()["id"]

        de = requests.delete(f"{API}/projects/{pid}", headers=h, timeout=30)
        if de.status_code >= 400:
            pytest.skip(f"Project delete not available ({de.status_code}) - "
                        f"can't simulate 'project deleted after DR' path")

        r = requests.post(
            f"{API}/daily-reports/{drid}/weather-autofill",
            headers=ctx["headers"], timeout=20,
        )
        _assert_json_and_status_lt_500(r)
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:400]}"

    # Failure path 4: DR submitted/locked -> 409
    def test_submitted_dr_returns_409(self, ctx):
        # Empty DR requires no_work_day flag - submit as no-work
        h = ctx["headers"]
        from datetime import date, timedelta
        d = (date.today() - timedelta(days=3)).isoformat()
        dr = requests.post(f"{API}/daily-reports", headers=h, json={
            "project_id": ctx["project_id"], "report_date": d,
        }, timeout=30)
        assert dr.status_code in (200, 201), dr.text[:400]
        drid = dr.json()["id"]

        sub = requests.post(
            f"{API}/daily-reports/{drid}/submit", headers=h,
            json={"is_no_work_day": True, "no_work_reason": "weather"},
            timeout=30,
        )
        if sub.status_code >= 400:
            pytest.skip(f"submit failed with {sub.status_code}: {sub.text[:300]}")

        r = requests.post(
            f"{API}/daily-reports/{drid}/weather-autofill",
            headers=h, timeout=20,
        )
        _assert_json_and_status_lt_500(r)
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text[:400]}"


# -----------------------------
# Tests: simulate provider failures via monkeypatch (in-process via ASGI)
# -----------------------------
@pytest.fixture
async def asgi_client():
    """Fresh ASGI client per-test so motor connections bind to the
    running event loop. Reconnects the module-level DB handle used by
    routers to the current loop's default executor."""
    import sys
    sys.path.insert(0, "/app/backend")
    from server import app  # type: ignore
    import database  # type: ignore
    from motor.motor_asyncio import AsyncIOMotorClient
    # Rebind motor client to current loop
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    database._client = cli  # type: ignore[attr-defined]
    database._db = cli[os.environ["DB_NAME"]]  # type: ignore[attr-defined]
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    cli.close()


async def _clear_dr_weather(dr_id: str):
    """Iter15 addition — the new 5-min freshness cache would return the DR
    from a previous happy-path test w/o calling monkeypatched _geocode. Clear
    weather_fetched_at so the monkeypatched failure path actually runs."""
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    await db.daily_reports.update_many(
        {"id": dr_id},
        {"$unset": {"weather_fetched_at": "", "weather_conditions": "",
                    "weather_temp_high_f": "", "weather_temp_low_f": "",
                    "weather_precipitation_in": "", "weather_wind_mph": ""}},
    )
    cli.close()


@pytest.fixture(autouse=True)
def _reset_wx_caches():
    """Iter15 addition — clear module-level TTL caches between tests."""
    try:
        from routers import daily_reports_router as drr
        drr._GEOCODE_CACHE.clear()
        drr._WEATHER_CACHE.clear()
    except Exception:
        pass
    yield


@pytest.mark.asyncio
async def test_geocode_returns_none_simulated_unreachable(monkeypatch, asgi_client, ctx):
    """Simulate Open-Meteo unreachable via _geocode returning None -> 422
    'Could not geocode ...' (NOT 5xx)."""
    from routers import daily_reports_router as drr
    await _clear_dr_weather(ctx["dr_id"])
    monkeypatch.setattr(drr, "_geocode", lambda city, state, postal=None: None)
    r = await asgi_client.post(
        f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
        headers=ctx["headers"],
    )
    _assert_json_and_status_lt_500(r)
    assert r.status_code == 422
    assert "geocode" in r.json()["detail"].lower() or "provider" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fetch_weather_returns_none_simulated_unreachable(monkeypatch, asgi_client, ctx):
    """Simulate weather provider unreachable via _fetch_weather returning
    None -> 422 'Weather provider unreachable' (NOT 5xx)."""
    from routers import daily_reports_router as drr
    await _clear_dr_weather(ctx["dr_id"])
    monkeypatch.setattr(drr, "_geocode", lambda c, s, p=None: (39.1, -94.6))
    monkeypatch.setattr(drr, "_fetch_weather", lambda lat, lng, d: None)
    r = await asgi_client.post(
        f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
        headers=ctx["headers"],
    )
    _assert_json_and_status_lt_500(r)
    assert r.status_code == 422
    assert "unreachable" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_geocode_timeout_simulated(monkeypatch, asgi_client, ctx):
    """Simulate geocoder hang > 6s -> asyncio.TimeoutError -> 422
    'Geocoding provider slow to answer' (NOT 504/5xx)."""
    def _hang(*a, **kw):
        time.sleep(10)  # exceeds 6s wait_for
        return (39.1, -94.6)
    from routers import daily_reports_router as drr
    await _clear_dr_weather(ctx["dr_id"])
    monkeypatch.setattr(drr, "_geocode", _hang)
    r = await asgi_client.post(
        f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
        headers=ctx["headers"],
        timeout=30,
    )
    _assert_json_and_status_lt_500(r)
    assert r.status_code == 422
    assert "slow" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fetch_weather_timeout_simulated(monkeypatch, asgi_client, ctx):
    """Simulate weather-fetch hang > 6s -> 422 'Weather provider slow'."""
    from routers import daily_reports_router as drr
    await _clear_dr_weather(ctx["dr_id"])
    monkeypatch.setattr(drr, "_geocode", lambda c, s, p=None: (39.1, -94.6))
    def _hang(*a, **kw):
        time.sleep(10)
        return {"weather_conditions": "clear"}
    monkeypatch.setattr(drr, "_fetch_weather", _hang)
    r = await asgi_client.post(
        f"/api/daily-reports/{ctx['dr_id']}/weather-autofill",
        headers=ctx["headers"],
        timeout=30,
    )
    _assert_json_and_status_lt_500(r)
    assert r.status_code == 422
    assert "slow" in r.json()["detail"].lower()
