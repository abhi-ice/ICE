// Per-project access (2026-11-16) -- a tick list of projects, shared by the
// Team page invite form and the member access dialog. Scrolls when the
// company has many projects; every row is a 44px touch target on phones.
export default function ProjectChecklist({ projects, value, onChange, testid = "project-checklist" }) {
  const picked = new Set(value || []);
  const toggle = (id) => {
    const next = new Set(picked);
    if (next.has(id)) next.delete(id); else next.add(id);
    onChange(Array.from(next));
  };
  if (!projects?.length) {
    return <div className="text-xs text-slate-500 italic">No projects yet.</div>;
  }
  return (
    <div data-testid={testid}>
      <div className="flex items-center gap-3 text-xs mb-1">
        <button type="button" className="text-blue-600 hover:underline max-md:min-h-[44px]"
          onClick={() => onChange(projects.map((p) => p.id))}>Select all</button>
        <button type="button" className="text-blue-600 hover:underline max-md:min-h-[44px]"
          onClick={() => onChange([])}>Clear</button>
        <span className="text-slate-500">{picked.size} of {projects.length} selected</span>
      </div>
      <div className="max-h-48 overflow-y-auto border border-slate-200 rounded divide-y divide-slate-100">
        {projects.map((p) => (
          <label key={p.id}
            className="flex items-center gap-2 px-2 py-1.5 max-md:min-h-[44px] text-sm cursor-pointer hover:bg-slate-50">
            <input type="checkbox" data-testid={`${testid}-${p.id}`}
              checked={picked.has(p.id)} onChange={() => toggle(p.id)}
              className="h-4 w-4 shrink-0" />
            <span className="truncate">{p.name}</span>
          </label>
        ))}
      </div>
    </div>
  );
}
