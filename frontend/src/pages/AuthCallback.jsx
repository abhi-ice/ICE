// AuthCallback — exchanges Emergent OAuth session_id (URL fragment) for a
// session_token cookie via /api/auth/google/session.
// REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
import { useEffect, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import axios from "axios";
import { setAuth } from "../lib/auth";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

export default function AuthCallback() {
  const nav = useNavigate();
  const location = useLocation();
  const ran = useRef(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const hash = location.hash || window.location.hash;
    const m = hash.match(/session_id=([^&]+)/);
    const sid = m ? decodeURIComponent(m[1]) : null;
    if (!sid) {
      setErr("Missing session_id in URL fragment.");
      return;
    }
    (async () => {
      try {
        const { data } = await axios.post(
          `${API}/auth/google/session`,
          { session_id: sid },
          { withCredentials: true }
        );
        // We don't get a JWT; cookie is set via HttpOnly. Cache user/tenant.
        setAuth({ access_token: "google", user: data.user, tenant: data.tenant });
        nav("/", { replace: true });
      } catch (e) {
        setErr(e.response?.data?.detail || e.message);
      }
    })();
  }, [location.hash, nav]);

  return (
    <div className="min-h-screen grid place-items-center bg-slate-50">
      <div className="surface p-6 w-[360px] text-center">
        {err ? (
          <>
            <div className="text-red-600 text-sm font-medium mb-2">Sign-in failed</div>
            <div className="text-xs text-slate-500">{err}</div>
            <button onClick={() => nav("/login")} className="mt-4 h-9 px-4 rounded bg-blue-600 text-white text-sm">
              Back to sign in
            </button>
          </>
        ) : (
          <>
            <div className="animate-pulse h-2 bg-blue-100 rounded mb-3" />
            <div className="text-sm text-slate-600">Completing Google sign-in…</div>
          </>
        )}
      </div>
    </div>
  );
}
