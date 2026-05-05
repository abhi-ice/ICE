// Active project context (shared across pages via localStorage + a simple
// hook). Picked once per session in the top bar.
import { useEffect, useState, useCallback } from "react";
import api from "./api";

const KEY = "ice.active_project_id";

export function getActiveProjectId() {
  return localStorage.getItem(KEY);
}
export function setActiveProjectId(id) {
  localStorage.setItem(KEY, id || "");
  window.dispatchEvent(new Event("ice:active_project_changed"));
}

export function useProjects() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState(getActiveProjectId());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/projects");
      setProjects(data);
      // Reconcile localStorage against the live list. If the stored id no
      // longer matches any project (e.g., switched tenant, or that project was
      // deleted), auto-select the first available so dependent pages don't
      // silently no-op (e.g., schedule import with no active project).
      const stored = getActiveProjectId();
      const match = data.find((p) => p.id === stored);
      if (match) {
        if (active !== match.id) setActive(match.id);
      } else if (data.length) {
        setActiveProjectId(data[0].id);
        setActive(data[0].id);
      } else {
        setActiveProjectId("");
        setActive(null);
      }
    } finally {
      setLoading(false);
    }
  }, [active]);

  useEffect(() => {
    load();
    const handler = () => setActive(getActiveProjectId());
    window.addEventListener("ice:active_project_changed", handler);
    return () => window.removeEventListener("ice:active_project_changed", handler);
  }, [load]);

  return { projects, loading, activeId: active, setActiveId: setActiveProjectId, reload: load };
}
