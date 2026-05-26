// Public voice-login — keypad for redeeming a PIN. Master prompt §13.
import { useState } from "react";
import { Mic, KeyRound, Delete } from "lucide-react";
import api from "../lib/api";
import { setAuth } from "../lib/auth";
import { useNavigate } from "react-router-dom";

export default function VoiceLogin() {
  const [tenantSlug, setTenantSlug] = useState("ice");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const nav = useNavigate();

  const tap = (d) => { if (pin.length < 6) setPin(pin + d); setErr(""); };
  const del = () => setPin(pin.slice(0, -1));
  const submit = async () => {
    if (pin.length !== 6) return;
    setBusy(true); setErr("");
    try {
      const { data } = await api.post("/auth/voice-pin/exchange", { pin, tenant_slug: tenantSlug });
      setAuth({ access_token: data.access_token, user: data.user, tenant: data.tenant });
      nav("/m/foreman");
    } catch (e) {
      setErr(e.response?.data?.detail || "Invalid PIN");
      setPin("");
    } finally { setBusy(false); }
  };

  return (
    <div className="min-h-screen bg-slate-900 text-white grid place-items-center p-6">
      <div data-testid="voice-login" className="w-full max-w-sm space-y-5">
        <div className="text-center">
          <div className="inline-flex items-center gap-2 mb-2">
            <Mic className="h-6 w-6 text-amber-400" />
            <span className="font-semibold tracking-wide text-lg">Voice login</span>
          </div>
          <p className="text-sm text-slate-400">Key in your 6-digit PIN issued by your PM.</p>
        </div>

        <div>
          <label className="text-xs text-slate-400">Tenant</label>
          <input data-testid="vl-tenant" value={tenantSlug} onChange={(e) => setTenantSlug(e.target.value)} className="w-full mt-1 h-10 px-3 rounded bg-slate-800 border border-slate-700 text-sm" />
        </div>

        <div className="bg-slate-800 rounded-lg p-4">
          <div data-testid="vl-pin-display" className="flex justify-center gap-2 my-3 h-14 items-center">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div key={i} className={`h-12 w-9 rounded grid place-items-center text-xl font-mono ${i < pin.length ? "bg-amber-500 text-slate-900" : "bg-slate-700 text-slate-500"}`}>
                {pin[i] || "·"}
              </div>
            ))}
          </div>
          {err && <div data-testid="vl-error" className="text-red-400 text-xs text-center mb-2">{err}</div>}
          <div className="grid grid-cols-3 gap-2">
            {[1, 2, 3, 4, 5, 6, 7, 8, 9].map((d) => (
              <button key={d} data-testid={`vl-key-${d}`} onClick={() => tap(String(d))} className="h-14 rounded bg-slate-700 hover:bg-slate-600 text-2xl font-mono">{d}</button>
            ))}
            <button data-testid="vl-key-back" onClick={del} className="h-14 rounded bg-slate-700 hover:bg-slate-600 grid place-items-center"><Delete className="h-5 w-5" /></button>
            <button data-testid="vl-key-0" onClick={() => tap("0")} className="h-14 rounded bg-slate-700 hover:bg-slate-600 text-2xl font-mono">0</button>
            <button data-testid="vl-submit" onClick={submit} disabled={pin.length !== 6 || busy} className="h-14 rounded bg-amber-500 hover:bg-amber-400 text-slate-900 disabled:opacity-50 grid place-items-center font-medium">
              <KeyRound className="h-5 w-5" />
            </button>
          </div>
        </div>
        <a href="/login" className="block text-xs text-slate-400 hover:text-slate-200 text-center">Use email/password instead</a>
      </div>
    </div>
  );
}
