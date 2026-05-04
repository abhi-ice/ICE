// Notifications bell — dropdown of unread + per-row mark-as-read.
// Master prompt §23.
import { useEffect, useState, useRef } from "react";
import { Bell, Check } from "lucide-react";
import api from "../lib/api";
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
} from "./ui/dropdown-menu";

export default function NotificationsBell() {
  const [count, setCount] = useState(0);
  const [items, setItems] = useState([]);
  const [open, setOpen] = useState(false);
  const ticker = useRef(null);

  const refresh = async () => {
    try {
      const cu = await api.get("/notifications/unread-count");
      setCount(cu.data.count || 0);
      if (open || count > 0) {
        const lst = await api.get("/notifications", { params: { limit: 20 } });
        setItems(lst.data || []);
      }
    } catch {}
  };

  useEffect(() => {
    refresh();
    ticker.current = setInterval(refresh, 60000);
    return () => clearInterval(ticker.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { if (open) refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [open]);

  const markRead = async (id) => {
    await api.post(`/notifications/${id}/mark-read`);
    refresh();
  };

  const markAll = async () => {
    await api.post("/notifications/mark-all-read");
    refresh();
  };

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button data-testid="topbar-notifications" className="relative h-8 w-8 grid place-items-center rounded hover:bg-muted">
          <Bell className="h-4 w-4" />
          {count > 0 && (
            <span data-testid="notif-badge" className="absolute -top-0.5 -right-0.5 inline-flex items-center justify-center text-[9px] h-4 min-w-[16px] px-1 rounded-full bg-amber-500 text-slate-900 font-semibold">
              {count > 9 ? "9+" : count}
            </span>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 p-0">
        <div className="px-3 py-2 border-b border-slate-200 flex items-center">
          <div className="text-sm font-medium">Notifications</div>
          {count > 0 && (
            <button data-testid="notif-mark-all" onClick={markAll} className="ml-auto text-xs text-blue-600 hover:underline">Mark all read</button>
          )}
        </div>
        <div className="max-h-96 overflow-y-auto">
          {items.length === 0 && (
            <div className="text-sm text-slate-500 text-center py-8">No notifications.</div>
          )}
          {items.map((n) => (
            <div key={n.id} data-testid={`notif-${n.id}`} className={`px-3 py-2 border-b border-slate-100 text-sm flex items-start gap-2 ${n.read_at ? "opacity-60" : ""}`}>
              <div className="flex-1">
                <div className="font-medium text-[13px]">{n.title}</div>
                {n.body && <div className="text-xs text-slate-500 mt-0.5 line-clamp-2">{n.body}</div>}
                <div className="text-[10px] text-slate-400 mt-1">{new Date(n.created_at).toLocaleString()}</div>
              </div>
              {!n.read_at && (
                <button data-testid={`notif-read-${n.id}`} onClick={() => markRead(n.id)} title="Mark read" className="h-6 w-6 grid place-items-center rounded hover:bg-slate-100">
                  <Check className="h-3.5 w-3.5 text-slate-500" />
                </button>
              )}
            </div>
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
