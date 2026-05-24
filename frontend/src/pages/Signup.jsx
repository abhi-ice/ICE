import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Layers, Building2 } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";

// Signup redesign (2026-05-24) — mirrors Login.jsx structure exactly:
// header with brand + back-to-home, single centered auth card,
// minimal footer. Removed the lg:grid-cols-2 split that left the
// form card stranded in the corner at 100% zoom.
export default function Signup() {
  const nav = useNavigate();
  const [form, setForm] = useState({ organization_name: "", full_name: "", email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const { data } = await api.post("/auth/register", form);
      setAuth(data);
      toast.success(`Welcome, ${data.user.full_name} — organization "${data.tenant.display_name}" created.`);
      nav("/getting-started");
    } catch (ex) {
      const msg = ex.response?.data?.detail;
      setErr(typeof msg === "string" ? msg : (Array.isArray(msg) ? msg.map((m) => m.msg).join(", ") : "Signup failed"));
    } finally { setBusy(false); }
  };

  return (
    <div className="min-h-screen login-bg flex flex-col relative" data-testid="signup-page">
      <Toaster />

      {/* Top-left logo lockup — same pattern as Login. Brand returns
          user to /landing. */}
      <header className="relative z-10 px-6 py-5 lg:px-10 lg:py-6 flex items-center justify-between">
        <Link to="/landing" className="flex items-center gap-2 text-white" data-testid="signup-logo">
          <div className="h-9 w-9 rounded grid place-items-center bg-[#ef233c]">
            <Layers className="h-5 w-5" strokeWidth={1.6} />
          </div>
          <div className="font-semibold tracking-tight text-base">ICE Schedule Ops</div>
        </Link>
        <Link
          to="/landing"
          data-testid="signup-back-landing"
          className="text-xs text-white/70 hover:text-white"
        >
          ← Back to home
        </Link>
      </header>

      {/* Centered card — same width + padding as the sign-in card so
          the two pages feel like siblings. */}
      <main className="relative z-10 flex-1 flex items-center justify-center px-4 py-8">
        <div
          className="w-full max-w-lg surface p-8 sm:p-10 shadow-2xl"
          data-testid="signup-card"
        >
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Building2 className="h-6 w-6 text-[#ef233c]" /> Create organization
          </h1>
          <p className="text-sm text-slate-500 mt-1.5 mb-6">
            You&apos;ll be the first admin of your tenant.
          </p>

          <form onSubmit={submit} className="space-y-4">
            <Field label="Organization name" testid="signup-org" value={form.organization_name} onChange={set("organization_name")} required minLength={2} />
            <Field label="Your name" testid="signup-name" value={form.full_name} onChange={set("full_name")} required />
            <Field label="Work email" testid="signup-email" type="email" value={form.email} onChange={set("email")} required autoComplete="email" />
            <Field label="Password (min 8 chars)" testid="signup-password" type="password" value={form.password} onChange={set("password")} required minLength={8} autoComplete="new-password" />
            {err && (
              <div data-testid="signup-error" className="text-sm text-red-600">
                {err}
              </div>
            )}
            <Button
              data-testid="signup-submit"
              type="submit"
              disabled={busy}
              className="w-full bg-blue-600 hover:bg-blue-700 h-11"
            >
              {busy ? "Creating…" : "Create organization & sign in"}
            </Button>
          </form>

          <div className="mt-6 text-sm text-slate-600 border-t pt-5">
            Already have an account?{" "}
            <Link
              data-testid="signin-link"
              to="/login"
              className="text-blue-600 hover:underline"
            >
              Sign in
            </Link>
          </div>
        </div>
      </main>

      <footer
        data-testid="signup-footer"
        className="relative z-10 px-6 pb-6 lg:px-10 text-center text-xs text-slate-300/80"
      >
        © {new Date().getFullYear()} ICE Construction. CMMC Level 2 ready.
      </footer>
    </div>
  );
}

function Field({ label, testid, ...rest }) {
  return (
    <div>
      <label className="field-label">{label}</label>
      <input data-testid={testid} {...rest}
        className="w-full h-11 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
    </div>
  );
}
