/**
 * Forgot-password + Reset-password pages (2026-02-XX, Abhi).
 *
 * Two small components sharing one file -- both are public (unauth)
 * pages and stylistically siblings. Mounted under /forgot-password
 * and /reset-password in App.js.
 *
 * UX choices:
 *  - We ALWAYS show a generic "if an account exists, an email is on
 *    the way" success message on /forgot-password, matching the
 *    backend's account-enumeration-resistant 200-always response.
 *  - The /reset-password page reads `?token=...` from the URL once;
 *    no other state. If the token is missing the user sees an
 *    inline "this link looks broken" message instead of a blank form.
 *  - On reset success we redirect to /login with a green toast so
 *    the user immediately knows what to do next.
 */
import { useState, useEffect } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "../components/ui/sonner";
import api from "../lib/api";


function CardShell({ title, subtitle, children }) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-[hsl(var(--bg))] px-4">
      <div className="w-full max-w-md surface p-7 dr-section">
        <div className="text-[11px] uppercase tracking-[0.18em] text-[#34D399] font-semibold mb-2">
          SiteOps · Construction OS
        </div>
        <h1 className="text-2xl font-bold tracking-tight mb-1.5">{title}</h1>
        {subtitle && (
          <p className="text-sm text-slate-400 mb-5">{subtitle}</p>
        )}
        {children}
      </div>
    </div>
  );
}


export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [genericMsg, setGenericMsg] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    if (!email.trim()) { toast.error("Enter your email."); return; }
    setBusy(true);
    try {
      const { data } = await api.post("/auth/forgot-password", { email: email.trim() });
      setGenericMsg(data?.message
        || "If an account exists for that email, a password-reset link has been sent.");
      setSent(true);
    } catch (err) {
      toast.error(err.response?.data?.detail || "Could not send reset email. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (sent) {
    return (
      <CardShell title="Check your inbox" subtitle="">
        <div className="text-sm text-slate-300 mb-4">{genericMsg}</div>
        <div className="text-xs text-slate-400 mb-4">
          Reset links expire in <strong>30 minutes</strong>. If you don't see the
          email within a minute, check your spam folder.
        </div>
        <Link to="/login" data-testid="back-to-login"
              className="text-sm text-emerald-400 hover:underline">
          ← Back to sign in
        </Link>
      </CardShell>
    );
  }

  return (
    <CardShell
      title="Forgot password"
      subtitle="Enter your email and we'll send you a link to reset it."
    >
      <form onSubmit={submit} className="space-y-3">
        <label className="block">
          <div className="text-xs text-slate-400 mb-1">Email</div>
          <input
            data-testid="forgot-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            required
            className="w-full h-10 px-3 rounded bg-slate-900/60 border border-white/10 text-sm focus:border-emerald-500 outline-none"
          />
        </label>
        <button
          data-testid="forgot-submit"
          type="submit"
          disabled={busy}
          className="w-full h-10 rounded bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
        >
          {busy ? "Sending…" : "Send reset link"}
        </button>
      </form>
      <div className="mt-4 flex justify-between text-xs text-slate-400">
        <Link to="/login" className="hover:text-slate-200">← Back to sign in</Link>
        <span className="text-right">New here? Ask your company admin to send you an invite.</span>
      </div>
    </CardShell>
  );
}


export function ResetPassword() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    if (!token) {
      toast.error("Reset link is missing its token. Request a fresh email.");
    }
  }, [token]);

  const submit = async (e) => {
    e.preventDefault();
    if (password.length < 8) {
      toast.error("Password must be at least 8 characters."); return;
    }
    if (password !== confirmPassword) {
      toast.error("Passwords don't match."); return;
    }
    setBusy(true);
    try {
      await api.post("/auth/reset-password", { token, new_password: password });
      toast.success("Password updated. Sign in with your new password.");
      navigate("/login", { replace: true });
    } catch (err) {
      toast.error(err.response?.data?.detail || "Could not reset password.");
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <CardShell title="Invalid reset link" subtitle="">
        <p className="text-sm text-slate-300 mb-4">
          The reset link you opened doesn't have a token. The email may have
          been forwarded or a copy-paste truncated the URL.
        </p>
        <Link to="/forgot-password" data-testid="back-to-forgot"
              className="text-sm text-emerald-400 hover:underline">
          Request a new reset email →
        </Link>
      </CardShell>
    );
  }

  return (
    <CardShell title="Choose a new password" subtitle="Minimum 8 characters.">
      <form onSubmit={submit} className="space-y-3">
        <label className="block">
          <div className="text-xs text-slate-400 mb-1">New password</div>
          <input
            data-testid="reset-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            required
            minLength={8}
            className="w-full h-10 px-3 rounded bg-slate-900/60 border border-white/10 text-sm focus:border-emerald-500 outline-none"
          />
        </label>
        <label className="block">
          <div className="text-xs text-slate-400 mb-1">Confirm new password</div>
          <input
            data-testid="reset-password-confirm"
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            minLength={8}
            className="w-full h-10 px-3 rounded bg-slate-900/60 border border-white/10 text-sm focus:border-emerald-500 outline-none"
          />
        </label>
        <button
          data-testid="reset-submit"
          type="submit"
          disabled={busy}
          className="w-full h-10 rounded bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
        >
          {busy ? "Updating…" : "Set new password"}
        </button>
      </form>
    </CardShell>
  );
}
