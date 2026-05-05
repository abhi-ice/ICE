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
}

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
