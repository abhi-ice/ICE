import { useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { Layers } from "lucide-react";
import { Button } from "../components/ui/button";
import { toast, Toaster } from "../components/ui/sonner";

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
    <div className="min-h-screen grid place-items-center bg-muted/40 px-4">
      <Toaster />
      <div className="w-full max-w-md bg-card border border-border rounded-lg shadow-sm p-8">
        <div className="flex items-center gap-2 mb-6">
          <Layers className="h-6 w-6 text-primary" />
          <div className="text-lg font-semibold tracking-tight">ICE Schedule Ops</div>
        </div>
        <h1 className="text-xl font-semibold mb-1">Sign in</h1>
        <p className="text-sm text-muted-foreground mb-6">
          The operating system for federal construction.
        </p>
        <form onSubmit={submit} className="space-y-3">
          <div>
            <label className="field-label">Email</label>
            <input
              data-testid="login-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full h-10 px-3 rounded border border-input bg-background"
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
              className="w-full h-10 px-3 rounded border border-input bg-background"
            />
          </div>
          {err && <div data-testid="login-error" className="text-sm text-destructive">{err}</div>}
          <Button
            data-testid="login-submit"
            type="submit"
            disabled={busy}
            className="w-full"
          >
            {busy ? "Signing in..." : "Sign in"}
          </Button>
        </form>
        <div className="mt-4 text-xs text-muted-foreground border-t pt-4">
          <div>Demo credentials are pre-filled. Click <b>Sign in</b> to enter the seeded ICE tenant.</div>
        </div>
      </div>
    </div>
  );
}
