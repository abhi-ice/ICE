// Feature Client DR Template Export (2026-05-20) -- per-project page
// for uploading the client's required daily-report template. The DR
// list page surfaces an "Upload DR Template" button that routes here.
// Admin or PM can upload / replace / remove; other roles can view the
// page state but the action buttons are disabled with a tooltip.
import { useEffect, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { ChevronLeft, Upload, Trash2, FileSpreadsheet } from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
import { getUser } from "../lib/auth";

const ADMIN_OR_PM = new Set(["admin", "pm"]);

export default function DrTemplate() {
  const { projectId } = useParams();
  const navigate = useNavigate();
  const userRole = (getUser()?.role || "").toLowerCase();
  const canManage = ADMIN_OR_PM.has(userRole);

  const [loading, setLoading] = useState(true);
  const [meta, setMeta] = useState(null);    // { present, file_name, uploaded_at, uploaded_by_name }
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    try {
      setLoading(true);
      const { data } = await api.get(`/projects/${projectId}/dr-template`);
      setMeta(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load template info");
      setMeta({ present: false });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { if (projectId) reload(); /* eslint-disable-next-line */ }, [projectId]);

  const onPick = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";  // allow re-picking the same filename later
    if (!file) return;
    if (!canManage) {
      toast.error("Only project admins or PMs can upload a client template.");
      return;
    }
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if (ext !== "xlsx") {
      toast.error("Only .xlsx templates are supported.");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api.post(`/projects/${projectId}/dr-template`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      toast.success("Template saved.");
      await reload();
    } catch (err) {
      toast.error(err.response?.data?.detail || "Upload failed");
    } finally {
      setBusy(false);
    }
  };

  const onRemove = async () => {
    if (!canManage) {
      toast.error("Only project admins or PMs can remove the template.");
      return;
    }
    if (!window.confirm("Remove the saved client DR template? Client-format exports will be disabled until a new one is uploaded.")) return;
    setBusy(true);
    try {
      await api.delete(`/projects/${projectId}/dr-template`);
      toast.success("Template removed.");
      await reload();
    } catch (err) {
      toast.error(err.response?.data?.detail || "Remove failed");
    } finally {
      setBusy(false);
    }
  };

  const fmtDate = (iso) => {
    if (!iso) return "";
    try { return new Date(iso).toLocaleString(); } catch { return String(iso); }
  };

  return (
    <div data-testid="dr-template-page" className="p-6 space-y-4 max-w-3xl">
      <Link to="/daily-reports"
        className="text-sm text-blue-600 hover:underline inline-flex items-center gap-1"
        data-testid="dr-template-back">
        <ChevronLeft className="h-4 w-4" /> Back to Daily Reports
      </Link>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Client DR Template</h1>
        <p className="text-sm text-muted-foreground">
          Upload your client's daily report template. ICE will fill it with DR
          data when you export.
        </p>
      </div>

      {!canManage && (
        <div className="rounded border border-amber-200 bg-amber-50 text-amber-900 px-3 py-2 text-sm"
          data-testid="dr-template-readonly-banner">
          You can view the current template state below. Only project admins
          or PMs can upload, replace, or remove the template.
        </div>
      )}

      {loading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : meta?.present ? (
        <div className="surface p-4 space-y-3" data-testid="dr-template-info">
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="h-5 w-5 text-blue-600" />
            <span className="font-medium" data-testid="dr-template-filename">
              {meta.file_name}
            </span>
          </div>
          <div className="text-xs text-slate-600 space-y-1">
            <div>Uploaded: <span data-testid="dr-template-uploaded-at">{fmtDate(meta.uploaded_at)}</span></div>
            <div>By: <span data-testid="dr-template-uploaded-by">{meta.uploaded_by_name || "—"}</span></div>
          </div>
          <div className="flex flex-wrap gap-2 pt-2">
            <label
              className={`h-9 px-3 rounded text-sm inline-flex items-center gap-1 cursor-pointer ${canManage ? "bg-blue-600 text-white hover:bg-blue-700" : "bg-slate-100 text-slate-400 cursor-not-allowed"}`}
              title={canManage ? undefined : "Admin / PM only"}
              data-testid="dr-template-replace-label">
              <Upload className="h-4 w-4" /> Replace Template
              <input type="file" accept=".xlsx" className="hidden"
                disabled={!canManage || busy}
                onChange={onPick}
                data-testid="dr-template-replace-input" />
            </label>
            <button onClick={onRemove}
              disabled={!canManage || busy}
              className="h-9 px-3 rounded border border-red-200 bg-white text-red-700 text-sm inline-flex items-center gap-1 hover:bg-red-50 disabled:opacity-50"
              title={canManage ? undefined : "Admin / PM only"}
              data-testid="dr-template-remove">
              <Trash2 className="h-4 w-4" /> Remove Template
            </button>
          </div>
        </div>
      ) : (
        <div className="surface p-6 space-y-3 border-dashed" data-testid="dr-template-empty">
          <div className="text-sm text-slate-700">
            No template uploaded yet for this project.
          </div>
          <label
            className={`h-12 px-4 rounded border-2 border-dashed flex items-center justify-center gap-2 text-sm font-medium cursor-pointer ${canManage ? "border-blue-300 text-blue-700 bg-blue-50 hover:bg-blue-100" : "border-slate-200 text-slate-400 cursor-not-allowed"}`}
            data-testid="dr-template-upload-label">
            <Upload className="h-4 w-4" />
            {busy ? "Uploading…" : "Save Template"}
            <input type="file" accept=".xlsx" className="hidden"
              disabled={!canManage || busy}
              onChange={onPick}
              data-testid="dr-template-upload-input" />
          </label>
          <p className="text-xs text-slate-500">
            Upload your client's Excel daily report template. Supported format: .xlsx
          </p>
        </div>
      )}
    </div>
  );
}
