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


async def broadcast(tenant_id: str, payload: dict) -> None:
    """Fan-out to every live WS connection for this tenant (or only the
    recipient's connections for a user-addressed notification). Best-effort."""
    target = _target_user(payload)
    async with _lock:
        conns = [
            ws for ws in _subs.get(tenant_id, set())
            if target is None or _owners.get(ws) == target
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

    await websocket.accept()
    async with _lock:
        _subs.setdefault(tenant_id, set()).add(websocket)
        _owners[websocket] = user_id
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
