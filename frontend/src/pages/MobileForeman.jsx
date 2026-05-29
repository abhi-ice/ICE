// Mobile-first foreman view — capture-only daily report, photos, voice notes.
// Master prompt §13 (foreman / voice user persona).
import { useEffect, useState } from "react";
import { Mic, Camera, ListChecks, FileText, Cloud, LogOut } from "lucide-react";
import api from "../lib/api";
import { getUser, clearAuth } from "../lib/auth";
import { useNavigate } from "react-router-dom";
import VoiceCaptureButton from "../components/voice/VoiceCaptureButton";
import { toast } from "../components/ui/sonner";

export default function MobileForeman() {
  const user = getUser();
  const projectId = user?.active_project_id;
  const [today, setToday] = useState(new Date().toISOString().slice(0, 10));
  const [tasks, setTasks] = useState([]);
  const [report, setReport] = useState(null);
  const [photoBusy, setPhotoBusy] = useState(false);
  const nav = useNavigate();

  useEffect(() => {
    if (!projectId) return;
    api.get(`/projects/${projectId}/lookahead?days=7`).then(({ data }) => setTasks(data));
    // ensure today's daily report exists
    api.post("/daily-reports", { project_id: projectId, report_date: today })
      .then(({ data }) => setReport(data))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, today]);

  const updatePct = async (taskId, pct) => {
    try {
      await api.patch(`/tasks/${taskId}`, { pct_complete: pct });
      setTasks((s) => s.map((t) => (t.id === taskId ? { ...t, pct_complete: pct } : t)));
      toast.success(`Updated to ${pct}%`);
    } catch { toast.error("Update failed"); }
  };

  const uploadPhoto = async (file) => {
    setPhotoBusy(true);
    const fd = new FormData();
    fd.append("file", file);
    fd.append("project_id", projectId);
    fd.append("daily_report_id", report?.id || "");
    fd.append("caption", `Foreman ${user?.full_name || ""} ${new Date().toLocaleString()}`);
    try {
      await api.post("/photos/upload", fd, { headers: { "Content-Type": "multipart/form-data" } });
      toast.success("Photo uploaded");
    } catch { toast.error("Upload failed"); }
    finally { setPhotoBusy(false); }
  };

  const logout = () => { clearAuth(); nav("/login"); };

  return (
    <div data-testid="mobile-foreman" className="min-h-screen bg-slate-50">
      <div className="bg-slate-900 text-white px-4 py-3 flex items-center gap-2 sticky top-0 z-10">
        <ListChecks className="h-5 w-5 text-amber-400" />
        <div>
          <div className="text-sm font-semibold">Foreman</div>
          <div className="text-xs text-slate-400">{user?.full_name}</div>
        </div>
        <button data-testid="m-logout" onClick={logout} className="ml-auto h-8 w-8 grid place-items-center rounded hover:bg-slate-800">
          <LogOut className="h-4 w-4" />
        </button>
      </div>

      <div className="p-3 space-y-3">
        <div className="bg-amber-50 border border-amber-200 rounded p-3 text-xs flex items-center gap-2">
          <Cloud className="h-4 w-4 text-amber-600" />
          Daily report for <b className="ml-1">{today}</b>
          {report && <span className="ml-auto pill pill-green text-xs">{report.status || "draft"}</span>}
        </div>

        {/* Voice quick-capture */}
        <div className="bg-white rounded shadow p-3 flex items-center gap-3" data-testid="m-voice-card">
          <Mic className="h-5 w-5 text-rose-500" />
          <div className="flex-1">
            <div className="text-sm font-medium">Voice note</div>
            <div className="text-xs text-slate-500">Tap to record · transcribed by Whisper · Claude extracts structured fields.</div>
          </div>
          <VoiceCaptureButton />
        </div>

        {/* Photo capture */}
        <div className="bg-white rounded shadow p-3" data-testid="m-photo-card">
          <div className="flex items-center gap-2 mb-2">
            <Camera className="h-5 w-5 text-blue-500" />
            <span className="text-sm font-medium">Photo</span>
          </div>
          <input
            data-testid="m-photo-input"
            type="file"
            accept="image/*"
            capture="environment"
            onChange={(e) => e.target.files[0] && uploadPhoto(e.target.files[0])}
            disabled={photoBusy}
            className="block w-full text-sm"
          />
          <div className="text-xs text-slate-500 mt-1">Tap to take with phone camera. EXIF + GPS auto-extracted.</div>
        </div>

        {/* Quick activity %-update */}
        <div className="bg-white rounded shadow overflow-hidden" data-testid="m-tasks">
          <div className="px-3 py-2 border-b border-slate-100 flex items-center gap-2">
            <FileText className="h-4 w-4 text-slate-500" />
            <span className="text-sm font-medium">My activities (next 7d)</span>
          </div>
          <div className="divide-y divide-slate-100">
            {tasks.map((t) => (
              <div key={t.id} data-testid={`m-task-${t.activity_code}`} className="p-3">
                <div className="flex items-center gap-2 mb-1.5">
                  <span className="font-mono text-xs text-slate-500">{t.activity_code}</span>
                  <span className="ml-auto font-mono text-sm font-semibold">{t.pct_complete}%</span>
                </div>
                <div className="text-sm mb-2">{t.description}</div>
                <div className="flex flex-wrap gap-1">
                  {[10, 25, 50, 75, 90, 100].map((p) => (
                    <button
                      key={p}
                      data-testid={`m-task-${t.activity_code}-${p}`}
                      onClick={() => updatePct(t.id, p)}
                      className={`h-8 px-3 rounded text-xs font-mono border ${t.pct_complete === p ? "bg-blue-600 text-white border-blue-600" : "bg-white border-slate-200"}`}
                    >
                      {p}%
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {!tasks.length && <div className="p-6 text-sm text-slate-500 text-center">No activities in window.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
