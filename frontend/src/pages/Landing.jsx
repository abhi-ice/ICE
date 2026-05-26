// Public landing page (2026-05-24, rev 3) — scrollable multi-section
// marketing site. Tightened copy + SiteOpsLogo + premium emerald accents
// per the Glass + Emerald design spec in design-mockup/.
//
// Section copy is intentionally short — punchy titles, one-line bodies.
// No paragraphs of feature explanation; users skim, then click.
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight, Shield, Workflow, FileSpreadsheet, ScanLine, Bot,
  ClipboardCheck, Activity, AlertTriangle, Camera, MessageSquare, HardHat,
  Lock, Database, Eye, Sparkles, Calendar, CheckCircle2, Building2,
} from "lucide-react";
import SiteOpsLogo from "../components/SiteOpsLogo";

const FEATURE_CHIPS = [
  { icon: Workflow,        label: "Multi-tenant" },
  { icon: Shield,          label: "NIST 800-171" },
  { icon: FileSpreadsheet, label: "Audit-immutable" },
  { icon: Bot,             label: "AI Copilot" },
  { icon: ScanLine,        label: "Field-first" },
];

const CONTRACT_LOGOS = [
  { label: "GSA"   },
  { label: "NYCHA" },
  { label: "USMS"  },
  { label: "VA"    },
  { label: "DoD"   },
  { label: "ICE"   },
];

const FEATURES = [
  { icon: ClipboardCheck, title: "Daily reports",        tag: "Schedule-driven. SOD/EOD rollups." },
  { icon: MessageSquare,  title: "RFIs & submittals",    tag: "Aging, ball-in-court, CSI tags." },
  { icon: Activity,       title: "Schedule diffs",       tag: "P6/MSP imports. Cascade slip view." },
  { icon: Camera,         title: "Photos, drawings, 3D", tag: "Geotagged. Versioned. Coordinated." },
  { icon: HardHat,        title: "Field-first mobile",   tag: "Mobile foreman. Voice dictation." },
  { icon: AlertTriangle,  title: "Delays, safety, QC",   tag: "All field events flow to DR." },
];

const COPILOT_BULLETS = [
  { icon: Sparkles,     text: "Drafts TIA narratives from delays and diffs." },
  { icon: Activity,     text: "Proposes recovery scenarios for PM review." },
  { icon: Eye,          text: "Tenant-isolated. Scoped to your open doc." },
  { icon: CheckCircle2, text: "Every AI write goes through approvals." },
];

const COMPLIANCE = [
  { icon: Lock,            title: "CMMC L2 ready",    tag: "Audit logs, MFA, encryption at rest." },
  { icon: Shield,          title: "NIST 800-171",     tag: "AC, AU, IA, SC, SI families covered." },
  { icon: Database,        title: "Tenant-isolated",  tag: "Row-level. No admin override." },
  { icon: FileSpreadsheet, title: "Audit-immutable",  tag: "Append-only ledger." },
];

const STEPS = [
  { n: "01", icon: Building2,      title: "Spin up a tenant",     tag: "Sign up. Invite your team." },
  { n: "02", icon: Calendar,       title: "Import your schedule", tag: "P6 XER or MSP XML in minutes." },
  { n: "03", icon: ClipboardCheck, title: "Log from the field",   tag: "DRs auto-populate. Voice dictation." },
  { n: "04", icon: Bot,            title: "Let the Copilot work", tag: "TIAs, recovery, all human-gated." },
];

export default function Landing() {
  const nav = useNavigate();
  return (
    <div className="min-h-screen flex flex-col relative bg-[#0a0a0c] text-white" data-testid="landing-page">

      {/* STICKY TOP NAV */}
      <header
        data-testid="landing-nav"
        className="sticky top-0 z-50 backdrop-blur-md bg-black/70 border-b border-white/10"
      >
        <div className="max-w-7xl mx-auto px-6 lg:px-10 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2.5" data-testid="landing-logo">
            <SiteOpsLogo className="h-8 w-8" />
            <span className="font-semibold tracking-tight text-base">SiteOps</span>
            <span className="text-xs text-white/40 font-mono hidden sm:inline ml-1">/ Construction OS</span>
          </div>
          <nav className="hidden md:flex items-center gap-7 text-sm text-white/70">
            <a href="#features"  className="hover:text-white transition">Platform</a>
            <a href="#copilot"   className="hover:text-white transition">Copilot</a>
            <a href="#security"  className="hover:text-white transition">Security</a>
            <a href="#workflow"  className="hover:text-white transition">How it works</a>
          </nav>
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
              className="h-9 px-4 rounded-md text-sm font-medium bg-[#10b981] hover:bg-[#059669] text-white inline-flex items-center gap-1.5 shadow-[0_0_18px_-4px_rgba(16,185,129,0.55)] transition"
            >
              Get started
              <ArrowRight className="h-4 w-4" strokeWidth={2} />
            </button>
          </div>
        </div>
      </header>

      {/* HERO */}
      <section className="relative login-bg" data-testid="landing-hero">
        <div className="relative z-10 max-w-7xl mx-auto px-6 lg:px-10 py-20 lg:py-28">
          <div className="max-w-4xl mx-auto text-center space-y-7">
            <span
              data-testid="landing-eyebrow"
              className="inline-flex items-center gap-2 px-3 py-1 rounded-full text-[11px] uppercase tracking-[0.16em] bg-white/10 border border-white/15 text-white/85"
            >
              <span className="relative flex h-2 w-2">
                <span className="absolute inset-0 rounded-full bg-[#34D399] opacity-75 animate-ping"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#34D399]"></span>
              </span>
              Federal Construction OS &middot; CMMC L2 ready
            </span>
            <h1 className="text-4xl sm:text-5xl lg:text-6xl font-semibold tracking-tight leading-[1.05]">
              The operating system for{" "}
              <span className="text-[#34D399]">federal construction.</span>
            </h1>
            <p className="text-base sm:text-lg text-slate-200 leading-relaxed max-w-2xl mx-auto">
              CPM math, AI mitigation, and field-team daily reports in one
              audit-clean platform &mdash; built for GSA, NYCHA, USMS work.
            </p>

            <div data-testid="landing-chips" className="flex flex-wrap gap-2 justify-center">
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

            <div className="flex flex-col sm:flex-row items-center justify-center gap-3 pt-2">
              <button
                data-testid="landing-cta-signin"
                onClick={() => nav("/login")}
                className="h-11 px-6 rounded-md text-sm font-semibold bg-[#10b981] hover:bg-[#059669] text-white inline-flex items-center gap-2 shadow-[0_0_28px_-6px_rgba(16,185,129,0.55)] transition"
              >
                Sign in
                <ArrowRight className="h-4 w-4" strokeWidth={2} />
              </button>
              <Link
                data-testid="landing-cta-invite"
                to="/accept-invite"
                className="h-11 px-6 rounded-md text-sm font-semibold border border-white/20 bg-white/5 hover:bg-white/10 text-white inline-flex items-center transition"
              >
                Accept an invite
              </Link>
            </div>
          </div>
        </div>
      </section>

      {/* FEDERAL CONTRACTS STRIP */}
      <section data-testid="landing-contracts" className="relative border-y border-white/10 bg-black/60">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 py-8">
          <div className="text-center text-[11px] uppercase tracking-[0.18em] text-white/50 mb-5">
            Built for the federal contract stack
          </div>
          <div className="flex flex-wrap items-center justify-center gap-10 md:gap-16 opacity-80">
            {CONTRACT_LOGOS.map(({ label }) => (
              <div key={label} className="text-base font-semibold tracking-tight text-white/70 hover:text-white transition">
                {label}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* FEATURES */}
      <section id="features" data-testid="landing-features" className="relative py-20 lg:py-24">
        <div
          className="pointer-events-none absolute inset-0 opacity-60"
          style={{ background: "radial-gradient(800px 400px at 80% 0%, rgba(52,211,153,0.10), transparent 70%)" }}
        />
        <div className="relative max-w-7xl mx-auto px-6 lg:px-10">
          <div className="max-w-2xl mb-10">
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#34D399] mb-3">Platform</div>
            <h2 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
              One platform. Every record a federal job touches.
            </h2>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {FEATURES.map(({ icon: Icon, title, tag }) => (
              <div
                key={title}
                className="rounded-lg border border-white/10 bg-white/[0.02] hover:bg-white/[0.04] hover:border-white/20 p-5 transition"
              >
                <div className="flex items-center gap-3">
                  <div className="h-9 w-9 rounded-md grid place-items-center bg-[#10b981]/15 border border-[#10b981]/30 shrink-0">
                    <Icon className="h-4 w-4 text-[#34D399]" strokeWidth={1.8} />
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm font-semibold tracking-tight text-white">{title}</div>
                    <div className="text-xs text-white/55 mt-0.5">{tag}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* AI COPILOT */}
      <section id="copilot" data-testid="landing-copilot" className="relative py-20 lg:py-24 border-y border-white/10 bg-black/40">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 grid grid-cols-1 lg:grid-cols-2 gap-12 items-center">
          <div>
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#34D399] mb-3 flex items-center gap-2">
              <Bot className="h-3.5 w-3.5" /> AI Copilot
            </div>
            <h2 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
              A scheduling Copilot inside your contract data.
            </h2>
            <ul className="mt-6 space-y-3">
              {COPILOT_BULLETS.map(({ icon: Icon, text }) => (
                <li key={text} className="flex items-start gap-3">
                  <div className="h-6 w-6 rounded-md grid place-items-center bg-[#10b981]/15 border border-[#10b981]/30 flex-shrink-0 mt-0.5">
                    <Icon className="h-3.5 w-3.5 text-[#34D399]" strokeWidth={2} />
                  </div>
                  <span className="text-sm text-white/80">{text}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="relative">
            <div
              className="rounded-lg border border-white/10 bg-gradient-to-br from-white/[0.04] to-white/[0.01] p-5 lg:p-6 shadow-2xl backdrop-blur"
              data-testid="landing-copilot-mock"
            >
              <div className="flex items-center gap-2 pb-3 border-b border-white/10 mb-4">
                <div className="h-2 w-2 rounded-full bg-[#34D399]"></div>
                <div className="h-2 w-2 rounded-full bg-white/30"></div>
                <div className="h-2 w-2 rounded-full bg-white/30"></div>
                <div className="ml-3 text-[11px] text-white/50 font-mono">copilot &middot; delay #DLY-118</div>
              </div>
              <div className="space-y-3 text-sm">
                <div className="text-white/60 text-[11px]">Draft TIA narrative</div>
                <div className="rounded-md bg-black/40 border border-white/10 p-3 text-white/85 leading-relaxed font-mono text-[12.5px]">
                  Activity 1.4.2 (Mech roughing &mdash; L3 East) impacted by 03/14 ConEd shutdown.
                  CP slip = 6 wd. Recommend resequencing 1.4.3 to float window 03/22&ndash;03/28.
                </div>
                <div className="grid grid-cols-3 gap-2 pt-2">
                  <div className="rounded border border-white/10 bg-white/[0.03] p-2">
                    <div className="text-[10px] text-white/50">Slip</div>
                    <div className="text-base font-semibold text-rose-400">+6d</div>
                  </div>
                  <div className="rounded border border-white/10 bg-white/[0.03] p-2">
                    <div className="text-[10px] text-white/50">CP impact</div>
                    <div className="text-base font-semibold text-white">Yes</div>
                  </div>
                  <div className="rounded border border-white/10 bg-white/[0.03] p-2">
                    <div className="text-[10px] text-white/50">Recoverable</div>
                    <div className="text-base font-semibold text-[#34D399]">4d</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* SECURITY */}
      <section id="security" data-testid="landing-security" className="relative py-20 lg:py-24">
        <div className="max-w-7xl mx-auto px-6 lg:px-10">
          <div className="max-w-2xl mb-10">
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#34D399] mb-3">Security</div>
            <h2 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
              Federal-grade from the schema up.
            </h2>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {COMPLIANCE.map(({ icon: Icon, title, tag }) => (
              <div key={title} className="rounded-lg border border-white/10 bg-white/[0.02] p-5">
                <Icon className="h-5 w-5 text-[#34D399] mb-3" strokeWidth={1.8} />
                <div className="text-sm font-semibold tracking-tight text-white mb-1">{title}</div>
                <div className="text-xs text-white/60">{tag}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section id="workflow" data-testid="landing-workflow" className="relative py-20 lg:py-24 border-y border-white/10 bg-black/40">
        <div className="max-w-7xl mx-auto px-6 lg:px-10">
          <div className="max-w-2xl mb-10">
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#34D399] mb-3">How it works</div>
            <h2 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
              Signup to first daily report in under an hour.
            </h2>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {STEPS.map(({ n, icon: Icon, title, tag }) => (
              <div key={n} className="rounded-lg border border-white/10 bg-white/[0.02] p-5">
                <div className="flex items-baseline justify-between mb-3">
                  <div className="text-3xl font-bold text-white/15 font-mono">{n}</div>
                  <Icon className="h-5 w-5 text-[#34D399]" strokeWidth={1.8} />
                </div>
                <div className="text-sm font-semibold tracking-tight text-white mb-1">{title}</div>
                <div className="text-xs text-white/60">{tag}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* FINAL CTA */}
      <section data-testid="landing-final-cta" className="relative py-20 lg:py-24">
        <div
          className="pointer-events-none absolute inset-0"
          style={{ background: "radial-gradient(700px 350px at 50% 50%, rgba(52,211,153,0.10), transparent 70%)" }}
        />
        <div className="relative max-w-3xl mx-auto px-6 text-center space-y-6">
          <h2 className="text-3xl sm:text-4xl lg:text-5xl font-semibold tracking-tight leading-[1.1]">
            Run your jobs out of{" "}
            <span className="text-[#34D399]">one platform.</span>
          </h2>
          <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
            <button
              data-testid="landing-final-create"
              onClick={() => nav("/signup")}
              className="h-12 px-7 rounded-md text-sm font-semibold bg-[#10b981] hover:bg-[#059669] text-white inline-flex items-center gap-2 shadow-[0_0_32px_-4px_rgba(16,185,129,0.55)] transition"
            >
              Create your organization
              <ArrowRight className="h-4 w-4" strokeWidth={2} />
            </button>
            <Link
              data-testid="landing-final-signin"
              to="/login"
              className="h-12 px-7 rounded-md text-sm font-semibold border border-white/20 bg-white/5 hover:bg-white/10 text-white inline-flex items-center transition"
            >
              Sign in
            </Link>
          </div>
        </div>
      </section>

      {/* FOOTER */}
      <footer data-testid="landing-footer" className="relative border-t border-white/10 bg-black/60">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 py-10">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-8 mb-8">
            <div>
              <div className="flex items-center gap-2 mb-3">
                <SiteOpsLogo className="h-7 w-7" />
                <div className="font-semibold tracking-tight text-sm">SiteOps</div>
              </div>
              <p className="text-xs text-white/55 leading-relaxed">
                A product of Integrated Construction Enterprises.
              </p>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-[0.16em] text-white/50 mb-3">Product</div>
              <ul className="space-y-2 text-sm text-white/70">
                <li><a href="#features" className="hover:text-white transition">Platform</a></li>
                <li><a href="#copilot"  className="hover:text-white transition">Copilot</a></li>
                <li><a href="#workflow" className="hover:text-white transition">How it works</a></li>
              </ul>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-[0.16em] text-white/50 mb-3">Trust</div>
              <ul className="space-y-2 text-sm text-white/70">
                <li><a href="#security" className="hover:text-white transition">Security</a></li>
                <li className="text-white/55">CMMC L2 ready</li>
                <li className="text-white/55">NIST 800-171</li>
              </ul>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-[0.16em] text-white/50 mb-3">Get started</div>
              <ul className="space-y-2 text-sm text-white/70">
                <li><Link to="/login"          className="hover:text-white transition">Sign in</Link></li>
                <li><Link to="/signup"         className="hover:text-white transition">Create your org</Link></li>
                <li><Link to="/accept-invite"  className="hover:text-white transition">Accept an invite</Link></li>
              </ul>
            </div>
          </div>
          <div className="pt-5 border-t border-white/10 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-white/55">
            <div>&copy; {new Date().getFullYear()} ICE Construction. All rights reserved.</div>
            <div>CMMC L2 &middot; NIST 800-171</div>
          </div>
        </div>
      </footer>
    </div>
  );
}
