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
import { useEffect, useRef, useState } from "react";
import { Plus, Trash2, Pencil, Briefcase, Truck, Upload, Download, FileSpreadsheet, Loader2, CheckCircle2, AlertCircle } from "lucide-react";
import * as XLSX from "xlsx";
import { toast } from "../components/ui/sonner";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../components/ui/tabs";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import api from "../lib/api";
import { confirmAction } from "../lib/confirmAction";

const BLANK = {
  name: "", trades: [], contact_name: "", contact_email: "",
  contact_phone: "", notes: "",
};

// v4 pt.25 (2026-05-26) — Excel/CSV import for both Subcontractors and
// Suppliers. Field aliases below cover the common header spellings users
// will paste in from their existing vendor spreadsheets — "Name" /
// "Company Name" / "Company", "Trades" / "Trade" / "Categories", etc.
// Lookup is case-insensitive + trim'd. First non-empty match wins.
const IMPORT_ALIASES = {
  name:          ["name", "company name", "company", "vendor", "vendor name", "subcontractor", "supplier"],
  trades:        ["trades", "trade", "categories", "category", "discipline", "scope"],
  contact_name:  ["contact name", "contact", "primary contact", "rep", "representative"],
  contact_email: ["contact email", "email", "e-mail", "email address"],
  contact_phone: ["contact phone", "phone", "phone number", "mobile", "tel", "telephone"],
  notes:         ["notes", "comments", "remarks", "description"],
};
function pickField(row, key) {
  const aliases = IMPORT_ALIASES[key] || [key];
  const normalized = {};
  Object.keys(row).forEach((k) => { normalized[String(k).trim().toLowerCase()] = row[k]; });
  for (const a of aliases) {
    const v = normalized[a];
    if (v !== undefined && v !== null && String(v).trim() !== "") return v;
  }
  return "";
}

function VendorTable({ kind }) {
  const path = kind === "subcontractor" ? "/subcontractors" : "/suppliers";
  const [rows, setRows] = useState([]);
  const [editing, setEditing] = useState(null); // null | { ...vendor } | "new"
  const [form, setForm] = useState(BLANK);
  const [importing, setImporting] = useState(false);
  const [importPreview, setImportPreview] = useState(null); // null | { rows: [...], filename }
  const fileInputRef = useRef(null);

  const load = () => api.get(path).then(({ data }) => setRows(data || []));
  useEffect(() => { load(); }, [path]);

  // -------- Excel / CSV import flow --------
  // 1) User picks a file via the hidden file input.
  // 2) We parse client-side with SheetJS (xlsx lib) and surface a preview
  //    Dialog so the user can sanity-check before any backend POST.
  // 3) On Confirm we loop POST each row to the existing /subcontractors or
  //    /suppliers endpoint — backend unchanged. Skipped rows (missing name)
  //    are flagged in the toast summary.
  const onFilePicked = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // reset so picking the same file twice still fires
    if (!file) return;
    try {
      const buf = await file.arrayBuffer();
      const wb = XLSX.read(buf, { type: "array" });
      const sheet = wb.Sheets[wb.SheetNames[0]];
      if (!sheet) { toast.error("File has no readable sheet."); return; }
      const raw = XLSX.utils.sheet_to_json(sheet, { defval: "" });
      if (!raw.length) { toast.error("File has no rows."); return; }
      const parsed = raw.map((r, idx) => {
        const tradesRaw = pickField(r, "trades");
        const trades = typeof tradesRaw === "string"
          ? tradesRaw.split(/[,;|]/).map((s) => s.trim()).filter(Boolean)
          : (Array.isArray(tradesRaw) ? tradesRaw : []);
        return {
          rowNum: idx + 2, // +2 because row 1 is the header and arrays are 0-indexed
          name:          String(pickField(r, "name") || "").trim(),
          trades,
          contact_name:  String(pickField(r, "contact_name") || "").trim(),
          contact_email: String(pickField(r, "contact_email") || "").trim(),
          contact_phone: String(pickField(r, "contact_phone") || "").trim(),
          notes:         String(pickField(r, "notes") || "").trim(),
        };
      });
      setImportPreview({ rows: parsed, filename: file.name });
    } catch (err) {
      toast.error("Couldn't parse the file: " + (err.message || "unknown error"));
    }
  };

  const confirmImport = async () => {
    if (!importPreview) return;
    const valid = importPreview.rows.filter((r) => r.name);
    if (!valid.length) {
      toast.error("No rows had a Name column — nothing to import.");
      setImportPreview(null);
      return;
    }
    setImporting(true);
    let ok = 0, errs = 0;
    for (const r of valid) {
      try {
        await api.post(path, {
          name: r.name,
          trades: r.trades,
          contact_name: r.contact_name,
          contact_email: r.contact_email,
          contact_phone: r.contact_phone,
          notes: r.notes,
        });
        ok++;
      } catch {
        errs++;
      }
    }
    setImporting(false);
    setImportPreview(null);
    const skipped = importPreview.rows.length - valid.length;
    const parts = [`Imported ${ok}`];
    if (errs) parts.push(`${errs} failed`);
    if (skipped) parts.push(`${skipped} skipped (no Name)`);
    if (ok > 0) toast.success(parts.join(" · "));
    else toast.error(parts.join(" · "));
    load();
  };

  const downloadTemplate = () => {
    // Hand the user a starter file with the expected headers + one sample
    // row so they don't have to guess the format.
    const header = ["Name", "Trades", "Contact Name", "Contact Email", "Contact Phone", "Notes"];
    const sample = kind === "subcontractor"
      ? ["ACME Electrical Inc.", "electrical, low-voltage", "Jane Smith", "jane@acme.com", "555-0100", "Preferred for federal jobs"]
      : ["Metro Steel Supply", "steel, rebar, decking", "Mike Lee", "mike@metrosteel.com", "555-0200", "Net-30, ships from NJ yard"];
    const ws = XLSX.utils.aoa_to_sheet([header, sample]);
    ws["!cols"] = [{ wch: 30 }, { wch: 30 }, { wch: 20 }, { wch: 26 }, { wch: 16 }, { wch: 30 }];
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, kind === "subcontractor" ? "Subcontractors" : "Suppliers");
    XLSX.writeFile(wb, `${kind === "subcontractor" ? "subcontractors" : "suppliers"}-import-template.xlsx`);
  };

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
    // pt.37: themed modal via confirmAction (was window.confirm).
    const ok = await confirmAction({
      title: `Delete "${v.name}"?`,
      message: "This cannot be undone.",
      confirmText: "Delete vendor",
      icon: "trash",
    });
    if (!ok) return;
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
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="text-sm" style={{ color: "var(--v4-fg-secondary)" }}>
          {rows.length} {kind === "subcontractor" ? "subcontractor" : "supplier"}{rows.length === 1 ? "" : "s"} in this organization
        </div>
        <div className="flex items-center gap-2">
          <button
            data-testid={`vendors-import-template-${kind}`}
            onClick={downloadTemplate}
            title="Download a starter .xlsx with the expected headers"
            className="h-8 px-3 rounded text-xs font-medium inline-flex items-center gap-1.5 transition"
            style={{ background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-secondary)", border: "1px solid var(--v4-border)" }}
          >
            <Download className="h-3.5 w-3.5" /> Template
          </button>
          <button
            data-testid={`vendors-import-${kind}`}
            onClick={() => fileInputRef.current?.click()}
            title={`Import ${titleSingular.toLowerCase()}s from .xlsx or .csv`}
            className="h-8 px-3 rounded text-xs font-medium inline-flex items-center gap-1.5 transition"
            style={{ background: "var(--v4-bg-surface-2)", color: "var(--v4-fg-primary)", border: "1px solid var(--v4-border-strong)" }}
          >
            <Upload className="h-3.5 w-3.5" /> Import
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".xlsx,.xls,.csv"
            onChange={onFilePicked}
            style={{ display: "none" }}
            data-testid={`vendors-import-input-${kind}`}
          />
          <button
            data-testid={`vendors-add-${kind}`}
            onClick={openNew}
            className="h-8 px-3 rounded bg-emerald-600 text-white text-xs font-medium inline-flex items-center gap-1.5 hover:bg-emerald-500 transition"
          >
            <Plus className="h-3.5 w-3.5" /> Add {titleSingular}
          </button>
        </div>
      </div>
      <div className="surface p-0 overflow-hidden">
        {/* pt.37: header-only vertical dividers via
            .dense-table-bordered. Vendors is 5 columns (small-table
            tier) per the table-column-lines mockup recommendation. */}
        <table className="w-full text-sm dense-table-bordered">
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

      {/* ---- Import preview dialog ---- */}
      <Dialog open={importPreview != null} onOpenChange={(v) => { if (!v) setImportPreview(null); }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              Import {titleSingular}s
              {importPreview && (
                <span className="ml-2 text-xs font-normal" style={{ color: "var(--v4-fg-secondary)" }}>
                  · {importPreview.filename}
                </span>
              )}
            </DialogTitle>
          </DialogHeader>
          {importPreview && (
            <>
              <div className="text-xs mt-1 flex items-center gap-3 flex-wrap" style={{ color: "var(--v4-fg-secondary)" }}>
                <span className="inline-flex items-center gap-1">
                  <FileSpreadsheet className="h-3.5 w-3.5" />
                  Found {importPreview.rows.length} row{importPreview.rows.length === 1 ? "" : "s"}
                </span>
                <span className="inline-flex items-center gap-1" style={{ color: "var(--brand-primary)" }}>
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  {importPreview.rows.filter((r) => r.name).length} will import
                </span>
                {importPreview.rows.filter((r) => !r.name).length > 0 && (
                  <span className="inline-flex items-center gap-1" style={{ color: "#b45309" }}>
                    <AlertCircle className="h-3.5 w-3.5" />
                    {importPreview.rows.filter((r) => !r.name).length} skipped (no Name)
                  </span>
                )}
              </div>
              <div className="mt-3 overflow-auto" style={{ maxHeight: "50vh", border: "1px solid var(--v4-border)", borderRadius: 6 }}>
                <table className="w-full text-xs">
                  <thead style={{ background: "var(--v4-bg-surface-2)", borderBottom: "1px solid var(--v4-border)" }}>
                    <tr>
                      <th className="text-left px-2 py-1.5 font-semibold w-10" style={{ color: "var(--v4-fg-secondary)" }}>#</th>
                      <th className="text-left px-2 py-1.5 font-semibold" style={{ color: "var(--v4-fg-secondary)" }}>Name</th>
                      <th className="text-left px-2 py-1.5 font-semibold" style={{ color: "var(--v4-fg-secondary)" }}>Trades</th>
                      <th className="text-left px-2 py-1.5 font-semibold" style={{ color: "var(--v4-fg-secondary)" }}>Contact</th>
                      <th className="text-left px-2 py-1.5 font-semibold" style={{ color: "var(--v4-fg-secondary)" }}>Phone</th>
                    </tr>
                  </thead>
                  <tbody>
                    {importPreview.rows.map((r, i) => (
                      <tr key={i} style={{
                        borderBottom: "1px solid var(--v4-border)",
                        opacity: r.name ? 1 : 0.45,
                        background: r.name ? "transparent" : "rgba(245,158,11,0.06)",
                      }}>
                        <td className="px-2 py-1.5 tabular-nums" style={{ color: "var(--v4-fg-muted)" }}>{r.rowNum}</td>
                        <td className="px-2 py-1.5 font-medium" style={{ color: r.name ? "var(--v4-fg-primary)" : "#b45309" }}>
                          {r.name || "— missing —"}
                        </td>
                        <td className="px-2 py-1.5" style={{ color: "var(--v4-fg-secondary)" }}>
                          {r.trades.length ? r.trades.join(", ") : "—"}
                        </td>
                        <td className="px-2 py-1.5" style={{ color: "var(--v4-fg-secondary)" }}>
                          {r.contact_name || "—"}
                          {r.contact_email && <div className="text-[10px]" style={{ color: "var(--v4-fg-muted)" }}>{r.contact_email}</div>}
                        </td>
                        <td className="px-2 py-1.5" style={{ color: "var(--v4-fg-secondary)" }}>{r.contact_phone || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-2 text-[11px]" style={{ color: "var(--v4-fg-muted)" }}>
                Header aliases supported: <code>Name</code> / <code>Company</code> / <code>Vendor</code>,
                {" "}<code>Trades</code> / <code>Trade</code> / <code>Categories</code> (comma-separated),
                {" "}<code>Contact Email</code>, <code>Contact Phone</code>, <code>Notes</code>.
              </div>
            </>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <button
              onClick={() => setImportPreview(null)}
              disabled={importing}
              className="h-8 px-3 rounded border text-xs disabled:opacity-50"
              style={{ borderColor: "var(--v4-border-strong)", color: "var(--v4-fg-secondary)" }}
            >
              Cancel
            </button>
            <button
              data-testid={`vendors-import-confirm-${kind}`}
              onClick={confirmImport}
              disabled={importing || !importPreview?.rows?.some((r) => r.name)}
              className="h-8 px-3 rounded bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-500 transition disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
              {importing ? "Importing…" : `Import ${importPreview?.rows?.filter((r) => r.name).length || 0} row${(importPreview?.rows?.filter((r) => r.name).length || 0) === 1 ? "" : "s"}`}
            </button>
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
        <TabsList className="v4-tabs-strip mb-4">
          <TabsTrigger value="subs" data-testid="vendors-tab-subs" className="v4-tab gap-1.5 px-3">
            <Briefcase className="h-3.5 w-3.5" /> Subcontractors
          </TabsTrigger>
          <TabsTrigger value="suppliers" data-testid="vendors-tab-suppliers" className="v4-tab gap-1.5 px-3">
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
