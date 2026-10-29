import { useEffect, useRef, useState } from "react";

// Per-activity super note. Saves on blur (only when changed); shared by the
// desktop guided view and the phone flow.
export default function ActivityNoteField({ value, onSave, disabled, className, testid, disabledHint }) {
  const [text, setText] = useState(value || "");
  const [state, setState] = useState("");
  const dirty = useRef(false);

  useEffect(() => { if (!dirty.current) setText(value || ""); }, [value]);

  const commit = async () => {
    if (!dirty.current || text === (value || "")) { dirty.current = false; return; }
    setState("saving");
    const ok = await onSave(text);
    if (ok === false) { setState("error"); return; }
    dirty.current = false;
    setState("saved");
    setTimeout(() => setState((s) => (s === "saved" ? "" : s)), 2000);
  };

  return (
    <div>
      <textarea
        data-testid={testid}
        className={className}
        rows={2}
        value={text}
        disabled={disabled}
        placeholder={disabled && disabledHint ? disabledHint : "Add a note for this activity (what happened, what's next…)"}
        onChange={(e) => { dirty.current = true; setText(e.target.value); setState(""); }}
        onBlur={commit}
      />
      <div data-testid={`${testid}-state`} style={{ fontSize: 11, minHeight: 14, marginTop: 2, color: state === "error" ? "#f87171" : "#34d399" }}>
        {state === "saving" ? "Saving…" : state === "saved" ? "Note saved" : state === "error" ? "Not saved — tap outside to retry" : ""}
      </div>
    </div>
  );
}
