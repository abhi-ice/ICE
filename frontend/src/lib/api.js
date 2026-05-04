import axios from "axios";
import { getToken, getActiveTenantId, clearAuth } from "./auth";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

const client = axios.create({
  baseURL: API,
  withCredentials: true, // send cookie session_token for Google flow
});

client.interceptors.request.use((config) => {
  const token = getToken();
  // Skip Bearer header for Google flow placeholder ("google")
  if (token && token !== "google") config.headers.Authorization = `Bearer ${token}`;
  const tenantId = getActiveTenantId();
  if (tenantId) config.headers["X-Tenant-ID"] = tenantId;
  return config;
});

client.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      // Avoid redirect loops on the callback page
      const path = window.location.pathname;
      if (path !== "/login" && path !== "/auth/callback") {
        clearAuth();
        window.location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);

export const asArray = (v) => (Array.isArray(v) ? v : []);

export default client;
