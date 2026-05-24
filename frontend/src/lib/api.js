import axios from "axios";
import { getToken, getActiveTenantId, clearAuth } from "./auth";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

// Bearer-JWT only. The Google-OAuth cookie path was removed in the
// 2026-05-11 purge (federal-contractor policy).
const client = axios.create({
  baseURL: API,
});

client.interceptors.request.use((config) => {
  const token = getToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  const tenantId = getActiveTenantId();
  if (tenantId) config.headers["X-Tenant-ID"] = tenantId;
  return config;
});

client.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      // Avoid redirect loops on the login page itself.
      const path = window.location.pathname;
      if (path !== "/login") {
        clearAuth();
        window.location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);

export const asArray = (v) => (Array.isArray(v) ? v : []);

// 5-Route DR Entry (2026-05-25) — install the offline request queue
// so PATCH/POST mutations against /daily-reports/* and friends survive
// connection loss. Drains automatically on the next 'online' event.
import { installOfflineInterceptor } from "./drOfflineSync";
installOfflineInterceptor(client);

// Bug #25 fix (2026-05-13) -- shared API error message extractor.
//
// Backend returns structured error envelopes in several shapes
// depending on the framework + handler:
//   - FastAPI default validation errors: {"detail": [{"loc":..., "msg":...}, ...]}
//   - FastAPI HTTPException raises:     {"detail": "<string>"}
//   - Custom structured errors:         {"detail": {"error": "...", "message": "..."}}
//   - Some legacy endpoints:            {"message": "..."}  or  {"error": "..."}
//   - Network failure (no response):    axios populates err.message only
//
// extractApiError walks this priority list and returns the most
// specific user-facing message it can find. Pages can call this
// directly: `toast.error(extractApiError(e))` -- one-liner replacement
// for the existing `e.response?.data?.detail || "Failed"` pattern that
// dropped structured fields and bare-fell-back to "Failed" too eagerly.
//
// Page-level adoption is incremental; existing inline error handlers
// continue to work unchanged.
export function extractApiError(err, fallback = "Failed") {
  if (!err) return fallback;
  const r = err.response;
  // No response object at all -> network / timeout / CORS
  if (!r) {
    return err.message ? `${fallback}: ${err.message}` : fallback;
  }
  const data = r.data;
  // FastAPI default detail (array of {loc, msg, type})
  if (Array.isArray(data?.detail)) {
    const lines = data.detail
      .map((d) => {
        const field = Array.isArray(d?.loc) && d.loc.length
          ? d.loc[d.loc.length - 1]
          : null;
        const msg = d?.msg || d?.message || String(d);
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (lines.length) return `${fallback} (${r.status}): ${lines.join("; ").slice(0, 240)}`;
  }
  // FastAPI HTTPException-style detail string
  if (typeof data?.detail === "string" && data.detail.trim()) {
    return `${fallback} (${r.status}): ${data.detail}`;
  }
  // Structured detail object with .message / .error / .reason
  if (data?.detail && typeof data.detail === "object") {
    const m = data.detail.message
      || data.detail.error
      || data.detail.reason;
    if (m) return `${fallback} (${r.status}): ${m}`;
  }
  // Legacy / non-FastAPI shapes
  if (typeof data?.message === "string" && data.message.trim()) {
    return `${fallback} (${r.status}): ${data.message}`;
  }
  if (typeof data?.error === "string" && data.error.trim()) {
    return `${fallback} (${r.status}): ${data.error}`;
  }
  // Truly empty body / unknown shape -> include status so support
  // can grep server logs by HTTP code.
  return `${fallback} (${r.status})`;
}

export default client;
