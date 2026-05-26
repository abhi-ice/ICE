// Bucket D Corrective (2026-05-08) -- master-schedule task picker.
//
// Used by RfisPanel / QcPanel / SafetyPanel / DeliveriesPanel /
// CoordinationEventForm (Phase 2). Mirrors delay's existing inline
// "Linked tasks" picker but without the per-task impact field +
// pushes-CP indicator -- those only matter for delays (which keep
// their own inline picker in DelayForm.jsx).
//
// Loads tasks from /projects/{projectId}/tasks and lets the user
// pick a multi-select list of master schedule task ids. Past, today,
// and future tasks are all available; the picker shows activity_code,
// description, and a "CP" tag for critical-path tasks.
//
// Props:
//   projectId       string, required -- fetch /projects/{id}/tasks
//   value           string[]         -- current selected task ids
//   onChange(arr)   function         -- called with the new array
//   required        bool             -- adds a * to the label
//   label           string           -- field label (default "Linked tasks")
//   testidPrefix    string           -- data-testid prefix
//   compact         bool             -- (reserved -- not used yet)
import { useEffect, useMemo, useState } from "react";
import { Plus, X } from "lucide-react";
import api from "../../lib/api";
import { isMasterTaskComplete } from "../../lib/taskCompletion";

export default function TaskPicker({
  projectId, value = [], onChange,
  required = false, label = "Linked tasks",
  testidPrefix = "task-picker",
}) {
  const [tasks, setTasks] = useState([]);
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [picker, setPicker] = useState("");
  // Feature F5 (2026-05-18) -- completed tasks (100% / status complete)
  // are hidden from the picker by default; this toggle reveals them.
  const [showCompleted, setShowCompleted] = useState(false);

  useEffect(() => {
    if (!projectId) {
      setTasks([]); setTasksLoaded(true);
      return;
    }
    api.get(`/projects/${projectId}/tasks`)
      .then(({ data }) => setTasks(Array.isArray(data) ? data : []))
      .catch(() => setTasks([]))
      .finally(() => setTasksLoaded(true));
  }, [projectId]);

  const taskById = useMemo(
    () => Object.fromEntries(tasks.map((t) => [t.id, t])),
    [tasks],
  );
  const valueSet = useMemo(() => new Set(value || []), [value]);
  // Feature F5 -- a completed task already linked still renders as a
  // chip (we never drop existing links); it's only hidden from the
  // dropdown of NEW options. The toggle reveals completed options.
  const unlinkedTasks = tasks.filter((t) => !valueSet.has(t.id));
  const hiddenCompletedCount = unlinkedTasks.filter(isMasterTaskComplete).length;
  const availableTasks = showCompleted
    ? unlinkedTasks
    : unlinkedTasks.filter((t) => !isMasterTaskComplete(t));

  const add = () => {
    if (!picker) return;
    onChange?.([...(value || []), picker]);
    setPicker("");
  };
  const remove = (tid) => {
    onChange?.((value || []).filter((id) => id !== tid));
  };

  return (
    <div data-testid={`${testidPrefix}-root`}>
      <label className="text-xs uppercase tracking-wide text-slate-500">
        {label}{required && " *"}
      </label>
      <div className="mt-1 space-y-1">
        {(value || []).map((tid) => {
          const t = taskById[tid];
          return (
            <div key={tid} data-testid={`${testidPrefix}-row-${tid}`}
              className="flex items-center gap-2 p-1.5 rounded border border-slate-200 bg-white text-sm">
              <button type="button" onClick={() => remove(tid)}
                data-testid={`${testidPrefix}-remove-${tid}`}
                className="h-5 w-5 grid place-items-center rounded hover:bg-red-50 text-red-500"
                aria-label="remove linked task">
                <X className="h-3 w-3" />
              </button>
              <span className="font-mono text-xs text-blue-700">
                {t?.activity_code || tid.slice(0, 6)}
              </span>
              <span className="text-slate-700 truncate flex-1">
                {t?.description || "(task not in current project list)"}
              </span>
              {t?.is_critical_path && (
                <span className="text-xs px-1 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200 font-mono uppercase">
                  CP
                </span>
              )}
            </div>
          );
        })}
        {!(value || []).length && (
          <div className="text-xs text-slate-400 italic">
            No tasks linked yet — pick one below.
          </div>
        )}
        {/* Feature F5 -- "Show completed tasks" toggle, default OFF. */}
        <label className="flex items-center gap-1.5 pt-1 text-xs text-slate-500 cursor-pointer">
          <input type="checkbox" data-testid={`${testidPrefix}-show-completed`}
            checked={showCompleted}
            onChange={(e) => setShowCompleted(e.target.checked)} />
          Show completed tasks
          {hiddenCompletedCount > 0 && !showCompleted && (
            <span className="text-slate-400">({hiddenCompletedCount} hidden)</span>
          )}
        </label>
        <div className="flex items-center gap-2 min-w-0">
          <select data-testid={`${testidPrefix}-select`} value={picker}
            onChange={(e) => setPicker(e.target.value)}
            className="flex-1 min-w-0 h-8 px-2 rounded border border-slate-200 text-sm"
            disabled={!tasksLoaded}>
            <option value="">
              {tasksLoaded
                ? `— select a task (${availableTasks.length} available) —`
                : "Loading project tasks…"}
            </option>
            {availableTasks.map((t) => (
              <option key={t.id} value={t.id}>
                [{t.activity_code}] {t.description}{t.is_critical_path ? " · CP" : ""}
              </option>
            ))}
          </select>
          <button type="button" onClick={add} disabled={!picker}
            data-testid={`${testidPrefix}-add`}
            title="Add selected task as linked"
            className="shrink-0 h-8 w-8 rounded bg-blue-600 text-white text-sm disabled:opacity-50 hover:bg-blue-700 inline-flex items-center justify-center"
            aria-label="Add task">
            <Plus className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
