import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Layers } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";

export default function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setAuth(data);
      toast.success(`Welcome, ${data.user.full_name}`);
      nav("/");
    } catch (ex) {
      setErr(ex.response?.data?.detail || "Login failed");
    } finally { setBusy(false); }
  };

  return (
    <div className="min-h-screen login-bg flex flex-col relative" data-testid="login-page">
      <Toaster />

      {/* Top-left logo lockup -- standard placement on every modern SaaS site. */}
      <header className="relative z-10 px-6 py-5 lg:px-10 lg:py-6">
        <div className="flex items-center gap-2 text-white" data-testid="login-logo">
          <div className="h-9 w-9 rounded grid place-items-center bg-blue-600">
            <Layers className="h-5 w-5" strokeWidth={1.6} />
          </div>
          <div className="font-semibold tracking-tight text-base">ICE Schedule Ops</div>
        </div>
      </header>

      {/* Centered sign-in card. flex-1 + items-center + justify-center keeps it
          vertically + horizontally centered no matter the viewport height. */}
      <main className="relative z-10 flex-1 flex items-center justify-center px-4 py-8">
        <div
          className="w-full max-w-lg surface p-8 sm:p-10 shadow-2xl"
          data-testid="login-card"
        >
          <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="text-sm text-slate-500 mt-1.5 mb-6">
            Use the email + password you registered with.
          </p>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="field-label">Email</label>
              <input
                data-testid="login-email"
                type="email"
                required
                value={email}
                autoComplete="email"
                onChange={(e) => setEmail(e.target.value)}
                className="w-full h-11 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="field-label">Password</label>
              <input
                data-testid="login-password"
                type="password"
                required
                value={password}
                autoComplete="current-password"
                onChange={(e) => setPassword(e.target.value)}
                className="w-full h-11 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            {err && (
              <div data-testid="login-error" className="text-sm text-red-600">
                {err}
              </div>
            )}
            <Button
              data-testid="login-submit"
              type="submit"
              disabled={busy}
              className="w-full bg-blue-600 hover:bg-blue-700 h-11"
            >
              {busy ? "Signing in..." : "Sign in"}
            </Button>
          </form>

          <div className="mt-6 text-sm text-slate-600 border-t pt-5 space-y-2">
            <div>
              New here?{" "}
              <Link
                data-testid="signup-link"
                to="/signup"
                className="text-blue-600 hover:underline"
              >
                Create your organization
              </Link>
            </div>
            <div>
              Got an invite?{" "}
              <Link
                data-testid="accept-invite-link"
                to="/accept-invite"
                className="text-blue-600 hover:underline"
              >
                Accept it here
              </Link>
            </div>
          </div>
        </div>
      </main>

      {/* Marketing strip BELOW the card -- pitch + capability pills + footer
          copyright. Stays out of the way of the auth flow but is visible on
          first paint so visitors know what this product is. */}
      <section
        className="relative z-10 px-6 pb-10 lg:px-10 text-white"
        data-testid="login-marketing"
      >
        <div className="max-w-3xl mx-auto text-center space-y-4">
          <h2 className="text-2xl sm:text-3xl font-semibold tracking-tight leading-tight">
            The operating system for federal construction.
          </h2>
          <p className="text-sm text-slate-200 leading-relaxed">
            Schedules, daily reports, RFIs, drawings, specs, photos, and AI
            agents — one tenant-isolated platform built for GSA, NYCHA, USMS,
            and other federal contracts.
          </p>
          <div className="flex flex-wrap gap-2 justify-center pt-1">
            {[
              "Multi-tenant",
              "NIST 800-171",
              "Audit-immutable",
              "AI Copilot",
              "Activity-driven daily reports",
            ].map((t) => (
              <span
                key={t}
                className="text-xs px-2.5 py-1 rounded bg-white/10 border border-white/15"
              >
                {t}
              </span>
            ))}
          </div>
          <div className="text-xs text-slate-400 pt-3">
            © {new Date().getFullYear()} ICE Construction. CMMC Level 2 ready.
          </div>
        </div>
      </section>
    </div>
  );
}
