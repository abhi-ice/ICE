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
import { progressLimitsOf, progressLimitNote } from "../../lib/progressLimits";
import { PROGRESS_DROP_EVENT } from "../../lib/drOfflineSync";

// Progress lock (2026-11-16): `limitsRow` is the grid row; its
// progress_floor / progress_ceiling (set by the server from the submitted
// reports) bound the slider. Moving past a limit stops there and says why
// once per drag; the screen also shows the same note under the slider.
export default function EodSlider({ rowId, initialValue, locked, updateRow, activityCode, gate, limitsRow }) {
  const [val, setVal] = useState(Number(initialValue) || 0);
  const [override, setOverride] = useState(false);
  const overrideReasonRef = useRef("");
  // Day-1 round 2 (2026-10-25): the last value this slider actually sent
  // (or the server value it was handed). A release is skipped only when it
  // equals THIS -- not the drag-start value -- so a quick 40 -> 60 -> 40
  // still saves the final 40, while a repeated release on the same value
  // (touchend + the emulated mouseup, a stray keyup) fires no duplicate.
  // null means "the last send was refused", so the next release always saves.
  const lastSentRef = useRef(Number(initialValue) || 0);
  useEffect(() => {
    setVal(Number(initialValue) || 0);
    lastSentRef.current = Number(initialValue) || 0;
  }, [initialValue]);
  // A save queued offline and later refused by the progress lock
  // (lib/drOfflineSync): the percent was never saved, so go back to the
  // saved value. initialValue alone can't do it -- the server value did not
  // change, so the effect above never fires. The message itself is shown
  // by lib/api.js.
  const initialRef = useRef(initialValue);
  initialRef.current = initialValue;
  useEffect(() => {
    const onDrop = (ev) => {
      if (!rowId || ev?.detail?.rowId !== rowId) return;
      const saved = Number(initialRef.current) || 0;
      setVal(saved);
      lastSentRef.current = saved;
    };
    window.addEventListener(PROGRESS_DROP_EVENT, onDrop);
    return () => window.removeEventListener(PROGRESS_DROP_EVENT, onDrop);
  }, [rowId]);

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
  const lim = progressLimitsOf(limitsRow);
  const limitToastAt = useRef(0);
  // Keep a value inside the range the submitted reports allow.
  const withinLimits = (n) => {
    const v = Math.min(lim.ceiling, Math.max(lim.floor, n));
    if (v !== n && Date.now() - limitToastAt.current > 2500) {
      limitToastAt.current = Date.now();
      const note = progressLimitNote(limitsRow, v);
      if (note) toast.error(note, { id: `limit-${rowId}` });
    }
    return v;
  };

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
    n = withinLimits(n);
    if (n === lastSentRef.current) { setVal(n); return; }
    const payload = { pct_complete_end_of_day: n };
    // Send the override reason only on a true increase past the gate cap.
    if (override && n > maxPct && n > startVal && overrideReasonRef.current) {
      payload.slider_gate_override_reason = overrideReasonRef.current;
    }
    setVal(n);
    lastSentRef.current = n;
    const sent = updateRow(rowId, payload);
    // updateRow resolves false when the save was refused or failed; forget
    // the value then so releasing on the same number tries again.
    if (sent && typeof sent.then === "function") {
      sent.then(
        (ok) => { if (ok === false && lastSentRef.current === n) lastSentRef.current = null; },
        () => { if (lastSentRef.current === n) lastSentRef.current = null; },
      );
    }
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
        onChange={(e) => setVal(withinLimits(Math.min(effMax, Number(e.target.value))))}
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
