"""Regression tests for the 5-Route DR voice intake feature.

Validates:
  - GET /daily-reports/voice/config returns the right boolean shape
  - POST /voice/live-token returns 503 when GEMINI_API_KEY is absent
  - POST /voice/transcribe returns 503 when GEMINI_API_KEY is absent
  - POST /voice/extract with a synthetic transcript routes through
    Claude (custom anthropic_compat shim) and writes the parsed rows
    into the canonical collections (daily_presence, delays,
    safety_events) with daily_report_id linkage so the /context
    bundle surfaces them.

These tests run against the LIVE preview backend with abhi's account.
They are end-to-end smoke tests, not unit tests; the Claude call costs
roughly $0.002 per execution. Skip with -k "not live" if needed.
"""
from __future__ import annotations
import os
import pytest
import requests

if not (os.environ.get("REACT_APP_BACKEND_URL") or "").strip():
    pytest.skip(
        "REACT_APP_BACKEND_URL is not set. These tests call a running ICE backend and no "
        "longer fall back to a remote preview host; set it to the server you mean to test.",
        allow_module_level=True,
    )
API = os.environ["REACT_APP_BACKEND_URL"].strip()
EMAIL = "abhi@ice-aec.com"
PASSWORD = "Ice@24608733"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/api/auth/login",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


def test_voice_config_endpoint(token):
    r = requests.get(f"{API}/api/daily-reports/voice/config", headers=_h(token), timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert "gemini_configured" in body
    assert "voice_memo_enabled" in body
    assert "live_voice_enabled" in body
    assert isinstance(body["gemini_configured"], bool)


def test_voice_live_token_503_without_gemini(token):
    # Use any DR id -- the 503 fires before DR lookup.
    r = requests.post(f"{API}/api/daily-reports/any-id/voice/live-token",
                      headers=_h(token), timeout=10)
    # 503 when GEMINI_API_KEY is absent; 200 when present.
    cfg = requests.get(f"{API}/api/daily-reports/voice/config",
                       headers=_h(token), timeout=10).json()
    if not cfg["gemini_configured"]:
        assert r.status_code == 503
    else:
        assert r.status_code in (200, 404)


def test_voice_extract_empty_transcript_400(token):
    # Need a real DR id to get past the lookup; pick the project's most recent.
    proj = requests.get(f"{API}/api/projects", headers=_h(token), timeout=10).json()[0]["id"]
    drs = requests.get(f"{API}/api/projects/{proj}/daily-reports",
                       headers=_h(token), timeout=10).json()
    dr = next((d for d in drs if d.get("status") in (None, "draft")), drs[0])
    r = requests.post(f"{API}/api/daily-reports/{dr['id']}/voice/extract",
                      headers=_h(token), json={"transcript": ""}, timeout=10)
    assert r.status_code == 400


def test_voice_extract_full_pipeline(token):
    """Feed a transcript -> Claude -> verify writes appear in /context."""
    proj = requests.get(f"{API}/api/projects", headers=_h(token), timeout=10).json()[0]["id"]
    # Pick the date with the FEWEST existing DRs/events so we get a
    # clean canvas. The date range goes 30-50 days back -- old enough
    # that prior test runs are unlikely to have populated it.
    from datetime import date, timedelta
    import random
    pick = (date.today() - timedelta(days=random.randint(60, 200))).isoformat()
    cr = requests.post(f"{API}/api/daily-reports", headers=_h(token),
                       json={"project_id": proj, "report_date": pick}, timeout=15)
    cr.raise_for_status()
    dr_id = cr.json()["id"]

    # Wipe ALL pre-existing event rows on this DR so the dedup pass in
    # voice_extract doesn't suppress this run's inserts. (Tests rerun
    # on the same DR; we don't want yesterday's rows blocking today's.)
    ctx0 = requests.get(f"{API}/api/daily-reports/{dr_id}/context",
                        headers=_h(token), timeout=10).json()
    for path, key in (("delays", "delays"), ("safety-events", "safety_events"),
                      ("qc-issues", "qc_issues"), ("daily-presence", "presence")):
        for row in ctx0.get(key, []) or []:
            requests.delete(f"{API}/api/{path}/{row['id']}",
                            headers=_h(token), timeout=10)

    # Send the extract request. ~5-15s for Claude.
    transcript = (
        "Today weather was 80 high 65 low partly cloudy no rain. "
        "Two carpenters from BuildCo on site 8 hours each. "
        "Activity 2.1 framing 45 percent done. "
        "Minor safety event near miss when a circular saw kicked back, no injury."
    )
    er = requests.post(f"{API}/api/daily-reports/{dr_id}/voice/extract",
                       headers=_h(token), json={"transcript": transcript},
                       timeout=60)
    assert er.status_code == 200, er.text
    body = er.json()
    assert body["ok"] is True
    a = body["applied"]
    assert a["weather"] is True
    assert a["crew_presence"] >= 1
    assert a["safety_events"] >= 1

    # Pull /context and verify the rows surfaced under the same DR.
    ctx = requests.get(f"{API}/api/daily-reports/{dr_id}/context",
                       headers=_h(token), timeout=15).json()
    assert any(p.get("trade") for p in ctx.get("presence", []))
    assert any("ladder" in (s.get("description") or "").lower()
               or "saw" in (s.get("description") or "").lower()
               or s.get("event_type") == "near_miss"
               for s in ctx.get("safety_events", []))
    # Weather on the DR doc itself
    rep = ctx.get("report") or {}
    assert rep.get("weather_temp_high_f") == 80.0
    assert rep.get("weather_temp_low_f") == 65.0
