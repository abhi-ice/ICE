"""WebSocket router — tenant-scoped fanout for notifications + drawing
markups. Master prompt §23 (real-time channel).

Connection URL: /api/ws?token=<jwt>
On connect we issue a {"hello": ...} envelope and stream events from a
Redis-style in-memory broker (one Python process per K8s pod is fine
for the current scale).

Backend code can call:
    from routers.ws_router import broadcast
    await broadcast(tenant_id, {"kind": "notification", "payload": {...}})

2026-11 security round:
  * A connection is only accepted for a signed-in user the REST API would
    also accept: the token is valid and not retired (token_version), the
    user still exists, the token's company still exists, and the user
    still belongs to it (an active membership, or it is their home
    company). Otherwise the socket is closed with 1008.
  * A notification addressed to one user (payload.user_id set) is sent
    only to that user's sockets. Company-wide notifications (no user_id)
    and every other message kind still go to the whole company.

2026-11-16 per-project access:
  * A message about one project (a "project_id" on the message itself or
    on its payload) is sent only to sockets whose user may see that
    project, when the FF_PROJECT_ACCESS switch is on. Messages with no
    project go to everyone in the company as before. With the switch off
    nothing changes.
  * Each socket keeps the set of projects its user may see, worked out
    when it opens and again at each minute-ly re-check, so an assignment
    change reaches open sockets within about a minute.
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Dict, Optional, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from auth import decode_token, _resolve_via_jwt
from database import get_db

router = APIRouter()

# tenant_id -> set of WebSocket connections
_subs: Dict[str, Set[WebSocket]] = {}
# WebSocket connection -> the user id it was opened for
_owners: Dict[WebSocket, str] = {}
# WebSocket connection -> the project ids its user may see. None = every
# project in the company (all-projects admin). Missing = not worked out
# (treated as "every project" so a lookup problem never hides messages
# while the switch is off; with the switch on it is filled at connect).
_allowed: Dict[WebSocket, Optional[frozenset]] = {}
# Sockets of admins limited to specific projects: filtered even with the
# per-project switch off (same rule as the REST API).
_limited: set = set()
_lock = asyncio.Lock()
# How often an open socket re-checks that its user may still be signed in.
RECHECK_SECONDS = 60


def _target_user(payload: dict) -> Optional[str]:
    """The single recipient of a message, or None when it is for everyone
    in the company. Only notifications carry a recipient today."""
    if not isinstance(payload, dict) or payload.get("kind") != "notification":
        return None
    inner = payload.get("payload")
    if isinstance(inner, dict) and inner.get("user_id"):
        return str(inner["user_id"])
    return None


def _message_project(payload: dict) -> Optional[str]:
    """The project a message is about, or None for a company-wide message.
    Read from the message itself, then from its payload."""
    if not isinstance(payload, dict):
        return None
    pid = payload.get("project_id")
    if not pid:
        inner = payload.get("payload")
        if isinstance(inner, dict):
            pid = inner.get("project_id")
    return pid if isinstance(pid, str) and pid else None


def _project_filter_on() -> bool:
    try:
        from services.project_access import is_enforced
        return is_enforced()
    except Exception:
        return False


def _may_see(ws: WebSocket, project_id: Optional[str]) -> bool:
    """True when this socket's user may see a message about project_id.

    Only called with a project_id while per-project access is on. A socket
    whose project list is not known yet (opened while the switch was off, or
    the lookup failed) gets no project messages until the minute-ly
    re-check fills it in: a missed live update is reloaded on the next page
    view, a leaked one cannot be taken back."""
    if not project_id:
        return True
    if ws not in _allowed:
        return False
    allowed = _allowed.get(ws)
    return allowed is None or project_id in allowed


async def _access_for(token: str, tenant_id: str) -> tuple:
    """(project ids this sign-in may see in tenant_id (None = all),
    always-enforced?). Same rules as the REST API
    (auth.build_request_context + services.project_access): an admin limited
    to specific projects is held to them even while the switch is off.
    Raises on failure; the caller keeps what it had."""
    from auth import build_request_context
    from services.project_access import allowed_project_ids, is_limited_admin

    async def _work():
        ctx = await build_request_context(token, tenant_id)
        if ctx.get("tenant_id") != tenant_id:
            return frozenset(), True
        return await allowed_project_ids(ctx), is_limited_admin(ctx)
    # A slow database must not hold up the socket.
    return await asyncio.wait_for(_work(), timeout=5)


async def _allowed_for(token: str, tenant_id: str) -> Optional[frozenset]:
    """The project ids this sign-in may see in tenant_id (None = all)."""
    return (await _access_for(token, tenant_id))[0]


async def broadcast(tenant_id: str, payload: dict) -> None:
    """Fan-out to every live WS connection for this tenant (or only the
    recipient's connections for a user-addressed notification). A message
    about one project only reaches sockets allowed to see it while
    per-project access is on. Best-effort."""
    target = _target_user(payload)
    project_id = _message_project(payload)
    switch_on = _project_filter_on()
    async with _lock:
        conns = [
            ws for ws in _subs.get(tenant_id, set())
            if (target is None or _owners.get(ws) == target)
            and (not project_id
                 or not (switch_on or ws in _limited)
                 or _may_see(ws, project_id))
        ]
    if not conns:
        return
    msg = json.dumps(payload, default=str)
    dead: list[WebSocket] = []
    for ws in conns:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    if dead:
        async with _lock:
            for ws in dead:
                _subs.get(tenant_id, set()).discard(ws)
                _owners.pop(ws, None)
                _allowed.pop(ws, None)


async def _authorize(token: str, on_db_error=None) -> Optional[tuple[str, str]]:
    """Return (tenant_id, user_id) when this token may open a socket, else
    None. Mirrors the REST sign-in checks (auth._resolve_via_jwt).
    `on_db_error` is returned if the database lookups themselves fail, so
    a re-check on an open socket does not drop it over a database hiccup."""
    try:
        claims = decode_token(token)
    except Exception:
        return None
    tenant_id = claims.get("tid") or claims.get("tenant_id")
    if not tenant_id:
        return None
    try:
        user = await _resolve_via_jwt(token)
        if not user:
            return None  # user deleted / removed, or token retired
        db = get_db()
        tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0, "id": 1})
        if not tenant:
            return None  # company closed
        # Same rule as REST: the home company is always allowed (older
        # accounts may have no membership row); any other company needs an
        # active membership.
        if user.get("tenant_id") != tenant_id:
            membership = await db.tenant_users.find_one(
                {"user_id": user["id"], "tenant_id": tenant_id, "status": "active"},
                {"_id": 0, "id": 1},
            )
            if not membership:
                return None
    except Exception:
        return on_db_error
    return tenant_id, user["id"]


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(...)):
    who = await _authorize(token)
    if not who:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    tenant_id, user_id = who
    # Projects this user may see (per-project access; only worked out while
    # the switch is on). Unset counts as "every project".
    # Worked out for every socket: an admin limited to specific projects is
    # filtered even while the switch is off.
    allowed, has_allowed, limited = None, False, False
    try:
        allowed, limited = await _access_for(token, tenant_id)
        has_allowed = True
    except Exception:
        pass

    await websocket.accept()
    async with _lock:
        _subs.setdefault(tenant_id, set()).add(websocket)
        _owners[websocket] = user_id
        if has_allowed:
            _allowed[websocket] = allowed
        if limited:
            _limited.add(websocket)
    try:
        await websocket.send_text(json.dumps({"kind": "hello", "tenant_id": tenant_id}))
        last_check = time.monotonic()
        while True:
            # Treat any inbound message as ping/keep-alive. Wait at most
            # RECHECK_SECONDS so the check below also runs for a client that
            # never sends anything (it would otherwise keep receiving the
            # company's messages after being removed).
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=RECHECK_SECONDS)
            except asyncio.TimeoutError:
                msg = ""
            # At most once a minute, re-run the sign-in checks so a removed
            # member, a password reset or a closed company also ends a
            # socket that is already open.
            if time.monotonic() - last_check >= RECHECK_SECONDS:
                last_check = time.monotonic()
                if await _authorize(token, on_db_error=who) != who:
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    break
                # Refresh the projects this user may see (assignments may
                # have changed). On a lookup problem keep the old set.
                try:
                    fresh, lim = await _access_for(token, tenant_id)
                    async with _lock:
                        _allowed[websocket] = fresh
                        if lim:
                            _limited.add(websocket)
                        else:
                            _limited.discard(websocket)
                except Exception:
                    pass
            if msg.strip() == "ping":
                await websocket.send_text(json.dumps({"kind": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with _lock:
            _subs.get(tenant_id, set()).discard(websocket)
            _owners.pop(websocket, None)
            _allowed.pop(websocket, None)
            _limited.discard(websocket)
