// Bucket D Fix H -- shared URL-state hook for gallery filters.
//
// Pattern: each gallery page calls useUrlState(initial) and gets back
// [state, setState] just like useState, BUT state is mirrored to the
// URL query string. Refresh, deep-link, or browser-Back all restore
// state. Filter changes use replaceState (not push) so Back doesn't
// trip across every keystroke.
//
// Initial values: object of {paramName: defaultValue}. Boolean values
// serialize as "1"/"0"; arrays serialize as comma-joined strings;
// everything else serializes via String().
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export function useUrlState(initial) {
  const [params, setParams] = useSearchParams();

  const state = useMemo(() => {
    const out = {};
    for (const [k, def] of Object.entries(initial || {})) {
      const raw = params.get(k);
      if (raw === null) {
        out[k] = def;
        continue;
      }
      if (typeof def === "boolean") {
        out[k] = raw === "1" || raw === "true";
      } else if (Array.isArray(def)) {
        out[k] = raw ? raw.split(",").filter(Boolean) : [];
      } else if (typeof def === "number") {
        const n = Number(raw);
        out[k] = Number.isFinite(n) ? n : def;
      } else {
        out[k] = raw;
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  const setState = useCallback((updater) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      const cur = {};
      for (const [k, def] of Object.entries(initial || {})) {
        const raw = prev.get(k);
        if (raw === null) cur[k] = def;
        else if (typeof def === "boolean") cur[k] = raw === "1" || raw === "true";
        else if (Array.isArray(def)) cur[k] = raw ? raw.split(",").filter(Boolean) : [];
        else if (typeof def === "number") {
          const n = Number(raw); cur[k] = Number.isFinite(n) ? n : def;
        } else cur[k] = raw;
      }
      const merged = typeof updater === "function" ? updater(cur) : { ...cur, ...updater };
      for (const [k, def] of Object.entries(initial || {})) {
        const v = merged[k];
        const isDefault = (typeof def === "boolean" ? v === def
          : Array.isArray(def) ? (Array.isArray(v) && v.length === 0)
          : v === def || v === "" || v === null || v === undefined);
        if (isDefault) {
          next.delete(k);
        } else if (typeof def === "boolean") {
          next.set(k, v ? "1" : "0");
        } else if (Array.isArray(def)) {
          next.set(k, v.join(","));
        } else {
          next.set(k, String(v));
        }
      }
      return next;
    }, { replace: true });
  }, [setParams, initial]);

  return [state, setState];
}
