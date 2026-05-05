"""WebSocket router — tenant-scoped fanout for notifications + drawing
markups. Master prompt §23 (real-time channel).

Connection URL: /api/ws?token=<jwt>
On connect we issue a {"hello": ...} envelope and stream events from a
Redis-style in-memory broker (one Python process per K8s pod is fine
for the current scale).

Backend code can call:
    from routers.ws_router import broadcast
    await broadcast(tenant_id, {"kind": "notification", "payload": {...}})
"""
from __future__ import annotations
import asyncio
import json
from typing import Dict, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from auth import decode_token

router = APIRouter()

# tenant_id -> set of WebSocket connections
_subs: Dict[str, Set[WebSocket]] = {}
_lock = asyncio.Lock()


async def broadcast(tenant_id: str, payload: dict) -> None:
    """Fan-out to every live WS connection for this tenant. Best-effort."""
    async with _lock:
        conns = list(_subs.get(tenant_id, set()))
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


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(...)):
    try:
        payload = decode_token(token)
        tenant_id = payload.get("tid") or payload.get("tenant_id")
        if not tenant_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    async with _lock:
        _subs.setdefault(tenant_id, set()).add(websocket)
    try:
        await websocket.send_text(json.dumps({"kind": "hello", "tenant_id": tenant_id}))
        while True:
            # Treat any inbound message as ping/keep-alive
            msg = await websocket.receive_text()
            if msg.strip() == "ping":
                await websocket.send_text(json.dumps({"kind": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with _lock:
            _subs.get(tenant_id, set()).discard(websocket)
