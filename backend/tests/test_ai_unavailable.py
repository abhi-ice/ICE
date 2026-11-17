"""AI unavailable message (2026-11-16).

With no AI key (ANTHROPIC_API_KEY / EMERGENT_LLM_KEY) and PHOTO_AI_DEV_MODE
not set, every AI feature answers with a plain message and saves nothing:

  * photo AI analysis + review: "AI analysis isn't available right now.
    Your photos are saved; try again later." (422, code "ai_unavailable");
    no made-up findings, no saved review, the tray row is left as it was.
  * old made-up dev stub results are never shown as real findings and
    never copied onto another photo.
  * copilot, voice memo extraction and the client-template export answer
    the same way before any AI call or database write.
  * PHOTO_AI_DEV_MODE=1 still gives the dev stub, on purpose.

Offline: a small in-memory fake database stands in for Mongo.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ai_unavailable import (
    AI_UNAVAILABLE_CODE,
    AiUnavailableError,
    PHOTO_AI_UNAVAILABLE_MESSAGE,
    COPILOT_UNAVAILABLE_MESSAGE,
    VOICE_EXTRACT_UNAVAILABLE_MESSAGE,
    CLIENT_TEMPLATE_UNAVAILABLE_MESSAGE,
    ai_unavailable_handler,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------
# Tiny in-memory Mongo stand-in
# ---------------------------------------------------------------------
def _match(doc, flt):
    for k, v in (flt or {}).items():
        val = doc.get(k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
        elif val != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    async def to_list(self, n=None, length=None):
        n = n or length
        return [dict(d) for d in self._docs[: n or None]]


class _Result:
    def __init__(self, n=0):
        self.matched_count = self.modified_count = self.deleted_count = n


class FakeCollection:
    def __init__(self):
        self.docs = []
        self.writes = 0

    async def find_one(self, flt=None, _proj=None, **_k):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    def find(self, flt=None, _proj=None, **_k):
        return _Cursor([d for d in self.docs if _match(d, flt)])

    async def insert_one(self, doc):
        self.writes += 1
        self.docs.append(dict(doc))
        return _Result(1)

    async def update_one(self, flt, upd, **_k):
        self.writes += 1
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set") or {})
                return _Result(1)
        return _Result(0)

    async def delete_one(self, flt):
        self.writes += 1
        return _Result(0)


class FakeDB:
    def __init__(self):
        self._cols = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._cols.setdefault(name, FakeCollection())

    def total_writes(self):
        return sum(c.writes for c in self._cols.values())


TENANT = "t1"
CTX = {"tenant_id": TENANT, "user": {"id": "u1", "role": "admin", "full_name": "Pat"},
       "request_id": "r1"}


@pytest.fixture
def no_ai(monkeypatch):
    """No AI key and dev mode off (another test file turns dev mode on
    for the whole run, so clear it explicitly)."""
    for k in ("ANTHROPIC_API_KEY", "EMERGENT_LLM_KEY", "PHOTO_AI_DEV_MODE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def dev_mode(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "EMERGENT_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PHOTO_AI_DEV_MODE", "1")


@pytest.fixture
def photo_env(monkeypatch):
    """services.photo_ai + routers.photo_tray_router on a fake database,
    with the safety/budget gates and image reads faked."""
    import services.photo_ai as pa
    import routers.photo_tray_router as ptr

    db = FakeDB()

    async def _safe(**_k):
        return {"safe_to_analyze": True}

    async def _budget(**_k):
        return {"budget_ok": True}

    async def _blob(_key):
        return b"img", "image/jpeg"

    async def _no_audit(**_k):
        return None

    async def _allowed(*_a, **_k):
        return None

    monkeypatch.setattr(pa, "get_db", lambda: db)
    monkeypatch.setattr(pa, "check_cui_safety", _safe)
    monkeypatch.setattr(pa, "check_budget", _budget)
    monkeypatch.setattr(pa, "cached_get_object", _blob)
    monkeypatch.setattr(pa, "_preprocess_image_for_vision",
                        lambda b: (b"jpeg", "image/jpeg", {}))
    monkeypatch.setattr(ptr, "get_db", lambda: db)
    monkeypatch.setattr(ptr, "write_audit", _no_audit)
    monkeypatch.setattr(ptr, "check_record_access", _allowed)
    monkeypatch.setattr(ptr, "_enforce_tray_access", _allowed)
    db.photo_tray.docs.append({
        "id": "tray1", "tenant_id": TENANT, "project_id": "p1",
        "storage_key": "k1", "phash": "ph1", "ai_analysis_status": "pending",
    })
    return pa, ptr, db


# ---------------------------------------------------------------------
# Mode switches
# ---------------------------------------------------------------------
def test_missing_key_is_not_dev_mode(no_ai):
    import services.photo_ai as pa
    assert pa._is_dev_mode() is False
    assert pa.ai_available() is False
    assert pa.photo_ai_can_run() is False


def test_dev_mode_only_with_explicit_flag(dev_mode):
    import services.photo_ai as pa
    assert pa._is_dev_mode() is True
    assert pa.photo_ai_can_run() is True


def test_handler_body_keeps_detail_text_and_adds_code():
    app = FastAPI()
    app.add_exception_handler(AiUnavailableError, ai_unavailable_handler)

    @app.get("/x")
    async def _x():
        raise AiUnavailableError(PHOTO_AI_UNAVAILABLE_MESSAGE)

    r = TestClient(app).get("/x")
    assert r.status_code == 422
    assert r.json() == {"detail": PHOTO_AI_UNAVAILABLE_MESSAGE,
                        "code": AI_UNAVAILABLE_CODE}


def test_server_registers_the_handler():
    src = open("server.py", encoding="utf-8").read()
    assert "add_exception_handler(AiUnavailableError, ai_unavailable_handler)" in src


# ---------------------------------------------------------------------
# Photo AI service
# ---------------------------------------------------------------------
def test_review_without_key_returns_message_and_no_findings(no_ai, photo_env):
    pa, _ptr, db = photo_env
    out = run(pa.review_photos(tenant_id=TENANT, tray_ids=["tray1"], lens="combined"))
    assert out == {"ok": False, "reason": AI_UNAVAILABLE_CODE,
                   "message": PHOTO_AI_UNAVAILABLE_MESSAGE}
    assert db.total_writes() == 0


def test_review_in_dev_mode_still_uses_stub(dev_mode, photo_env):
    pa, _ptr, _db = photo_env
    out = run(pa.review_photos(tenant_id=TENANT, tray_ids=["tray1"], lens="combined"))
    assert out["ok"] is True
    assert out["text"].startswith("(Dev stub)")


def test_analyze_without_key_leaves_row_untouched(no_ai, photo_env):
    pa, _ptr, db = photo_env
    before = dict(db.photo_tray.docs[0])
    out = run(pa.analyze_photo(tenant_id=TENANT, tray_id="tray1"))
    assert out["ok"] is False
    assert out["reason"] == AI_UNAVAILABLE_CODE
    assert out["message"] == PHOTO_AI_UNAVAILABLE_MESSAGE
    assert db.photo_tray.docs[0] == before  # still "pending", nothing made up
    assert db.photo_tray.writes == 0


def test_analyze_in_dev_mode_writes_stub(dev_mode, photo_env, monkeypatch):
    pa, _ptr, db = photo_env

    async def _noop(**_k):
        return None

    async def _lock(**_k):
        return True

    monkeypatch.setattr(pa, "record_call_timing", _noop)
    monkeypatch.setattr(pa, "_try_claim_phash_lock", _lock)
    monkeypatch.setattr(pa, "_release_phash_lock", _noop)
    out = run(pa.analyze_photo(tenant_id=TENANT, tray_id="tray1"))
    assert out["status"] == "complete"
    assert db.photo_tray.docs[0]["ai_model_version"] == pa.DEV_STUB_MODEL


def test_batch_without_key_attempts_nothing(no_ai, photo_env):
    pa, _ptr, db = photo_env
    out = run(pa.batch_analyze_pending(tenant_id=TENANT))
    assert out["attempted"] == 0
    assert out["ai_unavailable"] is True
    assert db.photo_tray.docs[0]["ai_analysis_status"] == "pending"


def test_dedup_never_copies_stub_results(no_ai, photo_env):
    pa, _ptr, db = photo_env
    db.photo_tray.docs.append({
        "id": "old", "tenant_id": TENANT, "phash": "ph1",
        "ai_analysis_status": "complete", "ai_model_version": pa.DEV_STUB_MODEL,
    })
    assert run(pa.find_dedup_source(tenant_id=TENANT, phash="ph1")) is None
    db.photo_tray.docs.append({
        "id": "real", "tenant_id": TENANT, "phash": "ph1",
        "ai_analysis_status": "complete", "ai_model_version": "claude-x",
    })
    assert run(pa.find_dedup_source(tenant_id=TENANT, phash="ph1"))["id"] == "real"


# ---------------------------------------------------------------------
# Photo tray routes
# ---------------------------------------------------------------------
def test_review_route_without_key_is_422_and_saves_nothing(no_ai, photo_env):
    _pa, ptr, db = photo_env
    payload = ptr._PhotoReviewPayload(tray_ids=["tray1"], lens="combined",
                                      daily_report_id="dr1")
    with pytest.raises(AiUnavailableError) as ei:
        run(ptr.photo_review(payload, CTX))
    assert ei.value.status_code == 422
    assert ei.value.detail == PHOTO_AI_UNAVAILABLE_MESSAGE
    assert db.photo_reviews.docs == []


def test_analyze_route_without_key_does_not_reset_row(no_ai, photo_env):
    _pa, ptr, db = photo_env
    db.photo_tray.docs[0]["ai_analysis_status"] = "failed"
    with pytest.raises(AiUnavailableError):
        run(ptr.analyze_single("tray1", None, CTX))
    assert db.photo_tray.docs[0]["ai_analysis_status"] == "failed"
    assert db.photo_tray.writes == 0


def test_batch_route_without_key_is_422(no_ai, photo_env):
    _pa, ptr, _db = photo_env
    payload = ptr._AiBatchPayload()
    with pytest.raises(AiUnavailableError):
        run(ptr.analyze_batch(payload, CTX))


def test_get_analysis_hides_old_stub_result(no_ai, photo_env):
    pa, ptr, db = photo_env
    db.photo_tray.docs[0].update({
        "ai_analysis_status": "complete", "ai_model_version": pa.DEV_STUB_MODEL,
        "ai_suggested_class": "damage", "ai_suggested_event_type": "safety_event",
        "ai_confidence_score": 0.9,
    })
    out = run(ptr.get_ai_analysis("tray1", CTX))
    assert out["ai_analysis_status"] == "failed"
    assert out["ai_failure_reason"] == AI_UNAVAILABLE_CODE
    assert out["ai_suggested_class"] is None
    assert out["ai_suggested_event_type"] is None
    assert out["ai_confidence_score"] is None
    # The saved row itself is left as it is.
    assert db.photo_tray.docs[0]["ai_suggested_class"] == "damage"


def test_get_analysis_pending_without_key_says_unavailable(no_ai, photo_env):
    _pa, ptr, _db = photo_env
    out = run(ptr.get_ai_analysis("tray1", CTX))
    assert out["ai_analysis_status"] == "failed"
    assert out["ai_failure_reason"] == AI_UNAVAILABLE_CODE


def test_get_analysis_real_result_unchanged(no_ai, photo_env):
    _pa, ptr, db = photo_env
    db.photo_tray.docs[0].update({
        "ai_analysis_status": "complete", "ai_model_version": "claude-x",
        "ai_suggested_class": "progress",
    })
    out = run(ptr.get_ai_analysis("tray1", CTX))
    assert out["ai_analysis_status"] == "complete"
    assert out["ai_suggested_class"] == "progress"


def test_saved_stub_reviews_are_hidden(no_ai, photo_env):
    _pa, ptr, db = photo_env
    db.photo_reviews.docs += [
        {"id": "a", "tenant_id": TENANT, "daily_report_id": "dr1",
         "text": "(Dev stub) Foreman would describe the work", "label": "1 safety · 1 QC"},
        {"id": "b", "tenant_id": TENANT, "daily_report_id": "dr1",
         "text": "Real review text", "label": "clear"},
    ]
    out = run(ptr.list_photo_reviews("dr1", CTX))
    assert [d["id"] for d in out] == ["b"]


def test_saved_stub_reviews_shown_in_dev_mode(dev_mode, photo_env):
    _pa, ptr, db = photo_env
    db.photo_reviews.docs.append(
        {"id": "a", "tenant_id": TENANT, "daily_report_id": "dr1",
         "text": "(Dev stub) x"})
    assert len(run(ptr.list_photo_reviews("dr1", CTX))) == 1


# ---------------------------------------------------------------------
# Copilot, voice extraction, client-template export
# ---------------------------------------------------------------------
def test_copilot_without_key_is_422_and_no_turn(no_ai, monkeypatch):
    import routers.system_router as sr
    import services.copilot_tools as ct
    from models import CopilotMessage

    async def _ok(*_a, **_k):
        return None

    async def _boom(**_k):
        raise AssertionError("copilot turn must not run without a key")

    monkeypatch.setattr(sr, "require_project_if_given", _ok)
    monkeypatch.setattr(sr, "check_record_access", _ok)
    monkeypatch.setattr(ct, "execute_copilot_turn", _boom)
    with pytest.raises(AiUnavailableError) as ei:
        run(sr.ai_copilot(CopilotMessage(message="hi"), CTX))
    assert ei.value.detail == COPILOT_UNAVAILABLE_MESSAGE


@pytest.fixture
def voice_env(monkeypatch):
    import routers.dr_voice_router as vr
    import services.project_access as pacc

    db = FakeDB()
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": TENANT,
                                  "project_id": "p1", "status": "draft"})

    async def _ok(*_a, **_k):
        return None

    async def _no_extract(*_a, **_k):
        raise AssertionError("AI extraction must not run without a key")

    async def _no_transcribe(*_a, **_k):
        raise AssertionError("transcription must not run without a key")

    monkeypatch.setattr(vr, "get_db", lambda: db)
    monkeypatch.setattr(pacc, "check_project_access", _ok)
    monkeypatch.setattr(vr, "extract_dr_fields", _no_extract)
    monkeypatch.setattr(vr, "_gemini_transcribe_audio", _no_transcribe)
    return vr, db


def test_voice_extract_without_key_changes_nothing(no_ai, voice_env):
    vr, db = voice_env
    with pytest.raises(AiUnavailableError) as ei:
        run(vr.voice_extract("dr1", {"transcript": "poured slab"}, CTX))
    assert ei.value.detail == VOICE_EXTRACT_UNAVAILABLE_MESSAGE
    assert db.total_writes() == 0


def test_voice_transcribe_and_extract_without_key_stops_first(no_ai, voice_env):
    vr, _db = voice_env

    class _Upload:
        content_type = "audio/webm"

        async def read(self):
            return b"audio-bytes"

    with pytest.raises(AiUnavailableError):
        run(vr.voice_transcribe_and_extract("dr1", _Upload(), CTX))


def test_client_template_export_without_key(no_ai, monkeypatch):
    import routers.daily_reports_router as drr
    import services.storage as storage

    db = FakeDB()
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": TENANT,
                                  "project_id": "p1", "report_date": "2026-11-16"})
    db.projects.docs.append({"id": "p1", "tenant_id": TENANT,
                             "dr_template_file_url": "k", "dr_template_file_name": "t.xlsx"})

    def _no_get(*_a, **_k):
        raise AssertionError("template must not be read without a key")

    monkeypatch.setattr(drr, "get_db", lambda: db)
    monkeypatch.setattr(storage, "get_object", _no_get)
    with pytest.raises(AiUnavailableError) as ei:
        run(drr.export_dr_via_client_template("dr1", CTX))
    assert ei.value.detail == CLIENT_TEMPLATE_UNAVAILABLE_MESSAGE


def test_voice_note_extract_fields_without_key_is_plain(no_ai):
    from services import voice_pipeline
    from services.ai_unavailable import VOICE_NOTE_EXTRACT_UNAVAILABLE_MESSAGE
    out = run(voice_pipeline.extract_fields("some words", "work_item"))
    assert out == {"_error": VOICE_NOTE_EXTRACT_UNAVAILABLE_MESSAGE}
