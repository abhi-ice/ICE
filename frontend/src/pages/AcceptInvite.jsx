import { useEffect, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Layers, MailCheck } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";

export default function AcceptInvite() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [form, setForm] = useState({ code: "", email: "", password: "", full_name: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    setForm((f) => ({
      ...f,
      code: params.get("code") || f.code,
      email: params.get("email") || f.email,
    }));
  }, [params]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const { data } = await api.post("/auth/accept-invite", form);
      setAuth(data);
      toast.success(`Joined ${data.tenant.display_name}.`);
      nav("/");
    } catch (ex) {
      const msg = ex.response?.data?.detail;
      setErr(typeof msg === "string" ? msg : (Array.isArray(msg) ? msg.map((m) => m.msg).join(", ") : "Accept failed"));
    } finally { setBusy(false); }
  };

  return (
    // Followup-fix (2026-05-07): the .login-bg utility paints a dark
    // gradient via ::before { position:absolute; inset:0 } -- without
    // a positioned, z-indexed inner wrapper that gradient renders ON
    // TOP of the form and captures every click/keystroke (page looks
    // greyed out, all inputs dead). Login.jsx and Signup.jsx fix this
    // with `relative z-10` on the inner content; AcceptInvite was
    // missing it, which blocked Sid from accepting his invite.
    <div className="min-h-screen grid place-items-center login-bg p-6">
      <Toaster />
      <div className="w-full max-w-md surface p-7 relative z-10">
        <div className="flex items-center gap-2 mb-4">
          <Layers className="h-5 w-5 text-blue-600" strokeWidth={1.6} />
          <div className="font-semibold tracking-tight">ICE Schedule Ops</div>
        </div>
        <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <MailCheck className="h-5 w-5 text-emerald-600" /> Accept invite
        </h1>
        <p className="text-sm text-slate-500 mt-1 mb-5">Use the code your team admin sent you.</p>

        <form onSubmit={submit} className="space-y-3">
          <Field label="Invite code" testid="invite-code" value={form.code} onChange={set("code")} required minLength={8} />
          <Field label="Your name" testid="invite-name" value={form.full_name} onChange={set("full_name")} required />
          <Field label="Email (must match the invite)" testid="invite-email" type="email" value={form.email} onChange={set("email")} required />
          <Field label="Password (min 8 chars)" testid="invite-password" type="password" value={form.password} onChange={set("password")} required minLength={8} autoComplete="new-password" />
          {err && <div data-testid="invite-error" className="text-sm text-red-600">{err}</div>}
          <Button data-testid="invite-submit" type="submit" disabled={busy} className="w-full bg-blue-600 hover:bg-blue-700 h-10">
            {busy ? "Joining…" : "Join organization"}
          </Button>
        </form>

        <div className="mt-5 text-sm text-slate-600 border-t pt-4">
          <Link to="/login" className="text-blue-600 hover:underline">Back to sign in</Link>
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
