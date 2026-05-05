// Compliance — NIST 800-171 control mapping + audit-log integrity verifier.
// Master prompt §28 (compliance) + §30 (audit chain).
import { useEffect, useState } from "react";
import { ShieldCheck, FileCheck, AlertTriangle, Check } from "lucide-react";
import api from "../lib/api";
import { Button } from "../components/ui/button";

export default function Compliance() {
  const [nist, setNist] = useState(null);
  const [chain, setChain] = useState(null);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    api.get("/compliance/nist-800-171").then(({ data }) => setNist(data));
  }, []);

  const runVerify = async () => {
    setVerifying(true);
    try {
      const { data } = await api.get("/audit-log/verify-chain");
      setChain(data);
    } finally { setVerifying(false); }
  };

  useEffect(() => { runVerify(); }, []);

  return (
    <div data-testid="compliance-page" className="space-y-4 max-w-6xl">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-5 w-5 text-emerald-600" />
        <h1 className="text-2xl font-semibold tracking-tight">Compliance</h1>
      </div>

      {/* Audit-chain integrity card */}
      <div data-testid="audit-chain" className="surface p-4">
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="flex items-center gap-2"><FileCheck className="h-4 w-4" /><b>Audit log integrity</b></div>
            <p className="text-xs text-slate-500">SHA-256 hash chain — every audit row hashes prev_hash + canonical_payload.</p>
          </div>
          <Button data-testid="audit-verify" onClick={runVerify} disabled={verifying} className="bg-blue-600 hover:bg-blue-700">{verifying ? "Verifying…" : "Verify chain"}</Button>
        </div>
        {chain && (
          <div className="grid grid-cols-3 gap-3">
            <Metric label="Rows examined" value={chain.rows_examined} testid="audit-rows" />
            <Metric label="Divergences" value={chain.divergences.length} bad={chain.divergences.length > 0} testid="audit-divergences" />
            <Metric label="Status" value={chain.ok ? "OK" : "FAILED"} good={chain.ok} bad={!chain.ok} testid="audit-status" />
          </div>
        )}
        {chain && !chain.ok && (
          <div className="mt-3 bg-red-50 border border-red-200 rounded p-3 text-xs">
            <AlertTriangle className="inline h-3.5 w-3.5 text-red-600 mr-1" />
            Chain integrity lost at index <b>{chain.first_failure_index}</b>. Investigate immediately — data may have been tampered with.
          </div>
        )}
      </div>

      {/* NIST 800-171 mapping */}
      <div className="surface overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <b>NIST 800-171 control mapping</b>
            {nist && <span className="ml-3 text-xs text-slate-500">{nist.summary.implemented} of {nist.summary.total} controls implemented · {nist.summary.families.length} families</span>}
          </div>
        </div>
        <table className="dense-table">
          <thead><tr><th>ID</th><th>Family</th><th>Control</th><th>Implementation</th><th>Status</th></tr></thead>
          <tbody>
            {(nist?.controls || []).map((c) => (
              <tr key={c.id} data-testid={`nist-row-${c.id}`}>
                <td className="font-mono text-xs">{c.id}</td>
                <td><span className="pill pill-slate">{c.family}</span></td>
                <td className="text-sm">{c.name}</td>
                <td className="text-xs text-slate-600">{c.implementation}</td>
                <td>
                  {c.implemented ? (
                    <span className="inline-flex items-center gap-1 text-emerald-600 text-xs"><Check className="h-3 w-3" /> implemented</span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-amber-600 text-xs"><AlertTriangle className="h-3 w-3" /> partial</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const Metric = ({ label, value, good, bad, testid }) => (
  <div data-testid={testid} className={`p-3 rounded border ${bad ? "border-red-300 bg-red-50" : good ? "border-emerald-300 bg-emerald-50" : "border-slate-200"}`}>
    <div className="text-xs text-slate-500">{label}</div>
    <div className="text-2xl font-semibold tabular-nums">{value}</div>
  </div>
);
