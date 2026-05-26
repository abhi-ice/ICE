/**
 * Vendors page (2026-02-XX).
 * Central repo for Subcontractors + Suppliers. Tenant-wide records
 * usable across all projects in the org. Used by:
 *   - Project create/edit forms (multi-select)
 *   - Daily Report → Manpower log "Company" autocomplete
 *   - DR Voice intake AI context
 *
 * Two tabs, identical CRUD shape backed by `/api/subcontractors` and
 * `/api/suppliers`.
 */
import { useEffect, useState } from "react";
import { Plus, Trash2, Pencil, Briefcase, Truck } from "lucide-react";
import { toast } from "../components/ui/sonner";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../components/ui/tabs";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import api from "../lib/api";

const BLANK = {
  name: "", trades: [], contact_name: "", contact_email: "",
  contact_phone: "", notes: "",
};

function VendorTable({ kind }) {
  const path = kind === "subcontractor" ? "/subcontractors" : "/suppliers";
  const [rows, setRows] = useState([]);
  const [editing, setEditing] = useState(null); // null | { ...vendor } | "new"
  const [form, setForm] = useState(BLANK);

  const load = () => api.get(path).then(({ data }) => setRows(data || []));
  useEffect(() => { load(); }, [path]);

  const openNew = () => { setEditing("new"); setForm(BLANK); };
  const openEdit = (v) => {
    setEditing(v);
    setForm({
      name: v.name || "",
      trades: v.trades || [],
      contact_name: v.contact_name || "",
      contact_email: v.contact_email || "",
      contact_phone: v.contact_phone || "",
      notes: v.notes || "",
    });
  };
  const close = () => { setEditing(null); setForm(BLANK); };

  const save = async () => {
    if (!form.name.trim()) { toast.error("Name is required."); return; }
    const payload = {
      ...form,
      trades: typeof form.trades === "string"
        ? form.trades.split(",").map((s) => s.trim()).filter(Boolean)
        : (form.trades || []),
    };
    try {
      if (editing === "new") {
        await api.post(path, payload);
        toast.success(`${kind === "subcontractor" ? "Subcontractor" : "Supplier"} added.`);
      } else {
        await api.patch(`${path}/${editing.id}`, payload);
        toast.success("Saved.");
      }
      close();
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    }
  };

  const remove = async (v) => {
    if (!window.confirm(`Delete "${v.name}"? This cannot be undone.`)) return;
    try {
      await api.delete(`${path}/${v.id}`);
      toast.success("Deleted.");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  const titleSingular = kind === "subcontractor" ? "Subcontractor" : "Supplier";
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-sm text-slate-400">
          {rows.length} {kind === "subcontractor" ? "subcontractor" : "supplier"}{rows.length === 1 ? "" : "s"} in this organization
        </div>
        <button
          data-testid={`vendors-add-${kind}`}
          onClick={openNew}
          className="h-8 px-3 rounded bg-emerald-600 text-white text-xs inline-flex items-center gap-1.5 hover:bg-emerald-500"
        >
          <Plus className="h-3.5 w-3.5" /> Add {titleSingular}
        </button>
      </div>
      <div className="surface p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-white/[0.03] border-b border-white/[0.06]">
            <tr className="text-xs text-slate-400">
              <th className="text-left px-3 py-2 font-semibold">Name</th>
              <th className="text-left px-3 py-2 font-semibold">Trades</th>
              <th className="text-left px-3 py-2 font-semibold">Contact</th>
              <th className="text-left px-3 py-2 font-semibold">Phone</th>
              <th className="text-right px-3 py-2 font-semibold w-32"></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((v) => (
              <tr key={v.id} data-testid={`vendor-row-${v.id}`}
                className="border-b border-white/[0.04] hover:bg-white/[0.02]">
                <td className="px-3 py-2 font-medium">{v.name}</td>
                <td className="px-3 py-2 text-slate-300">{(v.trades || []).join(", ") || "—"}</td>
                <td className="px-3 py-2 text-slate-400">
                  {v.contact_name || "—"}
                  {v.contact_email && <div className="text-xs text-slate-500">{v.contact_email}</div>}
                </td>
                <td className="px-3 py-2 text-slate-400">{v.contact_phone || "—"}</td>
                <td className="px-3 py-2 text-right">
                  <button onClick={() => openEdit(v)} className="text-xs text-slate-400 hover:text-white mr-3 inline-flex items-center gap-1">
                    <Pencil className="h-3 w-3" /> Edit
                  </button>
                  <button data-testid={`vendor-delete-${v.id}`}
                    onClick={() => remove(v)} className="text-xs text-rose-500 hover:text-rose-400 inline-flex items-center gap-1">
                    <Trash2 className="h-3 w-3" /> Delete
                  </button>
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={5} className="text-center py-6 text-slate-500 text-sm">
                No {kind === "subcontractor" ? "subcontractors" : "suppliers"} yet — click "Add {titleSingular}" to create the first one.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      <Dialog open={editing != null} onOpenChange={(v) => { if (!v) close(); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{editing === "new" ? `Add ${titleSingular}` : `Edit ${titleSingular}`}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 mt-2">
            <Field label="Name *" value={form.name} onChange={(v) => setForm({ ...form, name: v })} testid="vendor-form-name" />
            <Field label={`Trades (${kind === "subcontractor" ? "comma-separated, e.g. electrical, mechanical" : "categories supplied"})`}
              value={Array.isArray(form.trades) ? form.trades.join(", ") : form.trades}
              onChange={(v) => setForm({ ...form, trades: v })}
              testid="vendor-form-trades" />
            <div className="grid grid-cols-2 gap-3">
              <Field label="Contact name" value={form.contact_name} onChange={(v) => setForm({ ...form, contact_name: v })} />
              <Field label="Phone" value={form.contact_phone} onChange={(v) => setForm({ ...form, contact_phone: v })} />
            </div>
            <Field label="Email" value={form.contact_email} onChange={(v) => setForm({ ...form, contact_email: v })} />
            <Field label="Notes" value={form.notes} onChange={(v) => setForm({ ...form, notes: v })} textarea />
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={close} className="h-8 px-3 rounded border border-white/10 text-xs">Cancel</button>
            <button data-testid="vendor-save" onClick={save} className="h-8 px-3 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-500">Save</button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Field({ label, value, onChange, testid, textarea }) {
  return (
    <label className="block">
      <div className="text-xs text-slate-400 mb-1">{label}</div>
      {textarea
        ? <textarea data-testid={testid} value={value || ""}
            onChange={(e) => onChange(e.target.value)} rows={2}
            className="w-full px-2 py-1.5 rounded bg-slate-900/60 border border-white/10 text-sm focus:border-emerald-500 outline-none" />
        : <input data-testid={testid} value={value || ""}
            onChange={(e) => onChange(e.target.value)}
            className="w-full h-8 px-2 rounded bg-slate-900/60 border border-white/10 text-sm focus:border-emerald-500 outline-none" />
      }
    </label>
  );
}

export default function Vendors() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Vendors</h1>
        <p className="text-sm text-slate-400 mt-1">
          Your central repo of subcontractors and suppliers. These show up as autocomplete in the Daily Report "Manpower log" and inform the voice AI when supers mention a trade.
        </p>
      </div>
      <Tabs defaultValue="subs">
        <TabsList className="bg-white/[0.04] mb-4">
          <TabsTrigger value="subs" data-testid="vendors-tab-subs" className="gap-1.5 px-3 data-[state=active]:bg-[#34D399]/15 data-[state=active]:text-[#34D399]">
            <Briefcase className="h-3.5 w-3.5" /> Subcontractors
          </TabsTrigger>
          <TabsTrigger value="suppliers" data-testid="vendors-tab-suppliers" className="gap-1.5 px-3 data-[state=active]:bg-[#34D399]/15 data-[state=active]:text-[#34D399]">
            <Truck className="h-3.5 w-3.5" /> Suppliers
          </TabsTrigger>
        </TabsList>
        <TabsContent value="subs">
          <VendorTable kind="subcontractor" />
        </TabsContent>
        <TabsContent value="suppliers">
          <VendorTable kind="supplier" />
        </TabsContent>
      </Tabs>
    </div>
  );
}
