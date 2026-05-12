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

export default client;
