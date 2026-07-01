import { useEffect, useRef, useState } from "react";
import api from "../lib/api";
import { Plus, Pencil, Trash2 } from "lucide-react";
import { Button } from "../components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import FieldLabel from "../components/forms/FieldLabel";
// CPM Part G (2026-05-15) -- planned_completion_date rendered
// MM/DD/YYYY via the lib/dateUtil chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";

// Followup-fix 6 (2026-05-07): TIA header inputs added to project form
// so TIA Section 1 (Project Header) renders real values instead of nulls.
const EMPTY_FORM = {
  name: "",
  contract_number: "",
  client_name: "",
  // Feature DR Header Auto-Population (2026-05-20) -- station + GC
  // surfaced in every Daily Report header. Both optional.
  station_id: "",
  contractor_name: "",
  contract_value_usd: "",
  address_line1: "",
  city: "",
  state: "",
  postal_code: "",
  agency: "",
  ntp_date: "",
  original_substantial_completion: "",
  current_substantial_completion: "",
  contract_days: "",
  calendar_basis: "calendar_days",
  // 2026-02-XX (Abhi) -- subs / suppliers linked to this project,
  // selected from the tenant-wide vendor central repo.
  subcontractor_ids: [],
  supplier_ids: [],
};

// pt.44 (2026-05-27) — rebuilt vendor picker.
// Before: every vendor rendered as a chip showing "Name · trade1,
// trade2, trade3" — with 12+ vendors this turned into a messy wall
// of text in the project create modal. Now: standard multi-select
// pattern (Notion tags / Linear labels / GitHub assignees):
//   - Selected items render as small chips up top, names only, "×" to remove
//   - Searchable dropdown below to add more; dropdown shows name as
//     primary text + trades as muted secondary line (useful when
//     picking but doesn't clutter the form)
//   - Empty-state link to Vendors page is preserved
function VendorPicker({ kind, vendors, selectedIds, onToggle }) {
  const isSub = kind === "sub";
  const title = isSub ? "Subcontractors on this project" : "Suppliers on this project";
  const singular = isSub ? "subcontractor" : "supplier";
  const empty = isSub
    ? "No subcontractors in the repo yet."
    : "No suppliers in the repo yet.";

  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef(null);

  // Close dropdown on outside click.
  useEffect(() => {
    if (!open) return undefined;
    const onClickAway = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickAway);
    return () => document.removeEventListener("mousedown", onClickAway);
  }, [open]);

  const selectedSet = new Set(selectedIds || []);
  const selectedVendors = vendors.filter((v) => selectedSet.has(v.id));
  const q = search.trim().toLowerCase();
  const available = vendors.filter((v) => {
    if (selectedSet.has(v.id)) return false;
    if (!q) return true;
    return v.name.toLowerCase().includes(q)
      || (v.trades || []).some((t) => String(t).toLowerCase().includes(q));
  });

  return (
    <div className="border-t border-border pt-3 mt-3">
      <div className="flex items-center justify-between mb-2">
        <label className="field-label">{title}</label>
        <a href="/vendors" target="_blank" rel="noreferrer"
           className="text-xs text-emerald-600 hover:underline">
          Manage repo ↗
        </a>
      </div>

      {vendors.length === 0 ? (
        <div className="text-xs text-muted-foreground rounded border border-dashed border-border px-3 py-2">
          {empty}{" "}
          <a href="/vendors" target="_blank" rel="noreferrer"
             className="text-emerald-600 hover:underline">
            Add some →
          </a>
        </div>
      ) : (
        <div ref={wrapperRef} className="relative">
          {/* Selected chips (names only, × to remove) */}
          {selectedVendors.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-2">
              {selectedVendors.map((v) => (
                <button
                  type="button"
                  key={v.id}
                  data-testid={`np-${kind}-chip-${v.id}`}
                  onClick={() => onToggle(v.id)}
                  title={v.trades?.length ? `Trades: ${v.trades.join(", ")}` : "Remove"}
                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-emerald-600/15 border border-emerald-600/40 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-600/25 transition max-md:min-h-[44px] max-md:px-3"
                >
                  <span>{v.name}</span>
                  <span className="opacity-60 hover:opacity-100">×</span>
                </button>
              ))}
            </div>
          )}

          {/* Dropdown trigger — opens a searchable list of unselected vendors */}
          <button
            type="button"
            data-testid={`np-${kind}-add-trigger`}
            onClick={() => setOpen((o) => !o)}
            className="w-full text-left text-xs px-3 py-1.5 rounded border border-border bg-background hover:bg-muted text-muted-foreground hover:text-foreground transition inline-flex items-center justify-between"
          >
            <span>
              {selectedVendors.length === 0
                ? `Add ${singular}…`
                : `Add another ${singular}…`}
            </span>
            <span className="text-[10px] max-md:text-xs opacity-60">
              {available.length} available
            </span>
          </button>

          {open && (
            <div className="absolute left-0 right-0 mt-1 z-20 rounded-md border border-border bg-popover shadow-lg overflow-hidden">
              <input
                type="text"
                autoFocus
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={`Search ${singular}s…`}
                data-testid={`np-${kind}-search`}
                className="w-full px-3 py-2 text-xs border-b border-border bg-transparent focus:outline-none"
              />
              <div className="max-h-56 overflow-y-auto">
                {available.length === 0 ? (
                  <div className="text-xs text-muted-foreground px-3 py-3 text-center">
                    {q ? `No matches for "${search}".` : "All vendors already added."}
                  </div>
                ) : (
                  available.map((v) => (
                    <button
                      type="button"
                      key={v.id}
                      data-testid={`np-${kind}-option-${v.id}`}
                      onClick={() => { onToggle(v.id); setSearch(""); }}
                      className="w-full text-left px-3 py-1.5 hover:bg-muted transition"
                    >
                      <div className="text-xs font-medium text-foreground">{v.name}</div>
                      {v.trades?.length > 0 && (
                        <div className="text-[10px] max-md:text-xs text-muted-foreground truncate">
                          {v.trades.join(", ")}
                        </div>
                      )}
                    </button>
                  ))
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function Projects() {
  const [projects, setProjects] = useState([]);
  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  // Bucket D Fix E -- double-click guard.
  const [busy, setBusy] = useState(false);
  // 2026-02-XX (Abhi) -- subs + suppliers from central repo, loaded
  // once and reused across new + edit dialogs.
  const [subs, setSubs] = useState([]);
  const [suppliers, setSuppliers] = useState([]);
  useEffect(() => {
    api.get("/subcontractors").then(({ data }) => setSubs(data || [])).catch(() => {});
    api.get("/suppliers").then(({ data }) => setSuppliers(data || [])).catch(() => {});
  }, []);

  const toggleVendor = (kind, vid) => {
    const key = kind === "sub" ? "subcontractor_ids" : "supplier_ids";
    setForm((f) => {
      const cur = new Set(f[key] || []);
      if (cur.has(vid)) cur.delete(vid); else cur.add(vid);
      return { ...f, [key]: Array.from(cur) };
    });
  };

  const load = () => api.get("/projects").then(({ data }) => setProjects(data));
  useEffect(() => { load(); }, []);

  const removeProject = async (p) => {
    const confirmation = window.prompt(
      `This will PERMANENTLY delete the project "${p.name}" along with EVERY daily report, delay, RFI, photo, task, and audit record scoped to it. This cannot be undone.\n\nType the project name exactly to confirm:`,
      "",
    );
    if (confirmation == null) return;
    if (confirmation.trim() !== p.name.trim()) {
      toast.error("Confirmation text did not match — delete cancelled.");
      return;
    }
    try {
      await api.delete(`/projects/${p.id}`);
      toast.success(`Project "${p.name}" deleted.`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  const userRole = (() => {
    try { return (JSON.parse(localStorage.getItem("ice.user") || "null")?.role || "").toLowerCase(); }
    catch { return ""; }
  })();
  const isAdmin = userRole === "admin";

  const openCreate = () => {
    setEditId(null);
    setForm(EMPTY_FORM);
    setOpen(true);
  };

  const openEdit = (p) => {
    setEditId(p.id);
    setForm({
      name: p.name || "",
      contract_number: p.contract_number || "",
      client_name: p.client_name || "",
      // Feature DR Header Auto-Population (2026-05-20).
      station_id: p.station_id || "",
      contractor_name: p.contractor_name || "",
      contract_value_usd: p.contract_value_usd != null ? String(p.contract_value_usd) : "",
      address_line1: p.address_line1 || "",
      city: p.city || "",
      state: p.state || "",
      postal_code: p.postal_code || "",
      agency: p.agency || "",
      ntp_date: p.ntp_date || "",
      original_substantial_completion: p.original_substantial_completion || "",
      current_substantial_completion: p.current_substantial_completion || "",
      contract_days: p.contract_days != null ? String(p.contract_days) : "",
      calendar_basis: p.calendar_basis || "calendar_days",
      subcontractor_ids: p.subcontractor_ids || [],
      supplier_ids: p.supplier_ids || [],
    });
    setOpen(true);
  };

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const payload = { ...form,
        contract_value_usd: form.contract_value_usd ? Number(form.contract_value_usd) : null,
        contract_days: form.contract_days ? Number(form.contract_days) : null,
      };
      // Preserve subcontractor_ids / supplier_ids even when empty so
      // a user un-selecting everything actually clears the linkage.
      const ARRAY_KEEP = new Set(["subcontractor_ids", "supplier_ids"]);
      Object.keys(payload).forEach((k) => {
        if (ARRAY_KEEP.has(k)) return;
        if (payload[k] === "" || payload[k] === null) delete payload[k];
      });
      if (editId) {
        await api.patch(`/projects/${editId}`, payload);
        toast.success("Project updated");
      } else {
        await api.post("/projects", payload);
        toast.success("Project created");
      }
      setOpen(false);
      setEditId(null);
      setForm(EMPTY_FORM);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || "Failed to save");
    } finally { setBusy(false); }
  };

  return (
    <div data-testid="projects-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Projects</h1>
          <p className="text-sm text-muted-foreground">All federal contracts in this tenant.</p>
        </div>
        <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) { setEditId(null); setForm(EMPTY_FORM); } }}>
          <DialogTrigger asChild>
            {/* pt.40: page-header tier = sbtn-md (32px), no Plus icon. */}
            <button data-testid="new-project-button" onClick={openCreate} className="sbtn sbtn-md sbtn-primary">
              New project
            </button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>{editId ? "Edit project" : "New project"}</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div>
                <FieldLabel required className="field-label">Project name</FieldLabel>
                <input data-testid="np-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              {/* Feature DR Header Auto-Population (2026-05-20) -- the
                  four contract / client fields are surfaced (read-only)
                  in every Daily Report header. All optional. */}
              <div className="pt-1 text-xs font-semibold text-slate-500">
                Project / Contract Information
              </div>
              <div>
                <FieldLabel optional className="field-label">Contract number</FieldLabel>
                <input data-testid="np-contract" value={form.contract_number} onChange={(e) => setForm({ ...form, contract_number: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <FieldLabel optional className="field-label">Client</FieldLabel>
                <input data-testid="np-client" placeholder="e.g. VA Medical Center, Bronx NY" value={form.client_name} onChange={(e) => setForm({ ...form, client_name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
                <div>
                  <FieldLabel optional className="field-label">Station</FieldLabel>
                  <input data-testid="np-station" placeholder="e.g. 526-25-100" value={form.station_id} onChange={(e) => setForm({ ...form, station_id: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
                <div>
                  <FieldLabel optional className="field-label">Contractor</FieldLabel>
                  <input data-testid="np-contractor" placeholder="e.g. Integrated Construction Enterprises, Inc." value={form.contractor_name} onChange={(e) => setForm({ ...form, contractor_name: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
              </div>
              <div>
                <FieldLabel optional className="field-label">Contract value (USD)</FieldLabel>
                <input data-testid="np-value" type="number" value={form.contract_value_usd} onChange={(e) => setForm({ ...form, contract_value_usd: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <FieldLabel optional className="field-label">Address</FieldLabel>
                <input data-testid="np-address" placeholder="Street address" value={form.address_line1} onChange={(e) => setForm({ ...form, address_line1: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div>
                <FieldLabel optional className="field-label">City</FieldLabel>
                <input data-testid="np-city" value={form.city} onChange={(e) => setForm({ ...form, city: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
              </div>
              <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
                <div>
                  <FieldLabel optional className="field-label">State</FieldLabel>
                  <input data-testid="np-state" placeholder="e.g. NY or New York" value={form.state} onChange={(e) => setForm({ ...form, state: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
                <div>
                  <FieldLabel optional className="field-label">Postal code</FieldLabel>
                  <input data-testid="np-postal" value={form.postal_code} onChange={(e) => setForm({ ...form, postal_code: e.target.value })} className="w-full h-9 px-3 rounded border border-input bg-background" />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">City and state are required for the daily-report weather auto-fill.</p>

              {/* 2026-02-XX (Abhi) -- subs / suppliers from central
                  repo. Picking these on the project makes them
                  available in the DR Manpower log Company autocomplete
                  AND in the voice AI's trade→company matching for
                  this project specifically. */}
              <VendorPicker
                kind="sub"
                vendors={subs}
                selectedIds={form.subcontractor_ids}
                onToggle={(id) => toggleVendor("sub", id)}
              />
              <VendorPicker
                kind="supplier"
                vendors={suppliers}
                selectedIds={form.supplier_ids}
                onToggle={(id) => toggleVendor("supplier", id)}
              />

              {/* Followup-fix 6 (2026-05-07): TIA header fields. Optional --
                  TIA Section 1 falls back to nulls until populated. */}
              <details className="border-t border-border pt-3 mt-3">
                <summary className="cursor-pointer text-sm font-medium text-slate-700">
                  TIA header (agency, NTP, contract days, calendar basis)
                </summary>
                <div className="mt-3 space-y-3">
                  <div>
                    <FieldLabel optional className="field-label">Agency</FieldLabel>
                    <input data-testid="np-agency" placeholder="e.g. GSA, NYCHA, USMS"
                      value={form.agency} onChange={(e) => setForm({ ...form, agency: e.target.value })}
                      className="w-full h-9 px-3 rounded border border-input bg-background" />
                  </div>
                  <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-3 gap-3">
                    <div>
                      <FieldLabel optional className="field-label">NTP date</FieldLabel>
                      <input data-testid="np-ntp" type="date"
                        value={form.ntp_date} onChange={(e) => setForm({ ...form, ntp_date: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                    <div>
                      <FieldLabel optional className="field-label">Contract days</FieldLabel>
                      <input data-testid="np-contract-days" type="number" min="0"
                        value={form.contract_days} onChange={(e) => setForm({ ...form, contract_days: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                    <div>
                      <FieldLabel optional className="field-label">Calendar basis</FieldLabel>
                      <select data-testid="np-calendar-basis"
                        value={form.calendar_basis} onChange={(e) => setForm({ ...form, calendar_basis: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background">
                        <option value="calendar_days">calendar days</option>
                        <option value="working_days">working days</option>
                      </select>
                    </div>
                  </div>
                  <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
                    <div>
                      <FieldLabel optional className="field-label">Original substantial completion</FieldLabel>
                      <input data-testid="np-osc" type="date"
                        value={form.original_substantial_completion}
                        onChange={(e) => setForm({ ...form, original_substantial_completion: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                    <div>
                      <FieldLabel optional className="field-label">Current substantial completion</FieldLabel>
                      <input data-testid="np-csc" type="date"
                        value={form.current_substantial_completion}
                        onChange={(e) => setForm({ ...form, current_substantial_completion: e.target.value })}
                        className="w-full h-9 px-3 rounded border border-input bg-background" />
                    </div>
                  </div>
                </div>
              </details>

              {/* pt.40: modal primary action -> sbtn-md (32px). */}
              <button data-testid="np-submit" onClick={submit} disabled={!form.name || busy} className="sbtn sbtn-md sbtn-primary">{busy ? "Saving…" : (editId ? "Save" : "Create")}</button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        {/* pt.37: dense-table-bordered → header-only vertical
            dividers, small-column table per Option C. */}
        {/* crud-list-table-wrap: on phones this 8-col table reflows into compact
            cards (Name = headline, the rest inline meta, Location hidden) so it
            fits the frame instead of h-scrolling. Inert at >=768px -> desktop
            table byte-identical. */}
        <div className="crud-list-table-wrap">
        <table className="w-full dense-table dense-table-bordered">
          <thead>
            <tr><th>Name</th><th>Contract #</th><th>Client</th><th>Location</th><th>Status</th><th>Value</th><th>Plan finish</th><th></th></tr>
          </thead>
          <tbody>
            {projects.map((p) => {
              const loc = [p.city, p.state].filter(Boolean).join(", ");
              return (
                <tr key={p.id} data-testid={`project-row-${p.id}`}>
                  <td className="font-medium col-primary" data-label="Name">{p.name}</td>
                  <td className="font-mono text-xs" data-label="Contract #">{p.contract_number || "—"}</td>
                  <td data-label="Client">{p.client_name || "—"}</td>
                  <td className="text-xs col-hide-m" data-label="Location">{loc || <span className="text-muted-foreground">—</span>}</td>
                  <td data-label="Status"><span className={`pill ${p.status === "active" ? "pill-green" : "pill-slate"}`}>{p.status}</span></td>
                  <td className="tabular-nums" data-label="Value">{p.contract_value_usd ? `$${(p.contract_value_usd / 1_000_000).toFixed(2)}M` : "—"}</td>
                  <td className="font-mono text-xs" data-label="Plan finish">{formatDateDisplay(p.planned_completion_date)}</td>
                  <td className="text-right pr-3 col-fullwidth">
                    <button
                      data-testid={`edit-project-${p.id}`}
                      onClick={() => openEdit(p)}
                      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground mr-3 max-md:min-h-[44px] max-md:px-3"
                    >
                      <Pencil className="h-3 w-3" /> Edit
                    </button>
                    {isAdmin && (
                      <button
                        data-testid={`delete-project-${p.id}`}
                        onClick={() => removeProject(p)}
                        title="Permanently delete this project (admin only)"
                        className="inline-flex items-center gap-1 text-xs text-rose-500 hover:text-rose-400 max-md:min-h-[44px] max-md:px-3"
                      >
                        <Trash2 className="h-3 w-3" /> Delete
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {!projects.length && <tr><td colSpan="8" className="text-center text-sm text-muted-foreground py-6">No projects yet.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  );
}
