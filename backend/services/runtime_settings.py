"""UI-managed runtime settings (2026-07-02) -- feature flags + the MS Project
worker token, stored in Mongo and editable from the admin Settings page, so no
deployment ever needs an env-var edit to turn a scheduling feature on or off.

One document: db.runtime_settings, _id "global":

  {
    "_id": "global",
    "flags":  { "FF_SLIDER_GATING": true, ... }   # only keys an admin has touched
    "msp_worker_token": "<random>",               # set/rotated from the UI
    "msp_worker_last_seen": "<iso>",              # stamped on every worker claim
    "msp_worker_id": "worker-hostname",
    "updated_at": "<iso>", "updated_by": "<email>"
  }

Precedence contract (what makes the UI authoritative):
  - A flag key PRESENT in the doc wins over the env var, true or false.
  - A flag key ABSENT falls back to the env var, then to OFF.
  - The worker token: doc value wins if set, else env MS_PROJECT_WORKER_TOKEN.

The flag getters in services.feature_flags are sync and called on hot request
paths, so they read a module-level cache, never Mongo. The cache is refreshed
on startup, immediately after every settings write (same process), and by a
30-second background task (so a multi-process deployment converges quickly).
Unit tests that monkeypatch os.environ still pass untouched: with no doc
loaded the cache is empty and every flag falls through to the env path.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from database import get_db

DOC_ID = "global"
COLLECTION = "runtime_settings"

# The only flags the UI may set -- anything else in a PATCH is rejected.
ALLOWED_FLAGS = (
    "FF_SLIDER_GATING",
    "FF_PROGRESS_CASCADE",
    "FF_ENTERPRISE_CPM",
    "FF_SCHEDULE_MODE_2",
)

_cache: dict = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def refresh_cache() -> None:
    """Re-read the settings doc into the module cache. Cheap (one small doc)."""
    global _cache
    db = get_db()
    doc = await db[COLLECTION].find_one({"_id": DOC_ID})
    _cache = doc or {}


def reset_cache_for_tests() -> None:
    global _cache
    _cache = {}


def flag_override(name: str) -> Optional[bool]:
    """The UI-set value for a flag, or None when an admin has never touched it
    (None => caller falls back to the env var)."""
    flags = _cache.get("flags") or {}
    val = flags.get(name)
    return bool(val) if val is not None else None


def worker_token() -> Optional[str]:
    """The UI-generated MS Project worker token, or None if never generated."""
    tok = _cache.get("msp_worker_token")
    return str(tok) if tok else None


def worker_status() -> dict:
    return {
        "token_set": bool(_cache.get("msp_worker_token")),
        "last_seen": _cache.get("msp_worker_last_seen"),
        "worker_id": _cache.get("msp_worker_id"),
    }


async def set_flags(flags: dict, updated_by: str = "") -> None:
    """Persist admin toggles. Keys must be in ALLOWED_FLAGS; values are coerced
    to bool. Refreshes the cache before returning so the very next request in
    this process sees the new values."""
    updates = {f"flags.{k}": bool(v) for k, v in flags.items() if k in ALLOWED_FLAGS}
    if not updates:
        return
    updates["updated_at"] = _now_iso()
    updates["updated_by"] = updated_by
    db = get_db()
    await db[COLLECTION].update_one({"_id": DOC_ID}, {"$set": updates}, upsert=True)
    await refresh_cache()


async def ensure_flag_on(name: str, updated_by: str = "") -> None:
    """Turn a flag on iff it isn't already effective. Used by the per-project
    engine dropdown so picking an engine is ONE action: the project field and
    the global enable land together. Never turns anything off."""
    from services import feature_flags  # local import: feature_flags imports us

    if name not in ALLOWED_FLAGS:
        return
    if feature_flags._enabled(name):  # already on via UI or env
        return
    await set_flags({name: True}, updated_by=updated_by)


async def rotate_worker_token(updated_by: str = "") -> str:
    """Generate + persist a fresh worker token; returns it exactly once (the
    GET endpoint only ever reports token_set, never the value)."""
    tok = secrets.token_urlsafe(32)
    db = get_db()
    await db[COLLECTION].update_one(
        {"_id": DOC_ID},
        {"$set": {
            "msp_worker_token": tok,
            "msp_worker_token_rotated_at": _now_iso(),
            "updated_at": _now_iso(),
            "updated_by": updated_by,
        }},
        upsert=True,
    )
    await refresh_cache()
    return tok


async def touch_worker(worker_id: str) -> None:
    """Heartbeat: stamped on every worker claim call so the Settings card can
    show 'Worker online / last seen N minutes ago'."""
    global _cache
    now = _now_iso()
    db = get_db()
    await db[COLLECTION].update_one(
        {"_id": DOC_ID},
        {"$set": {"msp_worker_last_seen": now, "msp_worker_id": worker_id}},
        upsert=True,
    )
    # Patch the cache in place -- no full refresh needed for a heartbeat.
    _cache = {**_cache, "msp_worker_last_seen": now, "msp_worker_id": worker_id}
