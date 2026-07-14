// Guided prompt dialog — React port of the approved desktop mockup
// (ICE-DR-guided.html). One reusable dialog that fills every form one
// question at a time: big question, one input group per screen,
// Back · progress dots · Next/Save, optional steps with Skip, conditional
// steps via `when(answers)`, and a "Does this look right?" summary with
// Edit links. Closing mid-flow shows an in-page confirm strip — never a
// native popup.
//
// Session shapes (parent owns the state):
//   {kind:'chooser', title, sub, types:[[label, gloss, key]], onPick(key)}
//   {kind:'flow', flow, fromChooser?:fn}   — Back on step 0 returns to chooser
//   {kind:'custom', title, sub, body, foot} — e.g. the Review & submit screen
//
// Flow schema (identical to the mockup):
//   {title, sub, save, saving?, steps:[{q, gloss?, optional?, when?(a),
//     fields:[{k,label,t,opts?,req?,ph?,def?,min?,stp?,mini?,tasks?}]}],
//     onSave(answers) -> may return a Promise}
import { useEffect, useMemo, useRef, useState } from "react";

function hasVal(v) {
  if (v === undefined || v === null) return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return true; // File / {task,days} objects
  return String(v).trim() !== "";
}
function dispVal(v) {
  if (Array.isArray(v))
    return v
      .map((x) =>
        x && typeof x === "object"
          ? x.task + (hasVal(x.days) ? ` — ${x.days} day${String(x.days) === "1" ? "" : "s"}` : "")
          : x
      )
      .join(", ");
  if (v && typeof v === "object" && v.name) return v.name;
  return String(v);
}

function FieldInput({ fd, value, showDone, onSet, onToggleDone, solo }) {
  const [filter, setFilter] = useState("");
  switch (fd.t) {
    case "text":
      return (
        <input className="gi" value={value || ""} placeholder={fd.ph || ""}
          onChange={(e) => onSet(fd.k, e.target.value)} data-testid={`gf-${fd.k}`} />
      );
    case "textarea":
      return (
        <textarea className="gi ta" value={value || ""} placeholder={fd.ph || ""}
          onChange={(e) => onSet(fd.k, e.target.value)} data-testid={`gf-${fd.k}`} />
      );
    case "number":
      return (
        <input className="gi num" type="number" step={fd.stp || undefined} value={value ?? ""}
          placeholder={fd.ph || ""} onChange={(e) => onSet(fd.k, e.target.value)} data-testid={`gf-${fd.k}`} />
      );
    case "datetime":
      return (
        <input className="gi" type="datetime-local" value={value || ""}
          onChange={(e) => onSet(fd.k, e.target.value)} data-testid={`gf-${fd.k}`} />
      );
    case "date":
      return (
        <input className="gi" type="date" style={{ maxWidth: 220 }} value={value || ""}
          onChange={(e) => onSet(fd.k, e.target.value)} data-testid={`gf-${fd.k}`} />
      );
    case "stepper": {
      const step = (d) => {
        let cur = parseFloat(value);
        if (isNaN(cur)) cur = fd.def !== undefined ? fd.def : 0;
        let nv = cur + d * (fd.stp || 1);
        if (fd.min !== undefined && nv < fd.min) nv = fd.min;
        onSet(fd.k, Math.round(nv * 10) / 10);
      };
      return (
        <div className="stq">
          <button type="button" onClick={() => step(-1)}>&minus;</button>
          <input value={value ?? ""} onChange={(e) => onSet(fd.k, e.target.value)} data-testid={`gf-${fd.k}`} />
          <button type="button" onClick={() => step(1)}>+</button>
        </div>
      );
    }
    case "choice": {
      const short = fd.opts.every((o) => (o.label || o).length <= 22);
      const wrapCls = solo ? "opts" + (fd.opts.length >= 4 && short ? " two" : "") : "pillrow";
      const bcls = solo ? "opt" : "pl";
      return (
        <div className={wrapCls}>
          {fd.opts.map((o) => {
            const label = o.label || o;
            const val = o.value !== undefined ? o.value : o;
            return (
              <button type="button" key={`${label}::${String(val)}`} className={`${bcls}${value === val ? " sel" : ""}`}
                onClick={() => onSet(fd.k, val)}>{label}</button>
            );
          })}
        </div>
      );
    }
    case "chips":
    case "multichips": {
      const multi = fd.t === "multichips";
      const q = filter.toLowerCase();
      return (
        <div>
          {fd.opts.length > 8 && (
            <input className="gi gsearch" placeholder="Type to filter…" value={filter}
              onChange={(e) => setFilter(e.target.value)} />
          )}
          <div className="chipgrid">
            {fd.opts.map((o) => {
              const label = o.label || o;
              const val = o.value !== undefined ? o.value : o;
              if (q && !label.toLowerCase().includes(q)) return null;
              const sel = multi ? Array.isArray(value) && value.includes(val) : value === val;
              return (
                <button type="button" key={`${label}::${String(val)}`} className={`gchip${sel ? " sel" : ""}`}
                  onClick={() => {
                    if (!multi) return onSet(fd.k, val);
                    let arr = Array.isArray(value) ? value.slice() : [];
                    arr = arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val];
                    onSet(fd.k, arr);
                  }}>{label}</button>
              );
            })}
          </div>
        </div>
      );
    }
    case "file":
      return (
        <div className="fdrop">
          <div style={{ fontWeight: 700, marginBottom: 4 }}>Drop a photo here, or pick a file</div>
          <div className="hint" style={{ margin: "0 0 10px" }}>
            Photos are usually added from your phone. You can also pick a file from this computer.
          </div>
          <input type="file" accept="image/*" className="gi" style={{ padding: 10 }} multiple={!!fd.multiple}
            onChange={(e) => {
              const files = Array.from(e.target.files || []);
              onSet(fd.k, files.length ? (fd.multiple ? files : files[0]) : undefined);
            }} data-testid={`gf-${fd.k}`} />
          <div className="hint">{hasVal(value) ? dispVal(Array.isArray(value) ? value.map((f) => f.name) : value) : ""}</div>
        </div>
      );
    case "taskimpact": {
      const arr = Array.isArray(value) ? value : [];
      const tasks = (fd.tasks || []).filter((t) => showDone || !t.done || arr.some((x) => x.task === t.name));
      return (
        <div>
          <div className="chipgrid">
            {tasks.map((t) => {
              const sel = arr.some((x) => x.task === t.name);
              return (
                <button type="button" key={t.name} className={`gchip${sel ? " sel" : ""}`}
                  onClick={() => {
                    const next = arr.slice();
                    const i = next.findIndex((x) => x.task === t.name);
                    if (i >= 0) next.splice(i, 1);
                    else next.push({ task: t.name, id: t.id, days: "" });
                    onSet(fd.k, next);
                  }}>{t.name}</button>
              );
            })}
          </div>
          <div style={{ marginTop: 8 }}>
            <button type="button" className="btn link" onClick={onToggleDone}>
              {showDone ? "Hide completed tasks" : "Show completed tasks"}
            </button>
          </div>
          {arr.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {arr.map((x, i) => (
                <div key={x.task} style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 10, flexWrap: "wrap" }}>
                  <span style={{ flex: 1, minWidth: 180, fontWeight: 600 }}>{x.task}</span>
                  <label style={{ color: "var(--muted)", fontSize: 14, fontWeight: 600 }}>Days it loses</label>
                  <input className="gi num" type="number" step="0.5" style={{ maxWidth: 110 }} value={x.days}
                    placeholder="e.g. 1"
                    onChange={(e) => {
                      const next = arr.slice();
                      next[i] = { ...next[i], days: e.target.value };
                      onSet(fd.k, next);
                    }} />
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }
    default:
      return null;
  }
}

function FlowRunner({ flow, fromChooser, onClose, closeReq }) {
  const [answers, setAnswers] = useState(() => {
    const a = {};
    flow.steps.forEach((s) => s.fields.forEach((fd) => { if (fd.def !== undefined) a[fd.k] = fd.def; }));
    return a;
  });
  const [i, setI] = useState(0);
  const [confirm, setConfirm] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false); // guards Enter double-save (state is stale in the keydown closure)
  const dirtyRef = useRef(false);
  const bodyRef = useRef(null);
  // If this runner unmounts while onSave is in flight (user closed it, or a
  // new dialog replaced it), the late resolution must NOT call onClose —
  // that would close whatever dialog the user has open NOW.
  // Set true in the effect BODY (not just init) — StrictMode's simulated
  // unmount runs the cleanup once on mount, and the ref must recover.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const vs = useMemo(() => flow.steps.filter((s) => !s.when || s.when(answers)), [flow, answers]);
  const idx = Math.min(i, vs.length);
  const onSum = idx >= vs.length;
  const s = onSum ? null : vs[idx];
  const total = vs.length + 1;

  const stepValid = (st) => !st || st.fields.every((fd) => !fd.req || hasVal(answers[fd.k]));
  const canNext = onSum || stepValid(s);

  const set = (k, v) => { dirtyRef.current = true; setAnswers((p) => ({ ...p, [k]: v })); };

  const next = async () => {
    if (busy || busyRef.current) return;
    if (onSum) {
      // A required step may have slid into view (or emptied) since the
      // user last passed it — never save past an invalid visible step.
      const firstInvalid = vs.findIndex((st) => !stepValid(st));
      if (firstInvalid >= 0) { setI(firstInvalid); return; }
      busyRef.current = true;
      setBusy(true);
      try {
        // Submit only the answers belonging to VISIBLE steps — a value
        // entered on a step whose when() later turned false must not leak.
        const visKeys = new Set(vs.flatMap((st) => st.fields.map((fd) => fd.k)));
        const cleaned = Object.fromEntries(Object.entries(answers).filter(([k]) => visKeys.has(k)));
        await flow.onSave(cleaned);
        if (aliveRef.current) onClose();
      } catch (e) {
        if (aliveRef.current) { busyRef.current = false; setBusy(false); }
      }
      return;
    }
    if (!stepValid(s)) return;
    setI(idx + 1);
  };
  const back = () => {
    if (idx === 0) { if (fromChooser) fromChooser(); return; }
    setI(idx - 1);
  };
  const skip = () => {
    setAnswers((p) => {
      const n = { ...p };
      s.fields.forEach((fd) => { delete n[fd.k]; });
      return n;
    });
    dirtyRef.current = true;
    setI(idx + 1);
  };
  const requestClose = () => {
    if (dirtyRef.current) {
      // First request shows the confirm strip; further backdrop clicks /
      // Escapes while it's showing do nothing — only the strip's explicit
      // "Close" button discards (a double-click must not nuke answers).
      if (!confirm) setConfirm(true);
      return;
    }
    onClose();
  };

  // Backdrop clicks (mockup parity): a clean flow closes, a dirty flow
  // shows the in-page confirm strip. The overlay bumps closeReq; a ref of
  // the last-seen value keeps a stale count from closing a NEW flow that
  // mounts while closeReq > 0.
  const seenCloseReq = useRef(closeReq);
  useEffect(() => {
    if (closeReq !== seenCloseReq.current) {
      seenCloseReq.current = closeReq;
      requestClose();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeReq]);

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
    if (!confirm && !onSum && bodyRef.current) {
      const fi = bodyRef.current.querySelector("input.gi:not([type=file]),textarea.gi");
      if (fi) fi.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idx, confirm]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); requestClose(); return; }
      if (e.key === "Enter" && !confirm) {
        const t = e.target;
        // Let focused controls act natively: buttons activate, the file
        // picker opens, the chip filter keeps typing focus. Text inputs
        // keep Enter-advances (the intended fast path).
        if (t && (t.tagName === "TEXTAREA" || t.tagName === "BUTTON"
          || (t.tagName === "INPUT" && t.type === "file") || t.closest(".gsearch"))) return;
        if (canNext && !busyRef.current) { e.preventDefault(); next(); }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canNext, confirm, onSum, idx, answers, busy]);

  const summaryRows = () => {
    const rows = [];
    vs.forEach((st, si) => {
      const filled = st.fields.filter((fd) => hasVal(answers[fd.k]));
      if (!filled.length) {
        rows.push(
          <div className="sumrow" key={`s${si}`}>
            <span className="sl">{st.optional ? st.q : st.fields[0].label}</span>
            <span className="sv"><span className="none">{st.optional ? "Skipped" : "—"}</span></span>
            <button className="sedit" disabled={busy} onClick={() => { setI(si); setConfirm(false); }}>Edit</button>
          </div>
        );
      } else {
        filled.forEach((fd) => {
          let v = answers[fd.k];
          if (fd.opts && fd.opts.length && typeof fd.opts[0] === "object") {
            const toLabel = (val) => { const o = fd.opts.find((x) => x.value === val); return o ? o.label : val; };
            v = Array.isArray(v) ? v.map(toLabel) : toLabel(v);
          }
          rows.push(
            <div className="sumrow" key={`s${si}-${fd.k}`}>
              <span className="sl">{fd.label}</span>
              <span className="sv">{dispVal(v)}</span>
              <button className="sedit" disabled={busy} onClick={() => { setI(si); setConfirm(false); }}>Edit</button>
            </div>
          );
        });
      }
    });
    return rows;
  };

  return (
    <div className="gdlg" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
      <div className="ghead">
        <div>
          <div className="gt">{flow.title}</div>
          <div className="gs">{flow.sub || ""}</div>
        </div>
        <button className="gx" onClick={requestClose} title="Close">&times;</button>
      </div>
      <div className="gbody" ref={bodyRef}>
        {confirm && (
          <div className="cstrip">
            <span>Close without saving? Your answers here will be lost.</span>
            <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
              <button className="btn small" onClick={() => setConfirm(false)}>Keep going</button>
              <button className="btn small danger" onClick={onClose}>Close</button>
            </span>
          </div>
        )}
        {onSum ? (
          <>
            <div className="gq">Does this look right?</div>
            <div className="ggloss">Check the answers — tap Edit to change one.</div>
            <div className="sumwrap">{summaryRows()}</div>
          </>
        ) : (
          <>
            <div className="gq">
              {s.q}
              {s.fields.length === 1 && s.fields[0].req ? <> <span className="req">*</span></> : null}
            </div>
            {s.gloss && <div className="ggloss">{s.gloss}</div>}
            {s.fields.map((fd) => {
              const solo = s.fields.length === 1;
              const input = (
                <FieldInput key={fd.k} fd={fd} value={answers[fd.k]} solo={solo}
                  showDone={showDone} onSet={set} onToggleDone={() => setShowDone((v) => !v)} />
              );
              if (solo) return input;
              return (
                <div className="fg" key={fd.k}>
                  {fd.label !== s.q && (
                    <label className="fl">
                      {fd.label}
                      {fd.req ? <> <span className="req">*</span></> : null}
                      {fd.mini ? <span style={{ color: "var(--muted)", fontWeight: 500, fontSize: 14 }}> {fd.mini}</span> : null}
                    </label>
                  )}
                  {input}
                </div>
              );
            })}
          </>
        )}
      </div>
      <div className="gfoot">
        <button className="btn" disabled={busy || (idx === 0 && !fromChooser)} onClick={back}>Back</button>
        <div className="gprog">
          {idx + 1} of {total}
          <span className="dots">
            {Array.from({ length: total }, (_, n) => <i key={n} className={n <= idx ? "on" : ""} />)}
          </span>
        </div>
        {s && s.optional && <button className="btn" onClick={skip}>Skip</button>}
        <button className="btn green" disabled={!canNext || busy} onClick={next} data-testid="g-next">
          {onSum ? (busy ? (flow.saving || "Saving…") : flow.save) : "Next"}
        </button>
      </div>
    </div>
  );
}

export default function GuidedDialog({ session, onClose }) {
  const [closeReq, setCloseReq] = useState(0);
  useEffect(() => { setCloseReq(0); }, [session]);
  // Every new session gets a fresh remount key even if the caller forgot
  // flowKey — two same-titled flows must never share FlowRunner state.
  const autoKey = useMemo(() => `gd-${Math.random().toString(36).slice(2)}`,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [session]);
  useEffect(() => {
    if (!session || session.kind === "flow") return undefined;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [session, onClose]);

  if (!session) return null;
  return (
    <div className="goverlay show" onClick={(e) => {
      if (e.target !== e.currentTarget) return;
      if (session.kind === "flow") setCloseReq((c) => c + 1);
      else onClose();
    }}>
      {session.kind === "flow" ? (
        <FlowRunner key={session.flowKey || autoKey} flow={session.flow}
          fromChooser={session.fromChooser} onClose={onClose} closeReq={closeReq} />
      ) : (
        <div className="gdlg" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
          <div className="ghead">
            <div>
              <div className="gt">{session.title}</div>
              <div className="gs">{session.sub || ""}</div>
            </div>
            <button className="gx" onClick={onClose} title="Close">&times;</button>
          </div>
          <div className="gbody">{session.body}</div>
          <div className="gfoot">{session.foot}</div>
        </div>
      )}
    </div>
  );
}
