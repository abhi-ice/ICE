"""Railway move (2026-11-15): the AI, speech-to-text and Stripe code must run
without Emergent's `emergentintegrations` package, using only the owner's
own keys.

These tests:
  * import every AI module while `emergentintegrations` cannot be imported
    (simulated by putting None in sys.modules, which makes `import` fail);
  * check each AI feature reports "enabled" when only ANTHROPIC_API_KEY is
    set (no EMERGENT_LLM_KEY), and actually reaches the Claude client;
  * check the Claude client keeps conversation memory on one object;
  * check speech-to-text uses OPENAI_API_KEY, and fails with a clear 422
    when no key is set;
  * check the Stripe checkout, status and webhook code uses the official
    stripe package, including the webhook signature check.

No network calls: the Anthropic, OpenAI and Stripe clients are replaced by
fakes. No database: the webhook test uses a fake collection.
"""
import asyncio
import hashlib
import hmac
import importlib
import json
import sys
import time

import pytest

# Every module name the old package could be imported under. None in
# sys.modules makes `import x` raise ImportError, as if not installed.
_EMERGENT_NAMES = [
    "emergentintegrations",
    "emergentintegrations.llm",
    "emergentintegrations.llm.chat",
    "emergentintegrations.llm.openai",
    "emergentintegrations.payments",
    "emergentintegrations.payments.stripe",
    "emergentintegrations.payments.stripe.checkout",
]

# The app modules that used to import emergentintegrations.
_APP_MODULES = [
    "services.llm_keys",
    "services.anthropic_compat",
    "services.ai_agents",
    "services.copilot_tools",
    "services.dr_template_export",
    "services.dr_voice_extract",
    "services.photo_ai",
    "services.schedule_ai_review",
    "services.schedule_feedback",
    "services.schedule_work_category",
    "services.voice_pipeline",
    "routers.iter5_router",
    "routers.iter7_router",
    "routers.iter8_router",
    "routers.schedule_diffs_router",
    "routers.schedule_proposals_router",
]

FAKE_ANTHROPIC = "test-anthropic-key-not-real"


@pytest.fixture
def no_emergent(monkeypatch):
    """Block emergentintegrations and import the app modules fresh."""
    for name in _EMERGENT_NAMES:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setenv("MONGO_URL", "mongodb://127.0.0.1:9/never_used")
    monkeypatch.setenv("DB_NAME", "test_no_emergent_never_used")
    saved = {m: sys.modules.pop(m) for m in _APP_MODULES if m in sys.modules}
    mods = {m: importlib.import_module(m) for m in _APP_MODULES}
    yield mods
    # Put the original modules back so other test files are not affected.
    for m in _APP_MODULES:
        sys.modules.pop(m, None)
    sys.modules.update(saved)


@pytest.fixture
def only_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC)
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    monkeypatch.delenv("PHOTO_AI_DEV_MODE", raising=False)


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "EMERGENT_LLM_KEY", "OPENAI_API_KEY",
              "PHOTO_AI_DEV_MODE"):
        monkeypatch.delenv(k, raising=False)


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


def _install_fake_anthropic(monkeypatch, compat, reply):
    """Replace anthropic.AsyncAnthropic inside the compat module. Returns
    the list of (api_key, kwargs) calls made."""
    calls = []

    class _Messages:
        def __init__(self, key):
            self._key = key

        async def create(self, **kwargs):
            calls.append((self._key, kwargs))
            return _FakeResp(reply(kwargs) if callable(reply) else reply)

    class _Client:
        def __init__(self, api_key=None, **_kw):
            self.messages = _Messages(api_key)

    monkeypatch.setattr(compat.anthropic, "AsyncAnthropic", _Client)
    return calls


# ---------------------------------------------------------------- imports
def test_modules_import_without_emergent(no_emergent):
    for name, mod in no_emergent.items():
        assert mod is not None, name
    # No app module should have pulled the old package in.
    for name in _EMERGENT_NAMES:
        assert sys.modules.get(name) is None


def test_requirements_drop_emergent_and_pin_replacements():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    req = (root / "requirements.txt").read_text()
    lines = [l.strip().lower() for l in req.splitlines()]
    # The package stays listed for Emergent, but the Railway image filters it out.
    dockerfile = (root / "Dockerfile").read_text()
    assert "grep -viE '^emergentintegrations'" in dockerfile
    for pkg in ("anthropic", "openai", "stripe", "boto3"):
        assert any(l.startswith(pkg + "==") for l in lines), pkg


# ---------------------------------------------------------------- keys
def test_llm_keys_order(no_emergent, monkeypatch):
    k = no_emergent["services.llm_keys"]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    assert k.llm_api_key() is None and k.has_llm_key() is False
    monkeypatch.setenv("EMERGENT_LLM_KEY", "emergent-test")
    assert k.llm_api_key() == "emergent-test" and k.has_llm_key()
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC)
    assert k.llm_api_key() == FAKE_ANTHROPIC
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert k.llm_api_key() == "emergent-test"


def test_key_helpers_use_anthropic_key(no_emergent, only_anthropic_key):
    m = no_emergent
    assert m["services.ai_agents"]._key() == FAKE_ANTHROPIC
    assert m["services.dr_template_export"]._key() == FAKE_ANTHROPIC
    assert m["services.voice_pipeline"]._llm_key() == FAKE_ANTHROPIC
    assert m["services.photo_ai"]._anthropic_api_key() == FAKE_ANTHROPIC


def test_photo_ai_enabled_with_only_anthropic_key(no_emergent, only_anthropic_key):
    photo = no_emergent["services.photo_ai"]
    assert photo._GATEWAY_SDK_OK is True
    assert photo._is_dev_mode() is False


def test_photo_ai_unavailable_without_keys(no_emergent, no_keys):
    # 2026-11-16: a missing key no longer means dev mode (made-up results
    # reached users); it means AI is unavailable. Only PHOTO_AI_DEV_MODE=1
    # turns the dev stub on.
    photo = no_emergent["services.photo_ai"]
    assert photo._is_dev_mode() is False
    assert photo.ai_available() is False
    assert photo.photo_ai_can_run() is False


# ---------------------------------------------------------------- Claude client
def test_compat_direct_call_and_memory(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    calls = _install_fake_anthropic(monkeypatch, compat, "first reply")
    chat = compat.LlmChat(api_key="ignored", session_id="s", system_message="sys") \
        .with_model("anthropic", "claude-sonnet-4-5-20250929")
    img = compat.ImageContent(image_base64="iVBORw0KGgo=")
    out = asyncio.run(chat.send_message(compat.UserMessage(text="hi", file_contents=[img])))
    assert out == "first reply"
    key, kwargs = calls[0]
    assert key == FAKE_ANTHROPIC
    assert kwargs["system"] == "sys"
    blocks = kwargs["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1] == {"type": "text", "text": "hi"}

    # A second message on the same object carries the earlier turn.
    asyncio.run(chat.send_message(compat.UserMessage(text="follow up")))
    msgs = calls[1][1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "first reply"

    # A new object starts empty.
    chat2 = compat.LlmChat(system_message="sys")
    asyncio.run(chat2.send_message(compat.UserMessage(text="fresh")))
    assert len(calls[2][1]["messages"]) == 1


def test_compat_without_any_key_raises_auth_error(no_emergent, no_keys):
    compat = no_emergent["services.anthropic_compat"]
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(compat.LlmChat().send_message(compat.UserMessage(text="x")))
    assert "401" in str(ei.value)


def test_compat_emergent_key_without_package_raises(no_emergent, no_keys, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    monkeypatch.setenv("EMERGENT_LLM_KEY", "emergent-test")
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(compat.LlmChat(api_key="emergent-test").send_message(compat.UserMessage(text="x")))
    assert "401" in str(ei.value)


def test_compat_hands_off_to_emergent_when_only_emergent_key(no_emergent, no_keys, monkeypatch):
    """Running on Emergent without an Anthropic key: the call goes to the
    Emergent package (faked here), so that setup keeps working."""
    compat = no_emergent["services.anthropic_compat"]
    sent = []

    class _FakeEmergentChat:
        def __init__(self, api_key, session_id, system_message):
            self.api_key = api_key

        def with_model(self, provider, model):
            return self

        async def send_message(self, msg):
            sent.append((self.api_key, msg))
            return "from emergent"

    class _FakeMod:
        LlmChat = _FakeEmergentChat

        class UserMessage:
            def __init__(self, text, file_contents=None):
                self.text, self.file_contents = text, file_contents

        class ImageContent:
            def __init__(self, image_base64):
                self.image_base64 = image_base64

    monkeypatch.setenv("EMERGENT_LLM_KEY", "emergent-test")
    monkeypatch.setattr(compat, "_emergent_chat_module", lambda: _FakeMod)
    out = asyncio.run(compat.LlmChat().send_message(compat.UserMessage(text="x")))
    assert out == "from emergent"
    assert sent[0][0] == "emergent-test"


# ---------------------------------------------------------------- features reach Claude
def test_schedule_ai_review_uses_claude(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    calls = _install_fake_anthropic(monkeypatch, compat, json.dumps({
        "risk_level": "low", "summary": "ok", "flags": [], "recommendation": "approve",
    }))
    out = asyncio.run(no_emergent["services.schedule_ai_review"].review_schedule_diff({"summary": {}}))
    assert calls and out.get("model")
    assert out["risk_level"] == "low"


def test_schedule_feedback_ai_uses_claude(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    calls = _install_fake_anthropic(monkeypatch, compat, '{"ok": true}')
    out = asyncio.run(no_emergent["services.schedule_feedback"]._ask_ai("sys", "text"))
    assert calls and out == {"ok": True}


def test_schedule_work_category_uses_claude(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    calls = _install_fake_anthropic(monkeypatch, compat, "{}")
    swc = no_emergent["services.schedule_work_category"]
    tag_map, label, err = asyncio.run(swc._classify_via_llm([], "p", ""))
    # The reply is empty, but the call was made (not skipped for "no key").
    assert calls
    assert "no API key" not in (err or "")


def test_ai_agents_chat_uses_claude(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    calls = _install_fake_anthropic(monkeypatch, compat, "hello")
    chat = no_emergent["services.ai_agents"]._new_chat("s", "sys")
    assert asyncio.run(chat.send_message(compat.UserMessage(text="q"))) == "hello"
    assert calls[0][0] == FAKE_ANTHROPIC


def test_dr_template_export_chat_uses_claude(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    calls = _install_fake_anthropic(monkeypatch, compat, "{}")
    chat = no_emergent["services.dr_template_export"]._new_chat("s", "sys")
    asyncio.run(chat.send_message(compat.UserMessage(text="q")))
    assert calls[0][0] == FAKE_ANTHROPIC


def test_photo_ai_vision_call_reaches_claude(no_emergent, only_anthropic_key, monkeypatch):
    compat = no_emergent["services.anthropic_compat"]
    photo = no_emergent["services.photo_ai"]
    calls = _install_fake_anthropic(monkeypatch, compat, '{"tags": []}')
    try:
        asyncio.run(photo._call_anthropic_vision(
            jpeg_bytes=b"\xff\xd8\xff\xe0fakejpeg", media_type="image/jpeg"))
    except Exception:
        # The fake reply may not satisfy the photo schema; all we check is
        # that the call got past the key and import checks to Claude.
        pass
    assert calls, "photo AI did not reach the Claude client"
    blocks = calls[0][1]["messages"][0]["content"]
    assert blocks[0]["type"] == "image"


# ---------------------------------------------------------------- speech-to-text
def test_voice_transcribe_without_key_is_clear_422(no_emergent, no_keys):
    from fastapi import HTTPException
    vp = no_emergent["services.voice_pipeline"]
    with pytest.raises(HTTPException) as ei:
        asyncio.run(vp.transcribe_audio(b"abc", ext="webm"))
    assert ei.value.status_code == 422
    assert "OPENAI_API_KEY" in str(ei.value.detail)


def test_voice_transcribe_uses_openai(no_emergent, no_keys, monkeypatch):
    import openai
    seen = {}

    class _Tr:
        async def create(self, **kwargs):
            seen.update(kwargs)

            class R:
                text = "pour complete on level 3"
            return R()

    class _Audio:
        transcriptions = _Tr()

    class _Client:
        def __init__(self, api_key=None, **_kw):
            seen["api_key"] = api_key
            self.audio = _Audio()

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-not-real")
    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
    vp = no_emergent["services.voice_pipeline"]
    out = asyncio.run(vp.transcribe_audio(b"abc", ext="m4a"))
    assert out["text"] == "pour complete on level 3"
    assert seen["model"] == "whisper-1"
    assert seen["api_key"] == "test-openai-key-not-real"
    assert seen["file"][0].endswith(".m4a")


# ---------------------------------------------------------------- Stripe
def test_stripe_checkout_helpers(no_emergent, monkeypatch):
    import stripe
    it7 = no_emergent["routers.iter7_router"]
    made = {}

    def fake_create(**kwargs):
        made.update(kwargs)
        return {"id": "cs_test_123", "url": "https://checkout.example/cs_test_123"}

    def fake_retrieve(sid, api_key=None):
        return {"id": sid, "status": "complete", "payment_status": "paid"}

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(fake_create))
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", staticmethod(fake_retrieve))
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_not_real")
    assert it7._emergent_stripe_classes() is None

    sid, url = asyncio.run(it7._stripe_create_checkout(
        "sk_test_not_real", amount_usd=1234.56, currency="usd",
        success_url="https://a/s", cancel_url="https://a/c",
        metadata={"kind": "escrow_fund"}, product_name="Bid",
    ))
    assert (sid, url) == ("cs_test_123", "https://checkout.example/cs_test_123")
    assert made["line_items"][0]["price_data"]["unit_amount"] == 123456
    assert made["mode"] == "payment" and made["metadata"] == {"kind": "escrow_fund"}
    assert asyncio.run(it7._stripe_get_status("sk_test_not_real", "cs_test_123")) == ("complete", "paid")


class _FakeCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, flt, upd):
        self.updates.append((flt, upd))


class _FakeDb:
    def __init__(self):
        self.payment_transactions = _FakeCollection()


class _FakeRequest:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}
        self.base_url = "http://testserver/"

    async def body(self):
        return self._body


def _session_event():
    return json.dumps({
        "id": "evt_1", "object": "event", "type": "checkout.session.completed",
        "data": {"object": {"object": "checkout.session", "id": "cs_test_9",
                            "payment_status": "paid"}},
    }).encode()


def test_stripe_webhook_unsigned_when_no_secret(no_emergent, monkeypatch):
    it7 = no_emergent["routers.iter7_router"]
    db = _FakeDb()
    monkeypatch.setattr(it7, "get_db", lambda: db)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_not_real")
    # Unsigned events are refused unless explicitly allowed (local dev).
    monkeypatch.delenv("STRIPE_WEBHOOK_ALLOW_UNSIGNED", raising=False)
    refused = asyncio.run(it7.stripe_webhook(_FakeRequest(_session_event())))
    assert refused.status_code == 400 and not db.payment_transactions.updates
    monkeypatch.setenv("STRIPE_WEBHOOK_ALLOW_UNSIGNED", "1")
    out = asyncio.run(it7.stripe_webhook(_FakeRequest(_session_event())))
    assert out == {"ok": True}
    flt, upd = db.payment_transactions.updates[0]
    assert flt == {"session_id": "cs_test_9"}
    assert upd["$set"] == {"payment_status": "paid",
                           "webhook_event_type": "checkout.session.completed",
                           "webhook_event_id": "evt_1"}


def test_stripe_webhook_checks_signature(no_emergent, monkeypatch):
    it7 = no_emergent["routers.iter7_router"]
    db = _FakeDb()
    monkeypatch.setattr(it7, "get_db", lambda: db)
    secret = "whsec_test_not_real"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_not_real")
    body = _session_event()

    bad = asyncio.run(it7.stripe_webhook(_FakeRequest(body, {"Stripe-Signature": "t=1,v1=bad"})))
    assert bad.status_code == 400 and db.payment_transactions.updates == []

    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    good = asyncio.run(it7.stripe_webhook(_FakeRequest(body, {"Stripe-Signature": f"t={ts},v1={sig}"})))
    assert good == {"ok": True}
    assert db.payment_transactions.updates[0][0] == {"session_id": "cs_test_9"}
