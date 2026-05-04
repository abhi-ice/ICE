import { useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Layers } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";

// REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
function googleSignIn() {
  const redirect = window.location.origin + "/auth/callback";
  window.location.href = `https://auth.emergentagent.com/?redirect=${encodeURIComponent(redirect)}`;
}

export default function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("admin@iceops.com");
  const [password, setPassword] = useState("Demo123!");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setAuth(data);
      toast.success(`Welcome, ${data.user.full_name}`);
      nav("/");
    } catch (ex) {
      setErr(ex.response?.data?.detail || "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen grid grid-cols-1 lg:grid-cols-2 login-bg">
      <Toaster />

      {/* Left half: marketing copy on construction background */}
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
        <div className="text-[11px] text-slate-400">
          © {new Date().getFullYear()} ICE Construction. CMMC Level 2 ready.
        </div>
      </div>

      {/* Right half: sign-in card */}
      <div className="flex items-center justify-center p-6 lg:p-10 relative z-10">
        <div className="w-full max-w-md surface p-7">
          <div className="flex items-center gap-2 mb-5 lg:hidden">
            <Layers className="h-5 w-5 text-blue-600" strokeWidth={1.6} />
            <div className="font-semibold tracking-tight">ICE Schedule Ops</div>
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
          <p className="text-sm text-slate-500 mt-1 mb-5">Use your work email or Google.</p>

          <button
            data-testid="google-signin"
            onClick={googleSignIn}
            className="w-full h-10 rounded-md border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 flex items-center justify-center gap-2 text-sm font-medium"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" aria-hidden="true">
              <path fill="#4285F4" d="M21.6 12.227c0-.682-.061-1.337-.176-1.96H12v3.71h5.378a4.6 4.6 0 0 1-1.991 3.018v2.51h3.222c1.886-1.736 2.973-4.291 2.973-7.278z"/>
              <path fill="#34A853" d="M12 22c2.7 0 4.964-.895 6.62-2.495l-3.223-2.51c-.894.6-2.039.953-3.397.953-2.611 0-4.821-1.764-5.61-4.135H2.974v2.595A9.997 9.997 0 0 0 12 22z"/>
              <path fill="#FBBC05" d="M6.39 13.813a6.013 6.013 0 0 1 0-3.626V7.592H2.974a9.997 9.997 0 0 0 0 8.816l3.416-2.595z"/>
              <path fill="#EA4335" d="M12 5.96c1.474 0 2.795.507 3.836 1.502l2.86-2.86C16.96 3.062 14.696 2 12 2 7.94 2 4.426 4.34 2.974 7.592L6.39 10.187C7.179 7.816 9.389 5.96 12 5.96z"/>
            </svg>
            Sign in with Google
          </button>

          <div className="my-5 flex items-center gap-3 text-xs text-slate-400">
            <div className="flex-1 h-px bg-slate-200" />
            OR EMAIL
            <div className="flex-1 h-px bg-slate-200" />
          </div>

          <form onSubmit={submit} className="space-y-3">
            <div>
              <label className="field-label">Email</label>
              <input
                data-testid="login-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full h-10 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div>
              <label className="field-label">Password</label>
              <input
                data-testid="login-password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full h-10 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            {err && <div data-testid="login-error" className="text-sm text-red-600">{err}</div>}
            <Button data-testid="login-submit" type="submit" disabled={busy} className="w-full bg-blue-600 hover:bg-blue-700 h-10">
              {busy ? "Signing in..." : "Sign in"}
            </Button>
          </form>
          <div className="mt-4 text-[11px] text-slate-400 border-t pt-3">
            Demo creds pre-filled. Click <b>Sign in</b> to enter the seeded ICE tenant.
          </div>
        </div>
      </div>
    </div>
  );
}
