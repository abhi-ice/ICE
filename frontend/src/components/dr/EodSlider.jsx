// CPM B1 (2026-06-12) -- relationship-aware EOD-slider gating.
//
// v7 guided flow (2026-07-05): EXTRACTED VERBATIM from pages/DailyReports.jsx
// so the mobile guided flow (GuidedDrFlow, bottom of pages/DailyReports.jsx)
// reuses the exact same component the desktop activity grid renders. Logic
// unchanged.
//
// `gate` (r.slider_gate) is stamped by the backend ONLY when
// FF_SLIDER_GATING is on; when absent the slider behaves exactly as it did
// pre-B1 (free 0-100). When present, the slider clamps to gate.max_pct
// (FS lock 0% / SS lock 0% / FF cap 95% / SF cap 99%, most-restrictive
// predecessor) and shows the lock reason. A PM/Admin can override past the
// cap with a logged reason (sent as slider_gate_override_reason; the server
// re-validates role + reason and audits the override).
import { useEffect, useRef, useState } from "react";
import { Lock } from "lucide-react";
import { getUser } from "../../lib/auth";
import { toast } from "../ui/sonner";

export default function EodSlider({ rowId, initialValue, locked, updateRow, activityCode, gate }) {
  const [val, setVal] = useState(Number(initialValue) || 0);
  const [override, setOverride] = useState(false);
  const overrideReasonRef = useRef("");
  useEffect(() => { setVal(Number(initialValue) || 0); }, [initialValue]);

  const maxPct = gate && typeof gate.max_pct === "number" ? gate.max_pct : 100;
  const gated = maxPct < 100;
  const user = getUser();
  const canOverride = ["admin", "pm"].includes((user?.role || "").toLowerCase());
  // Effective ceiling for the slider. The gate only constrains INCREASES past
  // the cap (the server allows lowering or keeping a value already above the
  // cap -- e.g. a successor that was 50% before its predecessor was reopened).
  // So the user may set anything up to max(cap, current value); an active
  // override lifts it to 100. This mirrors evaluate_eod_gate exactly:
  // allow iff new <= max(old_eod, max_pct).
  const startVal = Number(initialValue) || 0;
  const effMax = override ? 100 : Math.max(maxPct, startVal);

  const commit = (v) => {
    let n = Math.max(0, Math.min(100, Number(v) || 0));
    if (n > effMax) {
      // Tried to increase past the gate (and past the current value).
      n = effMax;
      setVal(n);
      if (!override && gated) {
        toast.error(gate?.reason || "Progress is gated by this activity's predecessors.");
      }
    }
    if (n === startVal) { setVal(n); return; }
    const payload = { pct_complete_end_of_day: n };
    // Send the override reason only on a true increase past the gate cap.
    if (override && n > maxPct && n > startVal && overrideReasonRef.current) {
      payload.slider_gate_override_reason = overrideReasonRef.current;
    }
    setVal(n);
    updateRow(rowId, payload);
  };

  const startOverride = () => {
    const reason = window.prompt(
      `Override the schedule gate on ${activityCode}? ` +
      `${gate?.reason || ""}\n\nEnter a reason (min 8 characters) — this is logged.`,
    );
    if (reason && reason.trim().length >= 8) {
      overrideReasonRef.current = reason.trim();
      setOverride(true);
      toast.success("Gate override active for this activity. Move the slider to set progress.");
    } else if (reason !== null) {
      toast.error("Override reason must be at least 8 characters.");
    }
  };

  return (
    <div className="flex items-center gap-2 min-w-[180px] max-md:min-w-[120px]" data-testid={`dr-pct-wrap-${activityCode}`}>
      <input
        data-testid={`dr-pct-${activityCode}`}
        type="range" min="0" max="100" step="1"
        value={val}
        onChange={(e) => setVal(Math.min(effMax, Number(e.target.value)))}
        onMouseUp={(e) => commit(e.target.value)}
        onTouchEnd={(e) => commit(e.target.value)}
        onKeyUp={(e) => commit(e.target.value)}
        disabled={locked}
        className="eod-range flex-1 cursor-pointer disabled:opacity-50"
        style={{ "--eod-fill": `${val}%` }}
      />
      <span className="text-xs tabular-nums font-medium text-emerald-300 w-9 text-right">{val}%</span>
      {gated && !override && (
        <span
          data-testid={`dr-gate-${activityCode}`}
          title={gate?.reason || "Gated by predecessors"}
          className="inline-flex items-center text-amber-600"
        >
          <Lock className="h-3 w-3" />
          {canOverride && (
            <button
              type="button"
              data-testid={`dr-gate-override-${activityCode}`}
              onClick={startOverride}
              className="ml-1 text-[10px] underline text-amber-700 hover:text-amber-900"
            >
              override
            </button>
          )}
        </span>
      )}
    </div>
  );
}
