"""Write-freeze switch for the move to a new server.

While the switch is on, every change request under /api (POST, PUT, PATCH,
DELETE) is answered with 503 and a short message instead of being saved.
Phones treat any 5xx as "try again later", so their daily reports, photos
and voice notes stay queued on the device and are sent to the new server
once users switch over. Nothing lands on the old server after the final
data copy, so nothing is lost.

Still allowed while frozen:
  - every read (GET, HEAD, OPTIONS) and the WebSocket,
  - signing in (POST /api/auth/login) -- it only reads the user record,
  - the admin settings save (PATCH /api/admin/runtime-settings), so an
    admin can turn the switch back off.

How the switch is read (same rule as the other feature flags):
  1. the value an admin set in Settings, stored in db.runtime_settings
     ({_id: "global", flags: {FF_WRITE_FREEZE: true}}), wins when present;
  2. otherwise the FF_WRITE_FREEZE environment variable ("1", "true",
     "yes", "on" mean on);
  3. otherwise off.
The settings document is re-read at most every few seconds while write
requests are arriving, so a change made directly in the database (or by
another server process) takes effect within seconds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from services import runtime_settings

log = logging.getLogger(__name__)

FLAG_NAME = "FF_WRITE_FREEZE"
FREEZE_MESSAGE = (
    "ICE is moving to a new server. Your changes are saved on this device "
    "and will send automatically."
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Voice-PIN sign-in (POST /api/auth/voice-pin/exchange) is blocked on
# purpose: it records the PIN as used, and that write would be lost after
# the final copy, letting the PIN be reused on the new server.
# (method, path) pairs that keep working while frozen. Paths are compared
# without a trailing slash.
EXEMPT = frozenset({
    ("POST", "/api/auth/login"),
    ("PATCH", "/api/admin/runtime-settings"),
})

_TRUTHY = {"1", "true", "yes", "on"}
REFRESH_SECONDS = 5.0
REFRESH_TIMEOUT_SECONDS = 1.0
_last_refresh = 0.0


def write_freeze_enabled() -> bool:
    """True when the switch is on (admin setting first, then env var)."""
    override = runtime_settings.flag_override(FLAG_NAME)
    if override is not None:
        return override
    return (os.environ.get(FLAG_NAME) or "").strip().lower() in _TRUTHY


def is_exempt(method: str, path: str) -> bool:
    norm = path.rstrip("/") or "/"
    return (method.upper(), norm) in EXEMPT


def is_blocked_request(method: str, path: str) -> bool:
    """Would this request be refused while frozen? (Ignores the switch.)"""
    if method.upper() not in WRITE_METHODS:
        return False
    if not (path == "/api" or path.startswith("/api/")):
        return False
    return not is_exempt(method, path)


async def _refresh_if_stale() -> None:
    """Re-read the settings document if the copy in memory is older than
    REFRESH_SECONDS. Never raises and never waits more than about a second;
    on failure the last known value is used."""
    global _last_refresh
    now = time.monotonic()
    if now - _last_refresh < REFRESH_SECONDS:
        return
    _last_refresh = now
    try:
        await asyncio.wait_for(runtime_settings.refresh_cache(), timeout=REFRESH_TIMEOUT_SECONDS)
    except Exception as exc:  # cold database, timeout -- keep the last value
        log.debug("write-freeze settings refresh skipped: %s", type(exc).__name__)


def reset_for_tests() -> None:
    global _last_refresh
    _last_refresh = 0.0


_BODY = json.dumps({"detail": FREEZE_MESSAGE}).encode("utf-8")


class WriteFreezeMiddleware:
    """Plain ASGI middleware (works for every route, including routers that
    declare their own /api prefix, and leaves WebSockets alone)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method") or ""
        path = scope.get("path") or ""
        if not is_blocked_request(method, path):
            await self.app(scope, receive, send)
            return
        await _refresh_if_stale()
        if not write_freeze_enabled():
            await self.app(scope, receive, send)
            return
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_BODY)).encode("ascii")),
                (b"retry-after", b"120"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": _BODY})
