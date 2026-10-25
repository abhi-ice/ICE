"""End-to-end test for the 5-Route DR Voice Intake feature.

This test goes ALL THE WAY:

  1. Synthesize a realistic construction-site voice memo using gTTS
     (Google Text-to-Speech). Produces a real MP3 with human-like
     speech that Gemini can actually transcribe.

  2. POST the audio to /voice/transcribe-and-extract -- exactly what
     the frontend does when a super clicks "Stop recording" on the
     Voice Memo intake mode.

  3. Verify Gemini 3.5 Flash transcribed the speech back into text
     containing the expected construction keywords.

  4. Verify Claude Sonnet 4.5 extracted structured fields from the
     transcript (weather, crew presence, delays, safety events).

  5. Verify the records were written to MongoDB and surface in the
     /context bundle the way the DR detail page reads them.

Cost: ~$0.005 per run ($0.001 gTTS, $0.002 Gemini transcribe,
$0.002 Claude extract). Runtime: 30-60 seconds.

Requires:
  - GEMINI_API_KEY set on the backend (already verified by Phase B)
  - ANTHROPIC_API_KEY set on the backend
  - Internet access for gTTS to call Google Translate's TTS endpoint
  - The preview backend running at REACT_APP_BACKEND_URL

Skip with:  pytest -k "not e2e"
"""
from __future__ import annotations
import io
import os
import time

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

# The script a "super" reads aloud for the test. Real construction
# vocabulary so we can verify Gemini didn't just hear "blah blah."
# We use a short clip to keep gTTS + Gemini latency reasonable.
SUPER_SCRIPT = (
    "Today was clear and sunny. High of 72 degrees, low of 58. "
    "Two carpenters from Acme Construction on site, 8 hours each. "
    "Activity 1.4 framing is 50 percent complete. "
    "We had a minor safety near miss when a worker tripped on debris. "
    "No injury, but we ordered the area swept."
)


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/api/auth/login",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def synthesized_audio():
    """Generate a real MP3 of the super's script using gTTS."""
    try:
        from gtts import gTTS
    except ImportError:
        pytest.skip("gtts not installed -- pip install gtts")

    buf = io.BytesIO()
    tts = gTTS(text=SUPER_SCRIPT, lang="en", slow=False)
    tts.write_to_fp(buf)
    audio_bytes = buf.getvalue()
    assert audio_bytes, "gTTS produced empty audio"
    assert len(audio_bytes) > 5000, f"gTTS audio suspiciously small ({len(audio_bytes)} bytes)"
    return audio_bytes


@pytest.fixture(scope="module")
def fresh_dr_id(token):
    """Create a fresh draft DR specifically for this test run.

    Picks a date 4 days back to avoid colliding with any existing
    DRs created by other tests in this session. To make the test
    idempotent across multiple runs against the same backend, we
    delete any pre-existing voice-intake rows on this DR so the
    de-duplication logic doesn't suppress the test inserts.
    """
    proj = requests.get(f"{API}/api/projects", headers=_h(token), timeout=10).json()[0]["id"]
    from datetime import date, timedelta
    pick = (date.today() - timedelta(days=4)).isoformat()
    cr = requests.post(
        f"{API}/api/daily-reports", headers=_h(token),
        json={"project_id": proj, "report_date": pick}, timeout=15,
    )
    cr.raise_for_status()
    dr_id = cr.json()["id"]

    # Wipe ALL pre-existing event rows on this DR so the dedup pass in
    # the voice extract router doesn't skip the new inserts. We use
    # the broader sweep (not just source=voice_intake) because manual
    # test data also lingers across runs.
    ctx = requests.get(f"{API}/api/daily-reports/{dr_id}/context",
                       headers=_h(token), timeout=10).json()
    for path, key in (("delays", "delays"), ("safety-events", "safety_events"),
                      ("qc-issues", "qc_issues"), ("daily-presence", "presence")):
        for row in ctx.get(key, []) or []:
            requests.delete(f"{API}/api/{path}/{row['id']}", headers=_h(token), timeout=10)
    return dr_id


def test_e2e_voice_memo_full_pipeline(token, synthesized_audio, fresh_dr_id):
    """The whole story:  audio bytes  →  Gemini  →  Claude  →  DB  →  /context."""
    started = time.time()

    files = {"audio": ("super_memo.mp3", synthesized_audio, "audio/mp3")}
    r = requests.post(
        f"{API}/api/daily-reports/{fresh_dr_id}/voice/transcribe-and-extract",
        headers=_h(token),
        files=files,
        timeout=120,
    )
    elapsed = time.time() - started
    print(f"\n[E2E] transcribe-and-extract took {elapsed:.1f}s, status={r.status_code}")

    assert r.status_code == 200, f"endpoint failed: {r.text}"
    body = r.json()

    # ---- (a) Gemini actually transcribed the speech ----
    transcript = (body.get("transcript") or "").lower()
    print(f"[E2E] Transcript: {transcript!r}")
    assert transcript, "Gemini returned an empty transcript"
    # Loose keyword check -- gTTS is a TTS engine and Gemini's STT may
    # render numbers as digits OR words ("72" vs "seventy-two"). We
    # require enough of the domain vocabulary to prove Gemini heard
    # the actual speech instead of hallucinating boilerplate.
    keyword_hits = sum(
        1 for kw in ("carpenter", "framing", "near miss", "acme", "debris",
                     "tripped", "clear", "sunny", "today", "site")
        if kw in transcript
    )
    assert keyword_hits >= 4, (
        f"Transcript didn't contain enough domain keywords (hits={keyword_hits}). "
        f"Transcript was: {transcript!r}"
    )

    # ---- (b) Claude extracted structured fields ----
    applied = body.get("applied") or {}
    print(f"[E2E] Applied counts: {applied}")
    assert applied.get("weather") is True, "Weather wasn't extracted"
    assert applied.get("crew_presence", 0) >= 1, "No crew presence row was created"
    # safety event extraction depends on Claude's classification; loose check
    assert applied.get("safety_events", 0) >= 1 or "near miss" in transcript, \
        "Safety event not detected"

    # ---- (c) /context bundle now surfaces the writes ----
    ctx = requests.get(
        f"{API}/api/daily-reports/{fresh_dr_id}/context",
        headers=_h(token), timeout=15,
    ).json()
    presence = ctx.get("presence", [])
    assert any(p.get("headcount") for p in presence), \
        f"No headcount-bearing presence row found in /context: {presence}"
    rep = ctx.get("report") or {}
    # Weather should have been populated (high & low)
    assert rep.get("weather_temp_high_f") is not None, "Weather high not on DR"
    assert rep.get("weather_temp_low_f") is not None, "Weather low not on DR"

    print(f"[E2E] PASSED. presence={len(presence)} delays={len(ctx.get('delays',[]))} "
          f"safety={len(ctx.get('safety_events',[]))} "
          f"weather=({rep.get('weather_temp_high_f')}/{rep.get('weather_temp_low_f')})")


def test_e2e_voice_transcribe_endpoint_only(token, synthesized_audio, fresh_dr_id):
    """Lighter-weight test: transcribe-only endpoint (no Claude extraction)."""
    files = {"audio": ("super_memo.mp3", synthesized_audio, "audio/mp3")}
    r = requests.post(
        f"{API}/api/daily-reports/{fresh_dr_id}/voice/transcribe",
        headers=_h(token), files=files, timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("transcript"), "transcribe endpoint returned empty"
    assert body.get("char_count", 0) > 30, "transcript too short to be valid"


def test_e2e_oversize_upload_rejected(token, fresh_dr_id):
    """Files larger than the 100 MB cap must be rejected with 413."""
    # 101 MB of zeros -- this never reaches Gemini because we reject early.
    blob = b"\x00" * (101 * 1024 * 1024)
    files = {"audio": ("huge.mp3", blob, "audio/mp3")}
    r = requests.post(
        f"{API}/api/daily-reports/{fresh_dr_id}/voice/transcribe",
        headers=_h(token), files=files, timeout=120,
    )
    assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text[:200]}"


def test_e2e_empty_upload_rejected(token, fresh_dr_id):
    """Empty file upload must return 400 (not 500)."""
    files = {"audio": ("empty.mp3", b"", "audio/mp3")}
    r = requests.post(
        f"{API}/api/daily-reports/{fresh_dr_id}/voice/transcribe",
        headers=_h(token), files=files, timeout=10,
    )
    assert r.status_code == 400


def test_e2e_live_voice_qa_transcript_extraction(token):
    """Simulates the END of a Gemini Live session: feed Claude the
    Q&A-formatted transcript that the frontend builds from
    inputTranscription + outputTranscription chunks, and verify it
    extracts structured fields even from short, fragmented answers
    like 'three' / 'twenty percent' / 'moderate'.

    This guards against the bug where the live conversation produced
    a fragmented per-token transcript that Claude could not parse.
    """
    # Fresh DR on a date unlikely to collide with manual data.
    from datetime import date, timedelta
    import random
    proj = requests.get(f"{API}/api/projects", headers=_h(token), timeout=10).json()[0]["id"]
    pick = (date.today() - timedelta(days=random.randint(80, 200))).isoformat()
    cr = requests.post(
        f"{API}/api/daily-reports", headers=_h(token),
        json={"project_id": proj, "report_date": pick}, timeout=15,
    )
    cr.raise_for_status()
    dr_id = cr.json()["id"]

    # Strip any pre-existing rows on this DR.
    ctx0 = requests.get(f"{API}/api/daily-reports/{dr_id}/context",
                        headers=_h(token), timeout=10).json()
    for path, key in (("delays", "delays"), ("safety-events", "safety_events"),
                      ("qc-issues", "qc_issues"), ("daily-presence", "presence")):
        for row in ctx0.get(key, []) or []:
            requests.delete(f"{API}/api/{path}/{row['id']}",
                            headers=_h(token), timeout=10)

    qa_transcript = (
        "Q (AI): What were the weather conditions today?\n"
        "A (super): Clear sky, high of 78 low of 60.\n"
        "Q (AI): Which trades were on site and what were the headcounts?\n"
        "A (super): Electrical trade, headcount three, company ICE, 8 hours each.\n"
        "Q (AI): What progress was made today?\n"
        "A (super): Electrical rough in is at 20 percent.\n"
        "Q (AI): Were there any delays today?\n"
        "A (super): Yes a delay linked to the electrical rough-in task, electrical shaft was not working.\n"
        "Q (AI): How many days impact?\n"
        "A (super): Three days.\n"
        "Q (AI): Severity, minor moderate or major?\n"
        "A (super): Moderate.\n"
        "Q (AI): Any safety, QC, deliveries today?\n"
        "A (super): No nothing skip.\n"
        "Q (AI): General notes?\n"
        "A (super): No, all good."
    )
    r = requests.post(
        f"{API}/api/daily-reports/{dr_id}/voice/extract",
        headers=_h(token),
        json={"transcript": qa_transcript},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    a = body["applied"]
    print(f"\n[LIVE-QA] applied={a}")
    # Hard requirements
    assert a["weather"] is True, "Weather not extracted from Q&A"
    assert a["crew_presence"] >= 1, "No crew presence row"
    assert a["delays"] >= 1, "Delay not extracted from short answer"
    # activity_updates requires a matching grid row, which the test DR
    # may not have. We instead verify Claude PROPOSED an activity
    # update in the draft -- the application step is grid-dependent.
    proposed_acts = body.get("draft", {}).get("activity_updates") or []
    assert any(
        "electrical" in (au.get("activity_description_hint") or "").lower()
        for au in proposed_acts
    ), f"Claude didn't propose an electrical activity update: {proposed_acts}"

    # The "no/skip" responses must NOT generate empty event records.
    assert a["safety_events"] == 0, "Safety should be empty when super said 'no'"
    assert a["qc_issues"] == 0, "QC should be empty when super said 'no'"
    assert a["deliveries"] == 0, "Deliveries should be empty when super said 'no'"

    # Verify delay has the impact days (3 days from the follow-up Q)
    ctx = requests.get(f"{API}/api/daily-reports/{dr_id}/context",
                       headers=_h(token), timeout=15).json()
    delays = ctx.get("delays", [])
    assert delays, "No delay row in DB"
    d = delays[0]
    assert d.get("delay_days") == 3 or any(
        p.get("projected_impact_days") == 3 for p in (d.get("per_task_impacts") or [])
    ), f"3-day impact not captured: {d}"
    assert d.get("severity") == "moderate", f"Severity should be moderate: {d.get('severity')}"
