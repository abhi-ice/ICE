import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Layers, Building2 } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";

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
      toast.success(`Welcome, ${data.user.full_name} — organization “${data.tenant.display_name}” created.`);
      nav("/getting-started");
    } catch (ex) {
      const msg = ex.response?.data?.detail;
      setErr(typeof msg === "string" ? msg : (Array.isArray(msg) ? msg.map((m) => m.msg).join(", ") : "Signup failed"));
    } finally { setBusy(false); }
  };

  return (
    <div className="min-h-screen grid grid-cols-1 lg:grid-cols-2 login-bg">
      <Toaster />
      <div className="hidden lg:flex flex-col justify-between p-10 relative z-10 text-white">
        <div className="flex items-center gap-2">
          <div className="h-9 w-9 rounded grid place-items-center bg-blue-600"><Layers className="h-5 w-5" strokeWidth={1.6} /></div>
          <div className="font-semibold tracking-tight text-base">ICE Schedule Ops</div>
        </div>
        <div className="max-w-lg space-y-4">
          <h2 className="text-3xl font-semibold tracking-tight leading-tight">
            Create your organization in 30 seconds.
          </h2>
          <p className="text-sm text-slate-200 leading-relaxed">
            Every signup gets its own isolated tenant — your projects, drawings, RFIs, daily reports, and AI sessions are walled off from every other contractor on the platform.
            Invite teammates from the Team page once you&apos;re in.
          </p>
        </div>
        <div className="text-[11px] text-slate-400">© {new Date().getFullYear()} ICE Construction.</div>
      </div>

      <div className="flex items-center justify-center p-6 lg:p-10 relative z-10">
        <div className="w-full max-w-md surface p-7">
          <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2">
            <Building2 className="h-5 w-5 text-blue-600" /> Create organization
          </h1>
          <p className="text-sm text-slate-500 mt-1 mb-5">You&apos;ll be the first admin of your tenant.</p>

          <form onSubmit={submit} className="space-y-3">
            <Field label="Organization name" testid="signup-org" value={form.organization_name} onChange={set("organization_name")} required minLength={2} />
            <Field label="Your name" testid="signup-name" value={form.full_name} onChange={set("full_name")} required />
            <Field label="Work email" testid="signup-email" type="email" value={form.email} onChange={set("email")} required autoComplete="email" />
            <Field label="Password (min 8 chars)" testid="signup-password" type="password" value={form.password} onChange={set("password")} required minLength={8} autoComplete="new-password" />
            {err && <div data-testid="signup-error" className="text-sm text-red-600">{err}</div>}
            <Button data-testid="signup-submit" type="submit" disabled={busy} className="w-full bg-blue-600 hover:bg-blue-700 h-10">
              {busy ? "Creating…" : "Create organization & sign in"}
            </Button>
          </form>

          <div className="mt-5 text-sm text-slate-600 border-t pt-4">
            Already have an account? <Link to="/login" className="text-blue-600 hover:underline">Sign in</Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, testid, ...rest }) {
  return (
    <div>
      <label className="field-label">{label}</label>
      <input data-testid={testid} {...rest}
        className="w-full h-10 px-3 rounded-md border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
    </div>
  );
}
