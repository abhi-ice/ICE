// Companies for the Add crew pickers (subcontractors linked to the project,
// or the whole company list when none are linked, plus companies already
// logged on this project's crews), each with its last named crew here so
// the supervisor doesn't retype the same names every day.
import api from "./api";

const cache = new Map(); // projectId -> Promise<companies[]>

export function loadCrewCompanies(projectId, { fresh = false } = {}) {
  if (!projectId) return Promise.resolve([]);
  if (fresh || !cache.has(projectId)) {
    const p = api.get(`/projects/${projectId}/crew-companies`)
      .then((r) => (Array.isArray(r.data?.companies) ? r.data.companies : []))
      .catch(() => { cache.delete(projectId); return []; });
    cache.set(projectId, p);
  }
  return cache.get(projectId);
}

export { companyFor, crewFill } from "./crewFill";
