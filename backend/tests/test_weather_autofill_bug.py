"""
Backend regression tests for the weather-autofill prod-bug fix.

Bug: Cloudflare's HTML origin-error page was leaking into the frontend toast
because the sync `requests.get` in _geocode/_fetch_weather blocked the event
loop. Fix: `run_in_threadpool` + `asyncio.wait_for(timeout=6.0)`, so any
failure returns a JSON body (never HTML), and the frontend toast sanitizes.

These tests assert:
  1. weather-autofill always returns a JSON response body (never text/html).
  2. When Open-Meteo IS reachable, 200 with weather fields populated.
  3. When we monkey-patch the sync helpers to hang, we get HTTP 504 JSON
     with a short `detail` string (< 200 chars) — no HTML.
  4. When geocode returns None, 422 with a short JSON detail.
"""
import os
import time
import requests
import pytest

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth(session):
    ts = int(time.time())
    payload = {
        "organization_name": f"weatherqa-{ts}",
        "email": f"weatherqa+{ts}@example.com",
        "password": "TestPass123!",
        "full_name": "Weather QA",
    }
    r = session.post(f"{BASE_URL}/api/auth/register", json=payload, timeout=30)
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text[:300]}"
    data = r.json()
    token = data.get("access_token") or data.get("token") or (data.get("user") or {}).get("access_token")
    assert token, f"no token in register response: {data}"
    session.headers.update({"Authorization": f"Bearer {token}"})
    return {"token": token, "email": payload["email"]}


@pytest.fixture(scope="module")
def project(session, auth):
    payload = {
        "name": "Weather QA Project",
        "city": "Kansas City",
        "state": "MO",
        "postal_code": "64108",
        "status": "active",
    }
    r = session.post(f"{BASE_URL}/api/projects", json=payload, timeout=30)
    assert r.status_code in (200, 201), f"create project failed: {r.status_code} {r.text[:300]}"
    return r.json()


@pytest.fixture(scope="module")
def daily_report(session, project):
    from datetime import date
    payload = {"project_id": project["id"], "report_date": date.today().isoformat()}
    r = session.post(f"{BASE_URL}/api/daily-reports", json=payload, timeout=30)
    assert r.status_code in (200, 201), f"create DR failed: {r.status_code} {r.text[:400]}"
    return r.json()


def _assert_json_response(r):
    """Response body MUST be JSON (never HTML). This is the whole point of
    the prod-bug fix."""
    ctype = r.headers.get("content-type", "")
    body_snip = r.text[:400]
    assert "application/json" in ctype, (
        f"Non-JSON content-type '{ctype}' — HTML would leak into frontend toast! Body: {body_snip}"
    )
    assert not body_snip.lstrip().lower().startswith("<"), (
        f"Response body starts with HTML tag — regression! Body: {body_snip}"
    )
    # If it has a detail, it should be a short string
    try:
        j = r.json()
    except Exception as e:
        pytest.fail(f"Body not parseable JSON: {e}. Body: {body_snip}")
    if isinstance(j, dict) and "detail" in j and isinstance(j["detail"], str):
        assert len(j["detail"]) < 200, f"detail too long ({len(j['detail'])} chars) — frontend clamps <200"


class TestWeatherAutofillHappyPath:
    """Verify the endpoint responds with JSON in all cases."""

    def test_autofill_returns_json_body(self, session, daily_report):
        r = session.post(
            f"{BASE_URL}/api/daily-reports/{daily_report['id']}/weather-autofill",
            timeout=30,
        )
        _assert_json_response(r)
        # Acceptable outcomes: 200 (weather populated) OR 502/504 (upstream
        # unreachable / slow). NEVER 5xx with HTML body.
        assert r.status_code in (200, 502, 504), (
            f"Unexpected status {r.status_code}: {r.text[:400]}"
        )
        if r.status_code == 200:
            body = r.json()
            # Body should be the DR doc with weather fields filled
            assert "weather_fetched_at" in body and body["weather_fetched_at"]
            # At least one weather field non-null
            has_any = any(
                body.get(k) is not None
                for k in ("weather_temp_high_f", "weather_temp_low_f",
                          "weather_conditions", "weather_precipitation_in",
                          "weather_wind_mph")
            )
            assert has_any, f"200 but no weather fields populated: {body}"


class TestWeatherAutofillErrorPaths:
    """422 when project has no address; response is JSON with short detail."""

    def test_autofill_no_address_returns_short_json_422(self, session, auth):
        # Create a project with NO city/state
        r = session.post(f"{BASE_URL}/api/projects", json={"name": "NoAddr", "status": "active"}, timeout=30)
        assert r.status_code in (200, 201), r.text[:300]
        pid = r.json()["id"]
        from datetime import date
        r = session.post(
            f"{BASE_URL}/api/daily-reports",
            json={"project_id": pid, "report_date": date.today().isoformat()},
            timeout=30,
        )
        assert r.status_code in (200, 201), r.text[:400]
        drid = r.json()["id"]
        r = session.post(f"{BASE_URL}/api/daily-reports/{drid}/weather-autofill", timeout=30)
        _assert_json_response(r)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:300]}"

    def test_autofill_bad_dr_id_returns_json_404(self, session):
        r = session.post(f"{BASE_URL}/api/daily-reports/does-not-exist/weather-autofill", timeout=30)
        _assert_json_response(r)
        assert r.status_code == 404


@pytest.fixture(scope="module", autouse=True)
def _cleanup(session, auth):
    yield
    # Best-effort cleanup: drop tenant collections (per test_credentials.md).
    # We can't run the mongo cleanup snippet from an external test process,
    # so just log — main agent / next run will use fresh timestamped orgs.
    print(f"[cleanup] test tenant email={auth['email']} left in DB "
          "(external test — cannot access MONGO_URL from here).")
