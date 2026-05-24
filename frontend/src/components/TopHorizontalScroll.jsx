// TopHorizontalScroll (2026-05-24) — render a horizontal scrollbar
// pinned to the TOP of a scrollable container so users don't have to
// scroll the whole table to the bottom + then scroll horizontally
// + then scroll back up. Common pattern for wide CPM / Gantt tables.
//
// Usage:
//   <TopHorizontalScroll targetRef={tableScrollRef} />
//   <div ref={tableScrollRef} className="overflow-auto">...table...</div>
//
// The component listens to scroll events on BOTH the ghost track and
// the real container, syncing scrollLeft both ways. ResizeObserver
// keeps the ghost track's inner width matched to the real scrollWidth
// so the thumb proportion stays correct as columns load.
import { useEffect, useRef } from "react";

export default function TopHorizontalScroll({ targetRef, className = "" }) {
  const ghostRef = useRef(null);
  const innerRef = useRef(null);

  useEffect(() => {
    const target = targetRef?.current;
    const ghost = ghostRef.current;
    const inner = innerRef.current;
    if (!target || !ghost || !inner) return;

    // Keep ghost inner width in sync with the real table's scrollWidth.
    const syncWidth = () => {
      if (!inner || !target) return;
      inner.style.width = `${target.scrollWidth}px`;
    };
    syncWidth();

    const ro = new ResizeObserver(syncWidth);
    ro.observe(target);
    // Children may resize independently (sort, filter); observe them too.
    if (target.firstElementChild) ro.observe(target.firstElementChild);

    // Two-way scroll sync — guard against re-entry so the listeners
    // don't ping-pong each other.
    let lock = false;
    const onGhost = () => {
      if (lock) return;
      lock = true;
      target.scrollLeft = ghost.scrollLeft;
      requestAnimationFrame(() => { lock = false; });
    };
    const onTarget = () => {
      if (lock) return;
      lock = true;
      ghost.scrollLeft = target.scrollLeft;
      requestAnimationFrame(() => { lock = false; });
    };
    ghost.addEventListener("scroll", onGhost, { passive: true });
    target.addEventListener("scroll", onTarget, { passive: true });

    return () => {
      ro.disconnect();
      ghost.removeEventListener("scroll", onGhost);
      target.removeEventListener("scroll", onTarget);
    };
  }, [targetRef]);

  return (
    <div
      ref={ghostRef}
      data-testid="top-horizontal-scroll"
      className={`overflow-x-auto overflow-y-hidden h-3 sticky top-0 z-20 bg-card border-b border-border ${className}`}
      style={{ scrollbarWidth: "thin" }}
      aria-hidden="true"
    >
      <div ref={innerRef} style={{ height: 1 }} />
    </div>
  );
}
