// EntityFromVoiceDialog — opens a kind-specific creation form pre-filled
// with the voice-extracted structured_fields. On save, creates the entity
// AND patches the voice annotation with linked_entity_type/id.
import { useState, useEffect } from "react";
import api from "../../lib/api";
import { Button } from "../ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "../ui/dialog";
import { useProjects } from "../../lib/projects";
import { toast } from "../ui/sonner";
import FieldLabel from "../forms/FieldLabel";

const KIND_TO_API = {
  delay: "/delays",
  safety_event: "/safety-events",
  qc_issue: "/qc-issues",
  work_item: "/work-items",
  rfi: "/rfis",
};

function buildPayload(kind, fields, ctx) {
  const base = { project_id: ctx.projectId };
  if (kind === "delay") {
    return {
      ...base,
      description: fields.description || "",
      severity: fields.severity || "minor",
      delay_minutes: fields.delay_minutes ?? null,
      reason_category: fields.reason_category || null,
      date: ctx.today,
      schedule_activity_id: ctx.activityId || null,
      location_id: ctx.locationId || null,
    };
  }
  if (kind === "safety_event") {
    return {
      ...base,
      description: fields.description || "",
      severity: fields.severity || "minor",
      event_type: fields.event_type || "near_miss",
      osha_recordable: !!fields.osha_recordable,
      date: ctx.today,
      schedule_activity_id: ctx.activityId || null,
      location_id: ctx.locationId || null,
    };
  }
  if (kind === "qc_issue") {
    return {
      ...base,
      description: fields.description || "",
      severity: fields.severity || "minor",
      spec_section_ref: fields.spec_section_ref || null,
      date: ctx.today,
      schedule_activity_id: ctx.activityId || null,
      location_id: ctx.locationId || null,
    };
  }
  if (kind === "work_item") {
    return {
      ...base,
      title: fields.title || (fields.description || "").slice(0, 60) || "Untitled",
      description: fields.description || "",
      priority: fields.priority || "normal",
      root_cause: fields.root_cause || null,
      estimated_cost_impact: fields.estimated_cost_impact ?? null,
      estimated_time_impact_days: fields.estimated_time_impact_days ?? null,
      location_id: ctx.locationId || null,
    };
  }
  if (kind === "rfi") {
    return {
      ...base,
      subject: fields.subject || (fields.description || "").slice(0, 60) || "Untitled",
      description: fields.description || "",
      priority: fields.priority || "normal",
      to_party: fields.to_party || null,
      schedule_activity_id: ctx.activityId || null,
      location_id: ctx.locationId || null,
    };
  }
  return base;
}

export default function EntityFromVoiceDialog({ open, onClose, voiceAnnotation }) {
  const { activeId } = useProjects();
  const [tasks, setTasks] = useState([]);
  const [locations, setLocations] = useState([]);
  const [activityId, setActivityId] = useState("");
  const [locationId, setLocationId] = useState("");
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);

  const kind = voiceAnnotation?.kind;
  const fields = voiceAnnotation?.structured_fields || {};

  useEffect(() => {
    if (!open || !activeId) return;
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data.filter((t) => t.status !== "deleted")));
    api.get(`/projects/${activeId}/locations`).then(({ data }) => setLocations(data));
    setActivityId("");
    setLocationId("");
    // pre-fill form from extracted fields
    setForm({ ...fields });
  }, [open, activeId, fields]);

  if (!open || !voiceAnnotation) return null;

  const submit = async () => {
    setBusy(true);
    try {
      const today = new Date().toISOString().slice(0, 10);
      const payload = buildPayload(kind, form, {
        projectId: activeId, today, activityId, locationId,
      });
      // strip nulls
      Object.keys(payload).forEach((k) => { if (payload[k] === null) delete payload[k]; });
      const apiPath = KIND_TO_API[kind];
      const { data } = await api.post(apiPath, payload);
      const linkType = ({
        delay: "delay",
        safety_event: "safety_event",
        qc_issue: "qc_issue",
        work_item: "work_item",
        rfi: "rfi",
      })[kind];
      // best-effort: patch the voice annotation to link the new entity
      try {
        await api.patch?.(`/voice-annotations/${voiceAnnotation.id}`, { linked_entity_type: linkType, linked_entity_id: data.id });
      } catch {}
      toast.success(`${kind.replace(/_/g, ' ')} created`);
      onClose(data);
    } catch (e) {
      toast.error("Save failed: " + (e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || e.message));
    } finally {
      setBusy(false);
    }
  };

  const setF = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>
            Create {kind?.replace(/_/g, " ")} from voice annotation
          </DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-3" data-testid="voice-entity-form">
          {(kind === "rfi" || kind === "work_item") && (
            <div className="col-span-2">
              <label className="field-label">{kind === "rfi" ? "Subject" : "Title"}</label>
              <input
                data-testid="vef-title"
                value={form.title || form.subject || ""}
                onChange={(e) => setF(kind === "rfi" ? "subject" : "title", e.target.value)}
                className="w-full h-9 px-3 rounded border border-slate-200"
              />
            </div>
          )}
          <div className="col-span-2">
            <label className="field-label">Description</label>
            <textarea
              data-testid="vef-description"
              value={form.description || ""}
              onChange={(e) => setF("description", e.target.value)}
              rows={3}
              className="w-full px-3 py-2 rounded border border-slate-200 text-sm"
            />
          </div>
          {(kind === "delay" || kind === "safety_event" || kind === "qc_issue") && (
            <div>
              <label className="field-label">Severity</label>
              <select value={form.severity || "minor"} onChange={(e) => setF("severity", e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                {["minor", "moderate", "major", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          )}
          {(kind === "rfi" || kind === "work_item") && (
            <div>
              <label className="field-label">Priority</label>
              <select value={form.priority || "normal"} onChange={(e) => setF("priority", e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                {["low", "normal", "high", "urgent"].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          )}
          {kind === "delay" && (
            <>
              <div>
                <FieldLabel optional className="field-label">Delay (min)</FieldLabel>
                <input type="number" value={form.delay_minutes || ""} onChange={(e) => setF("delay_minutes", e.target.value === "" ? null : Number(e.target.value))} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
              <div>
                <FieldLabel optional className="field-label">Reason</FieldLabel>
                <input value={form.reason_category || ""} onChange={(e) => setF("reason_category", e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
            </>
          )}
          {kind === "safety_event" && (
            <div>
              <label className="field-label">Type</label>
              <select value={form.event_type || "near_miss"} onChange={(e) => setF("event_type", e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                {["near_miss", "first_aid", "recordable", "lost_time", "fatality", "other"].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          )}
          {kind === "work_item" && (
            <>
              <div>
                <FieldLabel optional className="field-label">Est. cost ($)</FieldLabel>
                <input type="number" value={form.estimated_cost_impact ?? ""} onChange={(e) => setF("estimated_cost_impact", e.target.value === "" ? null : Number(e.target.value))} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
              <div>
                <FieldLabel optional className="field-label">Est. days</FieldLabel>
                <input type="number" value={form.estimated_time_impact_days ?? ""} onChange={(e) => setF("estimated_time_impact_days", e.target.value === "" ? null : Number(e.target.value))} className="w-full h-9 px-3 rounded border border-slate-200" />
              </div>
            </>
          )}
          {kind === "qc_issue" && (
            <div>
              <FieldLabel optional className="field-label">Spec ref</FieldLabel>
              <input value={form.spec_section_ref || ""} onChange={(e) => setF("spec_section_ref", e.target.value)} className="w-full h-9 px-3 rounded border border-slate-200 font-mono text-xs" />
            </div>
          )}

          {/* Cross-cutting linkage */}
          <div>
            <FieldLabel optional className="field-label">Activity (linkage)</FieldLabel>
            <select data-testid="vef-activity" value={activityId} onChange={(e) => setActivityId(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
              <option value="">— optional —</option>
              {tasks.map((t) => <option key={t.id} value={t.id}>[{t.activity_code}] {t.description.slice(0, 50)}</option>)}
            </select>
          </div>
          <div>
            <FieldLabel optional className="field-label">Location (linkage)</FieldLabel>
            <select value={locationId} onChange={(e) => setLocationId(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
              <option value="">— optional —</option>
              {locations.map((l) => <option key={l.id} value={l.id}>{l.code} {l.name}</option>)}
            </select>
          </div>
        </div>
        <Button data-testid="vef-submit" onClick={submit} disabled={busy} className="bg-blue-600 hover:bg-blue-700">
          {busy ? "Saving…" : `Create ${kind?.replace(/_/g, " ")}`}
        </Button>
        <div className="text-xs text-slate-400 mt-1">
          Pre-filled from <code>structured_fields</code> extracted by Claude. Adjust before saving.
        </div>
      </DialogContent>
    </Dialog>
  );
}
