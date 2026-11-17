"""Which collections search indexes, reads on demand, or leaves alone.

The chunker modules (services.search.chunkers.*) each export ENTRIES, a
list of `Entry`. This module adds the system collections below and builds
one dict, COLLECTIONS, keyed by collection name. It is built lazily by
get_registry() so importing this module never imports every chunker.

Tests can swap in their own registry with set_registry(...).
"""
from __future__ import annotations

import importlib
import logging
from typing import Optional

from services.search.store import SEARCH_CHUNKS, SEARCH_QUEUE, SEARCH_STATE
from services.search.types import Entry

logger = logging.getLogger(__name__)

CHUNKER_MODULES = (
    "services.search.chunkers.field",
    "services.search.chunkers.schedule",
    "services.search.chunkers.issues",
    "services.search.chunkers.docs_company",
)


def _x(name: str, reason: str) -> Entry:
    return Entry(collection=name, decision="exclude", reason=reason)


def _q(name: str, reason: str) -> Entry:
    return Entry(collection=name, decision="query_only", reason=reason)


# System collections. These win over anything a chunker module says.
SYSTEM_ENTRIES: list[Entry] = [
    _x("audit_log", "Holds full copies of records and old secrets. Never indexed and "
                    "never offered by search; full admins keep reading it through the "
                    "assistant's query tool, which has its own admin-only check."),
    _x("runtime_settings", "Holds settings and the MS Project worker token."),
    _x("team_invites", "Holds invite codes that let someone join a company."),
    _x("copilot_messages", "The assistant's own past answers, private to each user."),
    _x("backup_runs", "Backup bookkeeping, no project content."),
    _x("cron_runs", "Scheduled job bookkeeping, no project content."),
    _x("digest_runs", "Email digest bookkeeping, no project content."),
    _x("migrations", "Database upgrade bookkeeping, no project content."),
    _x("ai_call_timings", "AI timing measurements, no project content."),
    _x("ai_usage_tracking", "AI usage counts (search writes here too), no project content."),
    _x("photo_ai_locks", "Short-lived locks for photo analysis."),
    _x("photo_ai_calibration", "Photo analysis tuning numbers, no searchable text."),
    _x("imports", "Import job bookkeeping; the imported records are indexed themselves."),
    _x("field_suggestions", "Suggested values waiting for review, not project records."),
    _x("activity_name_suggestions", "Suggested activity names waiting for review, not project records."),
    _x("audit_logs", "Older name for the audit trail; same reasons as audit_log."),
    _x("audit_chain_heads", "Audit trail bookkeeping (hash chain), no project content."),
    _x("auth_attempts", "Sign-in attempts: security records, never searchable."),
    _x("payment_transactions", "Payment records: never sent to an outside AI service."),
    _x("project_access_watch", "Access-check bookkeeping, no project content."),
    _x("notifications", "Alerts about other records, which are indexed themselves; "
                        "each alert is personal to one user."),
    _x("voice_pins", "Voice notes: left out until speech-to-text ships."),
    _x("drawings", "Drawings: left out of this version (planned for the next one)."),
    _x("drawing_revisions", "Drawings: left out of this version (planned for the next one)."),
    _x("drawing_markups", "Drawing markups: left out of this version; personal markups "
                          "will need the private flag when added."),
    _x("drawings_spatial_metadata", "Drawings: left out of this version (planned for the next one)."),
    _x("marketplace_postings", "Marketplace listings shared outside the company; not project records."),
    _x("marketplace_responses", "Marketplace replies shared outside the company; not project records."),
    _x(SEARCH_CHUNKS, "The search index itself."),
    _x(SEARCH_QUEUE, "The search work queue."),
    _x(SEARCH_STATE, "Search bookkeeping."),
]

_registry: Optional[dict[str, Entry]] = None
_override: Optional[dict[str, Entry]] = None


def _entries_from(module_name: str) -> list[Entry]:
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        logger.error("search registry: could not import %s: %s", module_name, exc)
        return []
    entries = getattr(mod, "ENTRIES", None) or []
    if isinstance(entries, dict):
        entries = list(entries.values())
    return [e for e in entries if isinstance(e, Entry)]


def build_registry() -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for module_name in CHUNKER_MODULES:
        for e in _entries_from(module_name):
            if e.collection in out:
                logger.warning("search registry: %s is listed twice; keeping the first",
                               e.collection)
                continue
            out[e.collection] = e
    for e in SYSTEM_ENTRIES:
        prev = out.get(e.collection)
        if prev is not None and prev.decision == "index" and e.decision != "index":
            logger.warning("search registry: %s is a system collection; using %s, not %s",
                           e.collection, e.decision, prev.decision)
        out[e.collection] = e
    return out


def get_registry() -> dict[str, Entry]:
    global _registry
    if _override is not None:
        return _override
    if _registry is None:
        _registry = build_registry()
    return _registry


def set_registry(entries: Optional[dict[str, Entry]]) -> None:
    """Tests only: use this registry instead of the real one (None = reset)."""
    global _override
    _override = entries


def __getattr__(name: str):
    # `registry.COLLECTIONS` works too, built on first use.
    if name == "COLLECTIONS":
        return get_registry()
    raise AttributeError(name)


def INDEXED_COLLECTIONS() -> list[str]:
    """Collections the listener watches and the reconcile pages through."""
    return sorted(name for name, e in get_registry().items()
                  if e.decision == "index" and e.chunker is not None)
