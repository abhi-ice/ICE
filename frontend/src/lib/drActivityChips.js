// Bucket D Phase 3 + Corrective (2026-05-08) -- shared chip helpers.
//
// useDrActivityChips (legacy): resolves daily_report_activities ids
// into chips. Still used by the in-DR grid view; gallery pages have
// migrated to useTaskChips below since event collections now store
// master schedule task ids in linked_task_ids (the per-DR scope was
// dropped by the corrective).
//
// useTaskChips (current): resolves master schedule task ids into the
// same {id, activity_code, activity_description} chip shape, by
// fetching /projects/{pid}/tasks once and indexing by id. The chip
// links into /tasks/{id} since these reference master tasks, not DRs.
import { useEffect, useState } from "react";
import api from "./api";

export function useDrActivityChips(projectId, activityIds) {
  const [chips, setChips] = useState({});
  // Stable join key so the effect doesn't re-fire on identical arrays.
  const key = (activityIds || []).slice().sort().join(",");
  useEffect(() => {
    if (!projectId || !key) { setChips({}); return; }
    api.get(`/projects/${projectId}/dr-activity-chips`, {
      params: { ids: key },
    }).then(({ data }) => {
      const map = {};
      for (const c of (data || [])) map[c.id] = c;
      setChips(map);
    }).catch(() => setChips({}));
  }, [projectId, key]);
  return chips;
}

// Bucket D Corrective -- master-schedule task chips. Same shape as
// useDrActivityChips so GalleryActivityChips can render either with
// minor cosmetic differences. We fetch the project's task list once
// per project via /projects/{pid}/tasks and cache it on the hook
// instance; gallery pages share the result via a small in-module
// cache so the page-load round-trip count stays at 1 even if multiple
// gallery columns render chips.
const _taskCache = new Map();   // projectId -> {ts, list}
const _CACHE_TTL_MS = 30_000;

export function useTaskChips(projectId, taskIds) {
  const [chips, setChips] = useState({});
  // Re-render trigger when the cached fetch lands.
  const [, setTick] = useState(0);
  const idsKey = (taskIds || []).slice().sort().join(",");

  useEffect(() => {
    if (!projectId || !idsKey) { setChips({}); return; }
    const cached = _taskCache.get(projectId);
    const fresh = cached && (Date.now() - cached.ts) < _CACHE_TTL_MS;
    const buildMap = (list) => {
      const map = {};
      for (const t of (list || [])) {
        if (!t?.id) continue;
        map[t.id] = {
          id: t.id,
          activity_code: t.activity_code,
          activity_description: t.description,
          // tasks don't carry a dr_date; chip renderer falls back to
          // the activity_code-only label.
          is_critical_path: !!t.is_critical_path,
        };
      }
      return map;
    };
    if (fresh) {
      setChips(buildMap(cached.list));
      return;
    }
    api.get(`/projects/${projectId}/tasks`)
      .then(({ data }) => {
        const list = Array.isArray(data) ? data : [];
        _taskCache.set(projectId, { ts: Date.now(), list });
        setChips(buildMap(list));
        setTick((t) => t + 1);
      })
      .catch(() => setChips({}));
  }, [projectId, idsKey]);
  return chips;
}

// Pure render helper for an activity chip badge.
export function chipLabel(c) {
  if (!c) return null;
  return `${c.activity_code || c.id.slice(0, 6)}`;
}

export function chipTooltip(c) {
  if (!c) return null;
  const date = c.dr_date ? `[${c.dr_date}] ` : "";
  return `${date}${c.activity_code || ""} ${c.activity_description || ""}`.trim();
}
