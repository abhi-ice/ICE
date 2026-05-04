// Marketplace — cross-tenant postings (sub invitations, equipment, labor, materials)
// per master prompt §19.
import { useEffect, useState } from "react";
import { ShoppingBasket, Plus, Send, Eye } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";

const TYPES = ["sub_invitation", "equipment_rental", "labor_request", "materials"];
const VISIBILITY = ["network", "union_hall", "public", "private"];

export default function MarketplaceFull() {
  const { activeId } = useProjects();
  const [postings, setPostings] = useState([]);
  const [open, setOpen] = useState(false);
  const [respondOpen, setRespondOpen] = useState(null); // posting object
  const [view, setView] = useState(null); // posting object for "view responses"
  const [responses, setResponses] = useState([]);
  const [form, setForm] = useState({
    posting_type: "sub_invitation", title: "", description: "",
    visibility: "network", location_city: "", location_state: "",
    deadline: "", budget_low: "", budget_high: "",
  });
  const [respForm, setRespForm] = useState({ message: "", bid_amount_usd: "", proposed_start_date: "" });

  const load = () => api.get("/marketplace/postings").then(({ data }) => setPostings(data));
  useEffect(() => { load(); }, []);

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

  return (
    <div data-testid="marketplace-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShoppingBasket className="h-5 w-5 text-blue-600" />
          <h1 className="text-2xl font-semibold">Marketplace</h1>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button data-testid="mp-new-posting" className="bg-blue-600 hover:bg-blue-700"><Plus className="h-4 w-4 mr-1" /> New posting</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>New marketplace posting</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
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
              <div className="grid grid-cols-3 gap-3">
                <div><label className="field-label">City</label><input value={form.location_city} onChange={(e) => setForm({ ...form, location_city: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
                <div><label className="field-label">State</label><input value={form.location_state} onChange={(e) => setForm({ ...form, location_state: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
                <div><label className="field-label">Deadline</label><input type="date" value={form.deadline} onChange={(e) => setForm({ ...form, deadline: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div><label className="field-label">Budget low ($)</label><input type="number" value={form.budget_low} onChange={(e) => setForm({ ...form, budget_low: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
                <div><label className="field-label">Budget high ($)</label><input type="number" value={form.budget_high} onChange={(e) => setForm({ ...form, budget_high: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" /></div>
              </div>
              <Button data-testid="mp-create" onClick={create} disabled={!form.title || !form.description} className="bg-blue-600 hover:bg-blue-700">Post</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {postings.map((p) => (
          <div key={p.id} data-testid={`mp-card-${p.id}`} className="surface p-4">
            <div className="flex items-center gap-2 mb-2">
              <span className="pill pill-blue capitalize">{p.posting_type.replace(/_/g, " ")}</span>
              <span className="pill pill-slate text-[10px]">{p.visibility}</span>
              <span className="ml-auto text-[10px] text-slate-400">{new Date(p.created_at).toLocaleDateString()}</span>
            </div>
            <div className="font-medium">{p.title}</div>
            <div className="text-xs text-slate-600 mt-1 line-clamp-3">{p.description}</div>
            <div className="text-xs text-slate-500 mt-2 flex items-center gap-2">
              {p.location_city && <span>{p.location_city}, {p.location_state}</span>}
              {p.deadline && <span>· deadline {p.deadline}</span>}
            </div>
            {(p.budget_low || p.budget_high) && (
              <div className="text-xs font-mono mt-1">${p.budget_low?.toLocaleString() || "—"} – ${p.budget_high?.toLocaleString() || "—"}</div>
            )}
            <div className="flex gap-2 mt-3">
              <button data-testid={`mp-respond-${p.id}`} onClick={() => setRespondOpen(p)} className="text-xs bg-blue-600 text-white h-7 px-3 rounded inline-flex items-center gap-1">
                <Send className="h-3 w-3" /> Respond
              </button>
              <button data-testid={`mp-view-resp-${p.id}`} onClick={() => showResponses(p)} className="text-xs border border-slate-200 h-7 px-3 rounded inline-flex items-center gap-1">
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
            <div className="grid grid-cols-2 gap-3">
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
              <div key={r.id} className="border border-slate-200 rounded p-3 text-sm">
                <div className="text-xs text-slate-500 mb-1">{new Date(r.created_at).toLocaleString()}</div>
                <div>{r.message}</div>
                {r.bid_amount_usd && <div className="font-mono text-sm mt-1">Bid: ${r.bid_amount_usd.toLocaleString()}</div>}
                {r.proposed_start_date && <div className="text-xs text-slate-500">Start: {r.proposed_start_date}</div>}
              </div>
            ))}
            {!responses.length && <div className="text-sm text-slate-500 text-center py-6">No responses yet.</div>}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
