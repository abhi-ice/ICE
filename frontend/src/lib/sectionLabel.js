// The schedule heading to show next to an activity's name so same-named
// activities can be told apart (e.g. "Electrical" under "Demolition - Phase 2"
// vs under "Installation"). `path` is the task's section_path: the headings
// above it, outermost first.
//   - one heading only: that is the schedule's top section ("Construction
//     Activities"), which says nothing useful -> no label;
//   - otherwise the innermost heading, plus its parent when the innermost
//     does not already carry the parent's words ("Phase 2 › Area 1", but just
//     "Demolition - Phase 2" under "Demolition").
export function sectionLabel(path) {
  const p = (path || []).map((s) => String(s || "").trim()).filter(Boolean);
  if (p.length < 2) return "";
  const inner = p[p.length - 1];
  if (p.length === 2) return inner;
  const parent = p[p.length - 2];
  return inner.toLowerCase().includes(parent.toLowerCase()) ? inner : `${parent} › ${inner}`;
}
