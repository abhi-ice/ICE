// pt.49 (2026-05-28): one consistent required / optional marker for form
// field labels across the whole app. Standard SaaS convention — a small
// rose asterisk for required fields, a muted "(optional)" suffix for
// optional ones. Markers sit at the END of the label text so the UI stays
// clean and scannable; nothing else about the label layout changes.
//
// Usage:
//   <FieldLabel required>Severity</FieldLabel>      -> "Severity *"
//   <FieldLabel optional>Owner attribution</FieldLabel> -> "Owner attribution (optional)"
//   <FieldLabel>Notes</FieldLabel>                  -> "Notes" (no marker)
//
// `className` is appended so callers can keep their existing label classes.
export default function FieldLabel({ children, required = false, optional = false, className = "", title }) {
  return (
    <label className={`text-xs text-slate-500 ${className}`} title={title}>
      {children}
      {required && <span className="text-rose-500 ml-0.5" aria-hidden="true">*</span>}
      {optional && <span className="text-slate-400 font-normal ml-1">(optional)</span>}
    </label>
  );
}
