"""Model routing (search everything, 2026-11-17). No AI calls: the
classifier's chat is replaced by a stub."""
import asyncio
import os

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1")
os.environ.setdefault("DB_NAME", "test_router")

from services import model_router as mr


def _run(c):
    return asyncio.run(c)


def test_structural_signals_go_deep_without_asking():
    called = []

    async def stub(prompt):
        called.append(prompt)
        return "fast"
    assert _run(mr.route("=== New message ===\nhi", daily_report_id="dr1", chat_factory=stub)) == ("deep", "structural")
    assert _run(mr.route("hi", photos=[object()], chat_factory=stub)) == ("deep", "structural")
    assert called == []


def test_classifier_answer_is_used_and_only_new_message_is_sent():
    seen = []

    async def stub(prompt):
        seen.append(prompt)
        return " Deep."
    msg = "=== Prior conversation ===\nUser: SECRET-HISTORY\n\n=== New message ===\nare we good on abatement?"
    assert _run(mr.route(msg, chat_factory=stub)) == ("deep", "classifier")
    assert "SECRET-HISTORY" not in seen[0] and "are we good on abatement?" in seen[0]

    async def fast(prompt):
        return "fast"
    assert _run(mr.route("=== New message ===\nwho was on site 11/12", chat_factory=fast)) == ("fast", "classifier")


def test_failure_or_garbage_falls_back_to_rules():
    async def boom(prompt):
        raise RuntimeError("no key")

    async def junk(prompt):
        return "maybe"

    async def slow(prompt):
        await asyncio.sleep(10)
    mr.CLASSIFY_TIMEOUT_SECONDS = 0.05
    for f in (boom, junk, slow):
        label, how = _run(mr.route("=== New message ===\nlog a delay for the rain", chat_factory=f))
        assert how == "rules" and label == "deep"
        label, how = _run(mr.route("=== New message ===\nwho was on site 11/12", chat_factory=f))
        assert how == "rules" and label == "fast"


def test_empty_message_is_fast():
    async def stub(prompt):
        raise AssertionError("should not be called")
    assert _run(mr.classify("   ", chat_factory=stub)) == "fast"


def test_three_way_intent_maps_to_models():
    for raw, intent, route in (("change", "change", "deep"), (" Lookup.", "lookup", "fast"),
                               ("deep", "deep", "deep"), ("fast", "lookup", "fast")):
        async def stub(prompt, raw=raw):
            return raw
        assert _run(mr.classify_intent("log a delay", chat_factory=stub)) == intent
        assert _run(mr.classify("log a delay", chat_factory=stub)) == route
