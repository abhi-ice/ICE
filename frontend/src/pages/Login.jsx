import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";
import SiteOpsLogo from "../components/SiteOpsLogo";
import FieldLabel from "../components/forms/FieldLabel";

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

      <header className="relative z-10 px-6 py-5 lg:px-10 lg:py-6 flex items-center justify-between">
        <Link to="/landing" className="flex items-center gap-2 text-white" data-testid="login-logo">
          <SiteOpsLogo className="h-8 w-8" />
          <div className="font-semibold tracking-tight text-base">SiteOps</div>
        </Link>
        <Link
          to="/landing"
          data-testid="login-back-landing"
          className="text-xs text-white/70 hover:text-white"
        >
          &larr; Back to home
        </Link>
      </header>

      <main className="relative z-10 flex-1 flex items-center justify-center px-4 py-8">
        <div
          className="w-full max-w-lg surface p-8 sm:p-10 shadow-2xl"
          data-testid="login-card"
        >
          <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="text-sm text-slate-500 mt-1.5 mb-6">
            Use the email and password you registered with.
          </p>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <FieldLabel required className="field-label">Email</FieldLabel>
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
              <FieldLabel required className="field-label">Password</FieldLabel>
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

      <footer
        data-testid="login-footer"
        className="relative z-10 px-6 pb-6 lg:px-10 text-center text-xs text-slate-300/80"
      >
        &copy; {new Date().getFullYear()} ICE Construction. CMMC Level 2 ready.
      </footer>
    </div>
  );
}
