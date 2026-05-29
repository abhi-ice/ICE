/** Team page — invite + revoke + member roster (admin only). */
import { useEffect, useState, useCallback } from "react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
import { Users, Mail, Trash2, Copy, ShieldAlert } from "lucide-react";
import { confirmAction } from "../lib/confirmAction";
import { deleteWithUndo } from "../lib/deleteWithUndo";
import FieldLabel from "../components/forms/FieldLabel";

// Bug #38 fix (2026-05-13): expanded to include the specialty roles
// added by migration 015 (submittal_tech + scheduling_engineer) and
// the older `pe` role (Professional Engineer). Each entry is
// {value, label} so the dropdown can render human-readable text
// while the form posts the canonical role value.
//
// Kept in sync with InviteCreate.role Literal in backend/models.py.
const ROLES = [
  { value: "admin",                label: "Admin" },
  { value: "pm",                   label: "PM (Project Manager)" },
  { value: "super",                label: "Super (Superintendent)" },
  { value: "foreman",              label: "Foreman" },
  { value: "viewer",               label: "Viewer (read-only)" },
  { value: "pe",                   label: "PE (Professional Engineer)" },
  { value: "submittal_tech",       label: "Submittal Tech" },
  { value: "scheduling_engineer",  label: "Scheduling Engineer" },
];

export default function Team() {
  const [me, setMe] = useState(null);
  const [members, setMembers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [draft, setDraft] = useState({ email: "", role: "pm" });
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const [{ data: u }, { data: m }, { data: i }] = await Promise.all([
        api.get("/auth/me"),
        api.get("/team/members"),
        api.get("/team/invites"),
      ]);
      setMe(u); setMembers(m); setInvites(i);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load team");
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const isAdmin = (me?.role || "").toLowerCase() === "admin";
  const inviteUrl = (code) => `${window.location.origin}/accept-invite?code=${encodeURIComponent(code)}`;

  const sendInvite = async (e) => {
    e.preventDefault();
    if (!draft.email) return;
    setBusy(true);
    try {
      const { data } = await api.post("/team/invites", draft);
      const code = data.invite?.code;
      const invitee = data.invite?.email || draft.email;
      if (data.already_existed) {
        toast.info(`Pending invite re-used (code ${code}).`);
      } else if (data.email_sent) {
        // Bug #47 (2026-05-14): SendGrid delivery succeeded.
        toast.success(`Invite emailed to ${invitee}.`);
      } else {
        // Bug #47 (2026-05-14): email delivery failed (provider error,
        // disabled provider, or missing API key). The invite_code is
        // still persisted -- admin can copy the link from the table
        // below and share manually.
        const reason = data.email_error
          ? ` (${data.email_error})`
          : "";
        toast.error(
          `Invite created but email failed${reason}. Copy the link from the table below to share manually.`,
        );
      }
      setDraft({ email: "", role: "pm" });
      reload();
    } catch (ex) {
      toast.error(ex.response?.data?.detail || "Invite failed");
    } finally { setBusy(false); }
  };

  const revoke = (id) => {
    // pt.37: Tier-1 toast + Undo (was window.confirm). Cheap — the
    // user can re-send the invite from the same form in seconds.
    deleteWithUndo({
      label: "Pending invite revoked",
      onCommit: async () => {
        try { await api.delete(`/team/invites/${id}`); reload(); }
        catch (e) { toast.error(e.response?.data?.detail || "Revoke failed"); }
      },
    });
  };

  const remove = async (id) => {
    // pt.37: themed modal (was window.confirm). Higher stakes than
    // revoking an invite — removing an active member is something
    // we want the admin to pause and confirm.
    const ok = await confirmAction({
      title: "Remove this member from your organization?",
      message: "They will lose access immediately. Their historical records (DRs, RFIs, audit rows) stay intact and attributed to them.",
      confirmText: "Remove member",
    });
    if (!ok) return;
    try { await api.delete(`/team/members/${id}`); toast.success("Member removed"); reload(); }
    catch (e) { toast.error(e.response?.data?.detail || "Remove failed"); }
  };

  return (
    <div data-testid="team-page" className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2"><Users className="h-5 w-5" /> Team</h1>
        <p className="text-sm text-muted-foreground">Members of your organization. Admins can invite teammates and revoke pending invites.</p>
      </div>

      {/* Invite form (admin only) */}
      {isAdmin ? (
        <form data-testid="team-invite-form" onSubmit={sendInvite} className="surface p-4 flex flex-wrap gap-3 items-end">
          <div className="grow min-w-[240px]">
            <FieldLabel required className="field-label">Email to invite</FieldLabel>
            <input data-testid="invite-email-input" type="email" required value={draft.email}
              onChange={(e) => setDraft({ ...draft, email: e.target.value })}
              placeholder="teammate@yourcompany.com"
              className="w-full h-9 px-3 rounded border border-slate-200 text-sm" />
          </div>
          <div>
            <div className="field-label">Role</div>
            <select data-testid="invite-role-select" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })}
              className="h-9 px-2 rounded border border-slate-200 text-sm">
              {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </div>
          <button data-testid="send-invite-btn" type="submit" disabled={busy}
            className="h-9 px-4 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1">
            <Mail className="h-4 w-4" /> Send invite
          </button>
        </form>
      ) : (
        <div className="surface p-4 text-sm text-slate-500 flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-amber-600" /> Only tenant admins can invite teammates.
        </div>
      )}

      {/* Members */}
      <div className="surface p-4" data-testid="members-table">
        <div className="font-medium mb-2">Members ({members.length})</div>
        <table className="dense-table w-full">
          <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Joined</th><th></th></tr></thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.id} data-testid={`member-${m.id}`}>
                <td>{m.full_name}</td>
                <td className="text-xs text-slate-600">{m.email}</td>
                <td><span className="pill pill-slate">{m.role}</span></td>
                <td className="text-xs text-slate-500">{new Date(m.created_at).toLocaleDateString()}</td>
                <td className="text-right">
                  {isAdmin && m.id !== me?.id && (
                    <button data-testid={`remove-member-${m.id}`} onClick={() => remove(m.id)}
                      className="text-red-600 hover:bg-red-50 px-2 py-1 rounded text-xs flex items-center gap-1">
                      <Trash2 className="h-3.5 w-3.5" /> Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!members.length && <tr><td colSpan="5" className="text-center py-4 text-sm text-slate-500">No members yet.</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Invites */}
      <div className="surface p-4" data-testid="invites-table">
        <div className="font-medium mb-2">Invites ({invites.length})</div>
        <table className="dense-table w-full">
          <thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Code</th><th>Invite link</th><th>Sent</th><th></th></tr></thead>
          <tbody>
            {invites.map((i) => (
              <tr key={i.id} data-testid={`invite-${i.id}`}>
                <td className="text-xs">{i.email}</td>
                <td><span className="pill pill-slate">{i.role}</span></td>
                <td><span className={`pill ${i.status === "pending" ? "pill-blue" : i.status === "accepted" ? "pill-green" : "pill-slate"}`}>{i.status}</span></td>
                <td className="font-mono text-xs">{i.code}</td>
                <td>
                  {i.status === "pending" && (
                    <button data-testid={`copy-link-${i.id}`} onClick={() => { navigator.clipboard.writeText(inviteUrl(i.code)); toast.success("Link copied"); }}
                      className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline">
                      <Copy className="h-3 w-3" /> copy
                    </button>
                  )}
                </td>
                <td className="text-xs text-slate-500">{new Date(i.invited_at).toLocaleDateString()}</td>
                <td className="text-right">
                  {isAdmin && i.status === "pending" && (
                    <button data-testid={`revoke-invite-${i.id}`} onClick={() => revoke(i.id)}
                      className="text-red-600 hover:bg-red-50 px-2 py-1 rounded text-xs">Revoke</button>
                  )}
                </td>
              </tr>
            ))}
            {!invites.length && <tr><td colSpan="7" className="text-center py-4 text-sm text-slate-500">No invites yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
