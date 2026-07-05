// Marketplace — cross-tenant postings (sub invitations, equipment, labor, materials)
// per master prompt §19. Now includes Stripe-funded escrow milestone payments.
import { useEffect, useState } from "react";
import { ShoppingBasket, Plus, Send, Eye, CheckCircle2, DollarSign } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import { useNavigate, useSearchParams } from "react-router-dom";
// CPM Part G (2026-05-15) -- created_at + deadline rendered MM/DD/YYYY
// via the chokepoint helper.
import { formatDateDisplay } from "../lib/dateUtil";

const TYPES = ["sub_invitation", "equipment_rental", "labor_request", "materials"];
const VISIBILITY = ["network", "union_hall", "public", "private"];

export default function MarketplaceFull() {
  const { activeId } = useProjects();
  const [postings, setPostings] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [escrow, setEscrow] = useState(null);
  const [open, setOpen] = useState(false);
  const [respondOpen, setRespondOpen] = useState(null); // posting object
  const [view, setView] = useState(null); // posting object for "view responses"
  const [responses, setResponses] = useState([]);
  const [acceptOpen, setAcceptOpen] = useState(null); // {posting, response}
  const [acceptCfg, setAcceptCfg] = useState({ schedule_activity_id: "", milestone_pct: 50 });
  const [params, setParams] = useSearchParams();
  const sessionId = params.get("session_id");
  const [form, setForm] = useState({
    posting_type: "sub_invitation", title: "", description: "",
    visibility: "network", location_city: "", location_state: "",
    deadline: "", budget_low: "", budget_high: "",
  });
  const [respForm, setRespForm] = useState({ message: "", bid_amount_usd: "", proposed_start_date: "" });

  const load = () => {
    api.get("/marketplace/postings").then(({ data }) => setPostings(data));
    api.get("/marketplace/escrow-summary").then(({ data }) => setEscrow(data));
    if (activeId) api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data));
  };
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  // Poll Stripe checkout status when redirected back
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const poll = async (attempts = 0) => {
      if (cancelled || attempts >= 6) {
        setParams({}, { replace: true });
        return;
      }
      try {
        const { data } = await api.get(`/marketplace/checkout-status/${sessionId}`);
        if (data.payment_status === "paid") {
          toast.success(`Escrow funded $${data.amount_usd.toLocaleString()}`);
          setParams({}, { replace: true });
          load();
          return;
        }
        if (data.status === "expired") {
          toast.error("Checkout session expired");
          setParams({}, { replace: true });
          return;
        }
      } catch {}
      setTimeout(() => poll(attempts + 1), 2000);
    };
    poll();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const create = async () => {
    try {
      await api.post("/marketplace/postings", {
        project_id: activeId,
        ...form,
        budget_low: form.budget_low ? Number(form.budget_low) : undefined,
        budget_high: form.budget_high ? Number(form.budget_high) : undefined,
        deadline: form.deadline || undefined,
      });
      toast.success("Posting created");
      setOpen(false);
      setForm({ posting_type: "sub_invitation", title: "", description: "", visibility: "network", location_city: "", location_state: "", deadline: "", budget_low: "", budget_high: "" });
      load();
    } catch (e) {
      toast.error(JSON.stringify(e.response?.data?.detail || "Failed"));
    }
  };

  const respond = async () => {
    try {
      await api.post("/marketplace/responses", {
        posting_id: respondOpen.id,
        message: respForm.message,
        bid_amount_usd: respForm.bid_amount_usd ? Number(respForm.bid_amount_usd) : undefined,
        proposed_start_date: respForm.proposed_start_date || undefined,
      });
      toast.success("Response sent");
      setRespondOpen(null);
      setRespForm({ message: "", bid_amount_usd: "", proposed_start_date: "" });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    }
  };

  const showResponses = async (p) => {
    setView(p);
    const { data } = await api.get(`/marketplace/postings/${p.id}/responses`);
    setResponses(data);
  };

  const acceptBid = async () => {
    try {
      const { data } = await api.post("/marketplace/accept-bid", {
        response_id: acceptOpen.response.id,
        schedule_activity_id: acceptCfg.schedule_activity_id || null,
        milestone_pct: Number(acceptCfg.milestone_pct),
        origin_url: window.location.origin,
      });
      window.location.href = data.url;
    } catch (e) {
      toast.error(e.response?.data?.detail || "Accept failed");
    }
  };

  const releasePayment = async (responseId) => {
    try {
      const { data } = await api.post(`/marketplace/release-payment/${responseId}`);
      toast.success(`Released $${data.payout_amount_usd.toLocaleString()} (fee $${data.platform_fee_usd.toLocaleString()})`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Release failed");
    }
  };

  return (
    <div data-testid="marketplace-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShoppingBasket className="h-5 w-5 text-blue-600" />
          <h1 className="text-2xl font-semibold">Marketplace</h1>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            {/* pt.40: primary sbtn, no Plus icon. */}
            <button data-testid="new-posting-btn" className="sbtn sbtn-sm sbtn-primary">New posting</button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>New marketplace posting</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
                <div>
                  <label className="field-label">Type</label>
                  <select value={form.posting_type} onChange={(e) => setForm({ ...form, posting_type: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    {TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                  </select>
                </div>
                <div>
                  <label className="field-label">Visibility</label>
                  <select value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    {VISIBILITY.map((v) => <option key={v} value={v}>{v}</option>)}
                  </select>
                </div>
              </div>
              <div>
                <label className="field-label">Title *</label>
                <input data-testid="mp-title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
              <div>
                <label className="field-label">Description *</label>
                <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="w-full p-2 rounded border border-slate-200 text-sm h-24" />
              </div>
              <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-3 gap-3">
                <div><label className="field-label">City</label><input value={form.location_city} onChange={(e) => setForm({ ...form, location_city: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
                <div><label className="field-label">State</label><input value={form.location_state} onChange={(e) => setForm({ ...form, location_state: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
                <div><label className="field-label">Deadline</label><input type="date" value={form.deadline} onChange={(e) => setForm({ ...form, deadline: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
              </div>
              <div className="grid grid-cols-1 max-md:grid-cols-2 sm:grid-cols-2 gap-3">
                <div><label className="field-label">Budget low ($)</label><input type="number" value={form.budget_low} onChange={(e) => setForm({ ...form, budget_low: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
                <div><label className="field-label">Budget high ($)</label><input type="number" value={form.budget_high} onChange={(e) => setForm({ ...form, budget_high: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
              </div>
              <Button data-testid="mp-create" onClick={create} disabled={!form.title || !form.description} className="max-md:w-full max-md:min-h-[44px] bg-blue-600 hover:bg-blue-700">Post</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      {escrow && (escrow.transaction_count > 0 || escrow.in_escrow_usd > 0) && (
        <div data-testid="escrow-summary" className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div className="surface p-3"><div className="text-xs text-slate-500 flex items-center gap-1"><DollarSign className="h-3 w-3" /> Escrow funded</div><div className="text-2xl font-semibold tabular-nums">${escrow.total_funded_usd.toLocaleString()}</div></div>
          <div className="surface p-3"><div className="text-xs text-slate-500">Released to subs</div><div className="text-2xl font-semibold tabular-nums">${escrow.total_released_usd.toLocaleString()}</div></div>
          <div className="surface p-3"><div className="text-xs text-slate-500">In escrow now</div><div className="text-2xl font-semibold tabular-nums text-blue-600">${escrow.in_escrow_usd.toLocaleString()}</div></div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {postings.map((p) => (
          <div key={p.id} data-testid={`mp-card-${p.id}`} className="surface p-4">
            <div className="flex items-center gap-2 mb-2 max-md:flex-wrap">
              <span className="pill pill-blue capitalize">{p.posting_type.replace(/_/g, " ")}</span>
              <span className="pill pill-slate text-xs">{p.visibility}</span>
              <span className="ml-auto text-xs text-slate-400">{formatDateDisplay(p.created_at)}</span>
            </div>
            <div className="font-medium">{p.title}</div>
            <div className="text-xs text-slate-600 mt-1 line-clamp-3">{p.description}</div>
            <div className="text-xs text-slate-500 mt-2 flex items-center gap-2">
              {p.location_city && <span>{p.location_city}, {p.location_state}</span>}
              {p.deadline && <span>· deadline {formatDateDisplay(p.deadline)}</span>}
            </div>
            {(p.budget_low || p.budget_high) && (
              <div className="text-xs font-mono mt-1">${p.budget_low?.toLocaleString() || "—"} – ${p.budget_high?.toLocaleString() || "—"}</div>
            )}
            <div className="flex gap-2 mt-3">
              <button data-testid={`mp-respond-${p.id}`} onClick={() => setRespondOpen(p)} className="text-xs bg-blue-600 text-white h-7 max-md:min-h-[44px] px-3 rounded inline-flex items-center gap-1">
                <Send className="h-3 w-3" /> Respond
              </button>
              <button data-testid={`mp-view-resp-${p.id}`} onClick={() => showResponses(p)} className="text-xs border border-slate-200 h-7 max-md:min-h-[44px] px-3 rounded inline-flex items-center gap-1">
                <Eye className="h-3 w-3" /> Responses
              </button>
            </div>
          </div>
        ))}
        {!postings.length && (
          <div className="col-span-full text-center py-12 text-sm text-slate-500">No marketplace postings yet.</div>
        )}
      </div>

      {/* Respond dialog */}
      <Dialog open={!!respondOpen} onOpenChange={(o) => !o && setRespondOpen(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Respond to: {respondOpen?.title}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <textarea data-testid="mp-resp-msg" placeholder="Your message" value={respForm.message} onChange={(e) => setRespForm({ ...respForm, message: e.target.value })} className="w-full p-2 rounded border border-slate-200 text-sm h-24" />
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div><label className="field-label">Bid ($)</label><input type="number" value={respForm.bid_amount_usd} onChange={(e) => setRespForm({ ...respForm, bid_amount_usd: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
              <div><label className="field-label">Start date</label><input type="date" value={respForm.proposed_start_date} onChange={(e) => setRespForm({ ...respForm, proposed_start_date: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
            </div>
            <Button data-testid="mp-send-resp" onClick={respond} disabled={!respForm.message} className="bg-blue-600 hover:bg-blue-700">Send response</Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* View responses dialog */}
      <Dialog open={!!view} onOpenChange={(o) => !o && setView(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Responses to: {view?.title}</DialogTitle></DialogHeader>
          <div className="space-y-2 max-h-[60vh] overflow-y-auto">
            {responses.map((r) => (
              <div key={r.id} data-testid={`mp-resp-${r.id}`} className="border border-slate-200 rounded p-3 text-sm">
                <div className="text-xs text-slate-500 mb-1 flex items-center gap-2">
                  {new Date(r.created_at).toLocaleString()}
                  {r.escrow_status && <span className={`pill text-xs ${r.escrow_status === "released" ? "pill-green" : "pill-blue"}`}>escrow {r.escrow_status}</span>}
                </div>
                <div>{r.message}</div>
                {r.bid_amount_usd && <div className="font-mono text-sm mt-1">Bid: ${r.bid_amount_usd.toLocaleString()}</div>}
                {r.proposed_start_date && <div className="text-xs text-slate-500">Start: {r.proposed_start_date}</div>}
                <div className="flex gap-2 mt-2">
                  {!r.accepted_at && r.bid_amount_usd && (
                    <button
                      data-testid={`mp-accept-${r.id}`}
                      onClick={() => { setAcceptOpen({ posting: view, response: r }); setAcceptCfg({ schedule_activity_id: "", milestone_pct: 50 }); setView(null); }}
                      className="text-xs bg-emerald-600 text-white h-7 max-md:min-h-[44px] px-3 rounded inline-flex items-center gap-1"
                    >
                      <CheckCircle2 className="h-3 w-3" /> Accept & fund escrow
                    </button>
                  )}
                  {r.escrow_status === "funded" && (
                    <button
                      data-testid={`mp-release-${r.id}`}
                      onClick={() => releasePayment(r.id)}
                      className="text-xs bg-blue-600 text-white h-7 max-md:min-h-[44px] px-3 rounded inline-flex items-center gap-1"
                    >
                      <DollarSign className="h-3 w-3" /> Release payment
                    </button>
                  )}
                  {r.escrow_status === "released" && (
                    <span className="text-xs text-emerald-700 inline-flex items-center gap-1">
                      <CheckCircle2 className="h-3 w-3" /> Paid out ${r.payout_amount_usd?.toLocaleString()}
                    </span>
                  )}
                </div>
              </div>
            ))}
            {!responses.length && <div className="text-sm text-slate-500 text-center py-6">No responses yet.</div>}
          </div>
        </DialogContent>
      </Dialog>

      {/* Accept-bid + escrow config dialog */}
      <Dialog open={!!acceptOpen} onOpenChange={(o) => !o && setAcceptOpen(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Accept bid · fund escrow</DialogTitle></DialogHeader>
          <div className="space-y-3 text-sm">
            {acceptOpen && (
              <>
                <div className="bg-slate-50 rounded p-3 text-xs">
                  <div><b>Posting:</b> {acceptOpen.posting.title}</div>
                  <div><b>Bid:</b> ${acceptOpen.response.bid_amount_usd?.toLocaleString()}</div>
                  <div><b>Platform fee (5%):</b> ${(acceptOpen.response.bid_amount_usd * 0.05).toFixed(2)}</div>
                </div>
                <div>
                  <label className="field-label">Bind to schedule activity (optional)</label>
                  <select data-testid="mp-accept-activity" value={acceptCfg.schedule_activity_id} onChange={(e) => setAcceptCfg({ ...acceptCfg, schedule_activity_id: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    <option value="">Manual release only</option>
                    {tasks.filter((t) => t.status !== "complete" && t.status !== "deleted").map((t) => (
                      <option key={t.id} value={t.id}>{t.description.slice(0, 60)}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="field-label">Auto-release at milestone %</label>
                  <input data-testid="mp-accept-milestone" type="number" min={10} max={100} value={acceptCfg.milestone_pct} onChange={(e) => setAcceptCfg({ ...acceptCfg, milestone_pct: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                  <div className="text-xs text-slate-500 mt-1">When the bound activity's pct_complete reaches this value, the funded escrow is automatically released to the sub.</div>
                </div>
                <Button data-testid="mp-accept-confirm" onClick={acceptBid} className="max-md:w-full max-md:min-h-[44px] bg-emerald-600 hover:bg-emerald-700 gap-1">
                  <CheckCircle2 className="h-4 w-4" /> Confirm — fund ${acceptOpen.response.bid_amount_usd?.toLocaleString()} via Stripe
                </Button>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
