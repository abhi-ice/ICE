// Generic CRUD list — table + add/edit/delete dialogs.
// Used by every resource page (crews, people, equipment, certifications,
// rfis, deliveries, work-items, change-orders, locations, floors, delays,
// safety-events, qc-issues).
import { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, X } from "lucide-react";
import api from "../lib/api";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "./ui/dialog";
import { toast } from "./ui/sonner";

/**
 * Props:
 *   title            display title
 *   listEndpoint     GET path returning array of records
 *   createEndpoint   POST path. If null, hides "Add" button.
 *   patchEndpoint    function (record) => path. If null, hides edit.
 *   deleteEndpoint   function (record) => path. If null, hides delete.
 *   columns          [{key, label, render?(row)}]
 *   formFields       [{key, label, type: 'text'|'number'|'date'|'select'|'textarea'|'checkbox', options?: [{value,label}], required?, default?}]
 *   buildPayload     optional (form, ctx) => payload — defaults to identity
 *   testidPrefix     for data-testid hooks
 *   refreshKey       changes → triggers reload
 *   help             short paragraph below the page header
 *
 *   editReasonConfig Bug #54 (2026-05-13) -- optional. When present AND
 *                    in edit mode AND the row's parent DR is locked,
 *                    render an edit_reason textarea + gate Save when
 *                    the patch is a field edit. Shape:
 *                      {
 *                        allowedFields: Set<string>  // status-only allowed
 *                        parentDrIdKey: string       // e.g. "daily_report_id"
 *                        minChars?: number           // default 10
 *                      }
 *                    Allowed=empty-set means "no status-only path" --
 *                    every edit requires a reason on a locked DR
 *                    (used for safety_events, which has no status field).
 */
const EDIT_REASON_MIN_DEFAULT = 10;

export default function CrudList({
  title, subtitle, listEndpoint, createEndpoint, patchEndpoint, deleteEndpoint,
  columns, formFields, buildPayload, testidPrefix = "crud", help, extraActions,
  emptyHint,
  // Bucket D Fix G -- when set, the "+ Add" button is replaced with
  // this custom node (gallery pages route to today's DR instead of
  // opening an inline modal).
  customAddButton,
  // Bug #54 (2026-05-13) -- edit_reason capture for field edits.
  editReasonConfig = null,
}) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({});

  const refresh = async () => {
    if (!listEndpoint) return;
    setLoading(true);
    try {
      const { data } = await api.get(listEndpoint);
      setRows(Array.isArray(data) ? data : (data?.results || []));
    } catch (e) {
      toast.error("Load failed");
    } finally { setLoading(false); }
  };

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [listEndpoint]);

  // Bug #54 (2026-05-13) -- per-row edit_reason capture state. Reset
  // on every startCreate/startEdit; lazy-fetch the parent DR's status
  // on startEdit so the textarea + gate render conditionally.
  const [editReason, setEditReason] = useState("");
  const [editLockedDr, setEditLockedDr] = useState(false);
  const reasonMin = (editReasonConfig && editReasonConfig.minChars) || EDIT_REASON_MIN_DEFAULT;

  const startCreate = () => {
    const def = {};
    formFields.forEach((f) => { def[f.key] = f.default ?? (f.type === "checkbox" ? false : ""); });
    setForm(def); setEditing(null); setOpen(true);
    setEditReason(""); setEditLockedDr(false);
  };
  const startEdit = (row) => {
    const def = {};
    formFields.forEach((f) => { def[f.key] = row[f.key] ?? (f.type === "checkbox" ? false : ""); });
    setForm(def); setEditing(row); setOpen(true);
    setEditReason(""); setEditLockedDr(false);
    // Lazy-fetch parent DR lock state if configured.
    if (editReasonConfig && editReasonConfig.parentDrIdKey) {
      const drId = row[editReasonConfig.parentDrIdKey];
      if (drId) {
        api.get(`/daily-reports/${drId}`)
          .then(({ data }) => {
            const st = data?.status;
            if (st === "submitted" || st === "approved") setEditLockedDr(true);
          })
          .catch(() => {});  // defensive: treat as unlocked on fetch failure
      }
    }
  };
  // Bug #54 -- compute "is this an edit beyond the status-only allowed
  // set?" for the conditional Save gate. Compares each form field to
  // the original row, normalizing null vs "" so a no-touch save isn't
  // falsely flagged. If config.allowedFields is empty, every change
  // counts as a field edit (safety_events case).
  const isFieldEditCrud = (() => {
    if (!editReasonConfig || !editing) return false;
    const allowed = editReasonConfig.allowedFields || new Set();
    for (const f of formFields) {
      const fv = form[f.key];
      const ev = editing[f.key];
      const same = (
        (fv == null && (ev == null || ev === ""))
        || (ev == null && (fv == null || fv === ""))
        || (f.type === "number" ? Number(fv ?? 0) === Number(ev ?? 0) : String(fv ?? "") === String(ev ?? ""))
      );
      if (same) continue;
      if (!allowed.has(f.key)) return true;
    }
    return false;
  })();
  const needsEditReasonCrud = !!editing && editLockedDr && isFieldEditCrud;
  const editReasonValidCrud = (editReason || "").trim().length >= reasonMin;
  // Bucket D Fix E -- double-click guards: one for save, one for
  // per-row delete (Set tracks in-flight delete row ids).
  const [saving, setSaving] = useState(false);
  const [deletingIds, setDeletingIds] = useState(new Set());
  const submit = async () => {
    if (saving) return;
    // Bug #54 -- client-side gate before payload assembly. Backend
    // would 422 anyway but the textarea + counter give clearer UX.
    if (needsEditReasonCrud && !editReasonValidCrud) {
      toast.error(
        `Edit reason required (min ${reasonMin} chars) for field changes on a locked daily report.`,
      );
      return;
    }
    const payload = buildPayload ? buildPayload(form, editing) : { ...form };
    // strip empty strings → null for non-required text
    formFields.forEach((f) => {
      if (!f.required && payload[f.key] === "") payload[f.key] = null;
      if (f.type === "number" && payload[f.key] !== null && payload[f.key] !== undefined && payload[f.key] !== "") {
        payload[f.key] = Number(payload[f.key]);
      }
    });
    setSaving(true);
    try {
      if (editing) {
        // Bug #54 -- attach edit_reason on PATCH when supplied. Empty
        // is omitted (backend doesn't need it on draft DRs or on
        // status-only patches via Bug #49 exemption).
        const patchBody = (editReason && editReason.trim())
          ? { ...payload, edit_reason: editReason.trim() }
          : payload;
        await api.patch(patchEndpoint(editing), patchBody);
        toast.success("Saved");
      } else {
        await api.post(createEndpoint, payload);
        toast.success("Created");
      }
      setOpen(false);
      refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || "Save failed");
    } finally { setSaving(false); }
  };
  const remove = async (row) => {
    if (deletingIds.has(row.id)) return;
    if (!window.confirm(`Delete ${title.toLowerCase().replace(/s$/, "")}?`)) return;
    setDeletingIds((prev) => new Set(prev).add(row.id));
    try {
      await api.delete(deleteEndpoint(row));
      toast.success("Deleted");
      refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    } finally {
      setDeletingIds((prev) => { const n = new Set(prev); n.delete(row.id); return n; });
    }
  };

  return (
    <div data-testid={`${testidPrefix}-page`} className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {subtitle && <p className="text-sm text-slate-500">{subtitle}</p>}
        </div>
        <div className="flex items-center gap-2">
          {extraActions}
          {customAddButton ? customAddButton : createEndpoint && (
            <Button data-testid={`${testidPrefix}-add`} onClick={startCreate} className="bg-blue-600 hover:bg-blue-700 gap-1">
              <Plus className="h-4 w-4" /> Add
            </Button>
          )}
        </div>
      </div>

      {help && <div className="text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded p-2">{help}</div>}

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead>
            <tr>
              {columns.map((c) => <th key={c.key}>{c.label}</th>)}
              {(patchEndpoint || deleteEndpoint) && <th className="w-20 text-right">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.id || i} data-testid={`${testidPrefix}-row-${r.id || i}`}>
                {columns.map((c) => (
                  <td key={c.key}>{c.render ? c.render(r) : (r[c.key] ?? "—")}</td>
                ))}
                {(patchEndpoint || deleteEndpoint) && (
                  <td className="text-right">
                    {patchEndpoint && (
                      <button data-testid={`${testidPrefix}-edit-${r.id}`} onClick={() => startEdit(r)} className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100 text-slate-500"><Pencil className="h-3.5 w-3.5" /></button>
                    )}
                    {deleteEndpoint && (
                      <button data-testid={`${testidPrefix}-delete-${r.id}`} onClick={() => remove(r)}
                        disabled={deletingIds.has(r.id)}
                        className="h-7 w-7 grid place-items-center rounded hover:bg-red-50 text-red-500 disabled:opacity-50"><Trash2 className="h-3.5 w-3.5" /></button>
                    )}
                  </td>
                )}
              </tr>
            ))}
            {!rows.length && !loading && (
              <tr><td colSpan={columns.length + 1} className="text-center py-8 text-sm text-slate-500">{emptyHint || "No records yet."}</td></tr>
            )}
            {loading && rows.length === 0 && (
              <tr><td colSpan={columns.length + 1} className="text-center py-8 text-sm text-slate-400">Loading…</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader><DialogTitle>{editing ? `Edit ${title.replace(/s$/, "")}` : `New ${title.replace(/s$/, "")}`}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            {formFields.map((f) => (
              <FormField key={f.key} f={f} value={form[f.key]} onChange={(v) => setForm({ ...form, [f.key]: v })} testidPrefix={testidPrefix} />
            ))}
            {/* Bug #54 (2026-05-13) -- edit_reason textarea for field
                edits on locked-DR rows. Renders only in edit mode +
                locked parent DR. The empty-allowed-set case (safety
                events have no status-only path) shows the textarea on
                ANY edit; for others the label hints when it's optional
                (status-only) vs required (field edit). */}
            {editing && editLockedDr && editReasonConfig && (
              <div className="pt-2 border-t border-slate-100">
                <label className="field-label">
                  Edit reason{isFieldEditCrud ? " *" : " (optional for status-only)"}
                </label>
                <textarea
                  data-testid={`${testidPrefix}-edit-reason`}
                  rows={2}
                  value={editReason}
                  onChange={(e) => setEditReason(e.target.value)}
                  placeholder={`Why this edit is needed (min ${reasonMin} chars)`}
                  className="mt-1 w-full p-2 rounded border border-slate-200 text-sm" />
                <div className="text-[10px] text-slate-500 text-right">
                  {(editReason || "").trim().length} / {reasonMin} min
                  {isFieldEditCrud ? " — required for field changes" : " — optional for status-only"}
                </div>
              </div>
            )}
            <Button data-testid={`${testidPrefix}-save`} onClick={submit}
              disabled={saving || (needsEditReasonCrud && !editReasonValidCrud)}
              title={needsEditReasonCrud && !editReasonValidCrud
                ? `Edit reason required (min ${reasonMin} chars) for field changes on a locked DR.`
                : undefined}
              className="bg-blue-600 hover:bg-blue-700 w-full disabled:opacity-50">{saving ? "Saving…" : (editing ? "Save changes" : "Create")}</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function FormField({ f, value, onChange, testidPrefix }) {
  const id = `${testidPrefix}-field-${f.key}`;
  if (f.type === "checkbox") {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input data-testid={id} type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
        {f.label}
      </label>
    );
  }
  if (f.type === "select") {
    return (
      <div>
        <label className="field-label">{f.label}{f.required && " *"}</label>
        <select data-testid={id} value={value ?? ""} onChange={(e) => onChange(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
          <option value="">— select —</option>
          {(f.options || []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>
    );
  }
  if (f.type === "textarea") {
    return (
      <div>
        <label className="field-label">{f.label}{f.required && " *"}</label>
        <textarea data-testid={id} value={value ?? ""} onChange={(e) => onChange(e.target.value)} className="w-full p-2 rounded border border-slate-200 text-sm h-24" />
      </div>
    );
  }
  return (
    <div>
      <label className="field-label">{f.label}{f.required && " *"}</label>
      <input data-testid={id} type={f.type || "text"} value={value ?? ""} onChange={(e) => onChange(e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" />
    </div>
  );
}
