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

// Read-through GET cache (2026-07-05, v7 full-offline DR layer).
//
// On a successful GET to a DR-critical endpoint we mirror the JSON body into
// IndexedDB (best-effort, non-blocking — online behavior is unchanged). On a
// GET that fails with NO response (offline / network error) we resolve with
// the cached copy tagged `fromCache: true` so the DR page can render stale
// data and show an "offline" state instead of erroring.
//
// Ordering note: axios chains response interceptors in REGISTRATION order,
// so on a network failure the chain is 401-handler -> this cache-handler ->
// mutation-handler (installOfflineInterceptor, below). A cacheable GET that
// fails offline is RECOVERED here (we resolve with cached data) before the
// mutation-handler sees it; the mutation-handler only queues non-GET
// (PATCH/POST/DELETE) requests and re-rejects GETs, so the two never fight.
// We only serve JSON (skip blob/arraybuffer responses we can't cheaply store).
import { isCacheableGet, putCached, getCached } from "./offlineCache";

// Cache key: tenant-scoped, params-inclusive.
//  - axios keeps query params in cfg.params (NOT in cfg.url), so two calls
//    that differ only by params must not collide on one key. axios.getUri
//    serializes url+params exactly as the request went out.
//  - Prefix with the active tenant id so a shared device can never serve
//    tenant A's cached rows to a user who switched to tenant B offline.
function cacheKeyFor(cfg) {
  let uri = cfg.url || "";
  try {
    if (cfg.params) uri = axios.getUri({ url: cfg.url, params: cfg.params });
  } catch { /* fall back to the bare url */ }
  return `${getActiveTenantId() || "anon"}|${uri}`;
}

// Offline DR create (2026-07-06) — plant/read GET-cache entries under the
// SAME tenant-scoped key convention the read-through interceptor uses, so a
// provisionally-created report renders offline exactly like a cached one.
export function primeOfflineGet(url, data) {
  try { putCached(cacheKeyFor({ url }), data, 200); } catch { /* best-effort */ }
}

export async function readCachedGet(url) {
  try {
    const row = await getCached(cacheKeyFor({ url }));
    return row ? row.data : null;
  } catch { return null; }
}

client.interceptors.response.use(
  (resp) => {
    try {
      const cfg = resp.config || {};
      const rt = cfg.responseType;
      const isJsonish = !rt || rt === "json" || rt === "text";
      if (isJsonish && !resp.fromCache && isCacheableGet(cfg.method, cfg.url || "")) {
        // Fire-and-forget; never blocks or throws into the resolve path.
        putCached(cacheKeyFor(cfg), resp.data, resp.status);
      }
    } catch { /* best-effort only */ }
    return resp;
  },
  async (err) => {
    const cfg = err.config;
    // Only intercept true network failures (no response) on cacheable GETs.
    // Deliberate cancellations (AbortController / unmount) are NOT failures —
    // serving cached data to a caller that gave up invites stale setState.
    const canceled = err?.code === "ERR_CANCELED" || err?.name === "CanceledError";
    if (cfg && !err.response && !canceled) {
      const rt = cfg.responseType;
      const isJsonish = !rt || rt === "json" || rt === "text";
      if (isJsonish && isCacheableGet(cfg.method, cfg.url || "")) {
        const cached = await getCached(cacheKeyFor(cfg));
        if (cached) {
          return {
            data: cached.data,
            status: cached.status || 200,
            statusText: "OK (offline cache)",
            headers: { "x-from-cache": "1" },
            config: cfg,
            fromCache: true,
            cachedAt: cached.cachedAt || null,
          };
        }
      }
    }
    return Promise.reject(err);
  },
);

export const asArray = (v) => (Array.isArray(v) ? v : []);

// 5-Route DR Entry (2026-05-25) — install the offline request queue
// so PATCH/POST mutations against /daily-reports/* and friends survive
// connection loss. Drains automatically on the next 'online' event.
import { installOfflineInterceptor } from "./drOfflineSync";
installOfflineInterceptor(client);

// v7 full-offline (2026-07-05) — drain the offline PHOTO and VOICE queues on
// reconnect, using this same authenticated client. queuePhoto/queueVoiceNote
// are called by the guided flow; each drainQueue re-uploads via the existing
// endpoints (both are mutex-guarded, so a page-level drain can't race these).
import { wireAutoDrain as wirePhotoDrain } from "./drPhotoQueue";
import { wireAutoDrain as wireVoiceDrain } from "./drVoiceQueue";
wirePhotoDrain(client);
wireVoiceDrain(client);

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
