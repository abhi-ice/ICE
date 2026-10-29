import api, { extractApiError } from "./api";
import { confirmAction } from "./confirmAction";
import { toast } from "../components/ui/sonner";

const fmt = (iso) => {
  if (!iso) return "—";
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

// "Preview impact" step before a delay is saved: shows which activities
// move and by how many working days, then the super confirms.
export async function previewDelayThenConfirm({ projectId, impacts }) {
  const list = (impacts || []).filter((i) => i.task_id && Number(i.days) > 0);
  if (!list.length) return true;
  let data;
  try {
    ({ data } = await api.post(`/projects/${projectId}/delay-impact-preview`, {
      impacts: list.map((i) => ({ task_id: i.task_id, days: Number(i.days) })),
    }));
  } catch (e) {
    toast.error(extractApiError(e, "Couldn't preview the impact"));
    return confirmAction({ title: "Log this delay?", message: "The impact preview isn't available right now.", confirmText: "Log delay" });
  }
  if (!data.supported) {
    return confirmAction({ title: "Log this delay?", message: data.message, confirmText: "Log delay" });
  }
  const moved = data.project_finish_from !== data.project_finish_to;
  const message = (
    <div data-testid="delay-preview" className="space-y-3 text-left">
      <div className={`rounded-md px-3 py-2 text-sm ${moved ? "bg-red-500/10 text-red-400" : "bg-emerald-500/10 text-emerald-400"}`}
        data-testid="delay-preview-project-finish">
        {moved
          ? `Project finish moves ${fmt(data.project_finish_from)} → ${fmt(data.project_finish_to)}`
          : "Project finish does not move (absorbed by float)"}
      </div>
      {data.moves.length === 0 ? (
        <div className="text-sm opacity-80">No activity dates change.</div>
      ) : (
        <div className="max-h-72 overflow-y-auto space-y-1.5" data-testid="delay-preview-list">
          {data.moves.map((m) => (
            <div key={m.task_id} className="rounded-md border border-border px-3 py-2 text-sm" data-testid={`delay-preview-move-${m.task_id}`}>
              <div className="flex items-center gap-2">
                <span className="font-medium flex-1 min-w-0 [overflow-wrap:anywhere]">{m.name}</span>
                <span className="shrink-0 text-xs font-semibold text-amber-400">+{m.days} day{m.days === 1 ? "" : "s"}</span>
              </div>
              <div className="text-xs opacity-70 mt-0.5">
                {m.start_from !== m.start_to ? `Start ${fmt(m.start_from)} → ${fmt(m.start_to)} · ` : ""}
                Finish {fmt(m.finish_from)} → {fmt(m.finish_to)}{m.direct ? " · this activity" : " · knock-on"}{m.critical ? " · critical" : ""}
              </div>
            </div>
          ))}
          {data.total_moves > data.moves.length && <div className="text-xs opacity-60">+{data.total_moves - data.moves.length} more</div>}
        </div>
      )}
    </div>
  );
  return confirmAction({ title: "This delay will move these dates", message, confirmText: "Log delay" });
}
