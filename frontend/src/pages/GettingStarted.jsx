// Onboarding / Getting Started — explains the construction workflow,
// shows a checklist of what's set up vs not, and links each step to the
// right page. Backed by GET /api/onboarding/getting-started.
import { useEffect, useState } from "react";
import { Check, ArrowRight, BookOpen, Sparkles, AlertCircle } from "lucide-react";
import api from "../lib/api";
import { Link } from "react-router-dom";
import { Button } from "../components/ui/button";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

export default function GettingStarted() {
  const { activeId } = useProjects();
  const [data, setData] = useState(null);
  const [busyDetect, setBusyDetect] = useState(false);

  const refresh = async () => {
    try {
      const { data } = await api.get("/onboarding/getting-started");
      setData(data);
    } catch {}
  };
  useEffect(() => { refresh(); }, []);

  const autoDetectRooms = async () => {
    if (!activeId) { toast.error("Select a project first"); return; }
    setBusyDetect(true);
    try {
      const drawings = (await api.get(`/projects/${activeId}/drawings`)).data;
      if (!drawings.length) {
        toast.error("Upload a plan PDF first under Drawings");
        setBusyDetect(false); return;
      }
      const floors = (await api.get(`/projects/${activeId}/floors`)).data;
      if (!floors.length) {
        toast.error("Create at least one floor first under Locations");
        setBusyDetect(false); return;
      }
      const rev = drawings.find((d) => d.current_revision_id)?.current_revision_id;
      if (!rev) { toast.error("Selected drawing has no revision"); setBusyDetect(false); return; }
      const { data: r } = await api.post("/spatial/auto-detect-rooms", {
        drawing_revision_id: rev,
        floor_id: floors[0].id,
        page: 1, create_locations: true,
      });
      toast.success(`Detected ${r.detected_count} rooms — created ${r.created_count} new locations on floor ${floors[0].floor_name}`);
      refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Auto-detect failed");
    } finally { setBusyDetect(false); }
  };

  if (!data) return <div className="text-sm text-slate-500">Loading…</div>;

  return (
    <div data-testid="onboarding-page" className="space-y-4 max-w-4xl">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BookOpen className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">Getting started</h1>
          </div>
          <p className="text-sm text-slate-500">A construction project lives across {data.total} setup steps. You have completed {data.completed_count}.</p>
        </div>
        <div className="text-sm font-mono">
          <span className="text-emerald-600 font-semibold">{data.completed_count}</span>
          <span className="text-slate-400"> / {data.total}</span>
        </div>
      </div>

      <div className="bg-blue-50 border border-blue-200 rounded p-4 text-sm">
        <div className="flex items-start gap-2">
          <Sparkles className="h-4 w-4 text-blue-600 mt-0.5" />
          <div>
            <b>How rooms get into the platform.</b>
            <p className="text-slate-700 mt-1">
              The platform does not auto-extract rooms from a PDF you upload to Drawings.
              Drawings are reference documents only. Rooms are records in the Locations table
              that you create three ways: (1) one at a time on the Locations page, (2) by drawing
              a polygon on a plan in the Polygon editor, or (3) by clicking the AI auto-detect
              button below — Claude vision reads your plan PDF and creates a Location record
              for every room it identifies, complete with the polygon outline.
            </p>
            <Button
              data-testid="onboarding-auto-detect"
              onClick={autoDetectRooms}
              disabled={busyDetect}
              className="mt-3 bg-blue-600 hover:bg-blue-700 gap-1"
            >
              <Sparkles className="h-4 w-4" /> {busyDetect ? "Detecting…" : "AI auto-detect rooms (current project)"}
            </Button>
          </div>
        </div>
      </div>

      <div className="space-y-2">
        {data.steps.map((s, i) => (
          <div
            key={s.id}
            data-testid={`onboarding-step-${s.id}`}
            className={`surface p-4 flex items-start gap-3 ${s.complete ? "bg-emerald-50/40 border-emerald-200" : ""}`}
          >
            <div className={`h-7 w-7 rounded-full grid place-items-center text-xs font-mono ${s.complete ? "bg-emerald-500 text-white" : "bg-slate-200 text-slate-600"}`}>
              {s.complete ? <Check className="h-3.5 w-3.5" /> : i + 1}
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <b className="text-sm">{s.title}</b>
                {s.complete && <span className="pill pill-green text-xs">done</span>}
              </div>
              <p className="text-xs text-slate-600 mt-1">{s.body}</p>
              <div className="flex items-center gap-2 mt-2">
                {s.route ? (
                  <Link
                    data-testid={`onboarding-link-${s.id}`}
                    to={s.route}
                    className="text-xs text-blue-600 hover:underline inline-flex items-center gap-1"
                  >
                    {s.action} <ArrowRight className="h-3 w-3" />
                  </Link>
                ) : (
                  <span className="text-xs text-slate-500">{s.action}</span>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="bg-amber-50 border border-amber-200 rounded p-3 text-xs flex items-start gap-2">
        <AlertCircle className="h-4 w-4 text-amber-600 mt-0.5" />
        <div>
          <b>Tip for AI engineers new to construction.</b>
          A typical federal-construction project has these moving parts: a project (the building or scope), floors (one row per floor), rooms (one row per enclosed space), drawings (PDF sheets, separate from rooms), specifications (CSI MasterFormat document with sections like 03 30 00 Concrete), schedule activities (the tasks with dates and dependencies), submittals (sub-contractor product approvals), RFIs (formal questions), daily reports (one per work day), and resources (crews, equipment, people). The platform connects all of these so an RFI on a spec section can show you which activities are blocked, the AI can propose recovery, and the marketplace can be funded with escrow that releases automatically when the activity hits a milestone percent.
        </div>
      </div>
    </div>
  );
}
