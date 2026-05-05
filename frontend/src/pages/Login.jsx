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
    <div className="min-h-screen grid grid-cols-1 lg:grid-cols-2 login-bg">
      <Toaster />
      <div className="hidden lg:flex flex-col justify-between p-10 relative z-10 text-white">
        <div className="flex items-center gap-2">
          <div className="h-9 w-9 rounded grid place-items-center bg-blue-600">
            <Layers className="h-5 w-5" strokeWidth={1.6} />
          </div>
          <div className="font-semibold tracking-tight text-base">ICE Schedule Ops</div>
        </div>
        <div className="max-w-lg space-y-4">
          <h2 className="text-3xl font-semibold tracking-tight leading-tight">
            The operating system for federal construction.
          </h2>
          <p className="text-sm text-slate-200 leading-relaxed">
            Schedules, daily reports, RFIs, drawings, specs, photos, and AI agents — one tenant-isolated platform built for GSA, NYCHA, USMS, and other federal contracts.
          </p>
          <div className="flex flex-wrap gap-2 pt-2">
            {["Multi-tenant", "NIST 800-171", "Audit-immutable", "AI Copilot", "Activity-driven daily reports"].map((t) => (
              <span key={t} className="text-[11px] px-2 py-1 rounded bg-white/10 border border-white/15">{t}</span>
            ))}
          </div>
        </div>
        <div className="text-[11px] text-slate-400">© {new Date().getFullYear()} ICE Construction. CMMC Level 2 ready.</div>
      </div>

      <div className="flex items-center justify-center p-6 lg:p-10 relative z-10">
        <div className="w-full max-w-md surface p-7">
          <div className="flex items-center gap-2 mb-5 lg:hidden">
            <Layers className="h-5 w-5 text-blue-600" strokeWidth={1.6} />
            <div className="font-semibold tracking-tight">ICE Schedule Ops</div>
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
          <p className="text-sm text-slate-500 mt-1 mb-5">Use the email + password you registered with.</p>

          <form onSubmit={submit} className="space-y-3">
            <div>
              <label className="field-label">Email</label>
              <input data-testid="login-email" type="email" required value={email} autoComplete="email"
                onChange={(e) => setEmail(e.target.value)}
                className="w-full h-10 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="field-label">Password</label>
              <input data-testid="login-password" type="password" required value={password} autoComplete="current-password"
                onChange={(e) => setPassword(e.target.value)}
                className="w-full h-10 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            {err && <div data-testid="login-error" className="text-sm text-red-600">{err}</div>}
            <Button data-testid="login-submit" type="submit" disabled={busy} className="w-full bg-blue-600 hover:bg-blue-700 h-10">
              {busy ? "Signing in..." : "Sign in"}
            </Button>
          </form>

          <div className="mt-5 text-sm text-slate-600 border-t pt-4 space-y-1.5">
            <div>New here? <Link data-testid="signup-link" to="/signup" className="text-blue-600 hover:underline">Create your organization</Link></div>
            <div>Got an invite? <Link data-testid="accept-invite-link" to="/accept-invite" className="text-blue-600 hover:underline">Accept it here</Link></div>
          </div>
        </div>
      </div>
    </div>
  );
}
