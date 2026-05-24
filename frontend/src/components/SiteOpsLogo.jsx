// SiteOps brand mark — stacked floors (zinc top + zinc middle + emerald
// foundation) + a small emerald star at top-right.
// Pulled from design-mockup/landing-glass.html so the React app matches
// the approved Glass + Emerald visual identity (2026-05-24).
// Renders at any size — pass `className` to control width/height.
export default function SiteOpsLogo({ className = "h-7 w-7", title = "SiteOps logo" }) {
  return (
    <svg
      viewBox="0 0 32 32"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label={title}
      role="img"
    >
      <defs>
        <linearGradient id="siteops-foundation" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"   stopColor="#34D399" />
          <stop offset="100%" stopColor="#059669" />
        </linearGradient>
      </defs>
      {/* Top floor */}
      <rect x="9"  y="6"  width="14" height="5" rx="1.2" fill="#a1a1aa" />
      {/* Middle floor */}
      <rect x="5"  y="14" width="22" height="5" rx="1.2" fill="#d4d4d8" />
      {/* Foundation — emerald gradient */}
      <rect x="2"  y="22" width="28" height="6" rx="1.5" fill="url(#siteops-foundation)" />
      {/* Star */}
      <circle cx="26" cy="4" r="1.5" fill="#34D399" opacity="0.85" />
    </svg>
  );
}
