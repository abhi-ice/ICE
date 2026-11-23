// Pure helpers (no network) for filling a new crew from the company's last
// crew on this project. Kept separate so they can be unit tested.

const norm = (s) => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();

export function companyFor(companies, name) {
  const key = norm(name);
  if (!key) return null;
  return (companies || []).find((c) => norm(c.name) === key) || null;
}

const sameList = (a, b) => Array.isArray(a) && Array.isArray(b)
  && a.length === b.length && a.every((x, i) => String(x || "") === String(b[i] || ""));

// What to fill when a company is picked or typed. `current` holds the
// form's current names and the names it last filled (`filled`). Names the
// supervisor typed himself are never replaced; names we filled earlier are
// replaced when he switches company.
export function crewFill(companies, name, current = {}) {
  const c = companyFor(companies, name);
  const names = current.names || [];
  const typed = names.some((x) => String(x || "").trim()) && !sameList(names, current.filled);
  if (typed) return null;
  const last = c && c.last_crew;
  if (!last || !(last.worker_names || []).length) {
    // Switching away from a company we filled: clear our names again.
    return current.filled && current.filled.length ? { names: [], filled: [], trade: c?.trade || null } : (c ? { trade: c.trade || null } : null);
  }
  const list = [...last.worker_names];
  return {
    names: list,
    filled: list,
    headcount: Math.max(Number(last.headcount) || 0, list.length) || list.length,
    hours: Number(last.hours_per_person) || null,
    trade: last.trade || c.trade || null,
    fromDate: last.report_date || "",
  };
}
