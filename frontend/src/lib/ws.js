// useWebSocket — tenant-scoped event channel. Master prompt §23.
import { useEffect, useRef } from "react";
import { getToken } from "./auth";

const WS_URL = (() => {
  // Use the build-time site address; if it is missing, use the address the
  // page was loaded from, so older phones still get a full ws(s):// address.
  let base = (process.env.REACT_APP_BACKEND_URL || "").replace(/\/+$/, "");
  if (!base && typeof window !== "undefined" && window.location) {
    base = window.location.origin;
  }
  const wsBase = base.replace(/^http/, "ws");
  return `${wsBase}/api/ws`;
})();

export function useWebSocket(onEvent) {
  const wsRef = useRef(null);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    const token = getToken();
    if (!token) return undefined;
    let alive = true;
    const ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);
    wsRef.current = ws;
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        handlerRef.current && handlerRef.current(msg);
      } catch {}
    };
    const heart = setInterval(() => {
      if (alive && ws.readyState === 1) ws.send("ping");
    }, 30000);
    return () => {
      alive = false;
      clearInterval(heart);
      try { ws.close(); } catch {}
    };
  }, []);
}
