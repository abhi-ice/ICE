// One name box per worker on a crew row (sign-in sheet + certified payroll).
// The number of boxes follows the crew's worker count: 3 workers -> 3 boxes.
// Names typed beyond a lowered count are kept in memory (so a slip on the
// count doesn't erase them) but only the first `count` are saved.
import { useEffect, useRef, useState } from "react";

export const MAX_NAME_BOXES = 500;

export function fitNames(names, count) {
  const n = Math.max(0, Math.min(MAX_NAME_BOXES, parseInt(count, 10) || 0));
  const out = (Array.isArray(names) ? names : []).slice(0, n).map((x) => String(x ?? ""));
  while (out.length < n) out.push("");
  return out;
}

export function cleanNames(names, count) {
  return fitNames(names, count).map((x) => x.replace(/\s+/g, " ").trim());
}

export function missingNames(names, count) {
  // Workers beyond the box limit can't be named here: count them as missing.
  const over = Math.max(0, (parseInt(count, 10) || 0) - MAX_NAME_BOXES);
  return cleanNames(names, count).filter((x) => !x).length + over;
}

// Starting worker count for an edit screen: never below the names saved,
// so opening the editor never drops a name the supervisor typed.
export function editStartCount(crew) {
  return Math.max(Number(crew?.headcount) || 0, (crew?.worker_names || []).length, 1);
}


// Controlled (value + onChange) or save-on-leave (value + onCommit).
export default function WorkerNamesInput({
  count, value, onChange, onCommit, disabled = false,
  inputClassName = "", testid = "worker-names", showWarning = true,
}) {
  const [local, setLocal] = useState(() => (Array.isArray(value) ? value.slice() : []));
  const lastSaved = useRef(JSON.stringify(cleanNames(value, count)));
  const key = JSON.stringify(value || []);
  useEffect(() => {
    if (onCommit) {
      setLocal(Array.isArray(value) ? value.slice() : []);
      lastSaved.current = JSON.stringify(cleanNames(value, count));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const names = onCommit ? local : (Array.isArray(value) ? value : []);
  const boxes = fitNames(names, count);
  const n = boxes.length;
  if (n === 0) return <span className="wn-empty">Enter the number of workers first</span>;

  const setAt = (i, v) => {
    const next = names.slice();
    while (next.length <= i) next.push("");
    next[i] = v;
    if (onCommit) setLocal(next); else onChange && onChange(next);
  };
  const commit = () => {
    if (!onCommit) return;
    const cleaned = cleanNames(local, count);
    const k = JSON.stringify(cleaned);
    if (k === lastSaved.current) return;
    lastSaved.current = k;
    onCommit(cleaned);
  };
  const missing = boxes.filter((x) => !x.trim()).length;
  return (
    <div className="wn-list" data-testid={testid}>
      {boxes.map((v, i) => (
        <div className="wn-row" key={i}>
          <span className="wn-num">{i + 1}.</span>
          <input
            type="text" value={v} disabled={disabled} maxLength={100}
            autoComplete="off" aria-label={`Worker ${i + 1} name`}
            placeholder={`Worker ${i + 1} name`}
            data-testid={`${testid}-${i}`}
            className={`wn-input ${inputClassName}`}
            onChange={(e) => setAt(i, e.target.value)}
            onBlur={commit}
          />
        </div>
      ))}
      {showWarning && missing > 0 && (
        <span className="wn-warn">{missing === n ? `Type the ${n === 1 ? "worker's name" : `${n} workers' names`}` : `${missing} name${missing === 1 ? "" : "s"} still blank`}</span>
      )}
    </div>
  );
}
