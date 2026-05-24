// Public landing page (2026-05-24) — sits in front of /login.
// Holds the marketing content that previously lived inside Login.jsx,
// so the Sign-in screen can stay focused on the auth form alone.
// Reuses .login-bg (steel-construction photo + dark gradient overlay)
// for visual continuity with the auth flow.
import { Link, useNavigate } from "react-router-dom";
import { Layers, ArrowRight, Shield, Workflow, FileSpreadsheet, ScanLine, Bot } from "lucide-react";

const FEATURE_CHIPS = [
  { icon: Workflow,        label: "Multi-tenant" },
  { icon: Shield,          label: "NIST 800-171" },
  { icon: FileSpreadsheet, label: "Audit-immutable" },
  { icon: Bot,             label: "AI Copilot" },
  { icon: ScanLine,        label: "Activity-driven daily reports" },
];

export default function Landing() {
  const nav = useNavigate();
  return (
    <div className="min-h-screen login-bg flex flex-col relative" data-testid="landing-page">
      {/* Top bar — brand on the left, Sign-in CTA on the right */}
      <header className="relative z-10 px-6 py-5 lg:px-10 lg:py-6 flex items-center justify-between">
        <div className="flex items-center gap-2 text-white" data-testid="landing-logo">
          <div className="h-9 w-9 rounded grid place-items-center bg-[#ef233c]">
            <Layers className="h-5 w-5" strokeWidth={1.6} />
          </div>
          <div className="font-semibold tracking-tight text-base">ICE Schedule Ops</div>
        </div>
        <div className="flex items-center gap-2">
          <Link
            data-testid="landing-signin"
            to="/login"
            className="hidden sm:inline-flex h-9 px-4 items-center rounded-md text-sm font-medium text-white/90 hover:text-white hover:bg-white/10 transition"
          >
            Sign in
          </Link>
          <button
            data-testid="landing-cta-create"
            onClick={() => nav("/signup")}
            className="h-9 px-4 rounded-md text-sm font-medium bg-[#ef233c] hover:bg-[#b91c2c] text-white inline-flex items-center gap-1.5 shadow-[0_0_18px_-4px_rgba(239,35,60,0.6)] transition"
          >
            Create your organization
            <ArrowRight className="h-4 w-4" strokeWidth={2} />
          </button>
        </div>
      </header>

      {/* Hero — centered marketing block. Same content that used to live
          inside Login.jsx, expanded and given room to breathe. */}
      <main className="relative z-10 flex-1 flex items-center justify-center px-6 py-12">
        <div className="max-w-4xl text-center text-white space-y-8">
          <div className="space-y-4">
            <span
              data-testid="landing-eyebrow"
              className="inline-flex items-center gap-2 px-3 py-1 rounded-full text-[11px] uppercase tracking-[0.16em] bg-white/10 border border-white/15 text-white/85"
            >
              <span className="relative flex h-2 w-2">
                <span className="absolute inset-0 rounded-full bg-[#ef233c] opacity-75 animate-ping"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#ef233c]"></span>
              </span>
              Federal construction OS · CMMC Level 2 ready
            </span>
            <h1 className="text-4xl sm:text-5xl lg:text-6xl font-semibold tracking-tight leading-[1.05]">
              The operating system for{" "}
              <span className="text-[#ef233c]">federal construction.</span>
            </h1>
            <p className="text-base sm:text-lg text-slate-200 leading-relaxed max-w-2xl mx-auto">
              Schedules, daily reports, RFIs, drawings, specs, photos, and AI agents —
              one tenant-isolated platform built for GSA, NYCHA, USMS, and other
              federal contracts.
            </p>
          </div>

          {/* Feature chips */}
          <div
            data-testid="landing-chips"
            className="flex flex-wrap gap-2 justify-center"
          >
            {FEATURE_CHIPS.map(({ icon: Icon, label }) => (
              <span
                key={label}
                className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full bg-white/10 border border-white/15 text-white/90"
              >
                <Icon className="h-3.5 w-3.5" strokeWidth={1.8} />
                {label}
              </span>
            ))}
          </div>

          {/* Primary CTA stack */}
          <div className="flex flex-col sm:flex-row items-center justify-center gap-3 pt-2">
            <button
              data-testid="landing-cta-signin"
              onClick={() => nav("/login")}
              className="h-11 px-6 rounded-md text-sm font-semibold bg-[#ef233c] hover:bg-[#b91c2c] text-white inline-flex items-center gap-2 shadow-[0_0_28px_-6px_rgba(239,35,60,0.6)] transition"
            >
              Sign in
              <ArrowRight className="h-4 w-4" strokeWidth={2} />
            </button>
            <Link
              data-testid="landing-cta-invite"
              to="/accept-invite"
              className="h-11 px-6 rounded-md text-sm font-semibold border border-white/20 bg-white/5 hover:bg-white/10 text-white inline-flex items-center transition"
            >
              Got an invite? Accept it here
            </Link>
          </div>

          {/* Sub-tagline */}
          <p
            data-testid="landing-subtagline"
            className="text-xs text-slate-300 max-w-2xl mx-auto pt-4"
          >
            One source of truth for your schedule. Information-dense. Audit-immutable.
            Built by a federal GC, not a SaaS team.
          </p>
        </div>
      </main>

      {/* Footer */}
      <footer
        data-testid="landing-footer"
        className="relative z-10 px-6 pb-8 lg:px-10 text-center text-xs text-slate-300/80"
      >
        <div className="space-y-1">
          <div>
            © {new Date().getFullYear()} ICE Construction. CMMC Level 2 ready.
          </div>
          <div className="text-slate-400/80">
            Already a customer? <Link to="/login" className="text-white/90 hover:text-white underline-offset-4 hover:underline">Sign in</Link>.
            New here? <Link to="/signup" className="text-white/90 hover:text-white underline-offset-4 hover:underline">Create your organization</Link>.
          </div>
        </div>
      </footer>
    </div>
  );
}
