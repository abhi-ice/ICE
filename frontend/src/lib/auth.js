// Lightweight auth state — token + tenant_id in localStorage.
// SUBSTITUTION: master prompt §5.3 specified Clerk middleware. We use
// JWT (HS256) issued by FastAPI; tenant context overridable via X-Tenant-ID.

const TOKEN_KEY = "ice.token";
const USER_KEY = "ice.user";
const TENANT_KEY = "ice.tenant";
const ACTIVE_TENANT_KEY = "ice.active_tenant_id";
const ACTIVE_PROJECT_KEY = "ice.active_project_id";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function getUser() {
  const v = localStorage.getItem(USER_KEY);
  return v ? JSON.parse(v) : null;
}

export function getTenant() {
  const v = localStorage.getItem(TENANT_KEY);
  return v ? JSON.parse(v) : null;
}

export function getActiveTenantId() {
  return localStorage.getItem(ACTIVE_TENANT_KEY) || (getTenant()?.id || null);
}

export function setActiveTenantId(id) {
  localStorage.setItem(ACTIVE_TENANT_KEY, id);
}

// Owner stamp for the offline save queues (lib/drOfflineSync and the photo,
// voice and note queues): the signed-in user plus the tenant the app is
// working in. A phone can be shared, so each queued save remembers who made
// it and only replays when that same person is signed in again. Never
// throws: with no user (or a broken stored record) it returns null and the
// save is still kept, as an entry anyone signed in may sync.
export function getQueueOwner() {
  try {
    const userId = getUser()?.id;
    if (!userId) return null;
    return { userId: String(userId), tenantId: getActiveTenantId() || null };
  } catch {
    return null;
  }
}

// Fired after every sign-in so lib/api.js can sync the new user's queued
// saves right away instead of waiting for the next heartbeat.
function notifyAuthChanged() {
  try { window.dispatchEvent(new Event("ice-auth-changed")); }
  catch { /* non-window env */ }
}

export function setAuth({ access_token, user, tenant }) {
  // Tenant changed → drop any stale active project from a previous session.
  const prevTenant = getTenant();
  if (!prevTenant || prevTenant.id !== tenant.id) {
    localStorage.removeItem(ACTIVE_PROJECT_KEY);
  }
  localStorage.setItem(TOKEN_KEY, access_token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
  localStorage.setItem(TENANT_KEY, JSON.stringify(tenant));
  localStorage.setItem(ACTIVE_TENANT_KEY, tenant.id);
  notifyAuthChanged();
}

// Sign-out leaves the offline save queues alone ON PURPOSE: they hold field
// records that have not reached the server yet. Each entry carries its
// owner, so the next person to sign in on this device cannot replay them;
// they sync when their owner signs in again.
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(TENANT_KEY);
  localStorage.removeItem(ACTIVE_TENANT_KEY);
  localStorage.removeItem(ACTIVE_PROJECT_KEY);
}

export function isAuthenticated() {
  return !!getToken();
}
